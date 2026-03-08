from __future__ import annotations

import shutil
import tempfile
import zipapp
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "pingtop"
DIST_DIR = REPO_ROOT / "dist"
OUTPUT_PATH = DIST_DIR / "pingtop.pyz"


def build_pyz() -> Path:
    if not PACKAGE_DIR.is_dir():
        raise SystemExit(f"package directory not found: {PACKAGE_DIR}")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()

    with tempfile.TemporaryDirectory() as td:
        staging_root = Path(td)
        shutil.copytree(
            PACKAGE_DIR,
            staging_root / "pingtop",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        zipapp.create_archive(
            staging_root,
            target=OUTPUT_PATH,
            main="pingtop.__main__:main",
            compressed=True,
        )
    return OUTPUT_PATH


def main() -> int:
    output_path = build_pyz()
    print(f"built {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
