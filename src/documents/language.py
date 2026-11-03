"""Detect document language without retaining language profiles in the web process."""

import re
import subprocess
import sys
from pathlib import Path


def detect_language(content: str) -> str:
    """Run the existing detector with request-scoped model residency.

    Text travels through a private pipe, never command arguments or files.
    The caller retains its existing fallback when detection fails.
    """
    if not content:
        return "en"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        input=content,
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=30,
    )
    language = result.stdout.strip()
    if re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,3})?", language) is None:
        raise ValueError("Invalid language detection result")
    return language


if __name__ == "__main__":
    from langdetect import detect

    sys.stdout.write(detect(sys.stdin.buffer.read().decode("utf-8")))
