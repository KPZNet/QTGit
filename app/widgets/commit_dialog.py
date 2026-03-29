from __future__ import annotations

import threading
from datetime import date
import shutil
import subprocess

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush, QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.services.app_settings import AppSettings
from app.services.repo_scanner import (
    GitBranch,
    GitRepository,
    commit_local_changes,
    push_branch_commits,
)
from app.widgets.git_diff_viewer import GitDiffViewerWindow, LocalGitDiffViewerWindow


class _CommitWorkerSignals(QWidget):
    commit_done = Signal(object)
    push_done = Signal(object)


class CommitDialog(QDialog):
    """Split-screen commit utility for one active branch."""

    def __init__(
        self,
        repository: GitRepository,
        branch: GitBranch,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._branch = branch
        self._settings = AppSettings()
        self._signals = _CommitWorkerSignals()
        self._signals.commit_done.connect(self._on_commit_done)
        self._signals.push_done.connect(self._on_push_done)

        self.setWindowTitle(f"Commit - {repository.name} / {branch.name}")
        self.resize(980, 680)

        self._file_table: QTableWidget | None = None
        self._commit_table: QTableWidget | None = None
        self._commit_message_edit: QLineEdit | None = None
        self._status_output: QTextEdit | None = None
        self._commit_button: QPushButton | None = None
        self._push_button: QPushButton | None = None
        self._selected_commit_sha: str | None = None
        self._diff_windows: list[GitDiffViewerWindow] = []
        self._main_splitter: QSplitter | None = None
        self._top_splitter: QSplitter | None = None

        self._build_ui()
        self._restore_window_state()
        self._load_files()

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        header = QLabel(
            f"<b>Repository:</b> {self._repository.name}  |  "
            f"<b>Branch:</b> {self._branch.name}"
        )
        main_layout.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._main_splitter = splitter

        top_widget = QWidget(self)
        top_layout = QVBoxLayout(top_widget)
        top_layout.addWidget(QLabel("Commit rows and files"))

        top_splitter = QSplitter(Qt.Orientation.Horizontal, top_widget)
        self._top_splitter = top_splitter

        left_widget = QWidget(top_widget)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Commits"))
        filter_label = QLabel("Showing local changes and unpushed commits only")
        filter_label.setStyleSheet("color: #616161;")
        left_layout.addWidget(filter_label)

        self._commit_table = QTableWidget(left_widget)
        self._commit_table.setColumnCount(4)
        self._commit_table.setHorizontalHeaderLabels(["SHA", "Date", "Author", "Subject"])
        self._commit_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._commit_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._commit_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._commit_table.verticalHeader().setVisible(False)
        self._commit_table.horizontalHeader().setStretchLastSection(False)
        self._commit_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._commit_table.itemSelectionChanged.connect(self._handle_commit_selection_changed)
        left_layout.addWidget(self._commit_table)

        right_widget = QWidget(top_widget)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Files in selected row"))

        self._file_table = QTableWidget(right_widget)
        self._file_table.setColumnCount(4)
        self._file_table.setHorizontalHeaderLabels(["Status", "Added", "Deleted", "Path"])
        self._file_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._file_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._file_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._file_table.horizontalHeader().setStretchLastSection(False)
        self._file_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._file_table.verticalHeader().setVisible(False)
        self._file_table.itemClicked.connect(self._handle_file_clicked)
        right_layout.addWidget(self._file_table)

        top_splitter.addWidget(left_widget)
        top_splitter.addWidget(right_widget)
        top_splitter.setSizes([500, 460])

        top_layout.addWidget(top_splitter)

        bottom_widget = QWidget(self)
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.addWidget(QLabel("Commit local changes and push commits"))

        commit_form = QHBoxLayout()
        self._commit_message_edit = QLineEdit(bottom_widget)
        self._commit_message_edit.setPlaceholderText("Commit message")
        self._commit_message_edit.setText(
            f"Update {self._branch.name} - {date.today().isoformat()}"
        )
        commit_form.addWidget(self._commit_message_edit, 1)

        self._commit_button = QPushButton("Commit Local Changes", bottom_widget)
        self._commit_button.clicked.connect(self._handle_commit_clicked)
        commit_form.addWidget(self._commit_button)

        self._push_button = QPushButton("Push All Commits", bottom_widget)
        self._push_button.clicked.connect(self._handle_push_clicked)
        commit_form.addWidget(self._push_button)

        bottom_layout.addLayout(commit_form)

        self._status_output = QTextEdit(bottom_widget)
        self._status_output.setReadOnly(True)
        self._status_output.setPlaceholderText("Operation output will appear here.")
        bottom_layout.addWidget(self._status_output)

        splitter.addWidget(top_widget)
        splitter.addWidget(bottom_widget)
        splitter.setSizes([380, 280])

        main_layout.addWidget(splitter, 1)

    def _restore_window_state(self) -> None:
        saved_size = self._settings.load_commit_dialog_size()
        if saved_size is not None:
            self.resize(saved_size[0], saved_size[1])

        if self._main_splitter is not None:
            main_sizes = self._settings.load_commit_dialog_main_splitter_sizes()
            if main_sizes:
                self._main_splitter.setSizes(main_sizes)

        if self._top_splitter is not None:
            top_sizes = self._settings.load_commit_dialog_top_splitter_sizes()
            if top_sizes:
                self._top_splitter.setSizes(top_sizes)

        if self._commit_table is not None:
            commit_col_sizes = self._settings.load_commit_dialog_commit_column_sizes()
            if commit_col_sizes:
                self._apply_column_sizes(self._commit_table, commit_col_sizes)

        if self._file_table is not None:
            file_col_sizes = self._settings.load_commit_dialog_file_column_sizes()
            if file_col_sizes:
                self._apply_column_sizes(self._file_table, file_col_sizes)

    def _apply_column_sizes(self, table: QTableWidget, sizes: list[int]) -> None:
        for col, size in enumerate(sizes):
            if col >= table.columnCount():
                break
            if size > 0:
                table.setColumnWidth(col, size)

    def _save_window_state(self) -> None:
        self._settings.save_commit_dialog_size(self.width(), self.height())

        if self._main_splitter is not None:
            self._settings.save_commit_dialog_main_splitter_sizes(self._main_splitter.sizes())

        if self._top_splitter is not None:
            self._settings.save_commit_dialog_top_splitter_sizes(self._top_splitter.sizes())

        if self._commit_table is not None:
            commit_col_sizes = [self._commit_table.columnWidth(col) for col in range(self._commit_table.columnCount())]
            self._settings.save_commit_dialog_commit_column_sizes(commit_col_sizes)

        if self._file_table is not None:
            file_col_sizes = [self._file_table.columnWidth(col) for col in range(self._file_table.columnCount())]
            self._settings.save_commit_dialog_file_column_sizes(file_col_sizes)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_window_state()
        super().closeEvent(event)

    def _append_status(self, text: str) -> None:
        if self._status_output is None:
            return
        self._status_output.append(text)
        self._status_output.verticalScrollBar().setValue(
            self._status_output.verticalScrollBar().maximum()
        )

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        if self._commit_button is not None:
            self._commit_button.setEnabled(enabled)
        if self._push_button is not None:
            self._push_button.setEnabled(enabled)

    def _load_files(self) -> None:
        if self._commit_table is None:
            return

        rows = self._commit_rows(limit=40)
        self._commit_table.clearContents()
        self._commit_table.setRowCount(len(rows))

        for row_idx, row in enumerate(rows):
            for col_idx, value in enumerate(row):
                self._commit_table.setItem(row_idx, col_idx, QTableWidgetItem(value))

        if rows:
            self._commit_table.selectRow(0)
            self._selected_commit_sha = rows[0][0]
            self._load_files_for_selected_row()
        else:
            self._selected_commit_sha = None
            self._show_file_rows([])

    def _show_file_rows(self, rows: list[tuple[str, str, str, str]]) -> None:
        if self._file_table is None:
            return

        self._file_table.clearContents()
        self._file_table.setRowCount(len(rows))

        for row_idx, row in enumerate(rows):
            for col_idx, value in enumerate(row):
                item = QTableWidgetItem(value)
                if col_idx == 0:
                    item.setForeground(QBrush(QColor(self._status_color(value))))
                self._file_table.setItem(row_idx, col_idx, item)

    def _status_color(self, status_code: str) -> str:
        normalized = status_code.strip()
        if not normalized:
            return "#616161"

        if normalized == "??":
            return "#455a64"
        if normalized.startswith("R"):
            return "#6a1b9a"
        if normalized.startswith("D") or normalized.endswith("D"):
            return "#c62828"
        if normalized.startswith("A") or normalized.endswith("A"):
            return "#2e7d32"
        if normalized.startswith("M") or normalized.endswith("M"):
            return "#1565c0"

        return "#616161"

    def _handle_commit_selection_changed(self) -> None:
        if self._commit_table is None:
            return

        selected_rows = self._commit_table.selectionModel().selectedRows()
        if not selected_rows:
            self._selected_commit_sha = None
            self._show_file_rows([])
            return

        row_index = selected_rows[0].row()
        sha_item = self._commit_table.item(row_index, 0)
        if sha_item is None:
            self._selected_commit_sha = None
            self._show_file_rows([])
            return

        self._selected_commit_sha = sha_item.text().strip()
        self._load_files_for_selected_row()

    def _load_files_for_selected_row(self) -> None:
        selected_sha = self._selected_commit_sha
        if not selected_sha:
            self._show_file_rows([])
            return

        if selected_sha == "LOCAL":
            self._show_file_rows(self._local_file_rows())
            return

        self._show_file_rows(self._commit_file_rows(selected_sha))

    def _handle_file_clicked(self, item: QTableWidgetItem) -> None:
        del item
        if self._file_table is None:
            return

        selected_sha = self._selected_commit_sha
        if not selected_sha:
            return

        selected_rows = self._file_table.selectionModel().selectedRows()
        if not selected_rows:
            return

        row = selected_rows[0].row()
        path_item = self._file_table.item(row, 3)
        if path_item is None:
            return

        file_path = path_item.text().strip()
        if not file_path or file_path == "-":
            return

        if selected_sha == "LOCAL":
            status_item = self._file_table.item(row, 0)
            local_status = status_item.text().strip() if status_item is not None else ""
            local_diff_viewer = LocalGitDiffViewerWindow(
                repository_path=self._repository.path,
                file_path=file_path,
                local_status=local_status,
                parent=self,
            )
            self._diff_windows.append(local_diff_viewer)
            local_diff_viewer.destroyed.connect(lambda _: self._prune_diff_windows())
            local_diff_viewer.show()
            return

        selected_sha = self._selected_commit_sha
        if not selected_sha:
            QMessageBox.information(
                self,
                "Diff Viewer",
                "Select a committed row to open file diffs.",
            )
            return

        diff_viewer = GitDiffViewerWindow(
            repository_path=self._repository.path,
            commit_sha=selected_sha,
            file_path=file_path,
            parent=self,
        )
        self._diff_windows.append(diff_viewer)
        diff_viewer.destroyed.connect(lambda _: self._prune_diff_windows())
        diff_viewer.show()

    def _prune_diff_windows(self) -> None:
        self._diff_windows = [window for window in self._diff_windows if window is not None]

    def _commit_rows(self, limit: int) -> list[tuple[str, str, str, str]]:
        rows: list[tuple[str, str, str, str]] = []

        # Only show the LOCAL row when there are actual local file changes.
        if self._local_file_rows():
            rows.append(("LOCAL", "-", "-", "Local working tree changes"))

        if self._branch.upstream:
            revspec = f"{self._branch.upstream}..{self._branch.name}"
        else:
            # Without an upstream, all local commits are effectively unpushed.
            revspec = self._branch.name

        output = self._run_git(
            "log",
            revspec,
            "-n",
            str(limit),
            "--date=short",
            "--pretty=format:%h%x1f%ad%x1f%an%x1f%s",
        )
        if output is None:
            return rows

        for line in output.splitlines():
            if not line.strip():
                continue

            parts = line.split("\x1f", maxsplit=3)
            if len(parts) != 4:
                continue

            sha, commit_date, author, subject = [part.strip() for part in parts]
            rows.append((sha or "-", commit_date or "-", author or "-", subject or "-"))

        return rows

    def _local_file_rows(self) -> list[tuple[str, str, str, str]]:
        output = self._run_git("status", "--porcelain")
        if output is None:
            return []

        rows: list[tuple[str, str, str, str]] = []
        for raw_line in output.splitlines():
            parsed = self._parse_porcelain_line(raw_line)
            if parsed is None:
                continue
            status, path = parsed
            rows.append((status, "-", "-", path))
        return rows

    def _parse_porcelain_line(self, raw_line: str) -> tuple[str, str] | None:
        if len(raw_line) < 2:
            return None

        # Use split-based parsing instead of fixed offsets so paths are not
        # truncated when status output spacing varies.
        parts = raw_line.split(maxsplit=1)
        if len(parts) < 2:
            return None

        status_code = parts[0].strip() or "M"
        path = parts[1].strip()
        if not path:
            return None

        display_path = path.split(" -> ")[-1].strip() or path
        return status_code, display_path

    def _commit_file_rows(self, commit_sha: str) -> list[tuple[str, str, str, str]]:
        status_output = self._run_git(
            "show",
            "--pretty=format:",
            "--name-status",
            "-M",
            commit_sha,
        )
        numstat_output = self._run_git(
            "show",
            "--pretty=format:",
            "--numstat",
            "-M",
            commit_sha,
        )
        status_rows = self._parse_name_status_rows(status_output)
        numstat_rows = self._parse_numstat_rows(numstat_output)

        max_rows = max(len(status_rows), len(numstat_rows))
        rows: list[tuple[str, str, str, str]] = []
        for idx in range(max_rows):
            status, status_path = status_rows[idx] if idx < len(status_rows) else ("-", "-")
            added, deleted, numstat_path = (
                numstat_rows[idx] if idx < len(numstat_rows) else ("-", "-", "-")
            )
            path = status_path if status_path != "-" else numstat_path
            rows.append((status, added, deleted, path))

        return rows

    def _parse_name_status_rows(self, output: str | None) -> list[tuple[str, str]]:
        if output is None:
            return []

        rows: list[tuple[str, str]] = []
        for raw_line in output.splitlines():
            if not raw_line.strip():
                continue
            parts = raw_line.split("\t")
            if len(parts) < 2:
                continue

            status = parts[0].strip() or "-"
            if len(parts) == 2:
                path = parts[1].strip() or "-"
            else:
                path = parts[-1].strip() or "-"
            rows.append((status, path))

        return rows

    def _parse_numstat_rows(self, output: str | None) -> list[tuple[str, str, str]]:
        if output is None:
            return []

        rows: list[tuple[str, str, str]] = []
        for raw_line in output.splitlines():
            if not raw_line.strip():
                continue

            parts = raw_line.split("\t")
            if len(parts) < 3:
                continue

            added = parts[0].strip() or "0"
            deleted = parts[1].strip() or "0"
            path = parts[2].strip() or "-"
            if added == "-":
                added = "binary"
            if deleted == "-":
                deleted = "binary"

            rows.append((added, deleted, path))

        return rows

    def _run_git(self, *args: str) -> str | None:
        if shutil.which("git") is None:
            return None

        try:
            completed = subprocess.run(
                ["git", "-C", str(self._repository.path), *args],
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        return completed.stdout.strip()

    def _handle_commit_clicked(self) -> None:
        if self._commit_message_edit is None:
            return

        commit_message = self._commit_message_edit.text().strip()
        if not commit_message:
            QMessageBox.warning(self, "Commit", "Please enter a commit message.")
            return

        self._append_status("Committing local changes...")
        self._set_action_buttons_enabled(False)

        def worker() -> None:
            result = commit_local_changes(
                self._repository,
                self._branch,
                commit_message,
            )
            self._signals.commit_done.emit(result)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _on_commit_done(self, result) -> None:
        if result.success:
            if result.created_commit:
                self._append_status(f"Committed successfully: {result.output or 'OK'}")
            else:
                self._append_status("No local changes to commit.")
        else:
            self._append_status(f"Commit failed: {result.error or 'Unknown error'}")

        self._set_action_buttons_enabled(True)
        self._load_files()

    def _handle_push_clicked(self) -> None:
        self._append_status("Pushing commits to remote...")
        self._set_action_buttons_enabled(False)

        def worker() -> None:
            result = push_branch_commits(self._repository, self._branch)
            self._signals.push_done.emit(result)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _on_push_done(self, result) -> None:
        if result.success:
            self._append_status(f"Push completed: {result.output or 'OK'}")
        else:
            self._append_status(f"Push failed: {result.error or 'Unknown error'}")

        self._set_action_buttons_enabled(True)
        self._load_files()
