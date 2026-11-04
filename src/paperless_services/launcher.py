"""Load one explicitly selected service without importing Django settings."""

import argparse
import os
from importlib import import_module

DEFAULTS = {
    "consumer": "paperless_services.traditional.Consumer",
    "scheduler": "paperless_services.traditional.Scheduler",
    "worker": "paperless.worker.Worker",
}
LEGACY_FLAGS = {
    "consumer": "PAPERLESS_USE_API_CONSUMER",
    "scheduler": "PAPERLESS_USE_API_SCHEDULER",
    "worker": "PAPERLESS_USE_LEAN_WORKER",
}


def run(service: str) -> None:
    """Construct and run only the selected installed implementation."""
    setting = f"PAPERLESS_{service.upper()}_CLASS"
    legacy = LEGACY_FLAGS[service]
    if legacy in os.environ:
        raise ValueError(
            f"{legacy} was retired; remove it and select {setting} instead"
        )
    path = os.environ.get(setting, DEFAULTS[service])
    module, name = path.rsplit(".", 1)
    implementation = getattr(import_module(module), name)
    implementation().run()


def main() -> None:
    """Select a supervised consumer, scheduler or worker from the environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=DEFAULTS)
    run(parser.parse_args().service)


if __name__ == "__main__":
    main()
