from __future__ import annotations

import threading
import time

from .config import AppConfig, ConfigManager
from .logging_csv import CSVLogger
from .models import CheckResult
from .network import CheckCoordinator
from .state import StateStore


class BackgroundMonitor:
    def __init__(
        self,
        config_manager: ConfigManager,
        state_store: StateStore,
        logger: CSVLogger,
        coordinator: CheckCoordinator,
    ) -> None:
        self.config_manager = config_manager
        self.state_store = state_store
        self.logger = logger
        self.coordinator = coordinator
        self.stop_event = threading.Event()
        self.pause_lock = threading.Lock()
        self._paused = False
        self.sequence_lock = threading.Lock()
        self.sequence = 0
        self.cycle_id = 0
        self.thread = threading.Thread(target=self._run, name="scheduler", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5.0)

    def toggle_pause(self) -> bool:
        with self.pause_lock:
            self._paused = not self._paused
            return self._paused

    def is_paused(self) -> bool:
        with self.pause_lock:
            return self._paused

    def run_single_cycle(self, config: AppConfig) -> list[CheckResult]:
        self.cycle_id += 1
        results = self.coordinator.execute_cycle(config, self.cycle_id)
        self._stamp_sequences(results)
        return results

    def _run(self) -> None:
        next_run = time.monotonic()
        while not self.stop_event.is_set():
            if self.is_paused():
                next_run = time.monotonic()
                self.stop_event.wait(0.1)
                continue

            now = time.monotonic()
            if now < next_run:
                self.stop_event.wait(min(0.1, next_run - now))
                continue

            config = self.config_manager.snapshot()
            results = self.run_single_cycle(config)
            self.state_store.handle_cycle(results, config, self.cycle_id)
            self.logger.log_results(results, config)
            next_run = time.monotonic() + config.check_interval_seconds

    def _stamp_sequences(self, results: list[CheckResult]) -> None:
        with self.sequence_lock:
            for result in results:
                self.sequence += 1
                result.sequence = self.sequence
