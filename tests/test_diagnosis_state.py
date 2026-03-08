from __future__ import annotations

import unittest

from pingtop.config import AppConfig
from pingtop.diagnosis import diagnose_cycle
from pingtop.models import CheckResult
from pingtop.state import RollingWindowCounter, StateStore


def make_result(
    target: str,
    target_type: str,
    *,
    cycle_id: int,
    timestamp: float,
    ping_success: bool,
    dns_success,
    resolved_ip: str = "",
    error_category: str = "ok",
    latency_ms: float | None = None,
) -> CheckResult:
    return CheckResult(
        cycle_id=cycle_id,
        timestamp=timestamp,
        target=target,
        target_type=target_type,
        resolved_ip=resolved_ip,
        dns_success=dns_success,
        ping_success=ping_success,
        latency_ms=latency_ms,
        error_category=error_category,
        error_message="" if error_category == "ok" else error_category,
    )


class DiagnosisTests(unittest.TestCase):
    def test_diagnose_cycle_prefers_general_network_issue(self) -> None:
        config = AppConfig.default()
        results = [
            make_result("1.1.1.1", "ip", cycle_id=1, timestamp=1.0, ping_success=False, dns_success=None, resolved_ip="1.1.1.1", error_category="timeout"),
            make_result("8.8.8.8", "ip", cycle_id=1, timestamp=1.0, ping_success=False, dns_success=None, resolved_ip="8.8.8.8", error_category="timeout"),
            make_result("google.com", "hostname", cycle_id=1, timestamp=1.0, ping_success=False, dns_success=False, error_category="dns_failure"),
            make_result("cloudflare.com", "hostname", cycle_id=1, timestamp=1.0, ping_success=False, dns_success=False, error_category="dns_failure"),
            make_result("microsoft.com", "hostname", cycle_id=1, timestamp=1.0, ping_success=False, dns_success=False, error_category="dns_failure"),
        ]
        assessment = diagnose_cycle(results, config)
        self.assertEqual(assessment.key, "network_issue")

    def test_state_store_requires_confirmation_for_failure_and_recovery(self) -> None:
        config = AppConfig.default()
        config.diagnosis_confirm_cycles = 2
        config.recovery_confirm_cycles = 2
        store = StateStore(config)

        fail_cycle = [
            make_result("1.1.1.1", "ip", cycle_id=1, timestamp=1.0, ping_success=False, dns_success=None, resolved_ip="1.1.1.1", error_category="timeout"),
            make_result("8.8.8.8", "ip", cycle_id=1, timestamp=1.0, ping_success=False, dns_success=None, resolved_ip="8.8.8.8", error_category="timeout"),
            make_result("google.com", "hostname", cycle_id=1, timestamp=1.0, ping_success=False, dns_success=False, error_category="dns_failure"),
            make_result("cloudflare.com", "hostname", cycle_id=1, timestamp=1.0, ping_success=False, dns_success=False, error_category="dns_failure"),
            make_result("microsoft.com", "hostname", cycle_id=1, timestamp=1.0, ping_success=False, dns_success=False, error_category="dns_failure"),
        ]
        ok_cycle = [
            make_result("1.1.1.1", "ip", cycle_id=2, timestamp=2.0, ping_success=True, dns_success=None, resolved_ip="1.1.1.1", latency_ms=20.0),
            make_result("8.8.8.8", "ip", cycle_id=2, timestamp=2.0, ping_success=True, dns_success=None, resolved_ip="8.8.8.8", latency_ms=21.0),
            make_result("google.com", "hostname", cycle_id=2, timestamp=2.0, ping_success=True, dns_success=True, resolved_ip="142.250.0.1", latency_ms=22.0),
            make_result("cloudflare.com", "hostname", cycle_id=2, timestamp=2.0, ping_success=True, dns_success=True, resolved_ip="104.16.0.1", latency_ms=23.0),
            make_result("microsoft.com", "hostname", cycle_id=2, timestamp=2.0, ping_success=True, dns_success=True, resolved_ip="20.70.0.1", latency_ms=24.0),
        ]

        store.handle_cycle(fail_cycle, config, 1)
        self.assertEqual(store.snapshot().diagnosis, "Suspected general network issue (1/2)")
        store.handle_cycle(fail_cycle, config, 2)
        self.assertEqual(store.snapshot().diagnosis, "Likely general network issue")
        store.handle_cycle(ok_cycle, config, 3)
        self.assertEqual(store.snapshot().diagnosis, "Recovery observed, confirming stability (1/2)")
        store.handle_cycle(ok_cycle, config, 4)
        snapshot = store.snapshot()
        self.assertEqual(snapshot.diagnosis, "All monitored targets are reachable")
        self.assertTrue(any("recovered" in event.message for event in snapshot.recent_events))

    def test_rolling_window_counter_prunes_old_buckets(self) -> None:
        counter = RollingWindowCounter(30)
        result = make_result("1.1.1.1", "ip", cycle_id=1, timestamp=0.0, ping_success=True, dns_success=None, resolved_ip="1.1.1.1")
        counter.observe(0.0, result)
        counter.observe(15.0, result)
        self.assertEqual(counter.snapshot(20.0).checks, 2)
        self.assertEqual(counter.snapshot(31.0).checks, 1)


if __name__ == "__main__":
    unittest.main()
