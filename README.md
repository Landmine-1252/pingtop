# pingtop

`pingtop` is a Python-only terminal tool for separating intermittent failures into three buckets:

- likely general network failure
- likely DNS failure
- isolated host or path failure

It uses only the Python standard library, shells out to the system `ping` command for ICMP checks, and stores config, logs, and snapshot reports next to the script.

## Files

- `pingtop.py`: main application
- `pingtop.json`: persisted settings and target list
- `pingtop_log.csv`: CSV log output
- `pingtop_snapshot_YYYYMMDD_HHMMSS.txt`: manual snapshots created with `s`

## Run

```bash
python pingtop.py
```

Useful smoke-test modes:

```bash
python pingtop.py --once
python pingtop.py --no-ui
```

`--once` runs a single concurrent check cycle and exits. `--no-ui` keeps monitoring without the full-screen ANSI UI.

## Default Targets

- IPs: `1.1.1.1`, `8.8.8.8`
- Hostnames: `google.com`, `cloudflare.com`, `microsoft.com`

For hostnames, `pingtop` does DNS resolution with `socket.getaddrinfo` first and then pings the resolved IP. That keeps DNS failures separate from reachability failures.

## Controls

- `q`: quit cleanly
- `p`: pause/resume monitoring
- `l`: cycle logging mode
- `+` or `=` / `-` or `_`: adjust check interval
- `>` or `.` / `<` or `,`: adjust UI refresh interval
- `a`: add a target using the in-app prompt
- `d`: delete a target by index or exact target value
- `w`: change the around-failure pre/post window
- `t`: change the rolling stats window
- `r`: reset counters
- `s`: save a snapshot report
- `h`: show/hide help

Prompt mode stays inside the UI. `Enter` submits, `Esc` cancels, and `Backspace` edits the prompt text.
Duration prompts accept plain seconds or `s` / `m` / `h` / `d` suffixes, for example `30s`, `15m`, or `1h`.

The UI uses ANSI color only, no extra library:

- green: healthy state / low latency
- yellow: warnings and elevated latency
- red: failures, high latency, and error text

`pingtop` now prefers confirmation over fast diagnosis changes:

- failure diagnoses are confirmed after `diagnosis_confirm_cycles` consecutive matching cycles
- recovery back to green is confirmed after `recovery_confirm_cycles` consecutive healthy cycles
- until then, the diagnosis banner shows a `Suspected ...` or `Recovery observed, confirming stability ...` message

## Logging Modes

- `all`: write every result row to `pingtop_log.csv`
- `failures_only`: write only failed checks
- `around_failure`: keep an in-memory rolling pre-failure buffer and write the buffered rows plus the active post-failure window

The default around-failure window is `15` seconds before and `15` seconds after a failure. Overlapping failures extend the active capture window instead of fragmenting it.

`pingtop_log.csv` also rotates automatically by size. When the active log reaches `log_rotation_max_mb`, it is renamed to a timestamped file such as `pingtop_log_20260307_130500.csv`, a new `pingtop_log.csv` is started, and old rotated logs beyond `log_rotation_keep_files` are deleted.

The live event panel is intentionally selective. It prioritizes failures, recoveries, diagnosis changes, and meaningful in-app setting changes instead of showing every low-value info message.

## Stats Window

The main counters shown in the UI are rolling-window counters instead of lifetime-only counters.

- `stats_window_seconds` controls the rolling window length
- default is `3600` seconds (`1h`)
- the UI still shows abbreviated all-time totals for the current session
- changing the stats window during a running session resets the rolling counters, because the app does not keep full raw history forever

## Failure Classification

Per cycle, `pingtop` first scores the current mix of target results:

- most or all IP targets fail: likely general network issue
- IP targets succeed while hostname DNS lookups mostly fail: likely DNS issue
- DNS succeeds but the resolved IP does not answer ping: DNS okay, reachability bad
- only one target fails while peers succeed: likely isolated target or path issue
- anything else: mixed failure pattern

For accuracy, higher-confidence diagnoses also need corroboration:

- general network and DNS diagnoses prefer at least two similar corroborating targets
- the diagnosis banner does not switch from a single noisy cycle alone; it waits for consecutive confirmation cycles

This is intentionally heuristic. It is meant to make intermittent problems easier to categorize quickly, not to replace deeper packet-level troubleshooting.

## Config Format

`pingtop.json` is created automatically if missing and stores:

```json
{
  "version": 1,
  "check_interval_seconds": 5.0,
  "ping_timeout_ms": 1200,
  "ui_refresh_interval_seconds": 0.5,
  "stats_window_seconds": 3600,
  "diagnosis_confirm_cycles": 2,
  "recovery_confirm_cycles": 2,
  "latency_warning_ms": 100,
  "latency_critical_ms": 250,
  "logging_mode": "around_failure",
  "around_failure_before_seconds": 15,
  "around_failure_after_seconds": 15,
  "log_rotation_max_mb": 25,
  "log_rotation_keep_files": 10,
  "event_history_size": 40,
  "visible_event_lines": 8,
  "targets": [
    { "value": "1.1.1.1", "type": "ip" },
    { "value": "8.8.8.8", "type": "ip" },
    { "value": "google.com", "type": "hostname" }
  ]
}
```

UI changes that affect settings are written back to `pingtop.json`.

`visible_event_lines` limits how many recent events are shown in the live UI even if the in-memory history buffer is larger.
`log_rotation_max_mb` can be set to `0` to disable rotation.

## Notes and Tradeoffs

- Windows is the primary target. The app uses `msvcrt` for key input there.
- Linux and WSL use `select` plus `termios`/`tty`.
- ANSI escape sequences drive the full-screen UI. On Windows, the app attempts to enable virtual terminal mode automatically.
- Rolling stats are stored as time buckets instead of raw per-check history so long windows stay bounded in memory.
- Diagnosis changes are intentionally conservative by default. The app favors multi-cycle confirmation over reacting to a single dropped probe.
- Latency color thresholds are configurable with `latency_warning_ms` and `latency_critical_ms`.
- `ping` latency parsing is best-effort. Success and failure are determined by command exit status first, and wall-clock timing is used as a fallback latency when parsing is not reliable.
- The tool does not require admin/root privileges, but it still depends on a working system `ping` command.
- The UI is intentionally simple ASCII text instead of `curses` so it works on stock Windows Python.
