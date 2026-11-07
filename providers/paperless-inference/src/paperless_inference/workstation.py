"""Pull finite OCR/metadata operations without mounting server-owned storage."""

import argparse
import fcntl
import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import niquests

from paperless_inference.config import endpoint
from paperless_inference.provider_operations import compute


class APIError(RuntimeError):
    """Retain an HTTP status without exposing response bodies or credentials."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Paperless API returned HTTP {status}")


def map_files(value: object, transfer: Callable[[dict], object]) -> object:
    """Transfer explicit immutable file references wherever a contract declares them."""
    if isinstance(value, dict):
        if "path" in value and "sha256" in value:
            return transfer(value)
        return {key: map_files(item, transfer) for key, item in value.items()}
    if isinstance(value, list):
        return [map_files(item, transfer) for item in value]
    return value


class Workstation:
    """Execute the installed providers using only authenticated operation APIs."""

    def __init__(self, url: str, token_file: Path, state: Path) -> None:
        self.url = url.rstrip("/")
        self.token_file = token_file
        self.state = state
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.pending = self.state / "pending.json"
        self.session = niquests.Session()

    def request(self, method: str, path: str, **kwargs):
        """Make one authenticated request; redirects must not forward credentials."""
        token = self.token_file.read_text().strip()
        if not token or any(character.isspace() for character in token):
            raise ValueError("A nonempty Paperless token file is required")
        response = self.session.request(
            method,
            self.url + path,
            headers={"Authorization": "Token " + token, **kwargs.pop("headers", {})},
            timeout=30,
            allow_redirects=False,
            **kwargs,
        )
        if response.status_code not in {200, 201, 202}:
            response.close()
            raise APIError(response.status_code)
        return response

    def save(self, record: dict) -> None:
        """Persist computed results before delivery so retries do not repeat inference."""
        temporary = self.pending.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(record, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.pending)
        descriptor = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def base(job_id: str, key: str) -> str:
        """Encode server identifiers as individual URL segments."""
        from uuid import UUID

        identifier = str(UUID(job_id))
        return f"/api/operations/{identifier}/{quote(key, safe='')}/"

    def compute(self, claim: dict, base: str) -> dict:
        """Download declared immutable files, then execute a supported contract."""
        from paperless.parsers.contract import relative_path
        from paperless.parsers.contract import resolve_reference

        if claim.get("version") != 2 or claim.get("status") != "leased":
            raise ValueError("Unsupported operation claim")
        if claim.get("remote_allowed") is not True:
            raise ValueError("Operation does not permit workstation execution")
        if claim["operation"] not in {"parser.process", "metadata.enrich"}:
            raise ValueError("Unsupported workstation operation")
        root = self.state / "files"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(mode=0o700)

        def download(reference: dict) -> dict:
            """Bound each transfer and verify the complete immutable reference."""
            if (
                set(reference) != {"path", "size", "sha256"}
                or type(reference["size"]) is not int
                or reference["size"] < 0
                or not isinstance(reference["sha256"], str)
            ):
                raise ValueError("Invalid immutable input")
            target = relative_path(root, reference["path"])
            if target.exists():
                resolve_reference(root, reference)
                return reference
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (
                self.request(
                    "GET",
                    base + "files/",
                    params={"path": reference["path"]},
                    headers={"X-Operation-Token": claim["token"]},
                    stream=True,
                ) as response,
                target.open("wb") as output,
            ):
                size = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    size += len(chunk)
                    if size > reference["size"]:
                        raise ValueError("Downloaded input exceeds its declared size")
                    output.write(chunk)
            resolve_reference(root, reference)
            return reference

        map_files(claim["input"], download)
        result = compute(claim, root)
        map_files(claim["input"], lambda reference: resolve_reference(root, reference))
        return result

    def deliver(self, record: dict) -> None:
        """Retry private output uploads and idempotent result submission only."""
        from paperless.parsers.contract import resolve_reference

        if record["url"] != self.url:
            raise ValueError("Pending result belongs to another Paperless instance")
        base = self.base(record["job_id"], record["key"])
        result = record["result"]
        uploaded = record.setdefault("uploaded", {})

        def upload(reference: dict) -> dict:
            """Reuse acknowledged uploads, including records from older clients."""
            for remote in uploaded.values():
                if all(remote.get(key) == reference[key] for key in ("sha256", "size")):
                    return remote
            path = resolve_reference(self.state / "files", reference)
            # Content names keep different provider output paths collision-free.
            name = reference["sha256"] + path.suffix
            with path.open("rb") as stream:
                response = self.request(
                    "POST",
                    base + "files/",
                    headers={"X-Operation-Token": record["token"]},
                    files={"file": (name, stream, "application/octet-stream")},
                )
            remote = response.json()
            if any(remote.get(key) != reference[key] for key in ("sha256", "size")):
                raise ValueError("Uploaded output identity changed")
            uploaded[reference["path"]] = remote
            self.save(record)
            return remote

        delivered = map_files(result, upload)
        response = self.request(
            "POST",
            base + "result/",
            json={"token": record["token"], "result": delivered},
        )
        if response.json().get("status") not in {"staged", "succeeded"}:
            raise ValueError("Result was not accepted for validation")
        self.pending.unlink()
        shutil.rmtree(self.state / "files", ignore_errors=True)

    def run_once(self) -> int:
        """Resume uncertain delivery, then execute the supported pending operations."""
        if self.pending.exists():
            self.deliver(json.loads(self.pending.read_text()))
        count = 0
        operations = self.request("GET", "/api/operations/?status=pending").json()
        for operation in operations:
            if (
                operation["status"] != "pending"
                or operation["operation"] not in {"parser.process", "metadata.enrich"}
                or operation["job_status"]
                in {"cancelled", "failed", "rejected", "attention"}
            ):
                continue
            base = self.base(operation["job_id"], operation["key"])
            try:
                claim = self.request("POST", base + "claim/").json()
            except APIError as error:
                if error.status == 409:
                    continue
                raise
            if claim.get("status") != "leased":
                continue
            result = self.compute(claim, base)
            record = {
                "job_id": operation["job_id"],
                "key": operation["key"],
                "token": claim["token"],
                "result": result,
                "url": self.url,
            }
            self.save(record)
            self.deliver(record)
            count += 1
        return count


def main() -> None:
    """Run a serial pull worker; the one-pass mode is useful for bounded checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    url = endpoint("PAPERLESS_URL")
    token = Path(os.environ["PAPERLESS_API_TOKEN_FILE"])
    state = Path(os.environ.get("PAPERLESS_WORKSTATION_STATE_DIR", "/workstation"))
    worker = Workstation(url, token, state)
    with (state / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            completed = worker.run_once()
            print(json.dumps({"completed_operations": completed}), flush=True)
            if args.once:
                return
            time.sleep(5 if completed else 30)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__}), file=sys.stderr)
        sys.exit(1)
