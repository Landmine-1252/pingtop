from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field
from typing import Optional


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


def infer_target(value: str) -> TargetSpec:
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


@dataclass(frozen=True)
class DiagnosisAssessment:
    key: str
    confirmed_message: str
    suspected_message: str


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
    consecutive_successes: int = 0
    recovery_pending: bool = False
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
            self.consecutive_successes = 0
            self.recovery_pending = True
            self.last_state = "down"
            self.last_result = "DNS_FAIL"
            self.last_latency_ms = None
        elif result.ping_success:
            self.success_count += 1
            self.consecutive_failures = 0
            self.consecutive_successes += 1
            self.last_state = "up"
            self.last_result = "UP"
            self.last_latency_ms = result.latency_ms
        else:
            self.failure_count += 1
            self.ping_failure_count += 1
            self.consecutive_failures += 1
            self.consecutive_successes = 0
            self.recovery_pending = True
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
        self.consecutive_successes = 0
        self.recovery_pending = False


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
