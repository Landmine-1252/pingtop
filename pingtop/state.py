from __future__ import annotations

import copy
import threading
import time
from collections import deque
from typing import Deque, Optional

from .config import AppConfig
from .diagnosis import diagnose_cycle
from .models import (
    CheckResult,
    CounterSummary,
    DiagnosisAssessment,
    EventEntry,
    RollingWindowBucket,
    SessionTotals,
    StateSnapshot,
    TargetStats,
)
from .util import format_compact_span, format_latency, human_error_message


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
        self.confirmed_diagnosis_key = "waiting"
        self.pending_diagnosis_key = "waiting"
        self.pending_diagnosis_streak = 0
        self.last_cycle_completed_at = 0.0
        self.last_cycle_id = 0
        self.sync_targets(config)

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

                if (
                    result.ping_success
                    and stats.recovery_pending
                    and stats.consecutive_successes >= config.recovery_confirm_cycles
                ):
                    self.add_event(
                        "info",
                        f"{result.target} recovered ({format_latency(result.latency_ms)})",
                        timestamp=result.timestamp,
                    )
                    stats.recovery_pending = False
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

            assessment = diagnose_cycle(results, config)
            confirmed_changed = self._update_diagnosis(assessment, config)
            if confirmed_changed:
                self.add_event("info", f"Diagnosis changed: {self.diagnosis}", timestamp=self.last_cycle_completed_at)

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

    def _update_diagnosis(self, assessment: DiagnosisAssessment, config: AppConfig) -> bool:
        if assessment.key == self.pending_diagnosis_key:
            self.pending_diagnosis_streak += 1
        else:
            self.pending_diagnosis_key = assessment.key
            self.pending_diagnosis_streak = 1

        required_cycles = self._required_diagnosis_cycles(assessment.key, config)
        if assessment.key == self.confirmed_diagnosis_key:
            self.diagnosis = assessment.confirmed_message
            return False

        if self.pending_diagnosis_streak >= required_cycles:
            self.confirmed_diagnosis_key = assessment.key
            self.diagnosis = assessment.confirmed_message
            return True

        if assessment.key == "healthy":
            self.diagnosis = (
                "Recovery observed, confirming stability "
                f"({self.pending_diagnosis_streak}/{required_cycles})"
            )
        else:
            self.diagnosis = (
                f"Suspected {assessment.suspected_message} "
                f"({self.pending_diagnosis_streak}/{required_cycles})"
            )
        return False

    def _required_diagnosis_cycles(self, assessment_key: str, config: AppConfig) -> int:
        if assessment_key in ("waiting", "no_targets"):
            return 1
        if assessment_key == "healthy":
            if self.confirmed_diagnosis_key in ("waiting", "no_targets", "healthy"):
                return 1
            return config.recovery_confirm_cycles
        return config.diagnosis_confirm_cycles
