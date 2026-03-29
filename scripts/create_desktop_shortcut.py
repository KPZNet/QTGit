#!/usr/bin/env python3
"""Generate macOS Desktop launchers for QTGit using .venv."""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path


def build_command_launcher(project_root: Path, desktop_dir: Path) -> Path:
    """Create a .command shell launcher on Desktop."""
    venv_python = project_root / ".venv" / "bin" / "python"
    main_py = project_root / "main.py"
    launcher_path = desktop_dir / "QTGit.command"

    if not main_py.exists():
        raise SystemExit(f"main.py not found at: {main_py}")

    if not venv_python.exists():
        raise SystemExit(
            "Expected virtual environment python was not found:\n"
            f"{venv_python}\n"
            "Create it first (example): python3 -m venv .venv"
        )

    launcher_content = f"""#!/bin/zsh
set -euo pipefail

cd "{project_root}"
exec "{venv_python}" "{main_py}"
"""
    launcher_path.write_text(launcher_content, encoding="utf-8")

    mode = launcher_path.stat().st_mode
    launcher_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return launcher_path


def build_app_launcher(project_root: Path, desktop_dir: Path) -> Path:
    """Create a native .app bundle via AppleScript on Desktop."""
    venv_python = project_root / ".venv" / "bin" / "python"
    main_py = project_root / "main.py"

    if not main_py.exists():
        raise SystemExit(f"main.py not found at: {main_py}")

    if not venv_python.exists():
        raise SystemExit(
            "Expected virtual environment python was not found:\n"
            f"{venv_python}\n"
            "Create it first (example): python3 -m venv .venv"
        )

    app_path = desktop_dir / "QTGit.app"
    script = f'''
set projectRoot to "{project_root.as_posix()}"
set pythonPath to "{venv_python.as_posix()}"
set mainPath to "{main_py.as_posix()}"

do shell script "cd " & quoted form of projectRoot & " && exec " & quoted form of pythonPath & " " & quoted form of mainPath
'''

    subprocess.run(
        ["osacompile", "-o", str(app_path), "-e", script],
        check=True,
    )
    return app_path


def main() -> int:
    """Generate Desktop launchers."""
    project_root = Path(__file__).resolve().parents[1]
    desktop_dir = Path.home() / "Desktop"

    if not desktop_dir.exists():
        raise SystemExit(f"Desktop directory not found: {desktop_dir}")

    command_path = build_command_launcher(project_root, desktop_dir)

    app_path: Path | None = None
    try:
        app_path = build_app_launcher(project_root, desktop_dir)
    except FileNotFoundError:
        print("⚠️  Skipped .app generation: osacompile not available on this macOS setup.")
    except subprocess.CalledProcessError as exc:
        print(f"⚠️  Skipped .app generation: osacompile failed ({exc}).")

    print(f"✓ Created launcher: {command_path}")
    if app_path:
        print(f"✓ Created launcher: {app_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


