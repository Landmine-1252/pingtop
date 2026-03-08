from __future__ import annotations

import datetime as dt
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


CONFIG_FILENAME = "pingtop.json"
LOG_FILENAME = "pingtop_log.csv"
SNAPSHOT_PREFIX = "pingtop_snapshot_"


@dataclass(frozen=True)
class RuntimePaths:
    launch_path: Path
    runtime_dir: Path
    config_path: Path
    log_path: Path

    def snapshot_path(self, *, timestamp: Optional[dt.datetime] = None) -> Path:
        value = timestamp or dt.datetime.now()
        return self.runtime_dir / f"{SNAPSHOT_PREFIX}{value.strftime('%Y%m%d_%H%M%S')}.txt"


def resolve_runtime_paths(argv0: Optional[str] = None, cwd: Optional[Path] = None) -> RuntimePaths:
    launch_path = _resolve_launch_path(argv0=argv0, cwd=cwd)
    runtime_dir = _resolve_runtime_dir(launch_path)
    return RuntimePaths(
        launch_path=launch_path,
        runtime_dir=runtime_dir,
        config_path=runtime_dir / CONFIG_FILENAME,
        log_path=runtime_dir / LOG_FILENAME,
    )


def _resolve_launch_path(argv0: Optional[str] = None, cwd: Optional[Path] = None) -> Path:
    raw = sys.argv[0] if argv0 is None else argv0
    base_dir = Path.cwd() if cwd is None else Path(cwd)
    if not raw or raw in {"-", "-c", "-m"}:
        return base_dir
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return Path(os.path.abspath(str(candidate)))


def _resolve_runtime_dir(launch_path: Path) -> Path:
    if launch_path.is_dir():
        return launch_path
    if launch_path.suffix == ".pyz":
        return launch_path.parent
    if launch_path.name == "__main__.py" and launch_path.parent.name == "pingtop":
        return launch_path.parent.parent
    return launch_path.parent
