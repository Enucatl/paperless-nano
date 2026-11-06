"""Exercise real supervised processors against disposable PostgreSQL and Redis.

Run only after migrating an empty, isolated deployment, with
PAPERLESS_PROCESSOR_VALIDATION=1 and PAPERLESS_SEARCH_PROVIDER set to
processor_deployment_checks.EndpointSearchProvider. Add this directory to
PYTHONPATH. The HTTP stub tests lifecycle behavior, not OCR/model correctness.
"""

# ruff: noqa: INP001, T201

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from documents.search.tantivy import TantivyProvider


class EndpointSearchProvider(TantivyProvider):
    """Use a controllable real HTTP endpoint for replay-safe finite work."""

    replay_safe = True

    def wipe(self) -> None:
        """Complete only after the endpoint acknowledges the request."""
        with urlopen(
            os.environ["PAPERLESS_VALIDATION_ENDPOINT"], timeout=15
        ) as response:
            assert response.status == 200


def main() -> None:
    """Check contention, revocation, active-child death and endpoint retries."""
    if os.environ.get("PAPERLESS_PROCESSOR_VALIDATION") != "1":
        raise RuntimeError("Requires an explicitly isolated validation deployment")
    import django

    django.setup()
    from django.contrib.auth.models import Permission
    from django.contrib.auth.models import User
    from django.core.management import call_command
    from django.utils import timezone
    from documents.deferred_operations import recover_waiting
    from documents.models import DocumentJob
    from documents.models import PaperlessTask
    from documents.search import get_backend
    from documents.search_operations import run_search
    from documents.search_operations import submit_search

    assert not DocumentJob.objects.exists(), "Requires an empty disposable database"
    call_command("document_processor_account", user="validation", capability=["search"])
    user = User.objects.get(username="validation")
    permission = Permission.objects.get(codename="process_search")
    state = {"calls": 0, "failures": 0}
    gate = threading.Event()
    gate.set()

    class Endpoint(BaseHTTPRequestHandler):
        """Expose controlled outages without changing processor code."""

        def do_GET(self) -> None:
            state["calls"] += 1
            gate.wait(20)
            failed = state["failures"] > 0
            if failed:
                state["failures"] -= 1
            self.send_response(503 if failed else 200)
            self.end_headers()

        def log_message(self, *args) -> None:
            """Keep the check output limited to assertions and its result."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["PAPERLESS_VALIDATION_ENDPOINT"] = (
        f"http://127.0.0.1:{server.server_port}/"
    )
    supervisors = []

    def wait_for(predicate) -> None:
        """Bound each asynchronous assertion independently."""
        until = time.monotonic() + 30
        while not predicate():
            if time.monotonic() >= until:
                raise AssertionError("Processor condition did not complete within 30s")
            time.sleep(0.05)

    def entry(job) -> dict:
        """Read committed operation state, never an in-memory snapshot."""
        job.refresh_from_db()
        return job.result["invocations"]["maintenance"]

    def admit() -> DocumentJob:
        """Use ordinary admission and coordination before server processing."""
        job = submit_search(get_backend().provider, "wipe", {})
        job.timeout = 1
        job.save(update_fields=["timeout"])
        run_search(str(job.pk))
        return job

    def start() -> subprocess.Popen:
        """Launch the actual lightweight supervisor and its management child."""
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from paperless_services.processor import Processor; Processor().run()",
            ],
            cwd=Path(__file__).resolve().parents[2] / "src",
            stdout=log,
            stderr=log,
        )
        supervisors.append(process)
        return process

    def stop_all() -> None:
        """Reap every supervisor and its forwarded child shutdown."""
        for process in supervisors:
            process.terminate()
        for process in supervisors:
            process.wait(timeout=20)
        supervisors.clear()

    def finish(job) -> None:
        """Let the ordinary coordinator validate the staged receipt."""
        wait_for(lambda: entry(job)["status"] == "staged")

        def coordinated() -> bool:
            """Retry delivery if the processor still holds the operation lock."""
            run_search(str(job.pk))
            job.refresh_from_db()
            return job.status == DocumentJob.Status.SUCCEEDED

        wait_for(coordinated)

    with tempfile.TemporaryFile(mode="w+") as log:
        try:
            gate.clear()
            job = admit()
            start()
            start()
            wait_for(lambda: state["calls"] == 1)
            time.sleep(0.5)
            assert state["calls"] == 1 and entry(job)["attempts"] == 1
            gate.set()
            finish(job)
            stop_all()

            start()
            time.sleep(2)
            user.user_permissions.remove(permission)
            job = admit()
            wait_for(
                lambda: (
                    b"Processor grant revoked" in os.pread(log.fileno(), 1_000_000, 0)
                    or b"Processor lacks configured capability grants"
                    in os.pread(log.fileno(), 1_000_000, 0)
                )
            )
            assert entry(job)["status"] == "pending"
            assert entry(job).get("attempts", 0) == 0
            call_command(
                "document_processor_account", user="validation", capability=["search"]
            )
            finish(job)
            stop_all()

            gate.clear()
            previous_calls = state["calls"]
            job = admit()
            supervisor = start()
            wait_for(lambda: state["calls"] == previous_calls + 1)
            children = Path(f"/proc/{supervisor.pid}/task/{supervisor.pid}/children")
            child_pid = int(children.read_text().split()[0])
            os.killpg(child_pid, signal.SIGKILL)
            stop_all()
            gate.set()
            time.sleep(1.1)
            recover_waiting()
            start()
            finish(job)
            assert entry(job)["attempts"] == 2
            stop_all()

            state["failures"] = 1
            job = admit()
            start()
            finish(job)
            assert entry(job)["attempts"] == 2
            stop_all()

            state["failures"] = 10
            job = admit()
            task = PaperlessTask.objects.get(task_id=str(job.pk))
            task.status = PaperlessTask.Status.STARTED
            task.date_started = timezone.now()
            task.save(update_fields=["status", "date_started"])
            start()
            wait_for(lambda: entry(job)["status"] == "failed")
            assert entry(job)["attempts"] == 3
            assert job.status == DocumentJob.Status.FAILED and "503" in job.error
            wait_for(
                lambda: (
                    PaperlessTask.objects.get(pk=task.pk).status
                    == PaperlessTask.Status.FAILURE
                )
            )
            task.refresh_from_db()
            assert (
                task.date_done is not None
                and "503" in task.result_data["error_message"]
            )
            stop_all()
        except BaseException:
            gate.set()
            stop_all()
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            server.shutdown()
    print(
        json.dumps(
            {
                "competing_processors": True,
                "permission_revocation_and_reprovisioning": True,
                "active_child_crash_recovery": True,
                "endpoint_recovery": True,
                "bounded_terminal_failure": True,
                "top_level_task_failure": True,
            }
        )
    )


if __name__ == "__main__":
    main()
