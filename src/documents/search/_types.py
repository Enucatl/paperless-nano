"""Plain inputs for search execution."""

from dataclasses import dataclass
from datetime import tzinfo


@dataclass(frozen=True)
class QueryConfiguration:
    """Effective language, fuzzy threshold and request timezone."""

    language: str | None
    fuzzy_threshold: float | None
    timezone: tzinfo


@dataclass(frozen=True)
class SearchVisibility:
    """A user's ID and current group IDs, independent of the ORM."""

    user_id: int
    group_ids: tuple[int, ...]
