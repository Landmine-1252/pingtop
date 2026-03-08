from __future__ import annotations

import math
import os
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from .config import DEFAULT_TARGET_VALUES, AppConfig
from .models import CheckResult, TargetSpec
from .util import shorten


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
