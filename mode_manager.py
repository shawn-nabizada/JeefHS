"""Centralised Home/Away/Night mode management."""

from __future__ import annotations

import logging
import threading
from typing import Callable, List

logger = logging.getLogger(__name__)


class ModeManager:
    """Tracks the active security mode and notifies listeners on change."""

    VALID_MODES = ("HOME", "AWAY", "NIGHT")

    def __init__(self, initial_mode: str = "HOME") -> None:
        self._lock = threading.RLock()
        self._mode = self._normalise(initial_mode)
        self._callbacks: List[Callable[[str], None]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, new_mode: str) -> bool:
        candidate = self._normalise(new_mode)
        with self._lock:
            if candidate == self._mode:
                logger.debug("Mode unchanged (%s)", candidate)
                return False
            self._mode = candidate
            callbacks = list(self._callbacks)

        logger.info("System mode changed to %s", candidate)
        for callback in callbacks:
            try:
                callback(candidate)
            except Exception:  # pragma: no cover - defensive logging
                logger.exception("Mode change callback failed")
        return True

    def register_callback(self, callback: Callable[[str], None]) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _normalise(self, mode: str | None) -> str:
        candidate = (mode or "HOME").strip().upper()
        if candidate not in self.VALID_MODES:
            raise ValueError(f"Unsupported mode '{mode}'")
        return candidate