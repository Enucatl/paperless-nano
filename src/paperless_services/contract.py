"""Minimal service lifecycle, independent of Django and application settings."""

from typing import Protocol


class Service(Protocol):
    """A no-argument implementation supervised as one foreground process.

    run() blocks until shutdown. Implementations own termination signals,
    cleanup, retries and service compatibility. They may exec another foreground
    process; they must not daemonize. Startup/runtime failures must propagate
    or exit nonzero so the supervisor can observe and restart them.
    """

    def run(self) -> None:
        """Run until terminated, releasing owned resources before returning."""
        ...
