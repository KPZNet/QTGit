from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from pathlib import Path
import shutil
import subprocess
from urllib.parse import quote

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTextBrowser,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services.repo_scanner import GitBranch, GitRepository, RepoScanResult
from app.widgets.git_diff_viewer import LocalGitDiffViewerWindow


COMMIT_LIST_EMPTY_TEXT = "Select a repository or branch to view commit history."
COMMIT_FILES_EMPTY_TEXT = "Select a commit to view changed files."


def _apply_no_hover_highlight(table: QTableWidget) -> None:
    table.setStyleSheet(
        "QTableWidget {"
        "selection-background-color: palette(highlight);"
        "}"
        "QTableWidget::item {"
        "padding: 0px;"
        "}"
        "QTableWidget::item:hover {"
        "background-color: transparent;"
        "color: palette(text);"
        "}"
        "QTableWidget::item:selected {"
        "background-color: palette(highlight);"
        "color: palette(highlighted-text);"
        "}"
        "QTableWidget::item:selected:hover {"
        "background-color: palette(highlight);"
        "color: palette(highlighted-text);"
        "}"
    )


class InfoPanel(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._title_label = QLabel(title, self)
        self._body_text = QTextBrowser(self)
        self._body_text.setReadOnly(True)
        self._body_text.setOpenExternalLinks(True)
        self._body_text.setOpenLinks(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self._title_label)
        layout.addWidget(self._body_text)

    def set_body(self, text: str) -> None:
        self._body_text.setPlainText(text)

    def set_body_html(self, html_text: str) -> None:
        self._body_text.setHtml(html_text)


class CommitListPanel(QFrame):
    commit_selected = Signal(str)
    commit_requested = Signal()
    push_requested = Signal()

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._title_label = QLabel(title, self)
        self._context_label = QLabel(self)
        self._table = QTableWidget(self)
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["SHA", "Date", "Author", "Subject"])
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(False)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(False)
        for col in range(self._table.columnCount()):
            self._table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.Interactive
            )
        _apply_no_hover_highlight(self._table)
        self._table.itemClicked.connect(self._handle_item_clicked)

        header_row = QHBoxLayout()
        header_row.addWidget(self._title_label, 1)
        self._commit_button = QPushButton("Commit", self)
        self._commit_button.setToolTip("Create a local commit for the active branch")
        self._commit_button.setEnabled(False)
        self._commit_button.clicked.connect(self.commit_requested.emit)
        header_row.addWidget(self._commit_button)

        self._push_button = QPushButton("Push", self)
        self._push_button.setToolTip("Push local commits for the active branch")
        self._push_button.setEnabled(False)
        self._push_button.clicked.connect(self.push_requested.emit)
        header_row.addWidget(self._push_button)

        layout = QVBoxLayout(self)
        layout.addLayout(header_row)
        layout.addWidget(self._context_label)
        layout.addWidget(self._table)

        self.show_commits([], COMMIT_LIST_EMPTY_TEXT)

        self._last_emitted_commit_sha: str | None = None

    def column_sizes(self) -> list[int]:
        return [self._table.columnWidth(col) for col in range(self._table.columnCount())]

    def set_column_sizes(self, sizes: list[int]) -> None:
        for col, size in enumerate(sizes):
            if col >= self._table.columnCount():
                break
            if size > 0:
                self._table.setColumnWidth(col, size)

    def show_commits(
        self,
        commits: list[tuple[str, str, str, str]],
        context_text: str,
        highlight_shas: set[str] | None = None,
    ) -> None:
        highlight_shas = highlight_shas or set()
        self._context_label.setText(context_text)
        self._table.clearContents()
        self._table.clearSelection()
        self._table.setCurrentCell(-1, -1)
        self._table.setRowCount(len(commits))

        for row, commit in enumerate(commits):
            sha = commit[0].strip()
            should_highlight = sha in highlight_shas
            for col, value in enumerate(commit):
                # For SHA column (col 0), add up arrow for unpushed commits
                if col == 0 and should_highlight:
                    display_value = f"↑ {value}"
                else:
                    display_value = value
                item = QTableWidgetItem(display_value)
                if should_highlight:
                    item.setForeground(QColor("#1565c0"))
                self._table.setItem(row, col, item)

        self._last_emitted_commit_sha = None
        self._table.clearSelection()

    def _handle_item_clicked(self, item: QTableWidgetItem) -> None:
        """Handle click on a table item to select the entire row."""
        if item is None:
            return
        row = item.row()
        self._table.clearSelection()
        self._table.selectRow(row)
        self._emit_commit_selected(row)

    def _emit_commit_selected(self, row: int) -> None:
        sha_item = self._table.item(row, 0)
        if sha_item is None:
            return

        commit_sha = sha_item.text().strip()
        # Remove up arrow prefix if present (for unpushed commits)
        if commit_sha.startswith("↑ "):
            commit_sha = commit_sha[2:].strip()
        
        if not commit_sha or commit_sha == self._last_emitted_commit_sha:
            return

        self._last_emitted_commit_sha = commit_sha
        self.commit_selected.emit(commit_sha)

    def set_commit_enabled(self, enabled: bool, tooltip: str) -> None:
        self._commit_button.setEnabled(enabled)
        self._commit_button.setToolTip(tooltip)

    def set_push_enabled(self, enabled: bool, tooltip: str) -> None:
        self._push_button.setEnabled(enabled)
        self._push_button.setToolTip(tooltip)


class CommitFilesPanel(QFrame):
    file_double_clicked = Signal(str)  # Emits file path when double-clicked

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._title_label = QLabel(title, self)
        self._context_label = QLabel(self)
        self._table = QTableWidget(self)
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["Status", "Added", "Deleted", "Path"])
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(False)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(False)
        for col in range(self._table.columnCount()):
            self._table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.Interactive
            )
        _apply_no_hover_highlight(self._table)
        self._table.itemClicked.connect(self._handle_item_clicked)
        self._table.doubleClicked.connect(self._handle_file_double_clicked)

        layout = QVBoxLayout(self)
        layout.addWidget(self._title_label)
        layout.addWidget(self._context_label)
        layout.addWidget(self._table)

        self.show_files([], COMMIT_FILES_EMPTY_TEXT)

    def column_sizes(self) -> list[int]:
        return [self._table.columnWidth(col) for col in range(self._table.columnCount())]

    def set_column_sizes(self, sizes: list[int]) -> None:
        for col, size in enumerate(sizes):
            if col >= self._table.columnCount():
                break
            if size > 0:
                self._table.setColumnWidth(col, size)

    def selected_file_status(self) -> str:
        selected = self._table.selectionModel().selectedRows()
        if not selected:
            return ""
        row = selected[0].row()
        item = self._table.item(row, 0)
        return item.text().strip() if item is not None else ""

    def show_files(
        self,
        files: list[tuple[str, str, str, str]],
        context_text: str,
    ) -> None:
        self._context_label.setText(context_text)
        self._table.clearContents()
        self._table.clearSelection()
        self._table.setCurrentCell(-1, -1)
        self._table.setRowCount(len(files))

        for row, file_row in enumerate(files):
            for col, value in enumerate(file_row):
                self._table.setItem(row, col, QTableWidgetItem(value))

        self._table.clearSelection()

    def _handle_item_clicked(self, item: QTableWidgetItem) -> None:
        """Handle click on a table item to select the entire row."""
        if item is None:
            return
        row = item.row()
        self._table.clearSelection()
        self._table.selectRow(row)

    def _handle_file_double_clicked(self, index) -> None:
        """Handle double-click on a file in the table."""
        if not index.isValid():
            return

        # Get the file path from the last column (column 3)
        path_item = self._table.item(index.row(), 3)
        if path_item is None:
            return

        file_path = path_item.text()
        self.file_double_clicked.emit(file_path)


class CommitHistogramPanel(QFrame):
    """Panel displaying a histogram of commits over the last 30 days."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._title_label = QLabel(title, self)
        self._histogram_widget = CommitHistogramWidget(self)

        layout = QVBoxLayout(self)
        layout.addWidget(self._title_label)
        layout.addWidget(self._histogram_widget)

        self.show_empty("No commit data")

    def show_histogram(self, commits_by_date: dict[str, int]) -> None:
        """Update histogram with commit data. Dict maps YYYY-MM-DD to commit count."""
        self._histogram_widget.set_data(commits_by_date)

    def show_empty(self, message: str) -> None:
        """Show empty state message."""
        self._histogram_widget.set_data({})


class CommitHistogramWidget(QWidget):
    """Custom widget to render a commit histogram."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._commits_by_date: dict[str, int] = {}
        self._empty_message = "No commit data"
        self.setMinimumHeight(100)

    def set_data(self, commits_by_date: dict[str, int]) -> None:
        """Set commit data and trigger repaint. Dict maps YYYY-MM-DD to count."""
        self._commits_by_date = commits_by_date
        self.update()

    def paintEvent(self, event) -> None:
        """Render the histogram."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        width = self.width()
        height = self.height()
        default_font = self.font()
        painter.setFont(default_font)
        font_metrics = painter.fontMetrics()

        if not self._commits_by_date:
            # Draw empty state
            painter.drawText(
                10, height // 2,
                width - 20, height // 2,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                "No commits in selected range"
            )
            painter.end()
            return

        # Calculate 30-day range (today back 29 days = 30 days total)
        today = datetime.now().date()
        dates_range = [today - timedelta(days=i) for i in range(29, -1, -1)]

        # Get max commits in a single day for scaling
        max_commits = max(self._commits_by_date.values()) if self._commits_by_date else 1
        if max_commits == 0:
            max_commits = 1

        # Margins and dimensions
        margin_left = max(34, font_metrics.horizontalAdvance(str(int(max_commits))) + 10)
        margin_right = 10
        margin_top = 10
        margin_bottom = max(30, font_metrics.height() + 14)

        chart_width = width - margin_left - margin_right
        chart_height = height - margin_top - margin_bottom

        # Bar dimensions
        num_days = len(dates_range)
        bar_width = max(1, chart_width // num_days)
        spacing = 1

        # Draw bars for each day
        for idx, date in enumerate(dates_range):
            date_str = date.strftime("%Y-%m-%d")
            count = self._commits_by_date.get(date_str, 0)

            if count > 0:
                # Calculate bar height
                bar_height = (count / max_commits) * chart_height

                # Calculate position
                x = margin_left + (idx * (bar_width + spacing))
                y = margin_top + (chart_height - bar_height)

                # Draw bar
                painter.fillRect(
                    int(x), int(y),
                    int(bar_width - spacing), int(bar_height),
                    QColor(70, 130, 180)  # Steel blue
                )

                # Draw bar border
                painter.setPen(QPen(QColor(40, 80, 130), 0.5))
                painter.drawRect(
                    int(x), int(y),
                    int(bar_width - spacing), int(bar_height)
                )

        # Draw axes
        painter.setPen(QPen(QColor(128, 128, 128), 1))
        # X-axis
        painter.drawLine(
            margin_left, margin_top + chart_height,
            margin_left + chart_width, margin_top + chart_height
        )
        # Y-axis
        painter.drawLine(
            margin_left, margin_top,
            margin_left, margin_top + chart_height
        )

        # Draw Y-axis labels (0, max_commits)
        painter.setPen(QPen(QColor(32, 32, 32), 1))
        painter.setFont(default_font)
        label_height = font_metrics.height()
        label_width = max(24, font_metrics.horizontalAdvance(str(int(max_commits))) + 4)

        # Bottom label (0)
        painter.drawText(
            2, margin_top + chart_height - (label_height // 2),
            label_width, label_height,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "0"
        )
        # Top label (max_commits)
        painter.drawText(
            2, margin_top - (label_height // 2),
            label_width, label_height,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(int(max_commits))
        )

        # Draw month labels at bottom (every ~7 days or so)
        painter.setPen(QPen(QColor(32, 32, 32), 1))
        painter.setFont(default_font)
        for idx in range(0, num_days, 7):
            if idx < len(dates_range):
                date = dates_range[idx]
                label = date.strftime("%m-%d")
                x = margin_left + (idx * (bar_width + spacing))
                painter.drawText(
                    int(x), height - font_metrics.height() - 4,
                    max(int(bar_width * 2), font_metrics.horizontalAdvance(label) + 4),
                    font_metrics.height() + 4,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    label
                )

        painter.end()


class RightSplitPane(QSplitter):
    file_double_clicked = Signal(str, str)  # (commit_sha, file_path)
    commit_requested = Signal(object, object)  # (repository, active_branch)
    push_requested = Signal(object, object)  # (repository, active_branch)

    def __init__(self) -> None:
        super().__init__(Qt.Orientation.Vertical)
        self._summary_panel = InfoPanel("Details")
        self._commits_panel = CommitListPanel("Recent Commits (Last 30)")
        self._commit_files_panel = CommitFilesPanel("Files In Selected Commit")
        self._commit_histogram_panel = CommitHistogramPanel("Commit Histogram (Last 30 Days)")
        self._content_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._latest_result: RepoScanResult | None = None
        self._selected_repository: GitRepository | None = None
        self._selected_branch: GitBranch | None = None
        self._selected_repository_path: Path | None = None
        self._selected_context_label = ""
        self._selected_commit_sha: str | None = None

        self._commits_panel.commit_selected.connect(self._handle_commit_selected)
        self._commits_panel.commit_requested.connect(self._handle_commit_requested)
        self._commits_panel.push_requested.connect(self._handle_push_requested)
        self._commit_files_panel.file_double_clicked.connect(self._handle_file_double_clicked)
        self._diff_windows: list = []

        self.addWidget(self._summary_panel)
        self._content_splitter.addWidget(self._commits_panel)
        self._content_splitter.addWidget(self._commit_files_panel)
        self._content_splitter.setCollapsible(0, False)
        self._content_splitter.setCollapsible(1, False)
        self._content_splitter.setSizes([540, 540])
        self.addWidget(self._content_splitter)
        self.addWidget(self._commit_histogram_panel)
        self.setCollapsible(0, False)
        self.setCollapsible(1, False)
        self.setCollapsible(2, False)
        self.setSizes([360, 450, 150])

        self._summary_panel.set_body("Choose a directory from the toolbar to begin.")
        self._commits_panel.show_commits([], COMMIT_LIST_EMPTY_TEXT)
        self._commits_panel.set_commit_enabled(False, "Select a repository to commit local changes")
        self._commits_panel.set_push_enabled(False, "Select a repository to push local commits")
        self._commit_files_panel.show_files([], COMMIT_FILES_EMPTY_TEXT)
        self._commit_histogram_panel.show_empty("No commit data")

    def content_splitter_sizes(self) -> list[int]:
        return self._content_splitter.sizes()

    def set_content_splitter_sizes(self, sizes: list[int]) -> None:
        if sizes:
            self._content_splitter.setSizes(sizes)

    def commit_column_sizes(self) -> list[int]:
        return self._commits_panel.column_sizes()

    def set_commit_column_sizes(self, sizes: list[int]) -> None:
        self._commits_panel.set_column_sizes(sizes)

    def file_column_sizes(self) -> list[int]:
        return self._commit_files_panel.column_sizes()

    def set_file_column_sizes(self, sizes: list[int]) -> None:
        self._commit_files_panel.set_column_sizes(sizes)

    def update_context(self, directory: Path, result: RepoScanResult) -> None:
        self._latest_result = result
        self._selected_repository = None
        self._selected_branch = None
        self._selected_commit_sha = None
        summary_text = (
            f"Root directory: {directory}\n"
            f"Repositories found: {len(result.repositories)}\n"
            f"Directories scanned: {result.scanned_directories}"
        )
        details_text = ""

        if result.error_message:
            details_text = result.error_message
            self._show_top_only(f"{summary_text}\n\n{details_text}")
            self._commits_panel.show_commits([], "Commit history unavailable due to scan error.")
            self._commit_files_panel.show_files([], COMMIT_FILES_EMPTY_TEXT)
            self._selected_repository_path = None
            self._selected_context_label = ""
            self._update_commit_button_state()
            return

        if not result.repositories:
            details_text = (
                "No Git repositories were found. Use Browse to pick a different directory."
            )
            self._show_top_only(f"{summary_text}\n\n{details_text}")
            self._commits_panel.show_commits([], "No repositories found.")
            self._commit_files_panel.show_files([], COMMIT_FILES_EMPTY_TEXT)
            self._selected_repository_path = None
            self._selected_context_label = ""
            return

        details_text = self._repo_overview_text(result)
        self._show_top_only(f"{summary_text}\n\n{details_text}")
        self._commits_panel.show_commits([], COMMIT_LIST_EMPTY_TEXT)
        self._commit_files_panel.show_files([], COMMIT_FILES_EMPTY_TEXT)
        self._commit_histogram_panel.show_empty("No commit data")
        self._selected_repository_path = None
        self._selected_context_label = ""
        self._update_commit_button_state()

    def show_selection(
        self,
        repository: GitRepository | None,
        branch: GitBranch | None,
    ) -> None:
        self._selected_repository = repository
        self._selected_branch = branch
        self._update_commit_button_state()
        self._selected_commit_sha = None
        if repository is None:
            if self._latest_result is not None:
                self._show_repo_overview(self._latest_result)
            self._commits_panel.show_commits([], COMMIT_LIST_EMPTY_TEXT)
            self._commit_files_panel.show_files([], COMMIT_FILES_EMPTY_TEXT)
            self._commit_histogram_panel.show_empty("No commit data")
            self._selected_repository_path = None
            self._selected_context_label = ""
            return

        if branch is None:
            self._show_top_only_html(self._build_repository_details(repository))
            commits = self._recent_commit_rows_all_branches(repository.path, 30)
            context_label = f"{repository.name} - all branches (latest 30)"
            self._selected_repository_path = repository.path
            self._selected_context_label = context_label
            self._commit_files_panel.show_files([], COMMIT_FILES_EMPTY_TEXT)
            self._commits_panel.show_commits(commits, context_label)
            commits_by_date = self._commit_frequency_data_all_branches(repository.path)
            self._commit_histogram_panel.show_histogram(commits_by_date)
            return

        self._show_top_only_html(self._build_branch_details(repository, branch))
        commits, unpushed_shas = self._local_unpushed_commit_rows(repository.path, branch)
        context_label = f"{repository.name} - {branch.name} · local changes & unpushed commits"
        self._selected_repository_path = repository.path
        self._selected_context_label = context_label
        self._commit_files_panel.show_files([], COMMIT_FILES_EMPTY_TEXT)
        self._commits_panel.show_commits(commits, context_label, highlight_shas=unpushed_shas)
        commits_by_date = self._commit_frequency_data(repository.path, branch.name)
        self._commit_histogram_panel.show_histogram(commits_by_date)

    def _update_commit_button_state(self) -> None:
        repository = self._selected_repository
        branch = self._selected_branch
        if repository is None:
            self._commits_panel.set_commit_enabled(
                False,
                "Select a repository to commit local changes",
            )
            self._commits_panel.set_push_enabled(
                False,
                "Select a repository to push local commits",
            )
            return

        if branch is not None and not branch.is_current:
            self._commits_panel.set_commit_enabled(
                False,
                "Commit is only available for the active branch",
            )
            self._commits_panel.set_push_enabled(
                False,
                "Push is only available for the active branch",
            )
            return

        active_branch = next((b for b in repository.local_branches if b.is_current), None)
        if active_branch is None:
            self._commits_panel.set_commit_enabled(
                False,
                "No active branch found for this repository",
            )
            self._commits_panel.set_push_enabled(
                False,
                "No active branch found for this repository",
            )
            return

        self._commits_panel.set_commit_enabled(
            True,
            f"Commit all local changes on '{active_branch.name}'",
        )
        self._commits_panel.set_push_enabled(
            True,
            f"Push local commits on '{active_branch.name}'",
        )

    def _handle_commit_requested(self) -> None:
        repository = self._selected_repository
        if repository is None:
            return

        if self._selected_branch is not None and not self._selected_branch.is_current:
            return

        active_branch = next((b for b in repository.local_branches if b.is_current), None)
        if active_branch is None:
            return

        self.commit_requested.emit(repository, active_branch)

    def _handle_push_requested(self) -> None:
        repository = self._selected_repository
        if repository is None:
            return

        if self._selected_branch is not None and not self._selected_branch.is_current:
            return

        active_branch = next((b for b in repository.local_branches if b.is_current), None)
        if active_branch is None:
            return

        self.push_requested.emit(repository, active_branch)

    def _handle_commit_selected(self, commit_sha: str) -> None:
        self._selected_commit_sha = commit_sha
        if self._selected_repository_path is None:
            self._commit_files_panel.show_files([], COMMIT_FILES_EMPTY_TEXT)
            self._commit_histogram_panel.show_empty("No commit data")
            return

        if commit_sha == "LOCAL":
            local_files = self._local_file_rows_for_path(self._selected_repository_path)
            context_text = f"{self._selected_context_label} :: Local changes"
            if not local_files:
                self._commit_files_panel.show_files([], f"{context_text} (no local changes)")
            else:
                self._commit_files_panel.show_files(local_files, context_text)
            return

        commit_files = self._commit_file_rows(self._selected_repository_path, commit_sha)
        context_text = f"{self._selected_context_label} :: {commit_sha}"
        if not commit_files:
            self._commit_files_panel.show_files([], f"{context_text} (no changed files found)")
            return

        self._commit_files_panel.show_files(commit_files, context_text)

        # Update histogram panel with commit frequency data
        commits_by_date = self._commit_frequency_data(self._selected_repository_path, commit_sha)
        self._commit_histogram_panel.show_histogram(commits_by_date)

    def _handle_file_double_clicked(self, file_path: str) -> None:
        """Handle file double-click; open local diff or emit signal to parent."""
        if self._selected_commit_sha is None:
            return

        if self._selected_commit_sha == "LOCAL":
            if self._selected_repository_path is None:
                return
            status = self._commit_files_panel.selected_file_status()
            diff_viewer = LocalGitDiffViewerWindow(
                repository_path=self._selected_repository_path,
                file_path=file_path,
                local_status=status,
                parent=self,
            )
            self._diff_windows.append(diff_viewer)
            diff_viewer.destroyed.connect(lambda _: self._prune_diff_windows())
            diff_viewer.show()
            return

        self.file_double_clicked.emit(self._selected_commit_sha, file_path)

    def _prune_diff_windows(self) -> None:
        self._diff_windows = [w for w in self._diff_windows if w is not None]

    def _repo_overview_text(self, result: RepoScanResult) -> str:
        repo_lines = []
        for repository in result.repositories[:12]:
            active_branch = next(
                (branch.name for branch in repository.local_branches if branch.is_current),
                "no active branch",
            )
            repo_lines.append(
                f"{repository.path} [local: {len(repository.local_branches)}, active: {active_branch}]"
            )

        if len(result.repositories) > 12:
            repo_lines.append("...")

        return "Repositories:\n" + "\n".join(repo_lines)

    def _show_repo_overview(self, result: RepoScanResult) -> None:
        overview = self._repo_overview_text(result)
        self._show_top_only(overview)

    def _show_top_only(self, text: str) -> None:
        self._summary_panel.set_body(text)

    def _show_top_only_html(self, html_text: str) -> None:
        self._summary_panel.set_body_html(html_text)

    def _sync_label(self, branch: GitBranch) -> str:
        if not branch.is_current:
            return "N/A (only tracked for active branch)"

        if branch.sync_status == "in_sync":
            return "In sync with upstream"

        if branch.sync_status == "ahead":
            return "Ahead of upstream"

        if branch.sync_status == "behind":
            return "Behind upstream"

        if branch.sync_status == "diverged":
            return "Diverged from upstream"

        if branch.upstream:
            return "Unknown"

        return "No upstream"

    def _repository_web_url(self, repository_path: Path) -> str | None:
        origin_url = self._run_git_command(repository_path, "remote", "get-url", "origin")
        if origin_url:
            web_url = self._normalize_remote_url_to_web(origin_url)
            if web_url is not None:
                return web_url

        remotes = self._run_git_command(repository_path, "remote")
        if remotes is None:
            return None

        for remote_name in remotes.splitlines():
            remote_name = remote_name.strip()
            if not remote_name:
                continue

            remote_url = self._run_git_command(repository_path, "remote", "get-url", remote_name)
            if not remote_url:
                continue

            web_url = self._normalize_remote_url_to_web(remote_url)
            if web_url is not None:
                return web_url

        return None

    def _normalize_remote_url_to_web(self, remote_url: str) -> str | None:
        url = remote_url.strip()
        if not url:
            return None

        if url.startswith("https://") or url.startswith("http://"):
            normalized = url
        elif url.startswith("git@") and ":" in url:
            host_path = url.split("@", maxsplit=1)[1]
            host, path = host_path.split(":", maxsplit=1)
            normalized = f"https://{host}/{path}"
        elif url.startswith("ssh://git@"):
            host_path = url[len("ssh://git@"):]
            if "/" not in host_path:
                return None
            host, path = host_path.split("/", maxsplit=1)
            normalized = f"https://{host}/{path}"
        elif url.startswith("git://"):
            normalized = "https://" + url[len("git://"):]
        else:
            return None

        if normalized.endswith(".git"):
            normalized = normalized[:-4]

        return normalized.rstrip("/")

    def _branch_web_url(self, repo_web_url: str, branch_name: str) -> str:
        return f"{repo_web_url}/tree/{quote(branch_name, safe='')}"

    def _link(self, url: str, label: str) -> str:
        return f"<a href=\"{escape(url, quote=True)}\">{escape(label)}</a>"

    def _build_repository_details(self, repository: GitRepository) -> str:
        active_branch = next(
            (branch for branch in repository.local_branches if branch.is_current),
            None,
        )
        tracked_branch_count = sum(
            1 for branch in repository.local_branches if branch.upstream is not None
        )

        worktree_summary = self._worktree_summary(repository.path)
        remotes = self._remote_summary(repository.path)
        last_commit = self._latest_commit_summary_any_branch(repository.path)
        stash_count = self._stash_count(repository.path)
        repo_web_url = self._repository_web_url(repository.path)

        active_branch_label = active_branch.name if active_branch else "Unknown"
        if active_branch is not None and repo_web_url is not None:
            active_branch_label = self._link(
                self._branch_web_url(repo_web_url, active_branch.name),
                active_branch.name,
            )
        else:
            active_branch_label = escape(active_branch_label)

        repo_link_line = "Repository link: Unavailable"
        if repo_web_url is not None:
            repo_link_line = f"Repository link: {self._link(repo_web_url, repo_web_url)}"

        detail_lines = [
            "<b>Repository Overview</b>",
            f"Name: {escape(repository.name)}",
            f"Path: {escape(str(repository.path))}",
            repo_link_line,
            f"Total local branches: {len(repository.local_branches)}",
            f"Branches tracking upstream: {tracked_branch_count}",
            f"Active branch: {active_branch_label}",
            f"Stashes: {stash_count}",
            "",
            "<b>Working Tree</b>",
            f"Branch headline: {escape(str(worktree_summary['headline']))}",
            f"Clean: {'Yes' if worktree_summary['clean'] else 'No'}",
            f"Staged files: {worktree_summary['staged']}",
            f"Unstaged files: {worktree_summary['unstaged']}",
            f"Untracked files: {worktree_summary['untracked']}",
            f"Conflicts: {worktree_summary['conflicts']}",
            "",
            "<b>Remotes</b>",
        ]

        if remotes:
            detail_lines.extend(escape(remote) for remote in remotes)
        else:
            detail_lines.append("No remotes configured.")

        detail_lines.extend(
            [
                "",
                "<b>Latest Commit (any branch)</b>",
                f"SHA: {escape(last_commit['sha'])}",
                f"Author: {escape(last_commit['author'])}",
                f"Date: {escape(last_commit['date'])}",
                f"Subject: {escape(last_commit['subject'])}",
                "",
                "Tip: click any branch on the left for branch-specific diagnostics.",
            ]
        )

        # Local branches table
        detail_lines.append("")
        detail_lines.append("<b>Local Branches</b>")
        detail_lines.append(
            "<table style='border-collapse:collapse;width:100%'>"
            "<tr style='background:#e8e8e8'>"
            "<th style='text-align:left;padding:2px 6px'>Branch</th>"
            "<th style='text-align:left;padding:2px 6px'>Upstream</th>"
            "<th style='text-align:center;padding:2px 6px'>Ahead</th>"
            "<th style='text-align:center;padding:2px 6px'>Behind</th>"
            "<th style='text-align:left;padding:2px 6px'>Status</th>"
            "</tr>"
        )

        for b in repository.local_branches:
            ahead = b.ahead_count
            behind = b.behind_count

            if b.upstream and not b.is_current:
                computed_ahead, computed_behind = self._ahead_behind_counts(
                    repository.path, b.name, b.upstream
                )
                if computed_ahead is not None and computed_behind is not None:
                    ahead = computed_ahead
                    behind = computed_behind

            upstream_label = escape(b.upstream) if b.upstream else "<i>none</i>"

            if b.is_current:
                status_label = "&#10003; active"
                row_style = "background:#dff0d8"
            elif not b.upstream:
                status_label = "no upstream"
                row_style = ""
            else:
                sync = b.sync_status
                if not b.is_current:
                    if ahead == 0 and behind == 0:
                        sync = "in_sync"
                    elif ahead > 0 and behind > 0:
                        sync = "diverged"
                    elif behind > 0:
                        sync = "behind"
                    elif ahead > 0:
                        sync = "ahead"

                status_map = {
                    "in_sync": "in sync",
                    "ahead": "ahead",
                    "behind": "behind",
                    "diverged": "diverged",
                }
                status_label = escape(status_map.get(sync or "", "unknown"))
                row_style = ""

            if repo_web_url is not None:
                branch_link = self._link(self._branch_web_url(repo_web_url, b.name), b.name)
            else:
                branch_link = f"<b>{escape(b.name)}</b>" if b.is_current else escape(b.name)

            detail_lines.append(
                f"<tr style='{row_style}'>"
                f"<td style='padding:2px 6px'>{branch_link}</td>"
                f"<td style='padding:2px 6px'>{upstream_label}</td>"
                f"<td style='text-align:center;padding:2px 6px'>{ahead if b.upstream else '-'}</td>"
                f"<td style='text-align:center;padding:2px 6px'>{behind if b.upstream else '-'}</td>"
                f"<td style='padding:2px 6px'>{status_label}</td>"
                "</tr>"
            )

        detail_lines.append("</table>")

        return "<br>".join(detail_lines)

    def _build_branch_details(self, repository: GitRepository, branch: GitBranch) -> str:
        ahead_count = branch.ahead_count
        behind_count = branch.behind_count

        if not branch.is_current and branch.upstream:
            computed_ahead, computed_behind = self._ahead_behind_counts(
                repository.path,
                branch.name,
                branch.upstream,
            )
            if computed_ahead is not None and computed_behind is not None:
                ahead_count = computed_ahead
                behind_count = computed_behind

        branch_commit = self._latest_commit_summary(repository.path, branch.name)
        worktree_summary = self._worktree_summary(repository.path)
        repo_web_url = self._repository_web_url(repository.path)

        branch_label = escape(branch.name)
        repo_label = escape(repository.name)
        repo_link_line = "Repository link: Unavailable"
        if repo_web_url is not None:
            repo_label = self._link(repo_web_url, repository.name)
            branch_label = self._link(
                self._branch_web_url(repo_web_url, branch.name),
                branch.name,
            )
            repo_link_line = f"Repository link: {self._link(repo_web_url, repo_web_url)}"

        detail_lines = [
            "<b>Branch Overview</b>",
            f"Repository: {repo_label}",
            repo_link_line,
            f"Path: {escape(str(repository.path))}",
            f"Branch: {branch_label}",
            f"Active branch: {'Yes' if branch.is_current else 'No'}",
            f"Upstream: {escape(branch.upstream or 'None')}",
            f"Sync: {escape(self._sync_label(branch) if branch.is_current else 'Computed from upstream')}",
            f"Ahead of upstream: {ahead_count}",
            f"Behind upstream: {behind_count}",
            "",
            "<b>Branch Tip Commit</b>",
            f"SHA: {escape(branch_commit['sha'])}",
            f"Author: {escape(branch_commit['author'])}",
            f"Date: {escape(branch_commit['date'])}",
            f"Subject: {escape(branch_commit['subject'])}",
            "",
            "<b>Workspace State</b>",
            f"Current checkout headline: {escape(str(worktree_summary['headline']))}",
            f"Working tree clean: {'Yes' if worktree_summary['clean'] else 'No'}",
        ]

        return "<br>".join(detail_lines)

    def _worktree_summary(self, repository_path: Path) -> dict[str, object]:
        result = {
            "headline": "Unavailable",
            "clean": False,
            "staged": 0,
            "unstaged": 0,
            "untracked": 0,
            "conflicts": 0,
        }

        output = self._run_git_command(
            repository_path,
            "status",
            "--porcelain=v1",
            "--branch",
        )
        if output is None:
            return result

        lines = output.splitlines()
        if not lines:
            return result

        if lines[0].startswith("## "):
            result["headline"] = lines[0][3:]
        else:
            result["headline"] = lines[0]

        status_lines = lines[1:]
        result["clean"] = len(status_lines) == 0

        for entry in status_lines:
            if len(entry) < 2:
                continue

            code = entry[:2]
            staged_delta, unstaged_delta, untracked_delta, conflicts_delta = (
                self._status_deltas(code)
            )
            result["staged"] = int(result["staged"]) + staged_delta
            result["unstaged"] = int(result["unstaged"]) + unstaged_delta
            result["untracked"] = int(result["untracked"]) + untracked_delta
            result["conflicts"] = int(result["conflicts"]) + conflicts_delta

        return result

    def _status_deltas(self, code: str) -> tuple[int, int, int, int]:
        if code == "??":
            return 0, 0, 1, 0

        conflicts_delta = 0
        if code in {"UU", "AA", "DD"} or code[0] == "U" or code[1] == "U":
            conflicts_delta = 1

        staged_delta = 1 if code[0] != " " else 0
        unstaged_delta = 1 if code[1] != " " else 0
        return staged_delta, unstaged_delta, 0, conflicts_delta

    def _remote_summary(self, repository_path: Path) -> list[str]:
        output = self._run_git_command(repository_path, "remote", "-v")
        if output is None:
            return []

        remote_map: dict[str, dict[str, str]] = {}
        for line in output.splitlines():
            parts = line.split(maxsplit=2)
            if len(parts) != 3:
                continue

            name, url, kind = parts
            kind = kind.strip()
            if name not in remote_map:
                remote_map[name] = {}

            if kind == "(fetch)":
                remote_map[name]["fetch"] = url
            elif kind == "(push)":
                remote_map[name]["push"] = url

        lines = []
        for remote_name in sorted(remote_map):
            fetch_url = remote_map[remote_name].get("fetch", "Unknown")
            push_url = remote_map[remote_name].get("push", "Unknown")
            lines.append(f"{remote_name}: fetch={fetch_url} | push={push_url}")

        return lines

    def _latest_commit_summary(self, repository_path: Path, revspec: str) -> dict[str, str]:
        output = self._run_git_command(
            repository_path,
            "log",
            "-1",
            "--date=iso",
            "--pretty=format:%h%n%an%n%ad%n%s",
            revspec,
        )
        if output is None:
            return {
                "sha": "Unknown",
                "author": "Unknown",
                "date": "Unknown",
                "subject": "Unknown",
            }

        parts = output.splitlines()
        while len(parts) < 4:
            parts.append("Unknown")

        return {
            "sha": parts[0] or "Unknown",
            "author": parts[1] or "Unknown",
            "date": parts[2] or "Unknown",
            "subject": parts[3] or "Unknown",
        }

    def _latest_commit_summary_any_branch(self, repository_path: Path) -> dict[str, str]:
        """Return the most recent commit across ALL local branches (not just HEAD)."""
        output = self._run_git_command(
            repository_path,
            "log",
            "-1",
            "--all",
            "--date=iso",
            "--pretty=format:%h%n%an%n%ad%n%s",
        )
        if output is None:
            return {
                "sha": "Unknown",
                "author": "Unknown",
                "date": "Unknown",
                "subject": "Unknown",
            }

        parts = output.splitlines()
        while len(parts) < 4:
            parts.append("Unknown")

        return {
            "sha": parts[0] or "Unknown",
            "author": parts[1] or "Unknown",
            "date": parts[2] or "Unknown",
            "subject": parts[3] or "Unknown",
        }

    def _stash_count(self, repository_path: Path) -> int:
        output = self._run_git_command(repository_path, "stash", "list")
        if output is None or not output.strip():
            return 0

        return len(output.splitlines())

    def _ahead_behind_counts(
        self,
        repository_path: Path,
        branch_name: str,
        upstream: str,
    ) -> tuple[int | None, int | None]:
        output = self._run_git_command(
            repository_path,
            "rev-list",
            "--left-right",
            "--count",
            f"{upstream}...{branch_name}",
        )
        if output is None:
            return None, None

        counts = output.split()
        if len(counts) != 2:
            return None, None

        try:
            behind = int(counts[0])
            ahead = int(counts[1])
        except ValueError:
            return None, None

        return ahead, behind

    def _recent_commits(
        self,
        repository_path: Path,
        branch_name: str,
        limit: int,
    ) -> list[str]:
        output = self._run_git_command(
            repository_path,
            "log",
            "-n",
            str(limit),
            "--date=short",
            "--pretty=format:%h  %ad  %s",
            branch_name,
        )
        if output is None:
            return []

        lines = [line for line in output.splitlines() if line.strip()]
        return lines

    def _local_unpushed_commit_rows(
        self,
        repository_path: Path,
        branch: GitBranch,
    ) -> tuple[list[tuple[str, str, str, str]], set[str]]:
        """Return a LOCAL row (if dirty), followed by unpushed commits, then last 30 commits."""
        rows: list[tuple[str, str, str, str]] = []

        if self._local_file_rows_for_path(repository_path):
            rows.append(("LOCAL", "-", "-", "Local working tree changes"))
            # Reuse the same blue highlight path used for unpushed commits.
            unpushed_shas: set[str] = {"LOCAL"}
        else:
            unpushed_shas = set()

        # Unpushed commits
        if branch.upstream:
            revspec = f"{branch.upstream}..{branch.name}"
        else:
            revspec = branch.name

        output = self._run_git_command(
            repository_path,
            "log",
            revspec,
            "-n",
            "40",
            "--date=short",
            "--pretty=format:%h%x1f%ad%x1f%an%x1f%s",
        )
        if output is not None:
            for line in output.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\x1f", maxsplit=3)
                if len(parts) != 4:
                    continue
                sha, date, author, subject = [p.strip() for p in parts]
                unpushed_shas.add(sha)
                rows.append((sha or "-", date or "-", author or "-", subject or "-"))

        # Last 30 commits on the branch (skip any already shown as unpushed)
        history_output = self._run_git_command(
            repository_path,
            "log",
            branch.name,
            "-n",
            "30",
            "--date=short",
            "--pretty=format:%h%x1f%ad%x1f%an%x1f%s",
        )
        if history_output is not None:
            for line in history_output.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\x1f", maxsplit=3)
                if len(parts) != 4:
                    continue
                sha, date, author, subject = [p.strip() for p in parts]
                if sha in unpushed_shas:
                    continue
                rows.append((sha or "-", date or "-", author or "-", subject or "-"))

        return rows, unpushed_shas

    def _local_file_rows_for_path(
        self,
        repository_path: Path,
    ) -> list[tuple[str, str, str, str]]:
        """Return file rows for the working tree using git status --porcelain."""
        output = self._run_git_command(repository_path, "status", "--porcelain")
        if output is None:
            return []

        rows: list[tuple[str, str, str, str]] = []
        for raw_line in output.splitlines():
            if len(raw_line) < 2:
                continue
            parts = raw_line.split(maxsplit=1)
            if len(parts) < 2:
                continue
            status_code = parts[0].strip() or "M"
            path = parts[1].strip()
            if not path:
                continue
            display_path = path.split(" -> ")[-1].strip() or path
            rows.append((status_code, "-", "-", display_path))

        return rows

    def _recent_commit_rows_all_branches(
        self,
        repository_path: Path,
        limit: int,
    ) -> list[tuple[str, str, str, str]]:
        output = self._run_git_command(
            repository_path,
            "log",
            "--all",
            "-n",
            str(limit),
            "--date=short",
            "--pretty=format:%h%x1f%ad%x1f%an%x1f%s",
        )
        if output is None:
            return []

        rows: list[tuple[str, str, str, str]] = []
        for line in output.splitlines():
            if not line.strip():
                continue

            parts = line.split("\x1f", maxsplit=3)
            if len(parts) != 4:
                continue

            sha, date, author, subject = [part.strip() for part in parts]
            rows.append((sha or "-", date or "-", author or "-", subject or "-"))

        return rows

    def _commit_frequency_data_all_branches(
        self,
        repository_path: Path,
    ) -> dict[str, int]:
        output = self._run_git_command(
            repository_path,
            "log",
            "--all",
            "--since=30 days ago",
            "--date=short",
            "--pretty=format:%ad",
        )
        if output is None:
            return {}

        commits_by_date: dict[str, int] = {}
        for date_str in output.splitlines():
            date_str = date_str.strip()
            if date_str:
                commits_by_date[date_str] = commits_by_date.get(date_str, 0) + 1

        return commits_by_date

    def _recent_commit_rows(
        self,
        repository_path: Path,
        revspec: str,
        limit: int,
    ) -> list[tuple[str, str, str, str]]:
        output = self._run_git_command(
            repository_path,
            "log",
            "-n",
            str(limit),
            "--date=short",
            "--pretty=format:%h%x1f%ad%x1f%an%x1f%s",
            revspec,
        )
        if output is None:
            return []

        rows: list[tuple[str, str, str, str]] = []
        for line in output.splitlines():
            if not line.strip():
                continue

            parts = line.split("\x1f", maxsplit=3)
            if len(parts) != 4:
                continue

            sha, date, author, subject = [part.strip() for part in parts]
            rows.append((sha or "-", date or "-", author or "-", subject or "-"))

        return rows

    def _commit_file_rows(
        self,
        repository_path: Path,
        commit_sha: str,
    ) -> list[tuple[str, str, str, str]]:
        status_output = self._run_git_command(
            repository_path,
            "show",
            "--pretty=format:",
            "--name-status",
            "-M",
            commit_sha,
        )
        numstat_output = self._run_git_command(
            repository_path,
            "show",
            "--pretty=format:",
            "--numstat",
            "-M",
            commit_sha,
        )
        if status_output is None and numstat_output is None:
            return []

        status_rows = self._parse_commit_name_status(status_output)
        numstat_rows = self._parse_commit_numstat(numstat_output)
        return self._merge_commit_file_rows(status_rows, numstat_rows)

    def _parse_commit_name_status(self, output: str | None) -> list[tuple[str, str]]:
        if output is None:
            return []

        rows: list[tuple[str, str]] = []
        for raw_line in output.splitlines():
            if not raw_line.strip():
                continue

            parsed = self._parse_name_status_line(raw_line)
            if parsed is not None:
                rows.append(parsed)

        return rows

    def _parse_name_status_line(self, raw_line: str) -> tuple[str, str] | None:
        parts = raw_line.split("\t")
        if len(parts) < 2:
            return None

        status = parts[0].strip() or "-"
        if len(parts) == 2:
            display_path = parts[1].strip() or "-"
        else:
            display_path = f"{parts[1].strip()} -> {parts[2].strip()}"

        return status, display_path

    def _parse_commit_numstat(self, output: str | None) -> list[tuple[str, str, str]]:
        if output is None:
            return []

        rows: list[tuple[str, str, str]] = []
        for raw_line in output.splitlines():
            if not raw_line.strip():
                continue

            parsed = self._parse_numstat_line(raw_line)
            if parsed is not None:
                rows.append(parsed)

        return rows

    def _parse_numstat_line(self, raw_line: str) -> tuple[str, str, str] | None:
        parts = raw_line.split("\t")
        if len(parts) < 3:
            return None

        added = parts[0].strip() or "0"
        deleted = parts[1].strip() or "0"
        path = parts[2].strip() or "-"
        if added == "-":
            added = "binary"
        if deleted == "-":
            deleted = "binary"

        return added, deleted, path

    def _merge_commit_file_rows(
        self,
        status_rows: list[tuple[str, str]],
        numstat_rows: list[tuple[str, str, str]],
    ) -> list[tuple[str, str, str, str]]:
        max_rows = max(len(status_rows), len(numstat_rows))
        rows: list[tuple[str, str, str, str]] = []
        for idx in range(max_rows):
            status, display_path = self._status_row_at(status_rows, idx)
            added, deleted, numstat_path = self._numstat_row_at(numstat_rows, idx)
            if display_path == "-":
                display_path = numstat_path
            rows.append((status, added, deleted, display_path))

        return rows

    def _status_row_at(
        self,
        status_rows: list[tuple[str, str]],
        idx: int,
    ) -> tuple[str, str]:
        if idx < len(status_rows):
            return status_rows[idx]

        return "-", "-"

    def _numstat_row_at(
        self,
        numstat_rows: list[tuple[str, str, str]],
        idx: int,
    ) -> tuple[str, str, str]:
        if idx < len(numstat_rows):
            return numstat_rows[idx]

        return "-", "-", "-"

    def _commit_frequency_data(self, repository_path: Path, refspec: str) -> dict[str, int]:
        """Get commit counts per day for the last 30 days.

        Returns a dict mapping YYYY-MM-DD to commit count.
        """
        output = self._run_git_command(
            repository_path,
            "log",
            "--since=30 days ago",
            "--date=short",
            "--pretty=format:%ad",
            refspec,
        )
        if output is None:
            return {}

        commits_by_date: dict[str, int] = {}
        for date_str in output.splitlines():
            date_str = date_str.strip()
            if date_str:
                commits_by_date[date_str] = commits_by_date.get(date_str, 0) + 1

        return commits_by_date

    def _run_git_command(self, repository_path: Path, *args: str) -> str | None:
        if shutil.which("git") is None:
            return None

        try:
            completed = subprocess.run(
                ["git", "-C", str(repository_path), *args],
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        return completed.stdout.strip()
