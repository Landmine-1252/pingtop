# pingtop

`pingtop` is a stdlib-only terminal tool for separating intermittent connectivity problems into a few practical buckets:

- likely general network failure
- likely DNS failure
- isolated host or path failure

It uses Python DNS resolution via `socket.getaddrinfo`, shells out to the system `ping` command for ICMP checks, and keeps config, CSV logs, rotated logs, and snapshot reports beside the launched script or launched `.pyz`.

## Source Layout

The source is now split into a small set of cohesive modules instead of one large script:

- `pingtop.py`: thin local development entrypoint
- `pingtop/__main__.py`: entrypoint for `python -m pingtop` and the `.pyz`
- `pingtop/app.py`: app orchestration, argument parsing, headless modes
- `pingtop/paths.py`: runtime path resolution
- `pingtop/config.py`: config schema, normalization, persistence
- `pingtop/models.py`: dataclasses for targets, results, events, stats
- `pingtop/diagnosis.py`: cycle diagnosis logic
- `pingtop/state.py`: rolling counters and in-memory app state
- `pingtop/network.py`: DNS resolution, ping execution, threaded checks
- `pingtop/logging_csv.py`: CSV logging, around-failure buffering, rotation
- `pingtop/monitor.py`: background scheduler
- `pingtop/input.py`: portable keyboard input
- `pingtop/ui.py`: ANSI renderer and interactive controller
- `scripts/build_pyz.py`: stdlib-only zipapp build script
- `tests/`: focused `unittest` coverage

## Runtime Files

Runtime files keep their existing names:

- config: `pingtop.json`
- main CSV log: `pingtop_log.csv`
- snapshots: `pingtop_snapshot_YYYYMMDD_HHMMSS.txt`

These files are resolved from the launched artifact path, not from `__file__`.

That matters for `.pyz`: inside a zipapp, `__file__` points into the archive, which is not where writable runtime files belong. `pingtop/paths.py` centralizes this logic so:

- `python pingtop.py` writes beside `pingtop.py`
- `python -m pingtop` writes beside the source checkout entrypoint context, not inside `pingtop/__main__.py`
- `python dist/pingtop.pyz` writes beside `dist/pingtop.pyz`

## Run

Local development from source:

```bash
python pingtop.py
python pingtop.py --once
python pingtop.py --no-ui
python -m pingtop
python -m pingtop --once
```

Build the single-file artifact:

```bash
python scripts/build_pyz.py
```

This is the local build command for the deployable zipapp. It creates `dist/pingtop.pyz`.

Run the built artifact:

```bash
python dist/pingtop.pyz
python dist/pingtop.pyz --once
python dist/pingtop.pyz --no-ui
```

## Default Targets

- IPs: `1.1.1.1`, `8.8.8.8`
- Hostnames: `google.com`, `cloudflare.com`, `microsoft.com`

For hostnames, `pingtop` resolves DNS first and then pings the resolved IP. That keeps DNS failures separate from reachability failures.

## Controls

- `q`: quit cleanly
- `p`: pause/resume monitoring
- `l`: cycle logging mode
- `+` or `=` / `-` or `_`: adjust check interval
- `>` or `.` / `<` or `,`: adjust UI refresh interval
- `a`: add a target
- `d`: delete a target
- `w`: change the around-failure window
- `t`: change the rolling stats window
- `r`: reset counters
- `s`: save a snapshot report
- `h`: show/hide help

Prompt mode stays inside the UI. `Enter` submits, `Esc` cancels, and `Backspace` edits the prompt text.

## Logging Modes

- `all`: write every result row to `pingtop_log.csv`
- `failures_only`: write only failures
- `around_failure`: keep a rolling in-memory pre-failure buffer and capture post-failure results too

Around-failure logging defaults to `15` seconds before and `15` seconds after a failure. Overlapping failures extend the active capture window instead of fragmenting it.

`pingtop_log.csv` also rotates automatically by size. When the active log reaches `log_rotation_max_mb`, it is renamed to a timestamped file such as `pingtop_log_20260307_130500.csv`, a fresh `pingtop_log.csv` is created, and older rotated logs beyond `log_rotation_keep_files` are deleted.

## Accuracy and Diagnosis

`pingtop` uses per-cycle heuristics, but it now prefers conservative conclusions over single-probe noise:

- general network and DNS diagnoses prefer corroboration from multiple similar targets
- failure diagnoses require `diagnosis_confirm_cycles` consecutive matching cycles
- recovery back to green requires `recovery_confirm_cycles` consecutive healthy cycles
- before confirmation, the banner shows `Suspected ...` or `Recovery observed, confirming stability ...`

This keeps the tool useful for intermittent problems without overreacting to a single timeout.

## Config

`pingtop.json` is created automatically if missing and remains backward-compatible with the current schema:

```json
{
  "version": 1,
  "check_interval_seconds": 1.0,
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

## Tests

Run the focused unit test suite with:

```bash
python -m unittest discover -s tests -v
```

The tests avoid live network or TTY requirements and focus on pure or mockable behavior:

- target inference and config normalization
- diagnosis confirmation behavior
- rolling-window counters
- around-failure logging and rotation
- runtime path resolution
- ping command construction
- DNS/ping orchestration with mocks
- headless `--once` flow with mocks

## CI/CD

GitHub Actions in `.github/workflows/ci.yml` does the following:

- triggers on `push` and `pull_request`
- runs compile checks and unit tests on `ubuntu-latest` and `windows-latest`
- runs a dedicated build job after tests pass
- builds `dist/pingtop.pyz` with `python scripts/build_pyz.py`
- uploads the `.pyz` as a workflow artifact named like `pingtop-pyz-<sha>`
- on a pushed version tag like `v0.1.0`, creates or updates a GitHub Release for that tag and attaches `pingtop.pyz`

The build job runs once, so you do not get duplicate identical artifacts from every test OS.

Example release flow:

```bash
git tag v0.1.0
git push origin v0.1.0
```

## Limitations

- The `.pyz` is still a Python application, so Python must already be installed on the machine.
- `pingtop` still depends on the system `ping` command being available.
- ICMP success/failure uses the system command exit status first. Latency parsing is best-effort and falls back to wall-clock timing when parsing is unreliable.
- The UI is ANSI text, not `curses`, to keep Windows compatibility straightforward.
