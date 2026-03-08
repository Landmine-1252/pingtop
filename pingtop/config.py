from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .models import TargetSpec, infer_target
from .util import clamp


DEFAULT_TARGET_VALUES = [
    "1.1.1.1",
    "8.8.8.8",
    "google.com",
    "cloudflare.com",
    "microsoft.com",
]
LOGGING_MODES = ("all", "failures_only", "around_failure")


@dataclass
class AppConfig:
    version: int = 1
    check_interval_seconds: float = 1.0
    ping_timeout_ms: int = 1200
    ui_refresh_interval_seconds: float = 0.5
    stats_window_seconds: int = 3600
    update_check_enabled: bool = True
    update_repo_url: str = "https://github.com/Landmine-1252/pingtop"
    diagnosis_confirm_cycles: int = 2
    recovery_confirm_cycles: int = 2
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
            update_check_enabled=bool(data.get("update_check_enabled", base.update_check_enabled)),
            update_repo_url=str(data.get("update_repo_url", base.update_repo_url)),
            diagnosis_confirm_cycles=int(
                data.get("diagnosis_confirm_cycles", base.diagnosis_confirm_cycles)
            ),
            recovery_confirm_cycles=int(
                data.get("recovery_confirm_cycles", base.recovery_confirm_cycles)
            ),
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
        self.update_check_enabled = bool(self.update_check_enabled)
        self.update_repo_url = str(self.update_repo_url or "").strip().rstrip("/")
        self.diagnosis_confirm_cycles = int(clamp(float(self.diagnosis_confirm_cycles), 1.0, 10.0))
        self.recovery_confirm_cycles = int(clamp(float(self.recovery_confirm_cycles), 1.0, 10.0))
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
            "update_check_enabled": self.update_check_enabled,
            "update_repo_url": self.update_repo_url,
            "diagnosis_confirm_cycles": self.diagnosis_confirm_cycles,
            "recovery_confirm_cycles": self.recovery_confirm_cycles,
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
            return AppConfig.from_dict(data)
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

    def update(self, updater: Callable[[AppConfig], None]) -> AppConfig:
        with self.lock:
            updater(self.config)
            self.config.normalize()
            self._write(self.config)
            return copy.deepcopy(self.config)
