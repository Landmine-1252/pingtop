from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional

from .config import AppConfig, ConfigManager, LOGGING_MODES
from .input import create_input_handler
from .logging_csv import CSVLogger
from .models import EventEntry, PromptState, StateSnapshot, TargetStats
from .monitor import BackgroundMonitor
from .network import CheckCoordinator
from .paths import RuntimePaths
from .state import StateStore
from .util import (
    abbreviate_count,
    abbreviate_ratio,
    format_compact_span,
    format_duration,
    format_latency,
    format_timestamp_short,
    now_local_iso,
    parse_duration_input,
    shorten,
)

if sys.platform == "win32":
    import ctypes


class Renderer:
    COLORS = {
        "red": "31",
        "green": "32",
        "yellow": "33",
        "blue": "34",
        "magenta": "35",
        "cyan": "36",
        "white": "37",
    }

    def __init__(self) -> None:
        self.ansi = sys.stdout.isatty()
        self.last_rendered_line_count = 0
        if sys.platform == "win32" and self.ansi:
            self.ansi = self._enable_windows_ansi()

    def enter(self) -> None:
        if self.ansi:
            sys.stdout.write("\x1b[?1049h\x1b[2J\x1b[H\x1b[?25l")
            sys.stdout.flush()

    def leave(self) -> None:
        if self.ansi:
            sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()

    def draw(self, text: str) -> None:
        if not self.ansi:
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
            return

        lines = text.splitlines()
        for index, line in enumerate(lines, start=1):
            sys.stdout.write(f"\x1b[{index};1H\x1b[2K{line}")

        for index in range(len(lines) + 1, self.last_rendered_line_count + 1):
            sys.stdout.write(f"\x1b[{index};1H\x1b[2K")

        sys.stdout.write(f"\x1b[{len(lines) + 1};1H\x1b[J")
        self.last_rendered_line_count = len(lines)
        sys.stdout.flush()

    def build_screen(
        self,
        snapshot: StateSnapshot,
        config: AppConfig,
        *,
        paused: bool,
        help_visible: bool,
        prompt: Optional[PromptState],
    ) -> str:
        terminal_size = shutil.get_terminal_size((140, 42))
        width = max(40, terminal_size.columns)
        height = max(20, terminal_size.lines)

        status = "PAUSED" if paused else "RUNNING"
        window_label = format_compact_span(snapshot.stats_window_seconds)
        rotation_label = (
            f"{config.log_rotation_max_mb}MB/{config.log_rotation_keep_files}"
            if config.log_rotation_max_mb > 0
            else "off"
        )
        visible_events = self._interesting_events(snapshot.recent_events)
        shown_event_count = min(len(visible_events), config.visible_event_lines)
        diagnosis_lower = snapshot.diagnosis.lower()
        if paused or "waiting" in diagnosis_lower or "no targets" in diagnosis_lower:
            status_color = "yellow"
        elif "suspected" in diagnosis_lower or "confirming" in diagnosis_lower or "recovery observed" in diagnosis_lower:
            status_color = "yellow"
        elif "reachable" in diagnosis_lower:
            status_color = "green"
        else:
            status_color = "red"
        header_lines = [self.style("pingtop", bold=True, fg="green")]
        header_lines.append(self._diagnosis_banner(snapshot.diagnosis, width, status_color))
        header_lines.extend(
            self._wrap_pairs(
                "Status",
                [
                    self._kv_pair("mode", status, value_color=status_color),
                    self._kv_pair("events", f"{shown_event_count}/{len(visible_events)}"),
                    self._kv_pair(
                        "last",
                        format_timestamp_short(snapshot.last_cycle_completed_at)
                        if snapshot.last_cycle_completed_at
                        else "-",
                    ),
                ],
                width,
                label_color=status_color,
            )
        )
        header_lines.extend(
            self._wrap_pairs(
                "Timing",
                [
                    self._kv_pair("check", format_duration(config.check_interval_seconds)),
                    self._kv_pair("timeout", f"{config.ping_timeout_ms}ms"),
                    self._kv_pair("refresh", format_duration(config.ui_refresh_interval_seconds)),
                    self._kv_pair("targets", str(len(config.targets))),
                ],
                width,
            )
        )
        header_lines.extend(
            self._wrap_pairs(
                "Config",
                [
                    self._kv_pair("stats", window_label),
                    self._kv_pair(
                        "confirm",
                        f"{config.diagnosis_confirm_cycles}/{config.recovery_confirm_cycles}",
                    ),
                    self._kv_pair("latency", f"{config.latency_warning_ms}/{config.latency_critical_ms}ms"),
                    self._kv_pair("logging", config.logging_mode),
                    self._kv_pair("rotate", rotation_label),
                ],
                width,
            )
        )
        header_lines.extend(
            self._wrap_pairs(
                "Rolling",
                [
                    self._kv_pair("checks", abbreviate_count(snapshot.session_window.checks)),
                    self._kv_pair("ok", abbreviate_count(snapshot.session_window.successes), value_color="green"),
                    self._kv_pair("fail", abbreviate_count(snapshot.session_window.failures), value_color="red"),
                    self._kv_pair("dns", abbreviate_count(snapshot.session_window.dns_failures), value_color="yellow"),
                    self._kv_pair("ping", abbreviate_count(snapshot.session_window.ping_failures), value_color="yellow"),
                ],
                width,
            )
        )
        header_lines.extend(
            self._wrap_pairs(
                "Session",
                [
                    self._kv_pair("cycles", abbreviate_count(snapshot.session.cycles_completed)),
                    self._kv_pair("checks", abbreviate_count(snapshot.session.total_checks)),
                    self._kv_pair("ok", abbreviate_count(snapshot.session.successes), value_color="green"),
                    self._kv_pair("fail", abbreviate_count(snapshot.session.failures), value_color="red"),
                    self._kv_pair("dns", abbreviate_count(snapshot.session.dns_failures), value_color="yellow"),
                    self._kv_pair("ping", abbreviate_count(snapshot.session.ping_failures), value_color="yellow"),
                ],
                width,
            )
        )
        header_lines.append(self._rule(width, "="))

        table_lines = self._build_target_table(snapshot.target_stats, width, config)

        footer_lines: list[str] = []
        if help_visible:
            footer_lines.extend(
                self._wrap_pairs(
                    "Controls",
                    [
                        self._shortcut_pair("q", "quit"),
                        self._shortcut_pair("p", "pause"),
                        self._shortcut_pair("h", "help"),
                        self._shortcut_pair("s", "snapshot"),
                        self._shortcut_pair("r", "reset"),
                    ],
                    width,
                )
            )
            footer_lines.extend(
                self._wrap_pairs(
                    "Tuning",
                    [
                        self._shortcut_pair("l", "logging"),
                        self._shortcut_pair("+/-", "check"),
                        self._shortcut_pair("</>", "refresh"),
                        self._shortcut_pair("w", "fail window" if width >= 92 else "fail win"),
                        self._shortcut_pair("t", "stats window" if width >= 92 else "stats win"),
                    ],
                    width,
                )
            )
            footer_lines.extend(
                self._wrap_pairs(
                    "Targets",
                    [
                        self._shortcut_pair("a", "add"),
                        self._shortcut_pair("d", "delete"),
                    ],
                    width,
                )
            )
            footer_lines.extend(
                self._wrap_pairs(
                    "Prompt",
                    [
                        ("Enter submit", "Enter submit"),
                        ("Esc cancel", "Esc cancel"),
                        ("Backspace edit", "Backspace edit"),
                    ],
                    width,
                )
            )
        else:
            footer_lines.extend(
                self._wrap_pairs(
                    "Help",
                    [
                        self._shortcut_pair("h", "show help"),
                        self._shortcut_pair("q", "quit"),
                    ],
                    width,
                )
            )

        prompt_line = ""
        if prompt is not None:
            prompt_line = self._render_prompt_line(
                f"{prompt.kind}: {prompt.message} > {prompt.buffer}",
                width,
            )

        footer_block = [self._rule(width, "-")]
        footer_block.extend(footer_lines)
        if prompt_line:
            footer_block.append(prompt_line)

        middle_capacity = max(0, height - len(header_lines) - len(footer_block))
        middle_lines: list[str] = []
        event_title = self._section_title(
            "Events",
            f"showing {shown_event_count}/{len(visible_events)}",
            width,
        )

        if middle_capacity <= len(table_lines):
            middle_lines.extend(table_lines[:middle_capacity])
        else:
            middle_lines.extend(table_lines)
            remaining_after_table = middle_capacity - len(table_lines)
            if remaining_after_table >= 3:
                available_event_lines = min(config.visible_event_lines, remaining_after_table - 2)
                event_lines = self._build_event_panel(visible_events, width, available_event_lines)
                event_block = [self._rule(width, "-"), event_title]
                event_block.extend(event_lines[:available_event_lines] if available_event_lines > 0 else [])
                if len(event_block) > remaining_after_table:
                    event_block = event_block[:remaining_after_table]
                middle_lines.extend(event_block)
            filler_count = max(0, middle_capacity - len(middle_lines))
            middle_lines.extend([""] * filler_count)

        lines = header_lines + middle_lines + footer_block
        return "\n".join(lines[:height])

    def build_report(self, snapshot: StateSnapshot, config: AppConfig, *, paused: bool) -> str:
        width = 180
        header = [
            f"pingtop session snapshot - {now_local_iso()}",
            f"status: {'paused' if paused else 'running'}",
            f"diagnosis: {snapshot.diagnosis}",
            (
                f"check_interval_seconds={config.check_interval_seconds}, ping_timeout_ms={config.ping_timeout_ms}, "
                f"stats_window_seconds={config.stats_window_seconds}, "
                f"ui_refresh_interval_seconds={config.ui_refresh_interval_seconds}, "
                f"diagnosis_confirm_cycles={config.diagnosis_confirm_cycles}, "
                f"recovery_confirm_cycles={config.recovery_confirm_cycles}, "
                f"latency_warning_ms={config.latency_warning_ms}, latency_critical_ms={config.latency_critical_ms}, "
                f"log_rotation_max_mb={config.log_rotation_max_mb}, log_rotation_keep_files={config.log_rotation_keep_files}, "
                f"logging_mode={config.logging_mode}, around_failure={config.around_failure_before_seconds}/{config.around_failure_after_seconds}s, "
                f"visible_event_lines={config.visible_event_lines}"
            ),
            (
                f"rolling_window: checks={snapshot.session_window.checks}, ok={snapshot.session_window.successes}, "
                f"fail={snapshot.session_window.failures}, dns={snapshot.session_window.dns_failures}, "
                f"ping={snapshot.session_window.ping_failures}"
            ),
            "",
        ]
        visible_events = self._interesting_events(snapshot.recent_events)
        body = self._build_target_table(snapshot.target_stats, width, config, ansi=False)
        events = ["", "Recent events"] + self._build_event_panel(
            visible_events,
            width,
            min(15, config.visible_event_lines),
            ansi=False,
        )
        return "\n".join(header + body + events) + "\n"

    def style(
        self,
        text: str,
        *,
        fg: Optional[str] = None,
        bold: bool = False,
        dim: bool = False,
    ) -> str:
        if not self.ansi:
            return text
        codes = []
        if bold:
            codes.append("1")
        if dim:
            codes.append("2")
        if fg in self.COLORS:
            codes.append(self.COLORS[fg])
        if not codes:
            return text
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"

    def _rule(self, width: int, char: str) -> str:
        line = char * width
        return self.style(line, fg="blue", dim=True)

    def _diagnosis_banner(self, diagnosis: str, width: int, color: str) -> str:
        label = self.style("Diagnosis", fg=color, bold=True)
        available = max(1, width - 11)
        return f"{label}  {self.style(shorten(diagnosis, available), fg=color, bold=True)}"

    def _section_title(self, title: str, subtitle: str = "", width: Optional[int] = None) -> str:
        rendered_title = self.style(title, fg="cyan", bold=True)
        if subtitle:
            suffix = shorten(subtitle, width - len(title) - 2) if width else subtitle
            return f"{rendered_title}  {suffix}".rstrip()
        return rendered_title

    def _kv_pair(
        self,
        key: str,
        value: str,
        *,
        key_color: str = "white",
        value_color: Optional[str] = None,
    ) -> tuple[str, str]:
        plain = f"{key} {value}"
        rendered = (
            f"{self.style(key, fg=key_color, dim=True)} "
            f"{self.style(value, fg=value_color, bold=value_color is not None)}"
        )
        return plain, rendered

    def _shortcut_pair(self, key: str, description: str) -> tuple[str, str]:
        plain = f"[{key}] {description}"
        rendered = f"{self.style(f'[{key}]', fg='yellow', bold=True)} {description}"
        return plain, rendered

    def _wrap_pairs(
        self,
        label: str,
        segments: list[tuple[str, str]],
        width: int,
        *,
        label_color: str = "cyan",
    ) -> list[str]:
        prefix_plain = f"{label:<8} "
        prefix_rendered = self.style(f"{label:<8}", fg=label_color, bold=True) + " "
        continuation_plain = " " * len(prefix_plain)
        available = max(8, width - len(prefix_plain))
        rows: list[list[tuple[str, str]]] = []
        current_row: list[tuple[str, str]] = []
        current_length = 0

        for plain, rendered in segments:
            if not plain:
                continue
            segment_length = len(plain)
            separator_length = 2 if current_row else 0
            if current_row and current_length + separator_length + segment_length > available:
                rows.append(current_row)
                current_row = [(plain, rendered)]
                current_length = segment_length
            else:
                current_row.append((plain, rendered))
                current_length += separator_length + segment_length

        if current_row or not rows:
            rows.append(current_row)

        wrapped: list[str] = []
        for index, row in enumerate(rows):
            prefix = prefix_rendered if index == 0 else continuation_plain
            content = "  ".join(rendered for _, rendered in row) if row else "-"
            wrapped.append(prefix + content)
        return wrapped

    def _render_prompt_line(self, text: str, width: int) -> str:
        prompt_prefix_plain = f"{'Prompt':<8} "
        prompt_prefix_rendered = self.style(f"{'Prompt':<8}", fg="magenta", bold=True) + " "
        plain = prompt_prefix_plain + text
        if len(plain) > width:
            shortened = shorten(text, max(1, width - len(prompt_prefix_plain)))
            return prompt_prefix_rendered + shortened
        return prompt_prefix_rendered + text

    def _build_target_table(
        self,
        stats_list: list[TargetStats],
        width: int,
        config: AppConfig,
        *,
        ansi: Optional[bool] = None,
    ) -> list[str]:
        use_ansi = self.ansi if ansi is None else ansi
        original_ansi = self.ansi
        self.ansi = use_ansi
        try:
            window_label = format_compact_span(config.stats_window_seconds)
            loss_header = f"{window_label} Loss%"
            ratio_header = f"{window_label} OK/Fail"
            loss_width = max(8, len(loss_header))
            ratio_width = max(13, len(ratio_header))
            lines = [self.style("Targets", fg="cyan", bold=True)]
            header = (
                f"{'Idx':>3} {'Target':24} {'Type':8} {'State':10} {'Latency':>9} "
                f"{'Consec':>6} {loss_header:>{loss_width}} {ratio_header:>{ratio_width}} {'Last IP':18}  Error"
            )
            lines.append(self.style(shorten(header, width), fg="white", dim=True))
            if not stats_list:
                lines.append("  - no targets configured")
                return lines
            for index, stats in enumerate(stats_list, start=1):
                state_plain = f"{stats.last_result.lower():10}"
                state_text = self.style(
                    state_plain,
                    fg=self._state_color(stats),
                    bold=stats.last_state == "down",
                )
                latency_plain = f"{format_latency(stats.last_latency_ms):>9}"
                latency_color = self._latency_color(stats.last_latency_ms, config)
                latency_text = self.style(
                    latency_plain,
                    fg=latency_color,
                    bold=latency_color == "red",
                )
                consecutive_plain = f"{stats.consecutive_failures:>6}"
                consecutive_text = self.style(
                    consecutive_plain,
                    fg="red" if stats.consecutive_failures > 0 else "green",
                    bold=stats.consecutive_failures > 0,
                )
                loss_plain = f"{stats.window_summary.loss_percentage:>{loss_width - 1}.1f}%"
                loss_text = self.style(loss_plain, fg=self._loss_color(stats.window_summary.loss_percentage))
                ok_fail_plain = f"{abbreviate_ratio(stats.window_summary.successes, stats.window_summary.failures):>{ratio_width}}"
                ok_fail_text = self.style(
                    ok_fail_plain,
                    fg="red" if stats.window_summary.failures > 0 else "green",
                )
                error_text = stats.last_error_category if stats.last_error_category not in ("", "ok") else "-"
                if stats.last_error_message and stats.last_error_category not in ("", "ok"):
                    error_text = f"{stats.last_error_category}: {stats.last_error_message}"
                fixed_width = (
                    3 + 1 + 24 + 1 + 8 + 1 + 10 + 1 + 9 + 1 + 6 + 1 + loss_width + 1 + ratio_width + 1 + 18 + 2
                )
                error_text = shorten(error_text, max(10, width - fixed_width))
                if error_text != "-":
                    error_text = self.style(error_text, fg="red", bold=True)
                lines.append(
                    f"{index:>3} "
                    f"{shorten(stats.target, 24):24} "
                    f"{stats.target_type:8} "
                    f"{state_text} "
                    f"{latency_text} "
                    f"{consecutive_text} "
                    f"{loss_text} "
                    f"{ok_fail_text} "
                    f"{shorten(stats.last_resolved_ip or '-', 18):18}  "
                    f"{error_text}"
                )
            return lines
        finally:
            self.ansi = original_ansi

    def _build_event_panel(
        self,
        events: list[EventEntry],
        width: int,
        available_lines: int,
        *,
        ansi: Optional[bool] = None,
    ) -> list[str]:
        use_ansi = self.ansi if ansi is None else ansi
        original_ansi = self.ansi
        self.ansi = use_ansi
        try:
            if not events:
                return [self.style("  - no notable failures, recoveries, or config changes yet", fg="white", dim=True)]
            selected = events[-available_lines:]
            lines = []
            for event in selected:
                prefix = f"{format_timestamp_short(event.timestamp)} {event.level.upper():5}"
                lines.append(
                    self.style(
                        shorten(f"{prefix} {event.message}", width),
                        fg=self._event_color(event.level),
                        bold=event.level in ("warn", "error"),
                    )
                )
            return lines
        finally:
            self.ansi = original_ansi

    def _interesting_events(self, events: list[EventEntry]) -> list[EventEntry]:
        return [event for event in events if self._is_interesting_event(event)]

    def _is_interesting_event(self, event: EventEntry) -> bool:
        if event.level in ("warn", "error"):
            return True
        notable_prefixes = (
            "Diagnosis changed:",
            "Monitoring ",
            "Counters reset",
            "Snapshot saved to ",
            "Added target ",
            "Deleted target ",
            "Logging mode set to ",
            "Check interval set to ",
            "UI refresh interval set to ",
            "Around-failure window set to ",
            "Stats window ",
        )
        if event.message.startswith(notable_prefixes):
            return True
        return " recovered (" in event.message

    def _state_color(self, stats: TargetStats) -> str:
        if stats.last_state == "up":
            return "green"
        if stats.last_state == "down":
            return "red"
        return "yellow"

    def _latency_color(self, latency_ms: Optional[float], config: AppConfig) -> Optional[str]:
        if latency_ms is None:
            return None
        if latency_ms >= config.latency_critical_ms:
            return "red"
        if latency_ms >= config.latency_warning_ms:
            return "yellow"
        return "green"

    def _loss_color(self, loss_percentage: float) -> str:
        if loss_percentage >= 50.0:
            return "red"
        if loss_percentage > 0.0:
            return "yellow"
        return "green"

    def _event_color(self, level: str) -> str:
        if level == "error":
            return "red"
        if level == "warn":
            return "yellow"
        return "cyan"

    def _enable_windows_ansi(self) -> bool:
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
                return False
            enable_vt = 0x0004
            if mode.value & enable_vt:
                return True
            return kernel32.SetConsoleMode(handle, mode.value | enable_vt) != 0
        except Exception:
            return False


class PingTopUI:
    def __init__(
        self,
        runtime_paths: RuntimePaths,
        config_manager: ConfigManager,
        state_store: StateStore,
        logger: CSVLogger,
        coordinator: CheckCoordinator,
    ) -> None:
        self.runtime_paths = runtime_paths
        self.config_manager = config_manager
        self.state_store = state_store
        self.logger = logger
        self.coordinator = coordinator
        self.monitor = BackgroundMonitor(config_manager, state_store, logger, coordinator)
        self.renderer = Renderer()
        self.help_visible = True
        self.prompt: Optional[PromptState] = None
        self.running = True

        if self.config_manager.load_warning:
            self.state_store.add_event("warn", self.config_manager.load_warning)

    def run(self) -> int:
        self.monitor.start()
        try:
            self.renderer.enter()
            with create_input_handler() as input_handler:
                while self.running:
                    config = self.config_manager.snapshot()
                    snapshot = self.state_store.snapshot()
                    screen = self.renderer.build_screen(
                        snapshot,
                        config,
                        paused=self.monitor.is_paused(),
                        help_visible=self.help_visible,
                        prompt=self.prompt,
                    )
                    self.renderer.draw(screen)
                    for key in input_handler.read_keys(config.ui_refresh_interval_seconds):
                        self.handle_key(key)
        except KeyboardInterrupt:
            self.running = False
        finally:
            self.monitor.stop()
            self.renderer.leave()
        return 0

    def handle_key(self, key: str) -> None:
        if key == "\x03":
            self.running = False
            return
        if self.prompt is not None:
            self._handle_prompt_key(key)
            return

        if key.lower() == "q":
            self.running = False
        elif key.lower() == "p":
            paused = self.monitor.toggle_pause()
            self.state_store.add_event("info", "Monitoring paused" if paused else "Monitoring resumed")
        elif key.lower() == "l":
            self._cycle_logging_mode()
        elif key in ("+", "="):
            self._adjust_check_interval(0.5)
        elif key in ("-", "_"):
            self._adjust_check_interval(-0.5)
        elif key in (">", "."):
            self._adjust_ui_refresh(-0.1)
        elif key in ("<", ","):
            self._adjust_ui_refresh(0.1)
        elif key.lower() == "a":
            self.prompt = PromptState(kind="add", message="enter a hostname or IP to add")
        elif key.lower() == "d":
            self.prompt = PromptState(kind="delete", message="enter target index or exact target to delete")
        elif key.lower() == "w":
            self.prompt = PromptState(
                kind="window",
                message="duration or before,after (example 10s or 10s,20s)",
            )
        elif key.lower() == "t":
            self.prompt = PromptState(kind="stats_window", message="stats window like 15m, 1h, or 1d")
        elif key.lower() == "r":
            self.state_store.reset_counters()
            self.state_store.add_event("info", "Counters reset")
        elif key.lower() == "s":
            path = self._save_snapshot_report()
            self.state_store.add_event("info", f"Snapshot saved to {path.name}")
        elif key.lower() == "h":
            self.help_visible = not self.help_visible

    def _handle_prompt_key(self, key: str) -> None:
        assert self.prompt is not None
        if key in ("\r", "\n"):
            value = self.prompt.buffer.strip()
            kind = self.prompt.kind
            self.prompt = None
            if kind == "add":
                self._submit_add_target(value)
            elif kind == "delete":
                self._submit_delete_target(value)
            elif kind == "window":
                self._submit_window(value)
            elif kind == "stats_window":
                self._submit_stats_window(value)
            return
        if key == "\x1b":
            self.prompt = None
            return
        if key in ("\x7f", "\b"):
            self.prompt.buffer = self.prompt.buffer[:-1]
            return
        if key.isprintable():
            self.prompt.buffer += key

    def _cycle_logging_mode(self) -> None:
        current = self.config_manager.snapshot().logging_mode
        index = LOGGING_MODES.index(current)
        next_mode = LOGGING_MODES[(index + 1) % len(LOGGING_MODES)]
        self.config_manager.update(lambda config: setattr(config, "logging_mode", next_mode))
        self.state_store.add_event("info", f"Logging mode set to {next_mode}")

    def _adjust_check_interval(self, delta: float) -> None:
        def updater(config: AppConfig) -> None:
            config.check_interval_seconds = round(config.check_interval_seconds + delta, 2)

        config = self.config_manager.update(updater)
        self.state_store.add_event("info", f"Check interval set to {config.check_interval_seconds:.2f}s")

    def _adjust_ui_refresh(self, delta: float) -> None:
        def updater(config: AppConfig) -> None:
            config.ui_refresh_interval_seconds = round(config.ui_refresh_interval_seconds + delta, 2)

        config = self.config_manager.update(updater)
        self.state_store.add_event("info", f"UI refresh interval set to {config.ui_refresh_interval_seconds:.2f}s")

    def _submit_add_target(self, raw_value: str) -> None:
        from .models import infer_target

        if not raw_value:
            self.state_store.add_event("warn", "Add target canceled: empty input")
            return
        try:
            target = infer_target(raw_value)
        except ValueError as exc:
            self.state_store.add_event("warn", f"Invalid target: {exc}")
            return

        def updater(config: AppConfig) -> None:
            config.targets.append(target)

        config = self.config_manager.update(updater)
        self.state_store.sync_targets(config)
        self.state_store.add_event("info", f"Added target {target.value}")

    def _submit_delete_target(self, raw_value: str) -> None:
        from .models import infer_target

        if not raw_value:
            self.state_store.add_event("warn", "Delete target canceled: empty input")
            return
        config_snapshot = self.config_manager.snapshot()
        target_to_remove = ""
        if raw_value.isdigit():
            index = int(raw_value)
            if 1 <= index <= len(config_snapshot.targets):
                target_to_remove = config_snapshot.targets[index - 1].value
        else:
            try:
                target_to_remove = infer_target(raw_value).value
            except ValueError:
                target_to_remove = raw_value.strip().lower()
        if not target_to_remove:
            self.state_store.add_event("warn", f"Delete target failed: no match for {raw_value}")
            return

        removed = {"value": ""}

        def updater(config: AppConfig) -> None:
            kept = [target for target in config.targets if target.value != target_to_remove]
            if len(kept) != len(config.targets):
                removed["value"] = target_to_remove
                config.targets = kept

        config = self.config_manager.update(updater)
        self.state_store.sync_targets(config)
        if removed["value"]:
            self.state_store.add_event("info", f"Deleted target {removed['value']}")
        else:
            self.state_store.add_event("warn", f"Delete target failed: no match for {raw_value}")

    def _submit_window(self, raw_value: str) -> None:
        if not raw_value:
            self.state_store.add_event("warn", "Around-failure window unchanged")
            return
        try:
            if "," in raw_value:
                before_text, after_text = [part.strip() for part in raw_value.split(",", 1)]
                before_value = parse_duration_input(before_text)
                after_value = parse_duration_input(after_text)
            else:
                before_value = after_value = parse_duration_input(raw_value)
        except ValueError:
            self.state_store.add_event("warn", "Window must be a duration like 10s or 10s,20s")
            return

        def updater(config: AppConfig) -> None:
            config.around_failure_before_seconds = before_value
            config.around_failure_after_seconds = after_value

        config = self.config_manager.update(updater)
        self.state_store.add_event(
            "info",
            (
                "Around-failure window set to "
                f"{config.around_failure_before_seconds}/{config.around_failure_after_seconds}s"
            ),
        )

    def _submit_stats_window(self, raw_value: str) -> None:
        if not raw_value:
            self.state_store.add_event("warn", "Stats window unchanged")
            return
        try:
            stats_window_seconds = parse_duration_input(raw_value)
        except ValueError as exc:
            self.state_store.add_event("warn", f"Stats window error: {exc}")
            return

        def updater(config: AppConfig) -> None:
            config.stats_window_seconds = stats_window_seconds

        config = self.config_manager.update(updater)
        window_reset = self.state_store.sync_targets(config)
        if window_reset:
            self.state_store.add_event(
                "info",
                f"Stats window set to {format_compact_span(config.stats_window_seconds)}; rolling counters reset",
            )

    def _save_snapshot_report(self) -> Path:
        snapshot = self.state_store.snapshot()
        config = self.config_manager.snapshot()
        path = self.runtime_paths.snapshot_path()
        path.write_text(
            self.renderer.build_report(snapshot, config, paused=self.monitor.is_paused()),
            encoding="utf-8",
        )
        return path
