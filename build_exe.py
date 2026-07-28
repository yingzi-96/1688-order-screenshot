from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_NAME = "1688订单截图工具"
DIST_APP = ROOT / "dist" / APP_NAME


def run(command: list[str]) -> None:
    print(" ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)


def ensure_runtime_dirs() -> None:
    for name in ("auth", "logs", "output", "input"):
        (DIST_APP / name).mkdir(parents=True, exist_ok=True)

    sample_orders = ROOT / "input" / "orders.txt"
    if sample_orders.exists():
        shutil.copy2(sample_orders, DIST_APP / "input" / "orders.txt")

    for filename in ("README.md", "requirements.txt"):
        source = ROOT / filename
        if source.exists():
            shutil.copy2(source, DIST_APP / filename)


def main() -> int:
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--windowed",
            "--name",
            APP_NAME,
            "--collect-all",
            "playwright",
            "--hidden-import",
            "playwright.sync_api",
            "--hidden-import",
            "pandas",
            "--hidden-import",
            "openpyxl",
            "--hidden-import",
            "xlrd",
            "web_gui.py",
        ]
    )
    ensure_runtime_dirs()
    print(f"\n打包完成: {DIST_APP / (APP_NAME + '.exe')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
