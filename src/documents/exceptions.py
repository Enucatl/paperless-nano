"""Exceptions shared across document task registration and implementations."""


class SearchWriteBusy(Exception):
    """A provider declares a write safe to retry against fresh document state."""


class SearchIndexLockError(SearchWriteBusy):
    """Raised when the search index file lock cannot be acquired within the timeout."""


class ConsumerError(Exception):
    """Report consumption failures without importing processing code to unpickle."""
