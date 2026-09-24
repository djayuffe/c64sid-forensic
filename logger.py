"""Small, dependency-free logging facade used by the emulator core."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any


class SystemLogger:
    """Opt-in category logger compatible with the original host application API."""

    _categories: set[str] = set()
    _last: dict[tuple[str, str], float] = {}
    _logger = logging.getLogger("c64sid")

    @classmethod
    def enable_categories(cls, *categories: str) -> None:
        cls._categories.update(category.lower() for category in categories)

    @classmethod
    def disable_categories(cls, *categories: str) -> None:
        for category in categories:
            cls._categories.discard(category.lower())

    @classmethod
    def category_enabled(cls, category: str) -> bool:
        return category.lower() in cls._categories

    @classmethod
    def rate_limit(cls, component: str, event: str, seconds: float) -> bool:
        key = (component, event)
        now = time.monotonic()
        previous = cls._last.get(key, float("-inf"))
        if now - previous < seconds:
            return False
        cls._last[key] = now
        return True

    @classmethod
    def log(
        cls,
        component: str,
        message: str,
        level: str = "info",
        *,
        category: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        if level == "debug" and category and not cls.category_enabled(category):
            return
        log_level = getattr(logging, level.upper(), logging.INFO)
        suffix = f" {dict(fields)}" if fields else ""
        cls._logger.log(log_level, "%s: %s%s", component, message, suffix)
