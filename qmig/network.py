"""Waiting for the network to come back.

When a request fails because the connection is gone (no route, DNS, refused, reset, timeout, TLS), the run does not
count a failed item.  It pauses, probes the instance with a cheap request and continues as soon as the instance
answers.  The pause starts at ``interval`` seconds and doubles up to ``max_interval``; after ``retries`` probes
without an answer :class:`NetworkInterrupted` ends the run.  Everything created so far is in SQLite, so running the
same command again continues where it stopped.

Defaults (10 retries, 30 s first pause, 600 s longest pause: about 1 h 10 min) can be changed with
``--network-retries``, ``--network-retry-interval``, ``--network-retry-max-interval`` or the environment variables
``QMIG_NETWORK_RETRIES``, ``QMIG_NETWORK_RETRY_INTERVAL``, ``QMIG_NETWORK_RETRY_MAX_INTERVAL``.
``retries = 0`` switches the wait off.
"""
from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass

DEFAULT_RETRIES = 10
DEFAULT_INTERVAL = 30.0
DEFAULT_MAX_INTERVAL = 600.0


class NetworkInterrupted(RuntimeError):
    """The network did not come back within the configured retries; the run stopped cleanly."""

    def __init__(self, instance: str, retries: int, waited: float, why: str = ""):
        self.instance, self.retries, self.waited = instance, retries, waited
        super().__init__(f"[{instance}] no answer after {retries} retries (waited {format_seconds(waited)})"
                         + (f": {why}" if why else ""))


@dataclass(frozen=True)
class NetworkPolicy:
    retries: int = DEFAULT_RETRIES
    interval: float = DEFAULT_INTERVAL
    max_interval: float = DEFAULT_MAX_INTERVAL

    @property
    def enabled(self) -> bool:
        return self.retries > 0

    def delay(self, number: int) -> float:
        """Pause before retry ``number`` (1-based): interval, 2x, 4x ... capped at max_interval."""
        return min(self.interval * (2 ** (number - 1)), max(self.max_interval, self.interval))


OFF = NetworkPolicy(retries=0)


def is_unsent(exc: BaseException) -> bool:
    """True when the failure proves the request never reached the server (safe to repeat a write)."""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (socket.gaierror, ConnectionRefusedError))


def parse_seconds(text: str | float | int, *, label: str = "value") -> float:
    """``90``, ``30s``, ``10m``, ``2h`` -> seconds."""
    if isinstance(text, (int, float)):
        return float(text)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(text))
    if not match:
        raise ValueError(f"{label}: expected a number of seconds or a value like 30s, 10m, 2h (got {text!r})")
    return float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


def format_seconds(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return " ".join(p for p in (f"{hours} h" if hours else "", f"{minutes} min" if minutes else "",
                                f"{secs} s" if secs or not seconds else "") if p)


def policy_from(retries=None, interval=None, max_interval=None) -> NetworkPolicy:
    """Command line value, then environment variable, then default."""
    def pick(value, env, default):
        return value if value not in (None, "") else (os.environ.get(env) or default)

    count = pick(retries, "QMIG_NETWORK_RETRIES", DEFAULT_RETRIES)
    try:
        count = int(count)
    except ValueError:
        raise ValueError(f"network retries: expected a whole number (got {count!r})") from None
    if count < 0:
        raise ValueError("network retries must be 0 or more")
    first = parse_seconds(pick(interval, "QMIG_NETWORK_RETRY_INTERVAL", DEFAULT_INTERVAL), label="network retry interval")
    longest = parse_seconds(pick(max_interval, "QMIG_NETWORK_RETRY_MAX_INTERVAL", DEFAULT_MAX_INTERVAL),
                            label="network retry max interval")
    return NetworkPolicy(count, first, longest)
