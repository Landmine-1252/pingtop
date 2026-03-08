from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pingtop.app import run_headless
from pingtop.config import AppConfig
from pingtop.models import CheckResult, SessionTotals, StateSnapshot
from pingtop.network import CheckCoordinator, PingRunner, resolve_hostname


class NetworkTests(unittest.TestCase):
    def test_ping_runner_builds_windows_and_posix_commands(self) -> None:
        runner = PingRunner()
        runner.is_windows = True
        self.assertEqual(runner._build_command("1.1.1.1", 1200), ["ping", "-n", "1", "-w", "1200", "1.1.1.1"])
        runner.is_windows = False
        self.assertEqual(runner._build_command("1.1.1.1", 1200), ["ping", "-n", "-c", "1", "-W", "2", "1.1.1.1"])

    @mock.patch("pingtop.network.socket.getaddrinfo")
    def test_resolve_hostname_prefers_ipv4(self, getaddrinfo: mock.Mock) -> None:
        getaddrinfo.return_value = [
            (0, 0, 0, "", ("2001:db8::1", 0)),
            (0, 0, 0, "", ("104.16.133.229", 0)),
        ]
        ok, address, error = resolve_hostname("cloudflare.com")
        self.assertTrue(ok)
        self.assertEqual(address, "104.16.133.229")
        self.assertEqual(error, "")

    @mock.patch("pingtop.network.resolve_hostname")
    def test_check_coordinator_orchestrates_dns_then_ping(self, resolve_hostname_mock: mock.Mock) -> None:
        ping_runner = mock.Mock()
        ping_runner.ping.return_value = (True, 18.5, "ok", "")
        coordinator = CheckCoordinator(ping_runner)
        resolve_hostname_mock.return_value = (True, "104.16.133.229", "")

        ip_result = coordinator._check_target(mock.Mock(value="1.1.1.1", kind="ip"), 1200, 1)
        host_result = coordinator._check_target(mock.Mock(value="cloudflare.com", kind="hostname"), 1200, 1)

        self.assertTrue(ip_result.ping_success)
        self.assertTrue(host_result.dns_success)
        self.assertEqual(host_result.resolved_ip, "104.16.133.229")
        self.assertEqual(ping_runner.ping.call_count, 2)
        coordinator.close()


class HeadlessTests(unittest.TestCase):
    @mock.patch("pingtop.app.BackgroundMonitor")
    def test_run_headless_once_uses_monitor_and_prints_summary(self, monitor_cls: mock.Mock) -> None:
        config = AppConfig.default()
        config_manager = mock.Mock()
        config_manager.load_warning = ""
        config_manager.snapshot.return_value = config

        state_snapshot = StateSnapshot(
            diagnosis="All monitored targets are reachable",
            target_stats=[],
            recent_events=[],
            session=SessionTotals(cycles_completed=1, total_checks=1, successes=1),
            session_window=mock.Mock(checks=1, successes=1, failures=0, dns_failures=0, ping_failures=0),
            stats_window_seconds=config.stats_window_seconds,
            last_cycle_completed_at=1.0,
            last_cycle_id=1,
        )
        state_store = mock.Mock()
        state_store.snapshot.return_value = state_snapshot
        logger = mock.Mock()
        coordinator = mock.Mock()

        result = CheckResult(
            cycle_id=1,
            timestamp=1.0,
            target="1.1.1.1",
            target_type="ip",
            resolved_ip="1.1.1.1",
            dns_success=None,
            ping_success=True,
            latency_ms=10.0,
            error_category="ok",
        )
        monitor = mock.Mock()
        monitor.cycle_id = 1
        monitor.run_single_cycle.return_value = [result]
        monitor_cls.return_value = monitor

        output = io.StringIO()
        with redirect_stdout(output):
            rc = run_headless(config_manager, state_store, logger, coordinator, once=True)

        self.assertEqual(rc, 0)
        state_store.handle_cycle.assert_called_once()
        logger.log_results.assert_called_once()
        self.assertIn("Session summary:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
