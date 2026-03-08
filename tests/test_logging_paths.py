from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from pingtop.config import AppConfig
from pingtop.logging_csv import CSVLogger
from pingtop.models import CheckResult
from pingtop.paths import resolve_runtime_paths


def make_result(
    timestamp: float,
    *,
    target: str = "1.1.1.1",
    target_type: str = "ip",
    ping_success: bool = True,
    dns_success=None,
    error_category: str = "ok",
) -> CheckResult:
    return CheckResult(
        cycle_id=1,
        timestamp=timestamp,
        target=target,
        target_type=target_type,
        resolved_ip=target if target_type == "ip" else "",
        dns_success=dns_success,
        ping_success=ping_success,
        latency_ms=10.0 if ping_success else None,
        error_category=error_category,
        error_message="" if error_category == "ok" else error_category,
    )


class RuntimePathTests(unittest.TestCase):
    def test_resolve_runtime_paths_for_script_module_and_pyz(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script_paths = resolve_runtime_paths(argv0="pingtop.py", cwd=root)
            module_paths = resolve_runtime_paths(argv0=str(Path("pingtop") / "__main__.py"), cwd=root)
            pyz_paths = resolve_runtime_paths(argv0=str(Path("dist") / "pingtop.pyz"), cwd=root)

            self.assertEqual(script_paths.runtime_dir, root)
            self.assertEqual(module_paths.runtime_dir, root)
            self.assertEqual(pyz_paths.runtime_dir, root / "dist")


class CSVLoggerTests(unittest.TestCase):
    def test_around_failure_logging_captures_before_and_after(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pingtop_log.csv"
            logger = CSVLogger(path)
            config = AppConfig.default()
            config.logging_mode = "around_failure"
            config.around_failure_before_seconds = 10
            config.around_failure_after_seconds = 10

            logger.log_results([make_result(100.0), make_result(105.0)], config)
            logger.log_results([make_result(109.0, ping_success=False, dns_success=None, error_category="timeout")], config)
            logger.log_results([make_result(115.0)], config)
            logger.log_results([make_result(121.0)], config)

            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["target"], "1.1.1.1")
            self.assertEqual(rows[2]["error_category"], "timeout")

    def test_rotation_by_size_creates_rotated_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pingtop_log.csv"
            logger = CSVLogger(path)
            config = AppConfig.default()
            config.logging_mode = "all"
            config.log_rotation_max_mb = 1
            path.write_text("x" * (1024 * 1024 + 10), encoding="utf-8")

            logger.log_results([make_result(100.0)], config)

            rotated = list(Path(td).glob("pingtop_log_*.csv"))
            self.assertEqual(len(rotated), 1)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
