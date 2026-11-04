"""Traditional foreground services, loaded only when explicitly selected."""

import os
import sys


class Consumer:
    """Run the existing Django consumer after Docker's initial system checks."""

    def run(self) -> None:
        """Replace this process so supervision and signals reach the consumer."""
        os.execv(
            sys.executable,
            [sys.executable, "manage.py", "document_consumer", "--skip-checks"],
        )


class Scheduler:
    """Run the existing Celery beat scheduler with its normal persistence."""

    def run(self) -> None:
        """Replace this process so supervision and signals reach Celery beat."""
        os.environ["CELERY_SKIP_CHECKS"] = "1"
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-m",
                "celery",
                "--skip-checks",
                "--app",
                "paperless",
                "beat",
                "--loglevel",
                "INFO",
            ],
        )
