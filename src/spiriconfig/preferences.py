"""Where a person's UI preferences are stored.

This module exists to be *one* seam. Today SpiriConfig has no concept of a user,
so a preference belongs to a browser: NiceGUI keeps a session cookie, and we hang
the preference off that. When real users arrive, a preference should follow the
person across their devices instead.

Rather than sprinkling ``app.storage.user`` through the codebase and having to
find every call site later, everything goes through :func:`preferences`. Adding
user support then means registering a different store here, once:

.. code-block:: python

    from spiriconfig import preferences

    class DatabasePreferences:
        def __init__(self, user_id: str) -> None:
            self.user_id = user_id

        def get(self, key, default):
            return db.fetch_preference(self.user_id, key, default)

        def set(self, key, value) -> None:
            db.store_preference(self.user_id, key, value)

    preferences.use(lambda: DatabasePreferences(current_user().id))

Nothing else changes. Plugins call :func:`spiriconfig.advanced.enabled`; they do
not know, and must not care, whether that preference is keyed on a cookie or on a
person.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from loguru import logger
from nicegui import app


@runtime_checkable
class PreferenceStore(Protocol):
    """Somewhere to read and write one person's UI preferences."""

    def get(self, key: str, default: Any) -> Any:
        """Return the stored value for ``key``, or ``default``."""
        ...

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``."""
        ...


class BrowserPreferences:
    """Preferences kept against NiceGUI's per-browser session cookie.

    The default store, and the correct one while there are no users: it is the
    finest-grained identity we actually have. It survives a reload and is shared
    across a browser's tabs, but not across devices -- which is exactly the
    limitation that user support will lift.

    Requires a storage secret (see :attr:`~spiriconfig.config.Settings.storage_secret`),
    because the cookie that identifies the browser is signed.
    """

    def get(self, key: str, default: Any) -> Any:
        return app.storage.user.get(key, default)

    def set(self, key: str, value: Any) -> None:
        app.storage.user[key] = value


_resolve: Callable[[], PreferenceStore] = BrowserPreferences


def use(factory: Callable[[], PreferenceStore]) -> None:
    """Register the store that :func:`preferences` will return from now on.

    ``factory`` is called per access rather than once, so it can resolve the
    *current* person from request context -- which is what a user system needs.
    """
    global _resolve  # noqa: PLW0603 - a deliberate, documented process-wide seam
    _resolve = factory
    logger.debug("preference store is now {}", factory)


def reset() -> None:
    """Restore the default per-browser store. Mostly for tests."""
    global _resolve  # noqa: PLW0603
    _resolve = BrowserPreferences


def preferences() -> PreferenceStore:
    """Return the preference store for whoever is being served right now."""
    return _resolve()
