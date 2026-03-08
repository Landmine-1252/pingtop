from __future__ import annotations

import csv
import datetime as dt
import threading
from collections import deque
from pathlib import Path
from typing import Deque

from .config import AppConfig
from .models import BufferedLogResult, CheckResult
from .util import now_local_iso


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
            self.path.parent.glob(f"{self.path.stem}_*{self.path.suffix}"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
            reverse=True,
        )
        for path in rotated_files[keep_files:]:
            try:
                path.unlink()
            except OSError:
                continue
