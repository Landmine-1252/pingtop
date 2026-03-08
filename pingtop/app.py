from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Optional

from .config import ConfigManager
from .logging_csv import CSVLogger
from .models import CheckResult, StateSnapshot
from .monitor import BackgroundMonitor
from .network import CheckCoordinator, PingRunner
from .paths import RuntimePaths, resolve_runtime_paths
from .state import StateStore
from .ui import PingTopUI
from .util import format_latency, human_error_message, now_local_iso


@dataclass
class AppServices:
    runtime_paths: RuntimePaths
    config_manager: ConfigManager
    state_store: StateStore
    logger: CSVLogger
    coordinator: CheckCoordinator


def build_services(runtime_paths: RuntimePaths) -> AppServices:
    config_manager = ConfigManager(runtime_paths.config_path)
    state_store = StateStore(config_manager.snapshot())
    logger = CSVLogger(runtime_paths.log_path)
    coordinator = CheckCoordinator(PingRunner())
    return AppServices(
        runtime_paths=runtime_paths,
        config_manager=config_manager,
        state_store=state_store,
        logger=logger,
        coordinator=coordinator,
    )


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
    services = build_services(resolve_runtime_paths())
    try:
        if args.no_ui or args.once:
            return run_headless(
                services.config_manager,
                services.state_store,
                services.logger,
                services.coordinator,
                once=args.once,
            )
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("Interactive UI requires a TTY; falling back to --no-ui mode.")
            return run_headless(
                services.config_manager,
                services.state_store,
                services.logger,
                services.coordinator,
            )
        result = PingTopUI(
            services.runtime_paths,
            services.config_manager,
            services.state_store,
            services.logger,
            services.coordinator,
        ).run()
        print(build_exit_summary(services.state_store.snapshot()))
        return result
    finally:
        services.coordinator.close()
