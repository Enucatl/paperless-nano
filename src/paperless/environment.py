"""Load configuration shared by Django and lightweight service processes."""

import os
from pathlib import Path

from dotenv import load_dotenv


def load_configuration() -> None:
    """Load the first existing configuration file without overriding environment."""
    for path in [
        os.getenv("PAPERLESS_CONFIGURATION_PATH"),
        "../paperless.conf",
        "/etc/paperless.conf",
        "/usr/local/etc/paperless.conf",
    ]:
        if path and Path(path).exists():
            load_dotenv(path)
            break
