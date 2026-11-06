"""Exercise Celery's shared runner against disposable PostgreSQL and Redis.

The endpoint represents finite provider work; this checks lifecycle and durable
handoffs, not OCR/model quality. Requires PAPERLESS_PROCESSOR_VALIDATION=1.
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
from datetime import timedelta
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request
from urllib.request import urlopen

from documents.search.tantivy import TantivyProvider


class EndpointSearchProvider(TantivyProvider):
    """Use a controllable HTTP endpoint for replay-safe finite work."""

    replay_safe = True

    def wipe(self) -> None:
        """Complete only after the endpoint acknowledges the request."""
        request = Request(
            os.environ["PAPERLESS_VALIDATION_ENDPOINT"],
            headers={"X-Processor-Pid": str(os.getpid())},
        )
        with urlopen(request, timeout=60) as response:
            assert response.status == 200


def main() -> None:
    """Check retries, offline claims, concurrency, child reuse and idle release."""
    if os.environ.get("PAPERLESS_PROCESSOR_VALIDATION") != "1":
        raise RuntimeError("Requires an explicitly isolated validation deployment")
    import django

    django.setup()
    from django.contrib.auth.models import User
    from django.core.management import call_command
    from django.test import override_settings
    from django.utils import timezone
    from rest_framework.test import APIClient

    from documents.deferred_operations import recover_waiting
    from documents.models import DocumentJob
    from documents.search import get_backend
    from documents.search_operations import run_search
    from documents.search_operations import submit_search

    assert not DocumentJob.objects.exists(), "Requires an empty disposable database"
    call_command("document_processor_account", user="validation", capability=["search"])
    workstation = User.objects.create_superuser(username="workstation", password=None)
    state = {"calls": 0, "failures": 0, "block_next": False}
    execution_pids = []
    gate = threading.Event()
    gate.set()

    class Endpoint(BaseHTTPRequestHandler):
        """Expose controlled outages and long work without changing production code."""

        def do_GET(self) -> None:
            execution_pids.append(int(self.headers["X-Processor-Pid"]))
            state["calls"] += 1
            if state["block_next"]:
                state["block_next"] = False
                gate.wait(60)
            failed = state["failures"] > 0
            if failed:
                state["failures"] -= 1
            self.send_response(503 if failed else 200)
            self.end_headers()

        def log_message(self, *args) -> None:
            """Keep output limited to assertions and results."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["PAPERLESS_VALIDATION_ENDPOINT"] = (
        f"http://127.0.0.1:{server.server_port}/"
    )
    worker = None
    observed_children = set()

    def children() -> set[int]:
        """Read direct children, including pool processes and outcome helpers."""
        if worker is None:
            return set()
        path = Path(f"/proc/{worker.pid}/task/{worker.pid}/children")
        pids = {int(pid) for pid in path.read_text().split()}
        observed_children.update(pids)
        return pids

    def wait_for(predicate, timeout=90) -> None:
        """Bound each asynchronous assertion independently."""
        until = time.monotonic() + timeout
        while not predicate():
            children()
            if time.monotonic() >= until:
                raise AssertionError("Celery condition did not complete")
            time.sleep(0.1)

    def entry(job) -> dict:
        """Read committed operation state."""
        job.refresh_from_db()
        return job.result["invocations"]["maintenance"]

    def admit() -> DocumentJob:
        """Use ordinary admission and Celery dispatch."""
        return submit_search(get_backend().provider, "wipe", {})

    def finish(job) -> None:
        """Require authoritative completion, including validation in the same task."""
        wait_for(lambda: DocumentJob.objects.get(pk=job.pk).status == "succeeded")
        assert entry(job)["status"] == "succeeded"

    with tempfile.TemporaryFile(mode="w+") as log:
        try:
            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from paperless_worker.bootstrap import main; main()",
                ],
                cwd=Path(__file__).resolve().parents[2] / "src",
                stdout=log,
                stderr=log,
            )
            wait_for(lambda: b"ready." in os.pread(log.fileno(), 2_000_000, 0))
            assert children() == set()

            # Build a durable unselected operation using the shared finite search
            # fixture, then place its capability on a workstation for this check.
            with patch("documents.search_operations.run_search.apply_async"):
                offline = admit()
            with override_settings(PROCESSOR_CAPABILITIES=()):
                run_search(str(offline.pk))
            offline.refresh_from_db()
            offline.status = DocumentJob.Status.WAITING
            offline.result["invocations"]["maintenance"].update(
                operation="metadata.classify",
                policy={"permission": "documents.process_metadata"},
                status="pending",
                attempts=0,
                result=None,
            )
            for field in ("token", "processor", "lease_until", "processor_location"):
                offline.result["invocations"]["maintenance"].pop(field, None)
            offline.updated = timezone.now() - timedelta(days=8)
            offline.save(update_fields=["status", "result", "updated"])
            previous_calls = state["calls"]
            for _ in range(3):
                recover_waiting()
            time.sleep(1)
            assert state["calls"] == previous_calls
            assert entry(offline)["attempts"] == 0
            assert offline.status == DocumentJob.Status.WAITING
            assert not children()
            assert str(offline.pk).encode() not in os.pread(log.fileno(), 2_000_000, 0)

            # One long operation occupies a child; another must still complete.
            state["block_next"] = True
            gate.clear()
            blocked = admit()
            wait_for(lambda: state["calls"] == previous_calls + 1)
            unrelated = admit()
            finish(unrelated)
            assert DocumentJob.objects.get(pk=blocked.pk).status != "succeeded"
            assert len(children()) == 2
            gate.set()
            finish(blocked)

            client = APIClient()
            client.force_authenticate(workstation)
            base = f"/api/operations/{offline.pk}/maintenance/"
            response = client.post(base + "claim/")
            assert response.status_code == 200, response.content
            leased = response.json()
            response = client.post(
                base + "result/",
                {"token": leased["token"], "result": None},
                format="json",
            )
            assert response.status_code == 202, response.content
            finish(offline)
            assert entry(offline)["attempts"] == 1

            warmed_pids = set(execution_pids)
            before_reuse = len(execution_pids)
            started = time.monotonic()
            for _ in range(4):
                finish(admit())
            reuse_seconds = time.monotonic() - started
            warm_pids = execution_pids[before_reuse:]
            assert len(warm_pids) == 4
            assert set(warm_pids) <= warmed_pids
            assert len(set(warm_pids)) < len(warm_pids), (
                "Warm tasks did not reuse an initialized executing child"
            )

            state["failures"] = 1
            job = admit()
            finish(job)
            assert entry(job)["attempts"] == 2

            state["failures"] = 10
            job = admit()
            wait_for(lambda: DocumentJob.objects.get(pk=job.pk).status == "failed")
            assert entry(job)["attempts"] == 3
            assert "503" in job.error
            state["failures"] = 0

            # Kill the child during safe work and recover only after lease expiry.
            state["block_next"] = True
            gate.clear()
            previous_calls = state["calls"]
            job = admit()
            wait_for(lambda: state["calls"] == previous_calls + 1)
            leased_entry = entry(job)
            # Find the active task's pid through Celery's standard inspection.
            from paperless.celery import app

            active = app.control.inspect(timeout=5).active()
            task = next(
                task
                for tasks in active.values()
                for task in tasks
                if task["id"] == str(job.pk)
            )
            os.kill(task["worker_pid"], signal.SIGKILL)
            wait_for(lambda: task["worker_pid"] not in children())
            gate.set()
            leased_entry["lease_until"] = (
                timezone.now() - timedelta(seconds=1)
            ).isoformat()
            job.result["invocations"]["maintenance"] = leased_entry
            job.save(update_fields=["result"])
            recover_waiting()
            finish(job)
            assert entry(job)["attempts"] == 2

            wait_for(lambda: not children(), timeout=90)
            assert not children()
            print(
                json.dumps(
                    {
                        "broker": "Redis 8",
                        "max_tasks_per_child": int(
                            os.environ.get("PAPERLESS_WORKER_MAX_TASKS_PER_CHILD", "10")
                        ),
                        "direct_child_pids_observed": len(observed_children),
                        "warm_task_execution_pids": warm_pids,
                        "four_warm_tasks_seconds": reuse_seconds,
                        "offline_eight_day_age_no_attempts_or_provider_calls": True,
                        "workstation_claim_result_continuation": True,
                        "concurrency_two": True,
                        "child_reuse": True,
                        "idle_children": 0,
                        "active_child_crash_recovery": True,
                        "bounded_endpoint_retries": True,
                    }
                )
            )
        except BaseException:
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            gate.set()
            if worker is not None and worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=30)
            server.shutdown()


if __name__ == "__main__":
    main()
