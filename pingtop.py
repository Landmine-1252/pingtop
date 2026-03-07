from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import ipaddress
import json
import math
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Optional

if os.name != "nt":
    import termios
    import tty
else:
    import ctypes
    import msvcrt


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "pingtop.json"
LOG_PATH = APP_DIR / "pingtop_log.csv"
SNAPSHOT_PREFIX = "pingtop_snapshot_"
DEFAULT_TARGET_VALUES = [
    "1.1.1.1",
    "8.8.8.8",
    "google.com",
    "cloudflare.com",
    "microsoft.com",
]
LOGGING_MODES = ("all", "failures_only", "around_failure")
ROTATED_LOG_GLOB = "pingtop_log_*.csv"
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)


def now_local_iso(timestamp: Optional[float] = None, *, milliseconds: bool = False) -> str:
    value = dt.datetime.fromtimestamp(timestamp or time.time()).astimezone()
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
    return dt.datetime.fromtimestamp(timestamp).astimezone().strftime("%H:%M:%S")


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


def infer_target(value: str) -> "TargetSpec":
    raw = value.strip()
    if not raw:
        raise ValueError("target cannot be empty")
    try:
        normalized = str(ipaddress.ip_address(raw))
        return TargetSpec(value=normalized, kind="ip")
    except ValueError:
        hostname = raw.strip().lower().rstrip(".")
        if not hostname:
            raise ValueError("hostname cannot be empty")
        if any(ch.isspace() for ch in hostname):
            raise ValueError("hostname cannot contain whitespace")
        return TargetSpec(value=hostname, kind="hostname")


@dataclass
class TargetSpec:
    value: str
    kind: str

    @classmethod
    def from_dict(cls, data: object) -> "TargetSpec":
        if isinstance(data, str):
            return infer_target(data)
        if not isinstance(data, dict):
            raise ValueError("target entry must be a string or object")
        value = str(data.get("value", "")).strip()
        kind = str(data.get("type", "")).strip().lower()
        target = infer_target(value)
        if kind and kind != target.kind:
            raise ValueError(f"target type mismatch for {value}")
        return target

    def to_dict(self) -> dict[str, str]:
        return {"value": self.value, "type": self.kind}


@dataclass
class AppConfig:
    version: int = 1
    check_interval_seconds: float = 5.0
    ping_timeout_ms: int = 1200
    ui_refresh_interval_seconds: float = 0.5
    stats_window_seconds: int = 3600
    latency_warning_ms: int = 100
    latency_critical_ms: int = 250
    logging_mode: str = "around_failure"
    around_failure_before_seconds: int = 15
    around_failure_after_seconds: int = 15
    log_rotation_max_mb: int = 25
    log_rotation_keep_files: int = 10
    event_history_size: int = 40
    visible_event_lines: int = 8
    targets: list[TargetSpec] = field(default_factory=list)

    @classmethod
    def default(cls) -> "AppConfig":
        return cls(targets=[infer_target(value) for value in DEFAULT_TARGET_VALUES])

    @classmethod
    def from_dict(cls, data: object) -> "AppConfig":
        base = cls.default()
        if not isinstance(data, dict):
            base.normalize()
            return base
        targets_data = data.get("targets", DEFAULT_TARGET_VALUES)
        targets: list[TargetSpec] = []
        if isinstance(targets_data, list):
            for item in targets_data:
                try:
                    targets.append(TargetSpec.from_dict(item))
                except ValueError:
                    continue
        config = cls(
            version=int(data.get("version", base.version)),
            check_interval_seconds=float(data.get("check_interval_seconds", base.check_interval_seconds)),
            ping_timeout_ms=int(data.get("ping_timeout_ms", base.ping_timeout_ms)),
            ui_refresh_interval_seconds=float(
                data.get("ui_refresh_interval_seconds", base.ui_refresh_interval_seconds)
            ),
            stats_window_seconds=int(data.get("stats_window_seconds", base.stats_window_seconds)),
            latency_warning_ms=int(data.get("latency_warning_ms", base.latency_warning_ms)),
            latency_critical_ms=int(data.get("latency_critical_ms", base.latency_critical_ms)),
            logging_mode=str(data.get("logging_mode", base.logging_mode)),
            around_failure_before_seconds=int(
                data.get("around_failure_before_seconds", base.around_failure_before_seconds)
            ),
            around_failure_after_seconds=int(
                data.get("around_failure_after_seconds", base.around_failure_after_seconds)
            ),
            log_rotation_max_mb=int(data.get("log_rotation_max_mb", base.log_rotation_max_mb)),
            log_rotation_keep_files=int(data.get("log_rotation_keep_files", base.log_rotation_keep_files)),
            event_history_size=int(data.get("event_history_size", base.event_history_size)),
            visible_event_lines=int(data.get("visible_event_lines", base.visible_event_lines)),
            targets=targets or base.targets,
        )
        config.normalize()
        return config

    def normalize(self) -> None:
        self.version = 1
        self.check_interval_seconds = round(clamp(float(self.check_interval_seconds), 0.5, 300.0), 2)
        self.ping_timeout_ms = int(clamp(float(self.ping_timeout_ms), 250.0, 30000.0))
        self.ui_refresh_interval_seconds = round(
            clamp(float(self.ui_refresh_interval_seconds), 0.1, 5.0),
            2,
        )
        self.stats_window_seconds = int(clamp(float(self.stats_window_seconds), 30.0, 2_592_000.0))
        self.latency_warning_ms = int(clamp(float(self.latency_warning_ms), 10.0, 10000.0))
        self.latency_critical_ms = int(clamp(float(self.latency_critical_ms), 10.0, 10000.0))
        if self.latency_critical_ms < self.latency_warning_ms:
            self.latency_critical_ms = self.latency_warning_ms
        if self.logging_mode not in LOGGING_MODES:
            self.logging_mode = "around_failure"
        self.around_failure_before_seconds = int(
            clamp(float(self.around_failure_before_seconds), 0.0, 600.0)
        )
        self.around_failure_after_seconds = int(
            clamp(float(self.around_failure_after_seconds), 0.0, 600.0)
        )
        self.log_rotation_max_mb = int(clamp(float(self.log_rotation_max_mb), 0.0, 1024.0))
        self.log_rotation_keep_files = int(clamp(float(self.log_rotation_keep_files), 1.0, 100.0))
        self.event_history_size = int(clamp(float(self.event_history_size), 10.0, 200.0))
        self.visible_event_lines = int(clamp(float(self.visible_event_lines), 3.0, 20.0))

        normalized_targets: list[TargetSpec] = []
        seen: set[str] = set()
        for target in self.targets:
            try:
                normalized = infer_target(target.value)
            except ValueError:
                continue
            key = f"{normalized.kind}:{normalized.value}"
            if key in seen:
                continue
            seen.add(key)
            normalized_targets.append(normalized)
        self.targets = normalized_targets

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "check_interval_seconds": self.check_interval_seconds,
            "ping_timeout_ms": self.ping_timeout_ms,
            "ui_refresh_interval_seconds": self.ui_refresh_interval_seconds,
            "stats_window_seconds": self.stats_window_seconds,
            "latency_warning_ms": self.latency_warning_ms,
            "latency_critical_ms": self.latency_critical_ms,
            "logging_mode": self.logging_mode,
            "around_failure_before_seconds": self.around_failure_before_seconds,
            "around_failure_after_seconds": self.around_failure_after_seconds,
            "log_rotation_max_mb": self.log_rotation_max_mb,
            "log_rotation_keep_files": self.log_rotation_keep_files,
            "event_history_size": self.event_history_size,
            "visible_event_lines": self.visible_event_lines,
            "targets": [target.to_dict() for target in self.targets],
        }


class ConfigManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.load_warning = ""
        self.config = self._load()

    def _load(self) -> AppConfig:
        if not self.path.exists():
            config = AppConfig.default()
            self._write(config)
            return config
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            config = AppConfig.from_dict(data)
            return config
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self.load_warning = f"Invalid config; using defaults in memory ({exc})"
            return AppConfig.default()

    def _write(self, config: AppConfig) -> None:
        payload = json.dumps(config.to_dict(), indent=2)
        self.path.write_text(payload + "\n", encoding="utf-8")

    def snapshot(self) -> AppConfig:
        with self.lock:
            return copy.deepcopy(self.config)

    def save(self) -> AppConfig:
        with self.lock:
            self.config.normalize()
            self._write(self.config)
            return copy.deepcopy(self.config)

    def update(self, updater) -> AppConfig:
        with self.lock:
            updater(self.config)
            self.config.normalize()
            self._write(self.config)
            return copy.deepcopy(self.config)


@dataclass
class CheckResult:
    sequence: int = 0
    cycle_id: int = 0
    timestamp: float = 0.0
    target: str = ""
    target_type: str = ""
    resolved_ip: str = ""
    dns_success: Optional[bool] = None
    ping_success: bool = False
    latency_ms: Optional[float] = None
    error_category: str = "ok"
    error_message: str = ""
    worker_id: str = ""

    @property
    def is_failure(self) -> bool:
        if self.error_category != "ok":
            return True
        if self.dns_success is False:
            return True
        return not self.ping_success

    @property
    def status_text(self) -> str:
        if self.dns_success is False:
            return "dns_fail"
        if self.ping_success:
            return "up"
        if self.error_category == "ping_unavailable":
            return "no_ping"
        return "ping_fail"


@dataclass
class BufferedLogResult:
    result: CheckResult
    written: bool = False


@dataclass
class EventEntry:
    timestamp: float
    level: str
    message: str


@dataclass
class CounterSummary:
    checks: int = 0
    successes: int = 0
    failures: int = 0
    dns_failures: int = 0
    ping_failures: int = 0

    def observe(self, result: CheckResult) -> None:
        self.checks += 1
        if result.is_failure:
            self.failures += 1
        else:
            self.successes += 1
        if result.dns_success is False:
            self.dns_failures += 1
        elif not result.ping_success:
            self.ping_failures += 1

    def add(self, other: "CounterSummary") -> None:
        self.checks += other.checks
        self.successes += other.successes
        self.failures += other.failures
        self.dns_failures += other.dns_failures
        self.ping_failures += other.ping_failures

    def subtract(self, other: "CounterSummary") -> None:
        self.checks -= other.checks
        self.successes -= other.successes
        self.failures -= other.failures
        self.dns_failures -= other.dns_failures
        self.ping_failures -= other.ping_failures

    def copy(self) -> "CounterSummary":
        return CounterSummary(
            checks=self.checks,
            successes=self.successes,
            failures=self.failures,
            dns_failures=self.dns_failures,
            ping_failures=self.ping_failures,
        )

    @property
    def loss_percentage(self) -> float:
        if self.checks <= 0:
            return 0.0
        return (self.failures / float(self.checks)) * 100.0


@dataclass
class RollingWindowBucket:
    bucket_start: int
    summary: CounterSummary = field(default_factory=CounterSummary)


class RollingWindowCounter:
    def __init__(self, window_seconds: int) -> None:
        self.window_seconds = int(window_seconds)
        self.bucket_seconds = choose_bucket_seconds(self.window_seconds)
        self.buckets: Deque[RollingWindowBucket] = deque()
        self.total = CounterSummary()

    def observe(self, timestamp: float, result: CheckResult) -> None:
        bucket_start = int(timestamp // self.bucket_seconds) * self.bucket_seconds
        if self.buckets and self.buckets[-1].bucket_start == bucket_start:
            bucket = self.buckets[-1]
        else:
            bucket = RollingWindowBucket(bucket_start=bucket_start)
            self.buckets.append(bucket)
        bucket.summary.observe(result)
        self.total.observe(result)
        self.prune(timestamp)

    def prune(self, now_timestamp: float) -> None:
        cutoff = now_timestamp - self.window_seconds
        while self.buckets and self.buckets[0].bucket_start + self.bucket_seconds <= cutoff:
            expired = self.buckets.popleft()
            self.total.subtract(expired.summary)

    def snapshot(self, now_timestamp: float) -> CounterSummary:
        self.prune(now_timestamp)
        return self.total.copy()


def choose_bucket_seconds(window_seconds: int) -> int:
    candidates = (1, 5, 10, 15, 30, 60, 300, 900, 1800, 3600, 7200, 21600, 43200, 86400)
    for candidate in candidates:
        if window_seconds / float(candidate) <= 240:
            return candidate
    return candidates[-1]


@dataclass
class TargetStats:
    target: str
    target_type: str
    total_checks: int = 0
    success_count: int = 0
    failure_count: int = 0
    dns_failure_count: int = 0
    ping_failure_count: int = 0
    consecutive_failures: int = 0
    last_state: str = "unknown"
    last_result: str = "pending"
    last_latency_ms: Optional[float] = None
    last_resolved_ip: str = ""
    last_error_category: str = ""
    last_error_message: str = ""
    last_checked_at: float = 0.0
    window_summary: CounterSummary = field(default_factory=CounterSummary)

    def apply(self, result: CheckResult) -> tuple[str, str]:
        previous_state = self.last_state
        previous_error = self.last_error_category
        self.total_checks += 1
        self.last_checked_at = result.timestamp
        if result.resolved_ip:
            self.last_resolved_ip = result.resolved_ip
        self.last_error_category = result.error_category
        self.last_error_message = result.error_message

        if result.dns_success is False:
            self.failure_count += 1
            self.dns_failure_count += 1
            self.consecutive_failures += 1
            self.last_state = "down"
            self.last_result = "DNS_FAIL"
            self.last_latency_ms = None
        elif result.ping_success:
            self.success_count += 1
            self.consecutive_failures = 0
            self.last_state = "up"
            self.last_result = "UP"
            self.last_latency_ms = result.latency_ms
        else:
            self.failure_count += 1
            self.ping_failure_count += 1
            self.consecutive_failures += 1
            self.last_state = "down"
            self.last_result = "PING_FAIL"
            self.last_latency_ms = None
        return previous_state, previous_error

    @property
    def packet_loss_percentage(self) -> float:
        if self.total_checks <= 0:
            return 0.0
        return (self.failure_count / float(self.total_checks)) * 100.0

    def reset_counters(self) -> None:
        self.total_checks = 0
        self.success_count = 0
        self.failure_count = 0
        self.dns_failure_count = 0
        self.ping_failure_count = 0
        self.consecutive_failures = 0


@dataclass
class SessionTotals:
    started_at: float = field(default_factory=time.time)
    last_reset_at: float = field(default_factory=time.time)
    cycles_completed: int = 0
    total_checks: int = 0
    successes: int = 0
    failures: int = 0
    dns_failures: int = 0
    ping_failures: int = 0

    def reset(self) -> None:
        self.last_reset_at = time.time()
        self.cycles_completed = 0
        self.total_checks = 0
        self.successes = 0
        self.failures = 0
        self.dns_failures = 0
        self.ping_failures = 0


@dataclass
class StateSnapshot:
    diagnosis: str
    target_stats: list[TargetStats]
    recent_events: list[EventEntry]
    session: SessionTotals
    session_window: CounterSummary
    stats_window_seconds: int
    last_cycle_completed_at: float
    last_cycle_id: int


@dataclass
class PromptState:
    kind: str
    message: str
    buffer: str = ""


class StateStore:
    def __init__(self, config: AppConfig) -> None:
        self.lock = threading.RLock()
        self.events: Deque[EventEntry] = deque(maxlen=config.event_history_size)
        self.stats: dict[str, TargetStats] = {}
        self.target_windows: dict[str, RollingWindowCounter] = {}
        self.session = SessionTotals()
        self.session_window = RollingWindowCounter(config.stats_window_seconds)
        self.stats_window_seconds = config.stats_window_seconds
        self.diagnosis = "Waiting for first cycle"
        self.last_cycle_completed_at = 0.0
        self.last_cycle_id = 0
        self.sync_targets(config)
        self.add_event("info", "Session started")

    def sync_targets(self, config: AppConfig) -> bool:
        with self.lock:
            window_reset = False
            if self.events.maxlen != config.event_history_size:
                self.events = deque(list(self.events)[-config.event_history_size :], maxlen=config.event_history_size)
            if self.stats_window_seconds != config.stats_window_seconds:
                self.stats_window_seconds = config.stats_window_seconds
                self.session_window = RollingWindowCounter(config.stats_window_seconds)
                window_reset = True
            ordered: dict[str, TargetStats] = {}
            ordered_windows: dict[str, RollingWindowCounter] = {}
            for target in config.targets:
                stats = self.stats.get(target.value)
                if stats is None:
                    stats = TargetStats(target=target.value, target_type=target.kind)
                else:
                    stats.target_type = target.kind
                ordered[target.value] = stats
                if window_reset:
                    ordered_windows[target.value] = RollingWindowCounter(self.stats_window_seconds)
                else:
                    ordered_windows[target.value] = self.target_windows.get(
                        target.value,
                        RollingWindowCounter(self.stats_window_seconds),
                    )
            self.stats = ordered
            self.target_windows = ordered_windows
            return window_reset

    def add_event(self, level: str, message: str, *, timestamp: Optional[float] = None) -> None:
        with self.lock:
            self.events.append(EventEntry(timestamp=timestamp or time.time(), level=level, message=message))

    def handle_cycle(self, results: list[CheckResult], config: AppConfig, cycle_id: int) -> None:
        with self.lock:
            window_reset = self.sync_targets(config)
            if window_reset and self.session.cycles_completed > 0:
                self.add_event(
                    "info",
                    f"Stats window changed to {format_compact_span(self.stats_window_seconds)}; rolling counters reset",
                )
            self.session.cycles_completed += 1
            self.last_cycle_id = cycle_id
            if results:
                self.last_cycle_completed_at = max(result.timestamp for result in results)
            else:
                self.last_cycle_completed_at = time.time()

            for result in results:
                stats = self.stats.setdefault(
                    result.target,
                    TargetStats(target=result.target, target_type=result.target_type),
                )
                previous_state, previous_error = stats.apply(result)
                self.session_window.observe(result.timestamp, result)
                self.target_windows.setdefault(
                    result.target,
                    RollingWindowCounter(self.stats_window_seconds),
                ).observe(result.timestamp, result)

                self.session.total_checks += 1
                if result.is_failure:
                    self.session.failures += 1
                else:
                    self.session.successes += 1
                if result.dns_success is False:
                    self.session.dns_failures += 1
                elif not result.ping_success:
                    self.session.ping_failures += 1

                if result.ping_success and previous_state == "down":
                    self.add_event(
                        "info",
                        f"{result.target} recovered ({format_latency(result.latency_ms)})",
                        timestamp=result.timestamp,
                    )
                elif result.is_failure:
                    should_report = (
                        previous_state != "down"
                        or previous_error != result.error_category
                        or stats.consecutive_failures in (1, 2, 3)
                        or stats.consecutive_failures % 5 == 0
                    )
                    if should_report:
                        self.add_event(
                            "warn",
                            f"{result.target} {result.status_text}: {human_error_message(result)}",
                            timestamp=result.timestamp,
                        )

            diagnosis = diagnose_cycle(results, config)
            if diagnosis != self.diagnosis:
                self.add_event("info", f"Diagnosis changed: {diagnosis}", timestamp=self.last_cycle_completed_at)
            self.diagnosis = diagnosis

    def reset_counters(self) -> None:
        with self.lock:
            self.session.reset()
            for stats in self.stats.values():
                stats.reset_counters()
                stats.window_summary = CounterSummary()
            self.session_window = RollingWindowCounter(self.stats_window_seconds)
            self.target_windows = {
                target: RollingWindowCounter(self.stats_window_seconds)
                for target in self.stats
            }

    def snapshot(self) -> StateSnapshot:
        with self.lock:
            now_timestamp = time.time()
            target_stats: list[TargetStats] = []
            for key, stats in self.stats.items():
                stats_copy = copy.deepcopy(stats)
                stats_copy.window_summary = self.target_windows.get(
                    key,
                    RollingWindowCounter(self.stats_window_seconds),
                ).snapshot(now_timestamp)
                target_stats.append(stats_copy)
            return StateSnapshot(
                diagnosis=self.diagnosis,
                target_stats=target_stats,
                recent_events=list(self.events),
                session=copy.deepcopy(self.session),
                session_window=self.session_window.snapshot(now_timestamp),
                stats_window_seconds=self.stats_window_seconds,
                last_cycle_completed_at=self.last_cycle_completed_at,
                last_cycle_id=self.last_cycle_id,
            )


def diagnose_cycle(results: list[CheckResult], config: AppConfig) -> str:
    if not config.targets:
        return "No targets configured"
    if not results:
        return "Waiting for first cycle"

    failures = [result for result in results if result.is_failure]
    if not failures:
        return "All monitored targets are reachable"

    ip_results = [result for result in results if result.target_type == "ip"]
    host_results = [result for result in results if result.target_type == "hostname"]
    ip_successes = sum(1 for result in ip_results if result.ping_success)
    ip_failures = sum(1 for result in ip_results if not result.ping_success)
    host_dns_failures = sum(1 for result in host_results if result.dns_success is False)
    host_reachability_failures = sum(
        1
        for result in host_results
        if result.dns_success is True and not result.ping_success
    )

    if ip_results:
        network_threshold = max(1, math.ceil(len(ip_results) * 0.75))
        if ip_failures >= network_threshold:
            return "Likely general network issue"

    if host_results:
        dns_threshold = max(1, math.ceil(len(host_results) * 0.75))
        if ip_successes > 0 and host_dns_failures >= dns_threshold:
            return "Likely DNS issue"
    if len(failures) == 1 and len(results) > 1:
        return "Likely isolated target or path issue"
    if host_results:
        if host_reachability_failures > 0 and host_dns_failures == 0:
            if host_reachability_failures == len(host_results) and ip_successes > 0:
                return "DNS okay, but resolved hosts are not reachable"
            return "DNS okay, reachability failed for one or more host targets"
    return "Mixed failures across monitored targets"


class PingRunner:
    LATENCY_PATTERNS = (
        re.compile(r"time[=<]?\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE),
        re.compile(r"tempo[=<]?\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE),
        re.compile(r"temps?[=<]?\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE),
        re.compile(r"(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE),
    )

    def __init__(self) -> None:
        self.is_windows = os.name == "nt"

    def ping(self, ip_address: str, timeout_ms: int) -> tuple[bool, Optional[float], str, str]:
        command = self._build_command(ip_address, timeout_ms)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(3.0, timeout_ms / 1000.0 + 2.0),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if self.is_windows else 0,
            )
        except FileNotFoundError:
            return False, None, "ping_unavailable", "system ping command not found"
        except subprocess.TimeoutExpired:
            return False, None, "timeout", f"ping command exceeded {timeout_ms} ms timeout"

        elapsed_ms = (time.monotonic() - started) * 1000.0
        combined_output = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip()
        )
        if completed.returncode == 0:
            parsed_latency = self._parse_latency(combined_output)
            return True, parsed_latency if parsed_latency is not None else elapsed_ms, "ok", ""

        category = "ping_failure"
        lowered = combined_output.lower()
        if "timed out" in lowered or "100% packet loss" in lowered or "100% loss" in lowered:
            category = "timeout"
        message = self._summarize_error(combined_output) or f"ping exited with status {completed.returncode}"
        return False, None, category, message

    def _build_command(self, ip_address: str, timeout_ms: int) -> list[str]:
        if self.is_windows:
            return ["ping", "-n", "1", "-w", str(timeout_ms), ip_address]
        timeout_seconds = max(1, math.ceil(timeout_ms / 1000.0))
        return ["ping", "-n", "-c", "1", "-W", str(timeout_seconds), ip_address]

    def _parse_latency(self, output: str) -> Optional[float]:
        for pattern in self.LATENCY_PATTERNS:
            match = pattern.search(output)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    continue
        return None

    def _summarize_error(self, output: str) -> str:
        text = " ".join(line.strip() for line in output.splitlines() if line.strip())
        return shorten(text, 180)


def resolve_hostname(hostname: str) -> tuple[bool, str, str]:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, "", str(exc)
    unique_addresses: list[str] = []
    for _, _, _, _, sockaddr in infos:
        candidate = str(sockaddr[0])
        if candidate not in unique_addresses:
            unique_addresses.append(candidate)
    if not unique_addresses:
        return False, "", "no DNS answers returned"
    preferred = next((item for item in unique_addresses if ":" not in item), unique_addresses[0])
    return True, preferred, ""


class CheckCoordinator:
    def __init__(self, ping_runner: PingRunner) -> None:
        worker_count = max(4, min(32, len(DEFAULT_TARGET_VALUES) * 2))
        self.executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="checker")
        self.ping_runner = ping_runner

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

    def execute_cycle(self, config: AppConfig, cycle_id: int) -> list[CheckResult]:
        if not config.targets:
            return []
        futures = {}
        for index, target in enumerate(config.targets):
            future = self.executor.submit(self._safe_check_target, target, config.ping_timeout_ms, cycle_id)
            futures[future] = index
        results: dict[int, CheckResult] = {}
        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()
        return [results[index] for index in sorted(results)]

    def _safe_check_target(self, target: TargetSpec, timeout_ms: int, cycle_id: int) -> CheckResult:
        try:
            return self._check_target(target, timeout_ms, cycle_id)
        except Exception as exc:
            return CheckResult(
                cycle_id=cycle_id,
                timestamp=time.time(),
                target=target.value,
                target_type=target.kind,
                resolved_ip=target.value if target.kind == "ip" else "",
                dns_success=None if target.kind == "ip" else False,
                ping_success=False,
                error_category="internal_error",
                error_message=shorten(str(exc), 180),
                worker_id=threading.current_thread().name,
            )

    def _check_target(self, target: TargetSpec, timeout_ms: int, cycle_id: int) -> CheckResult:
        worker_name = threading.current_thread().name
        if target.kind == "ip":
            ping_success, latency_ms, error_category, error_message = self.ping_runner.ping(
                target.value,
                timeout_ms,
            )
            return CheckResult(
                cycle_id=cycle_id,
                timestamp=time.time(),
                target=target.value,
                target_type="ip",
                resolved_ip=target.value,
                dns_success=None,
                ping_success=ping_success,
                latency_ms=latency_ms,
                error_category=error_category if not ping_success else "ok",
                error_message=error_message,
                worker_id=worker_name,
            )

        dns_success, resolved_ip, dns_error = resolve_hostname(target.value)
        if not dns_success:
            return CheckResult(
                cycle_id=cycle_id,
                timestamp=time.time(),
                target=target.value,
                target_type="hostname",
                resolved_ip="",
                dns_success=False,
                ping_success=False,
                latency_ms=None,
                error_category="dns_failure",
                error_message=shorten(dns_error, 180),
                worker_id=worker_name,
            )

        ping_success, latency_ms, error_category, error_message = self.ping_runner.ping(resolved_ip, timeout_ms)
        return CheckResult(
            cycle_id=cycle_id,
            timestamp=time.time(),
            target=target.value,
            target_type="hostname",
            resolved_ip=resolved_ip,
            dns_success=True,
            ping_success=ping_success,
            latency_ms=latency_ms,
            error_category=error_category if not ping_success else "ok",
            error_message=error_message,
            worker_id=worker_name,
        )


class CSVLogger:
    FIELDNAMES = [
        "timestamp",
        "target",
        "target_type",
        "resolved_ip",
        "dns_success",
        "ping_success",
        "latency_ms",
        "error_category",
        "error_message",
        "worker_id",
        "cycle_id",
        "sequence",
    ]

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.buffer: Deque[BufferedLogResult] = deque()
        self.capture_until = 0.0
        self.current_mode = ""
        self.ensure_header()

    def ensure_header(self) -> None:
        if self.path.exists() and self.path.stat().st_size > 0:
            return
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.FIELDNAMES)
            writer.writeheader()

    def log_results(self, results: list[CheckResult], config: AppConfig) -> None:
        with self.lock:
            self.ensure_header()
            if self.current_mode != config.logging_mode:
                self.buffer.clear()
                self.capture_until = 0.0
                self.current_mode = config.logging_mode

            for result in results:
                if config.logging_mode == "all":
                    self._write_rows([result], config)
                elif config.logging_mode == "failures_only":
                    if result.is_failure:
                        self._write_rows([result], config)
                else:
                    self._log_around_failure(result, config)

    def _log_around_failure(self, result: CheckResult, config: AppConfig) -> None:
        record = BufferedLogResult(result=result, written=False)
        self.buffer.append(record)
        self._prune_buffer(result.timestamp, config.around_failure_before_seconds)

        if result.is_failure:
            self.capture_until = max(self.capture_until, result.timestamp + config.around_failure_after_seconds)
            self._flush_buffer(config)

        if self.capture_until and result.timestamp <= self.capture_until:
            if not record.written:
                self._write_rows([result], config)
                record.written = True
        elif self.capture_until and result.timestamp > self.capture_until:
            self.capture_until = 0.0

    def _prune_buffer(self, current_timestamp: float, before_seconds: int) -> None:
        cutoff = current_timestamp - before_seconds
        while self.buffer and self.buffer[0].result.timestamp < cutoff:
            self.buffer.popleft()

    def _flush_buffer(self, config: AppConfig) -> None:
        rows: list[CheckResult] = []
        for record in self.buffer:
            if not record.written:
                rows.append(record.result)
                record.written = True
        if rows:
            self._write_rows(rows, config)

    def _write_rows(self, results: list[CheckResult], config: AppConfig) -> None:
        if not results:
            return
        self._rotate_if_needed(config)
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.FIELDNAMES)
            for result in results:
                writer.writerow(
                    {
                        "timestamp": now_local_iso(result.timestamp, milliseconds=True),
                        "target": result.target,
                        "target_type": result.target_type,
                        "resolved_ip": result.resolved_ip,
                        "dns_success": "" if result.dns_success is None else str(result.dns_success).lower(),
                        "ping_success": str(result.ping_success).lower(),
                        "latency_ms": "" if result.latency_ms is None else f"{result.latency_ms:.2f}",
                        "error_category": result.error_category,
                        "error_message": result.error_message,
                        "worker_id": result.worker_id,
                        "cycle_id": result.cycle_id,
                        "sequence": result.sequence,
                    }
                )

    def _rotate_if_needed(self, config: AppConfig) -> None:
        max_bytes = config.log_rotation_max_mb * 1024 * 1024
        if max_bytes <= 0:
            return
        if not self.path.exists():
            return
        try:
            current_size = self.path.stat().st_size
        except OSError:
            return
        if current_size < max_bytes:
            return

        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        rotated_path = self.path.with_name(f"{self.path.stem}_{timestamp}{self.path.suffix}")
        suffix = 1
        while rotated_path.exists():
            rotated_path = self.path.with_name(f"{self.path.stem}_{timestamp}_{suffix}{self.path.suffix}")
            suffix += 1
        self.path.replace(rotated_path)
        self.ensure_header()
        self._cleanup_rotated_logs(config.log_rotation_keep_files)

    def _cleanup_rotated_logs(self, keep_files: int) -> None:
        rotated_files = sorted(
            self.path.parent.glob(ROTATED_LOG_GLOB),
            key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
            reverse=True,
        )
        for path in rotated_files[keep_files:]:
            try:
                path.unlink()
            except OSError:
                continue


class BackgroundMonitor:
    def __init__(
        self,
        config_manager: ConfigManager,
        state_store: StateStore,
        logger: CSVLogger,
        coordinator: CheckCoordinator,
    ) -> None:
        self.config_manager = config_manager
        self.state_store = state_store
        self.logger = logger
        self.coordinator = coordinator
        self.stop_event = threading.Event()
        self.pause_lock = threading.Lock()
        self._paused = False
        self.sequence_lock = threading.Lock()
        self.sequence = 0
        self.cycle_id = 0
        self.thread = threading.Thread(target=self._run, name="scheduler", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5.0)

    def toggle_pause(self) -> bool:
        with self.pause_lock:
            self._paused = not self._paused
            return self._paused

    def is_paused(self) -> bool:
        with self.pause_lock:
            return self._paused

    def run_single_cycle(self, config: AppConfig) -> list[CheckResult]:
        self.cycle_id += 1
        results = self.coordinator.execute_cycle(config, self.cycle_id)
        self._stamp_sequences(results)
        return results

    def _run(self) -> None:
        next_run = time.monotonic()
        while not self.stop_event.is_set():
            if self.is_paused():
                next_run = time.monotonic()
                self.stop_event.wait(0.1)
                continue

            now = time.monotonic()
            if now < next_run:
                self.stop_event.wait(min(0.1, next_run - now))
                continue

            config = self.config_manager.snapshot()
            results = self.run_single_cycle(config)
            self.state_store.handle_cycle(results, config, self.cycle_id)
            self.logger.log_results(results, config)
            next_run = time.monotonic() + config.check_interval_seconds

    def _stamp_sequences(self, results: list[CheckResult]) -> None:
        with self.sequence_lock:
            for result in results:
                self.sequence += 1
                result.sequence = self.sequence


class InputHandler:
    def __enter__(self) -> "InputHandler":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read_keys(self, timeout: float) -> list[str]:
        raise NotImplementedError


class WindowsInputHandler(InputHandler):
    def read_keys(self, timeout: float) -> list[str]:
        deadline = time.monotonic() + timeout
        keys: list[str] = []
        while True:
            while msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                    continue
                keys.append(key)
            if keys or time.monotonic() >= deadline:
                return keys
            time.sleep(0.01)


class UnixInputHandler(InputHandler):
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.original_mode = None

    def __enter__(self) -> "UnixInputHandler":
        self.original_mode = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.original_mode is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original_mode)

    def read_keys(self, timeout: float) -> list[str]:
        keys: list[str] = []
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return keys
        while True:
            chunk = os.read(self.fd, 1)
            if not chunk:
                break
            keys.append(chunk.decode("utf-8", errors="ignore"))
            ready, _, _ = select.select([self.fd], [], [], 0)
            if not ready:
                break
        return keys


class Renderer:
    COLORS = {
        "red": "31",
        "green": "32",
        "yellow": "33",
        "blue": "34",
        "magenta": "35",
        "cyan": "36",
        "white": "37",
    }

    def __init__(self) -> None:
        self.ansi = sys.stdout.isatty()
        if os.name == "nt" and self.ansi:
            self.ansi = self._enable_windows_ansi()

    def enter(self) -> None:
        if self.ansi:
            sys.stdout.write("\x1b[?1049h\x1b[2J\x1b[H\x1b[?25l")
            sys.stdout.flush()

    def leave(self) -> None:
        if self.ansi:
            sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()

    def draw(self, text: str) -> None:
        if self.ansi:
            sys.stdout.write("\x1b[H\x1b[2J")
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()

    def build_screen(
        self,
        snapshot: StateSnapshot,
        config: AppConfig,
        *,
        paused: bool,
        help_visible: bool,
        prompt: Optional[PromptState],
    ) -> str:
        terminal_size = shutil.get_terminal_size((140, 42))
        width = max(40, terminal_size.columns)
        height = max(20, terminal_size.lines)

        status = "PAUSED" if paused else "RUNNING"
        window_label = format_compact_span(snapshot.stats_window_seconds)
        rotation_label = (
            f"{config.log_rotation_max_mb}MB/{config.log_rotation_keep_files}"
            if config.log_rotation_max_mb > 0
            else "off"
        )
        diagnosis_lower = snapshot.diagnosis.lower()
        if paused or "waiting" in diagnosis_lower or "no targets" in diagnosis_lower:
            status_color = "yellow"
        elif "reachable" in diagnosis_lower:
            status_color = "green"
        else:
            status_color = "red"
        title = f"pingtop | status {status} | diagnosis {snapshot.diagnosis}"
        header_lines = [
            self.style(shorten(title, width), bold=True, fg=status_color),
            shorten(
                (
                    f"checks {format_duration(config.check_interval_seconds)} | "
                    f"ping timeout {config.ping_timeout_ms}ms | "
                    f"ui refresh {format_duration(config.ui_refresh_interval_seconds)} | "
                    f"stats {window_label} | "
                    f"latency warn/crit {config.latency_warning_ms}/{config.latency_critical_ms}ms | "
                    f"logging {config.logging_mode} | "
                    f"rotate {rotation_label} | "
                    f"targets {len(config.targets)}"
                ),
                width,
            ),
            shorten(
                (
                    f"rolling {window_label}: checks {abbreviate_count(snapshot.session_window.checks)} | "
                    f"ok {abbreviate_count(snapshot.session_window.successes)} | "
                    f"fail {abbreviate_count(snapshot.session_window.failures)} | "
                    f"dns {abbreviate_count(snapshot.session_window.dns_failures)} | "
                    f"ping {abbreviate_count(snapshot.session_window.ping_failures)} | "
                    f"events {min(len(snapshot.recent_events), config.visible_event_lines)}/{len(snapshot.recent_events)} shown | "
                    f"last cycle {format_timestamp_short(snapshot.last_cycle_completed_at) if snapshot.last_cycle_completed_at else '-'}"
                ),
                width,
            ),
            shorten(
                (
                    f"all-time: cycles {abbreviate_count(snapshot.session.cycles_completed)} | "
                    f"checks {abbreviate_count(snapshot.session.total_checks)} | "
                    f"ok {abbreviate_count(snapshot.session.successes)} | "
                    f"fail {abbreviate_count(snapshot.session.failures)} | "
                    f"dns {abbreviate_count(snapshot.session.dns_failures)} | "
                    f"ping {abbreviate_count(snapshot.session.ping_failures)}"
                ),
                width,
            ),
            "=" * width,
        ]

        table_lines = self._build_target_table(snapshot.target_stats, width, config)

        footer_lines = []
        if help_visible:
            footer_lines.append(
                shorten(
                    "Keys: q quit | p pause | l logging | +/- check interval | </> ui refresh | a add | d delete | w fail window | t stats window | r reset | s snapshot | h help",
                    width,
                )
            )
            footer_lines.append(shorten("Prompt mode: Enter submits, Esc cancels, Backspace edits.", width))
        else:
            footer_lines.append(shorten("Press h for key help. q quits.", width))

        prompt_line = ""
        if prompt is not None:
            prompt_line = shorten(
                f"Prompt [{prompt.kind}]: {prompt.message} > {prompt.buffer}",
                width,
            )

        static_line_count = len(header_lines) + len(table_lines) + len(footer_lines) + 4 + (1 if prompt_line else 0)
        available_event_lines = min(config.visible_event_lines, max(3, height - static_line_count))
        event_lines = self._build_event_panel(snapshot.recent_events, width, available_event_lines)
        event_title = f"Recent events (last {min(len(snapshot.recent_events), available_event_lines)}/{len(snapshot.recent_events)})"

        lines = []
        lines.extend(header_lines)
        lines.extend(table_lines)
        lines.append("-" * width)
        lines.append(shorten(event_title, width))
        lines.extend(event_lines)
        lines.append("-" * width)
        lines.extend(footer_lines)
        if prompt_line:
            lines.append(prompt_line)

        return "\n".join(lines[:height])

    def build_report(self, snapshot: StateSnapshot, config: AppConfig, *, paused: bool) -> str:
        width = 180
        header = [
            f"pingtop session snapshot - {now_local_iso()}",
            f"status: {'paused' if paused else 'running'}",
            f"diagnosis: {snapshot.diagnosis}",
            (
                f"check_interval_seconds={config.check_interval_seconds}, ping_timeout_ms={config.ping_timeout_ms}, "
                f"stats_window_seconds={config.stats_window_seconds}, "
                f"ui_refresh_interval_seconds={config.ui_refresh_interval_seconds}, "
                f"latency_warning_ms={config.latency_warning_ms}, latency_critical_ms={config.latency_critical_ms}, "
                f"log_rotation_max_mb={config.log_rotation_max_mb}, log_rotation_keep_files={config.log_rotation_keep_files}, "
                f"logging_mode={config.logging_mode}, around_failure={config.around_failure_before_seconds}/{config.around_failure_after_seconds}s, "
                f"visible_event_lines={config.visible_event_lines}"
            ),
            (
                f"rolling_window: checks={snapshot.session_window.checks}, ok={snapshot.session_window.successes}, "
                f"fail={snapshot.session_window.failures}, dns={snapshot.session_window.dns_failures}, "
                f"ping={snapshot.session_window.ping_failures}"
            ),
            "",
        ]
        body = self._build_target_table(snapshot.target_stats, width, config, ansi=False)
        events = ["", "Recent events"] + self._build_event_panel(
            snapshot.recent_events,
            width,
            min(15, config.visible_event_lines),
            ansi=False,
        )
        return "\n".join(header + body + events) + "\n"

    def style(
        self,
        text: str,
        *,
        fg: Optional[str] = None,
        bold: bool = False,
        dim: bool = False,
    ) -> str:
        if not self.ansi:
            return text
        codes = []
        if bold:
            codes.append("1")
        if dim:
            codes.append("2")
        if fg in self.COLORS:
            codes.append(self.COLORS[fg])
        if not codes:
            return text
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"

    def _build_target_table(
        self,
        stats_list: list[TargetStats],
        width: int,
        config: AppConfig,
        *,
        ansi: Optional[bool] = None,
    ) -> list[str]:
        use_ansi = self.ansi if ansi is None else ansi
        original_ansi = self.ansi
        self.ansi = use_ansi
        try:
            lines = ["Targets"]
            header = (
                f"{'Idx':>3} {'Target':24} {'Type':8} {'State':10} {'Latency':>9} "
                f"{'Consec':>6} {'WinLoss%':>8} {'Win OK/Fail':>13} {'Last IP':18}  Error"
            )
            lines.append(shorten(header, width))
            if not stats_list:
                lines.append("  - no targets configured")
                return lines
            for index, stats in enumerate(stats_list, start=1):
                state_plain = f"{stats.last_result.lower():10}"
                state_text = self.style(
                    state_plain,
                    fg=self._state_color(stats),
                    bold=stats.last_state == "down",
                )
                latency_plain = f"{format_latency(stats.last_latency_ms):>9}"
                latency_color = self._latency_color(stats.last_latency_ms, config)
                latency_text = self.style(
                    latency_plain,
                    fg=latency_color,
                    bold=latency_color == "red",
                )
                consecutive_plain = f"{stats.consecutive_failures:>6}"
                consecutive_text = self.style(
                    consecutive_plain,
                    fg="red" if stats.consecutive_failures > 0 else "green",
                    bold=stats.consecutive_failures > 0,
                )
                loss_plain = f"{stats.window_summary.loss_percentage:>7.1f}%"
                loss_text = self.style(loss_plain, fg=self._loss_color(stats.window_summary.loss_percentage))
                ok_fail_plain = f"{abbreviate_ratio(stats.window_summary.successes, stats.window_summary.failures):>13}"
                ok_fail_text = self.style(
                    ok_fail_plain,
                    fg="red" if stats.window_summary.failures > 0 else "green",
                )
                error_text = stats.last_error_category if stats.last_error_category not in ("", "ok") else "-"
                if stats.last_error_message and stats.last_error_category not in ("", "ok"):
                    error_text = f"{stats.last_error_category}: {stats.last_error_message}"
                error_text = shorten(error_text, max(10, width - 104))
                if error_text != "-":
                    error_text = self.style(error_text, fg="red", bold=True)
                line = (
                    f"{index:>3} "
                    f"{shorten(stats.target, 24):24} "
                    f"{stats.target_type:8} "
                    f"{state_text} "
                    f"{latency_text} "
                    f"{consecutive_text} "
                    f"{loss_text} "
                    f"{ok_fail_text} "
                    f"{shorten(stats.last_resolved_ip or '-', 18):18}  "
                    f"{error_text}"
                )
                lines.append(line)
            return lines
        finally:
            self.ansi = original_ansi

    def _build_event_panel(
        self,
        events: list[EventEntry],
        width: int,
        available_lines: int,
        *,
        ansi: Optional[bool] = None,
    ) -> list[str]:
        use_ansi = self.ansi if ansi is None else ansi
        original_ansi = self.ansi
        self.ansi = use_ansi
        try:
            if not events:
                return ["  - no events yet"]
            selected = events[-available_lines:]
            lines = []
            for event in selected:
                prefix = f"{format_timestamp_short(event.timestamp)} {event.level.upper():5}"
                lines.append(
                    self.style(
                        shorten(f"{prefix} {event.message}", width),
                        fg=self._event_color(event.level),
                        bold=event.level in ("warn", "error"),
                    )
                )
            return lines
        finally:
            self.ansi = original_ansi

    def _state_color(self, stats: TargetStats) -> str:
        if stats.last_state == "up":
            return "green"
        if stats.last_state == "down":
            return "red"
        return "yellow"

    def _latency_color(self, latency_ms: Optional[float], config: AppConfig) -> Optional[str]:
        if latency_ms is None:
            return None
        if latency_ms >= config.latency_critical_ms:
            return "red"
        if latency_ms >= config.latency_warning_ms:
            return "yellow"
        return "green"

    def _loss_color(self, loss_percentage: float) -> str:
        if loss_percentage >= 50.0:
            return "red"
        if loss_percentage > 0.0:
            return "yellow"
        return "green"

    def _event_color(self, level: str) -> str:
        if level == "error":
            return "red"
        if level == "warn":
            return "yellow"
        return "cyan"

    def _enable_windows_ansi(self) -> bool:
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
                return False
            enable_vt = 0x0004
            if mode.value & enable_vt:
                return True
            return kernel32.SetConsoleMode(handle, mode.value | enable_vt) != 0
        except Exception:
            return False


def create_input_handler() -> InputHandler:
    if os.name == "nt":
        return WindowsInputHandler()
    return UnixInputHandler()


class PingTopUI:
    def __init__(
        self,
        config_manager: ConfigManager,
        state_store: StateStore,
        logger: CSVLogger,
        coordinator: CheckCoordinator,
    ) -> None:
        self.config_manager = config_manager
        self.state_store = state_store
        self.logger = logger
        self.coordinator = coordinator
        self.monitor = BackgroundMonitor(config_manager, state_store, logger, coordinator)
        self.renderer = Renderer()
        self.help_visible = True
        self.prompt: Optional[PromptState] = None
        self.running = True

        if self.config_manager.load_warning:
            self.state_store.add_event("warn", self.config_manager.load_warning)

    def run(self) -> int:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("Interactive UI requires a TTY; falling back to --no-ui mode.")
            return run_headless(self.config_manager, self.state_store, self.logger, self.coordinator)

        self.monitor.start()
        try:
            self.renderer.enter()
            with create_input_handler() as input_handler:
                while self.running:
                    config = self.config_manager.snapshot()
                    snapshot = self.state_store.snapshot()
                    screen = self.renderer.build_screen(
                        snapshot,
                        config,
                        paused=self.monitor.is_paused(),
                        help_visible=self.help_visible,
                        prompt=self.prompt,
                    )
                    self.renderer.draw(screen)
                    for key in input_handler.read_keys(config.ui_refresh_interval_seconds):
                        self.handle_key(key)
        except KeyboardInterrupt:
            self.running = False
        finally:
            self.monitor.stop()
            self.renderer.leave()
        print(build_exit_summary(self.state_store.snapshot()))
        return 0

    def handle_key(self, key: str) -> None:
        if key == "\x03":
            self.running = False
            return
        if self.prompt is not None:
            self._handle_prompt_key(key)
            return

        if key.lower() == "q":
            self.running = False
        elif key.lower() == "p":
            paused = self.monitor.toggle_pause()
            self.state_store.add_event("info", "Monitoring paused" if paused else "Monitoring resumed")
        elif key.lower() == "l":
            self._cycle_logging_mode()
        elif key in ("+", "="):
            self._adjust_check_interval(0.5)
        elif key in ("-", "_"):
            self._adjust_check_interval(-0.5)
        elif key in (">", "."):
            self._adjust_ui_refresh(-0.1)
        elif key in ("<", ","):
            self._adjust_ui_refresh(0.1)
        elif key.lower() == "a":
            self.prompt = PromptState(kind="add", message="enter a hostname or IP to add")
        elif key.lower() == "d":
            self.prompt = PromptState(kind="delete", message="enter target index or exact target to delete")
        elif key.lower() == "w":
            self.prompt = PromptState(
                kind="window",
                message="duration or before,after (example 10s or 10s,20s)",
            )
        elif key.lower() == "t":
            self.prompt = PromptState(kind="stats_window", message="stats window like 15m, 1h, or 1d")
        elif key.lower() == "r":
            self.state_store.reset_counters()
            self.state_store.add_event("info", "Counters reset")
        elif key.lower() == "s":
            path = self._save_snapshot_report()
            self.state_store.add_event("info", f"Snapshot saved to {path.name}")
        elif key.lower() == "h":
            self.help_visible = not self.help_visible

    def _handle_prompt_key(self, key: str) -> None:
        assert self.prompt is not None
        if key in ("\r", "\n"):
            value = self.prompt.buffer.strip()
            kind = self.prompt.kind
            self.prompt = None
            if kind == "add":
                self._submit_add_target(value)
            elif kind == "delete":
                self._submit_delete_target(value)
            elif kind == "window":
                self._submit_window(value)
            elif kind == "stats_window":
                self._submit_stats_window(value)
            return
        if key == "\x1b":
            self.prompt = None
            return
        if key in ("\x7f", "\b"):
            self.prompt.buffer = self.prompt.buffer[:-1]
            return
        if key.isprintable():
            self.prompt.buffer += key

    def _cycle_logging_mode(self) -> None:
        current = self.config_manager.snapshot().logging_mode
        index = LOGGING_MODES.index(current)
        next_mode = LOGGING_MODES[(index + 1) % len(LOGGING_MODES)]
        self.config_manager.update(lambda config: setattr(config, "logging_mode", next_mode))
        self.state_store.add_event("info", f"Logging mode set to {next_mode}")

    def _adjust_check_interval(self, delta: float) -> None:
        def updater(config: AppConfig) -> None:
            config.check_interval_seconds = round(config.check_interval_seconds + delta, 2)

        config = self.config_manager.update(updater)
        self.state_store.add_event(
            "info",
            f"Check interval set to {config.check_interval_seconds:.2f}s",
        )

    def _adjust_ui_refresh(self, delta: float) -> None:
        def updater(config: AppConfig) -> None:
            config.ui_refresh_interval_seconds = round(config.ui_refresh_interval_seconds + delta, 2)

        config = self.config_manager.update(updater)
        self.state_store.add_event(
            "info",
            f"UI refresh interval set to {config.ui_refresh_interval_seconds:.2f}s",
        )

    def _submit_add_target(self, raw_value: str) -> None:
        if not raw_value:
            self.state_store.add_event("warn", "Add target canceled: empty input")
            return
        try:
            target = infer_target(raw_value)
        except ValueError as exc:
            self.state_store.add_event("warn", f"Invalid target: {exc}")
            return

        def updater(config: AppConfig) -> None:
            config.targets.append(target)

        config = self.config_manager.update(updater)
        self.state_store.sync_targets(config)
        self.state_store.add_event("info", f"Added target {target.value}")

    def _submit_delete_target(self, raw_value: str) -> None:
        if not raw_value:
            self.state_store.add_event("warn", "Delete target canceled: empty input")
            return
        config_snapshot = self.config_manager.snapshot()
        target_to_remove = ""
        if raw_value.isdigit():
            index = int(raw_value)
            if 1 <= index <= len(config_snapshot.targets):
                target_to_remove = config_snapshot.targets[index - 1].value
        else:
            try:
                target_to_remove = infer_target(raw_value).value
            except ValueError:
                target_to_remove = raw_value.strip().lower()
        if not target_to_remove:
            self.state_store.add_event("warn", f"Delete target failed: no match for {raw_value}")
            return

        removed = {"value": ""}

        def updater(config: AppConfig) -> None:
            kept = [target for target in config.targets if target.value != target_to_remove]
            if len(kept) != len(config.targets):
                removed["value"] = target_to_remove
                config.targets = kept

        config = self.config_manager.update(updater)
        self.state_store.sync_targets(config)
        if removed["value"]:
            self.state_store.add_event("info", f"Deleted target {removed['value']}")
        else:
            self.state_store.add_event("warn", f"Delete target failed: no match for {raw_value}")

    def _submit_window(self, raw_value: str) -> None:
        if not raw_value:
            self.state_store.add_event("warn", "Around-failure window unchanged")
            return
        try:
            if "," in raw_value:
                before_text, after_text = [part.strip() for part in raw_value.split(",", 1)]
                before_value = parse_duration_input(before_text)
                after_value = parse_duration_input(after_text)
            else:
                before_value = after_value = parse_duration_input(raw_value)
        except ValueError:
            self.state_store.add_event("warn", "Window must be a duration like 10s or 10s,20s")
            return

        def updater(config: AppConfig) -> None:
            config.around_failure_before_seconds = before_value
            config.around_failure_after_seconds = after_value

        config = self.config_manager.update(updater)
        self.state_store.add_event(
            "info",
            (
                "Around-failure window set to "
                f"{config.around_failure_before_seconds}/{config.around_failure_after_seconds}s"
            ),
        )

    def _submit_stats_window(self, raw_value: str) -> None:
        if not raw_value:
            self.state_store.add_event("warn", "Stats window unchanged")
            return
        try:
            stats_window_seconds = parse_duration_input(raw_value)
        except ValueError as exc:
            self.state_store.add_event("warn", f"Stats window error: {exc}")
            return

        def updater(config: AppConfig) -> None:
            config.stats_window_seconds = stats_window_seconds

        config = self.config_manager.update(updater)
        window_reset = self.state_store.sync_targets(config)
        if window_reset:
            self.state_store.add_event(
                "info",
                f"Stats window set to {format_compact_span(config.stats_window_seconds)}; rolling counters reset",
            )

    def _save_snapshot_report(self) -> Path:
        snapshot = self.state_store.snapshot()
        config = self.config_manager.snapshot()
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = APP_DIR / f"{SNAPSHOT_PREFIX}{timestamp}.txt"
        path.write_text(
            self.renderer.build_report(snapshot, config, paused=self.monitor.is_paused()),
            encoding="utf-8",
        )
        return path


def build_exit_summary(snapshot: StateSnapshot) -> str:
    return (
        "Session summary: "
        f"cycles={snapshot.session.cycles_completed}, "
        f"checks={snapshot.session.total_checks}, "
        f"ok={snapshot.session.successes}, "
        f"failures={snapshot.session.failures}, "
        f"dns_failures={snapshot.session.dns_failures}, "
        f"ping_failures={snapshot.session.ping_failures}, "
        f"diagnosis={snapshot.diagnosis}"
    )


def print_cycle_summary(results: list[CheckResult], snapshot: StateSnapshot) -> None:
    timestamp = now_local_iso()
    if not results:
        print(f"[{timestamp}] no targets configured")
        return
    failures = [result for result in results if result.is_failure]
    print(
        f"[{timestamp}] cycle {results[0].cycle_id}: diagnosis={snapshot.diagnosis}; "
        f"ok={len(results) - len(failures)} fail={len(failures)}"
    )
    for result in results:
        status = "OK" if not result.is_failure else result.status_text.upper()
        message = human_error_message(result)
        print(
            f"  - {result.target:18} {status:9} "
            f"ip={result.resolved_ip or '-':15} "
            f"lat={format_latency(result.latency_ms):>8} "
            f"err={message}"
        )


def run_headless(
    config_manager: ConfigManager,
    state_store: StateStore,
    logger: CSVLogger,
    coordinator: CheckCoordinator,
    *,
    once: bool = False,
) -> int:
    if config_manager.load_warning:
        print(f"warning: {config_manager.load_warning}", file=sys.stderr)

    monitor = BackgroundMonitor(config_manager, state_store, logger, coordinator)
    try:
        if once:
            config = config_manager.snapshot()
            results = monitor.run_single_cycle(config)
            state_store.handle_cycle(results, config, monitor.cycle_id)
            logger.log_results(results, config)
            print_cycle_summary(results, state_store.snapshot())
            print(build_exit_summary(state_store.snapshot()))
            return 0

        print("pingtop headless mode. Press Ctrl+C to stop.")
        while True:
            config = config_manager.snapshot()
            results = monitor.run_single_cycle(config)
            state_store.handle_cycle(results, config, monitor.cycle_id)
            logger.log_results(results, config)
            print_cycle_summary(results, state_store.snapshot())
            time.sleep(config.check_interval_seconds)
    except KeyboardInterrupt:
        print(build_exit_summary(state_store.snapshot()))
        return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="pingtop network diagnosis monitor")
    parser.add_argument("--no-ui", action="store_true", help="run in headless text mode")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    config_manager = ConfigManager(CONFIG_PATH)
    state_store = StateStore(config_manager.snapshot())
    logger = CSVLogger(LOG_PATH)
    coordinator = CheckCoordinator(PingRunner())
    try:
        if args.no_ui or args.once:
            return run_headless(
                config_manager,
                state_store,
                logger,
                coordinator,
                once=args.once,
            )
        return PingTopUI(config_manager, state_store, logger, coordinator).run()
    finally:
        coordinator.close()


if __name__ == "__main__":
    raise SystemExit(main())
