"""Unified retry-cap policy — replaces the three ad-hoc
`if attempts >= cfg.X.Y: escalate` checks previously duplicated across
advance.py (checks-retry-count, resolver-attempts, ship-retry-count) with one
implementation."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int

    def attempts_remaining(self, current: int) -> int:
        """How many more attempts are allowed before exhaustion. Never
        negative — a `current` already at or past the cap reports 0, not a
        negative number, so callers can use this directly as a countdown."""
        return max(0, self.max_attempts - current)

    def exhausted(self, current: int) -> bool:
        """True once `current` (attempts already made) has reached or passed
        max_attempts — same boundary as the original `attempts >= cfg.X.Y`
        checks it replaces (>=, not >), so behavior is unchanged."""
        return current >= self.max_attempts
