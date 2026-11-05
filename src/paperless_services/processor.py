"""Supervise finite server sweeps without retaining Django or model state."""

import logging
import os
import signal
import subprocess
import sys
import threading

logger = logging.getLogger("paperless.processor")


class Processor:
    """Run explicitly enabled capabilities in disposable foreground children."""

    def run(self) -> None:
        """Forward shutdown to active work and release imports between sweeps."""
        user = os.environ.get("PAPERLESS_PROCESSOR_USER", "")
        capabilities = os.environ.get("PAPERLESS_PROCESSOR_CAPABILITIES", "").split()
        if not user or not capabilities:
            raise ValueError(
                "Set PAPERLESS_PROCESSOR_USER and PAPERLESS_PROCESSOR_CAPABILITIES to enable server processing"
            )
        if set(capabilities) - {"parser", "metadata", "search", "ingestion"}:
            raise ValueError("Unknown PAPERLESS_PROCESSOR_CAPABILITIES value")
        command = [
            sys.executable,
            "manage.py",
            "document_processor",
            "--skip-checks",
            "--user",
            user,
            "--configured",
        ]
        for capability in capabilities:
            command.extend(["--capability", capability])
        stopped = threading.Event()
        child = None

        def stop(signum: int, frame) -> None:
            """Stop admitting sweeps and signal the whole active process group."""
            stopped.set()
            if child is not None and child.poll() is None:
                try:
                    os.killpg(child.pid, signum)
                except ProcessLookupError:
                    pass

        previous = {
            number: signal.signal(number, stop)
            for number in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            while not stopped.is_set():
                child = subprocess.Popen(command, start_new_session=True)
                if stopped.is_set():
                    stop(signal.SIGTERM, None)
                returncode = child.wait()
                child = None
                if returncode and not stopped.is_set():
                    logger.error(
                        "Processor sweep exited %s; check account, configuration and Tasks",
                        returncode,
                    )
                stopped.wait(10)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
