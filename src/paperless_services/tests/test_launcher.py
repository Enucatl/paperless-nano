"""Selection and lifecycle boundaries without importing Django in the parent."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from paperless_services import launcher
from paperless_services.traditional import Consumer
from paperless_services.traditional import Scheduler


@pytest.mark.parametrize("service", launcher.DEFAULTS)
def test_custom_service(service: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every service selects, constructs and invokes an installed class directly."""
    monkeypatch.setenv(f"PAPERLESS_{service.upper()}_CLASS", "example.Custom")
    with patch.object(launcher, "import_module") as module:
        launcher.run(service)
    module.assert_called_once_with("example")
    module.return_value.Custom.assert_called_once_with()
    module.return_value.Custom.return_value.run.assert_called_once_with()


@pytest.mark.parametrize("service", launcher.DEFAULTS)
def test_default_service(service: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset class selection preserves each traditional implementation."""
    monkeypatch.delenv(f"PAPERLESS_{service.upper()}_CLASS", raising=False)
    with patch.object(launcher, "import_module") as module:
        launcher.run(service)
    path, name = launcher.DEFAULTS[service].rsplit(".", 1)
    module.assert_called_once_with(path)
    getattr(module.return_value, name).return_value.run.assert_called_once_with()


@pytest.mark.parametrize("service", launcher.LEGACY_FLAGS)
@pytest.mark.parametrize("value", ["", "0", "1"])
def test_retired_flag_fails(
    service: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even conflicting or formerly false values require explicit migration."""
    monkeypatch.setenv(launcher.LEGACY_FLAGS[service], value)
    monkeypatch.setenv(f"PAPERLESS_{service.upper()}_CLASS", "example.Custom")
    with patch.object(launcher, "import_module") as module:
        with pytest.raises(ValueError, match="was retired"):
            launcher.run(service)
    module.assert_not_called()


@pytest.mark.parametrize(
    "path",
    ["", "invalid", "missing.service.Class", "paperless_services.launcher.Missing"],
)
def test_invalid_selection_fails(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid imports fail visibly through normal supervisor startup."""
    monkeypatch.setenv("PAPERLESS_CONSUMER_CLASS", path)
    with pytest.raises((ValueError, ModuleNotFoundError, AttributeError)):
        launcher.run("consumer")


def test_startup_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher never hides implementation startup failures."""
    monkeypatch.setenv("PAPERLESS_CONSUMER_CLASS", "example.Custom")
    with patch.object(launcher, "import_module") as module:
        module.return_value.Custom.return_value.run.side_effect = RuntimeError(
            "startup"
        )
        with pytest.raises(RuntimeError, match="startup"):
            launcher.run("consumer")


@pytest.mark.parametrize(
    ("service", "path", "target"),
    [
        (
            "processor",
            "paperless_services.processor.Processor",
            "paperless_services.processor.Processor.run",
        ),
        (
            "consumer",
            "paperless_services.watcher.APIConsumer",
            "paperless_services.watcher.main",
        ),
        (
            "scheduler",
            "paperless_services.scheduler.APIScheduler",
            "paperless_services.scheduler.run",
        ),
        (
            "worker",
            "paperless_worker.bootstrap.LeanWorker",
            "paperless_worker.bootstrap.main",
        ),
    ],
)
def test_lightweight_boundary(service: str, path: str, target: str) -> None:
    """Selected lightweight startup imports neither Django nor heavyweight peers."""
    code = f"""
import os, sys
from unittest.mock import patch
from paperless_services import contract, launcher
os.environ["PAPERLESS_{service.upper()}_CLASS"] = {path!r}
with patch({target!r}) as run:
    launcher.run({service!r})
run.assert_called_once()
assert "django.conf" not in sys.modules
assert "django.apps" not in sys.modules
assert "paperless.celery" not in sys.modules
assert "paperless.worker" not in sys.modules
assert "paperless_services.traditional" not in sys.modules
"""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PAPERLESS_", "CELERY_", "DJANGO_"))
    }
    env["PAPERLESS_API_TOKEN"] = "test"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_traditional_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exec preserves foreground signals and initial-check skipping."""
    with patch("os.execv") as execute:
        Consumer().run()
        execute.assert_called_once_with(
            sys.executable,
            [sys.executable, "manage.py", "document_consumer", "--skip-checks"],
        )
    monkeypatch.delenv("CELERY_SKIP_CHECKS", raising=False)
    with patch("os.execv") as execute:
        Scheduler().run()
        assert execute.call_args.args == (
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
    assert os.environ["CELERY_SKIP_CHECKS"] == "1"


def test_launcher_interface_imports_no_django() -> None:
    """Selection and lifecycle imports are completely Django-free."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from paperless_services import launcher, contract; assert not any(name == 'django' or name.startswith('django.') for name in sys.modules)",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
