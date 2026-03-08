from __future__ import annotations

import datetime as dt
import re
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .models import CheckResult


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)


def _local_datetime(timestamp: Optional[float] = None) -> dt.datetime:
    seconds = time.time() if timestamp is None else float(timestamp)
    # Convert through UTC first so small epoch-adjacent timestamps work on Windows too.
    return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).astimezone()


def now_local_iso(timestamp: Optional[float] = None, *, milliseconds: bool = False) -> str:
    value = _local_datetime(timestamp)
    timespec = "milliseconds" if milliseconds else "seconds"
    return value.isoformat(timespec=timespec)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def format_duration(seconds: float) -> str:
    if seconds >= 60:
        minutes, remainder = divmod(int(seconds), 60)
        return f"{minutes}m{remainder:02d}s"
    if seconds >= 10:
        return f"{seconds:.0f}s"
    return f"{seconds:.1f}s"


def format_compact_span(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    units = (
        ("d", 86400),
        ("h", 3600),
        ("m", 60),
        ("s", 1),
    )
    remaining = int(seconds)
    parts: list[str] = []
    for suffix, size in units:
        if remaining >= size:
            value, remaining = divmod(remaining, size)
            parts.append(f"{value}{suffix}")
        if len(parts) == 2:
            break
    return "".join(parts) if parts else f"{seconds}s"


def abbreviate_count(value: int) -> str:
    absolute = abs(value)
    if absolute < 1000:
        return str(value)
    units = (
        (1_000_000_000, "b"),
        (1_000_000, "m"),
        (1_000, "k"),
    )
    for divisor, suffix in units:
        if absolute >= divisor:
            scaled = value / float(divisor)
            if abs(scaled) >= 100:
                return f"{scaled:.0f}{suffix}"
            if abs(scaled) >= 10:
                return f"{scaled:.1f}".rstrip("0").rstrip(".") + suffix
            return f"{scaled:.2f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def abbreviate_ratio(left: int, right: int) -> str:
    return f"{abbreviate_count(left)}/{abbreviate_count(right)}"


def parse_duration_input(raw_value: str) -> int:
    match = _DURATION_RE.match(raw_value.strip())
    if not match:
        raise ValueError("expected a number with optional s/m/h/d suffix")
    value = float(match.group(1))
    unit = match.group(2).lower() or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    seconds = int(value * multiplier)
    if seconds <= 0:
        raise ValueError("duration must be greater than zero")
    return seconds


def format_latency(latency_ms: Optional[float]) -> str:
    if latency_ms is None:
        return "-"
    if latency_ms >= 1000:
        return f"{latency_ms / 1000.0:.2f}s"
    if latency_ms >= 100:
        return f"{latency_ms:.0f}ms"
    return f"{latency_ms:.1f}ms"


def format_timestamp_short(timestamp: float) -> str:
    return _local_datetime(timestamp).strftime("%H:%M:%S")


def shorten(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def human_error_message(result: "CheckResult") -> str:
    if result.error_message:
        return result.error_message
    if result.error_category == "ok":
        return "ok"
    return result.error_category.replace("_", " ")
