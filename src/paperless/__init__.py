"""Public application exports, loaded when framework integration is requested."""

from importlib import import_module

__all__ = [
    "audit_log_check",
    "binaries_check",
    "celery_app",
    "paths_check",
    "settings_values_check",
]


def __getattr__(name: str):
    """Keep standalone parser contracts independent of Django initialization."""
    if name == "celery_app":
        value = import_module("paperless.celery").app
    elif name in __all__:
        value = getattr(import_module("paperless.checks"), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
