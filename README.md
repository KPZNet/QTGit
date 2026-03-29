# QTGit

QTGit is a small PySide6 desktop application shell for browsing to a directory and listing all Git repositories found beneath it.

## Features

- Top toolbar with Browse, Recent, current-directory display, Refresh, and About actions
- Left navigation pane listing discovered Git repositories with local branches
- Right-side vertical split pane with summary and detail placeholders
- Repository detection based on folders that contain a `.git` directory
- Persisted last browsed directory between launches
- Recent history for the last 5 browsed directories with restore support
- Clear Recent action for saved browse history
- Persisted window geometry and splitter positions between launches
- Active branch highlighting inside the repository tree
- Branch icons to distinguish active and local branches
- Active branch color icon status: green=in sync, red=behind, blue=ahead
- Branch selection updates the right pane with branch-specific metadata

## Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

## Run

```powershell
python main.py
```

## Desktop Shortcut (macOS)

Create clickable Desktop launchers that use your project `.venv`:

```bash
python3 scripts/create_desktop_shortcut.py
```

This generates:

- `~/Desktop/QTGit.command` — double-clickable shell launcher
- `~/Desktop/QTGit.app` — native macOS app bundle (when `osacompile` is available)

Both launchers will:
- Activate your project's `.venv`
- Set the working directory to the project root
- Launch `main.py`

## Usage

1. Launch the application.
2. Click Browse in the toolbar.
3. Select a directory that contains one or more Git repositories.
4. Review the detected repositories in the left pane.
5. Use Recent to reopen one of the last 5 saved browse locations or clear the saved list.
6. Use the read-only toolbar field to confirm the current restored directory.
7. Use Refresh to rescan the current directory.