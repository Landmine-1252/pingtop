from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pingtop.config import AppConfig, ConfigManager
from pingtop.models import infer_target


class TargetInferenceTests(unittest.TestCase):
    def test_infer_target_normalizes_ip_and_hostname(self) -> None:
        self.assertEqual(infer_target(" 1.1.1.1 ").kind, "ip")
        self.assertEqual(infer_target("Cloudflare.com.").value, "cloudflare.com")

    def test_infer_target_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            infer_target(" ")
        with self.assertRaises(ValueError):
            infer_target("bad host name")


class ConfigNormalizationTests(unittest.TestCase):
    def test_config_normalizes_and_deduplicates_targets(self) -> None:
        config = AppConfig.from_dict(
            {
                "check_interval_seconds": 0.1,
                "ping_timeout_ms": 999999,
                "ui_refresh_interval_seconds": 10,
                "stats_window_seconds": 5,
                "diagnosis_confirm_cycles": 0,
                "recovery_confirm_cycles": 99,
                "latency_warning_ms": 250,
                "latency_critical_ms": 100,
                "logging_mode": "nope",
                "around_failure_before_seconds": -5,
                "around_failure_after_seconds": 9999,
                "log_rotation_max_mb": -1,
                "log_rotation_keep_files": 0,
                "event_history_size": 999,
                "visible_event_lines": 1,
                "targets": ["8.8.8.8", "8.8.8.8", "Google.com."],
            }
        )
        self.assertEqual(config.check_interval_seconds, 0.5)
        self.assertEqual(config.ping_timeout_ms, 30000)
        self.assertEqual(config.ui_refresh_interval_seconds, 5.0)
        self.assertEqual(config.stats_window_seconds, 30)
        self.assertEqual(config.diagnosis_confirm_cycles, 1)
        self.assertEqual(config.recovery_confirm_cycles, 10)
        self.assertEqual(config.latency_warning_ms, 250)
        self.assertEqual(config.latency_critical_ms, 250)
        self.assertEqual(config.logging_mode, "around_failure")
        self.assertEqual(config.around_failure_before_seconds, 0)
        self.assertEqual(config.around_failure_after_seconds, 600)
        self.assertEqual(config.log_rotation_max_mb, 0)
        self.assertEqual(config.log_rotation_keep_files, 1)
        self.assertEqual(config.event_history_size, 200)
        self.assertEqual(config.visible_event_lines, 3)
        self.assertEqual([target.value for target in config.targets], ["8.8.8.8", "google.com"])

    def test_config_manager_writes_defaults_and_recovers_from_bad_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pingtop.json"
            manager = ConfigManager(path)
            self.assertTrue(path.exists())
            self.assertEqual(manager.snapshot().targets[0].value, "1.1.1.1")

            path.write_text("{not json", encoding="utf-8")
            manager = ConfigManager(path)
            self.assertIn("Invalid config", manager.load_warning)


if __name__ == "__main__":
    unittest.main()
