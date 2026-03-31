from __future__ import annotations

from pathlib import Path
import subprocess
from difflib import SequenceMatcher

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class DiffGutter(QWidget):
    """A gutter widget that shows where diffs are located in the file."""

    clicked = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(20)
        self.setMaximumWidth(20)
        self.diff_ranges = []  # List of (start_line, end_line, change_type)
        self.total_lines = 0
        self.current_viewport_top = 0
        self.current_viewport_height = 100
        self._is_scrubbing = False
        self._is_hovering = False

    def set_diff_ranges(self, ranges: list[tuple[int, int, str]], total_lines: int) -> None:
        """Set the ranges of diffs. change_type: 'add', 'remove', 'modify'"""
        self.diff_ranges = ranges
        self.total_lines = max(total_lines, 1)
        self.update()

    def set_viewport_range(self, top_line: int, height_in_lines: int) -> None:
        """Update the visible viewport position."""
        self.current_viewport_top = top_line
        self.current_viewport_height = height_in_lines
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(240, 240, 240))

        if self.total_lines == 0 or not self.diff_ranges:
            return

        width = self.width()
        height = self.height()

        # Draw diff indicators
        for start_line, end_line, change_type in self.diff_ranges:
            # Calculate position in gutter
            start_pos = (start_line / self.total_lines) * height
            end_pos = (end_line / self.total_lines) * height
            range_height = max(1, end_pos - start_pos)

            # Choose color based on change type
            if change_type == 'add':
                color = QColor(76, 175, 80)  # Green
            elif change_type == 'remove':
                color = QColor(244, 67, 54)  # Red
            else:  # modify
                color = QColor(33, 150, 243)  # Blue

            painter.fillRect(0, int(start_pos), width, int(range_height), color)

        # Draw viewport indicator
        if self.total_lines > 0:
            viewport_start = (self.current_viewport_top / self.total_lines) * height
            viewport_end = ((self.current_viewport_top + self.current_viewport_height) / self.total_lines) * height
            viewport_height = max(2, viewport_end - viewport_start)

            outline_color = QColor(100, 100, 100)
            painter.setPen(outline_color)
            painter.drawRect(0, int(viewport_start), width - 1, int(viewport_height))

    def mousePressEvent(self, event) -> None:
        """Jump to an approximate file position based on where the gutter is clicked."""
        if event.button() == Qt.MouseButton.LeftButton and self.height() > 0:
            self._is_scrubbing = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self._emit_ratio_from_y(event.position().y())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._is_scrubbing and (event.buttons() & Qt.MouseButton.LeftButton):
            self._emit_ratio_from_y(event.position().y())
            event.accept()
            return
        if self._is_scrubbing:
            self._finish_scrub()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._finish_scrub()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:
        self._is_hovering = True
        if not self._is_scrubbing:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._is_hovering = False
        if not self._is_scrubbing:
            self.unsetCursor()
        super().leaveEvent(event)

    def _emit_ratio_from_y(self, y_pos: float) -> None:
        if self.height() <= 0:
            return
        ratio = max(0.0, min(1.0, y_pos / self.height()))
        self.clicked.emit(ratio)

    def _finish_scrub(self) -> None:
        self._is_scrubbing = False
        if self._is_hovering:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()


class SynchronizedPlainTextEdit(QPlainTextEdit):
    """A plain text edit that can be synchronized with another editor for scrolling."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.sync_partner: SynchronizedPlainTextEdit | None = None
        self._syncing = False

        # Connect signals
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def set_sync_partner(self, partner: SynchronizedPlainTextEdit) -> None:
        """Set the partner editor to synchronize with."""
        self.sync_partner = partner

    def _on_scroll(self) -> None:
        """Called when this editor is scrolled."""
        if self._syncing or self.sync_partner is None:
            return

        # Synchronize with partner
        self.sync_partner._syncing = True
        self.sync_partner.verticalScrollBar().setValue(self.verticalScrollBar().value())
        self.sync_partner._syncing = False


class GitDiffViewerWindow(QDialog):
    """A window showing a side-by-side git diff with synchronized scrolling and minimap gutter."""

    def __init__(
        self,
        repository_path: Path,
        commit_sha: str,
        file_path: str,
        parent=None,
    ):
        super().__init__(parent)
        self.repository_path = repository_path
        self.commit_sha = commit_sha
        self.file_path = file_path

        self.setWindowTitle(f"Diff: {file_path} ({commit_sha[:7]})")
        self.resize(1400, 800)

        self._build_ui()
        self._load_diff()

    def _build_ui(self) -> None:
        """Build the UI with side-by-side editors and gutter."""
        layout = QVBoxLayout(self)

        # Header
        header = QLabel(f"<b>File:</b> {self.file_path} | <b>Commit:</b> {self.commit_sha[:7]}")
        layout.addWidget(header)

        # Main diff area with editors and gutters
        content_layout = QHBoxLayout()

        # Left side (old version)
        left_layout = QVBoxLayout()
        left_label = QLabel("Before (Parent Commit)")
        left_layout.addWidget(left_label)

        left_content = QHBoxLayout()
        self.left_editor = SynchronizedPlainTextEdit()
        self.left_gutter = DiffGutter()
        left_content.addWidget(self.left_editor, 1)
        left_content.addWidget(self.left_gutter)
        left_layout.addLayout(left_content)

        left_widget = QWidget()
        left_widget.setLayout(left_layout)
        content_layout.addWidget(left_widget, 1)

        # Right side (new version)
        right_layout = QVBoxLayout()
        right_label = QLabel("After (Selected Commit)")
        right_layout.addWidget(right_label)

        right_content = QHBoxLayout()
        self.right_editor = SynchronizedPlainTextEdit()
        self.right_gutter = DiffGutter()
        right_content.addWidget(self.right_editor, 1)
        right_content.addWidget(self.right_gutter)
        right_layout.addLayout(right_content)

        right_widget = QWidget()
        right_widget.setLayout(right_layout)
        content_layout.addWidget(right_widget, 1)

        content = QWidget()
        content.setLayout(content_layout)
        layout.addWidget(content, 1)

        # Synchronize scrolling
        self.left_editor.set_sync_partner(self.right_editor)
        self.right_editor.set_sync_partner(self.left_editor)

        # Connect scroll updates to gutter
        self.left_editor.verticalScrollBar().valueChanged.connect(self._update_gutter_viewports)
        self.right_editor.verticalScrollBar().valueChanged.connect(self._update_gutter_viewports)

        # Click in either gutter to jump to that relative location.
        self.left_gutter.clicked.connect(self._scroll_to_ratio)
        self.right_gutter.clicked.connect(self._scroll_to_ratio)

    def _load_diff(self) -> None:
        """Load and display the diff."""
        try:
            # Get the old version (from parent commit)
            old_content = self._get_file_content(f"{self.commit_sha}^:{self.file_path}")
            # Get the new version (from the commit)
            new_content = self._get_file_content(f"{self.commit_sha}:{self.file_path}")

            self.left_editor.setPlainText(old_content)
            self.right_editor.setPlainText(new_content)

            # Highlight diffs
            self._highlight_diffs(old_content, new_content)

            # Update gutters
            left_lines = old_content.count('\n') + 1 if old_content else 0
            right_lines = new_content.count('\n') + 1 if new_content else 0

            self._update_gutter_viewports()

        except Exception as e:
            error_text = f"Error loading diff: {e}"
            self.left_editor.setPlainText(error_text)
            self.right_editor.setPlainText("")

    def _get_file_content(self, revision_and_path: str) -> str:
        """Get file content from git at a specific revision."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repository_path), "show", revision_and_path],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout
            return ""
        except Exception as e:
            return f"Error: {e}"

    def _highlight_diffs(self, old_content: str, new_content: str) -> None:
        """Highlight differences between old and new content."""
        old_lines = old_content.splitlines(keepends=False) if old_content else []
        new_lines = new_content.splitlines(keepends=False) if new_content else []

        left_diff_ranges = []
        right_diff_ranges = []

        # Use SequenceMatcher for better diff detection
        matcher = SequenceMatcher(None, old_lines, new_lines)

        old_line_idx = 0
        new_line_idx = 0

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'replace':
                # Lines changed
                left_diff_ranges.append((i1, i2, 'modify'))
                right_diff_ranges.append((j1, j2, 'modify'))

                # Highlight these lines in editors
                for line_num in range(i1, i2):
                    self._highlight_line(self.left_editor, line_num, QColor(255, 200, 200))
                for line_num in range(j1, j2):
                    self._highlight_line(self.right_editor, line_num, QColor(200, 255, 200))

            elif tag == 'delete':
                # Lines removed
                left_diff_ranges.append((i1, i2, 'remove'))
                for line_num in range(i1, i2):
                    self._highlight_line(self.left_editor, line_num, QColor(255, 100, 100))

            elif tag == 'insert':
                # Lines added
                right_diff_ranges.append((j1, j2, 'add'))
                for line_num in range(j1, j2):
                    self._highlight_line(self.right_editor, line_num, QColor(100, 255, 100))

        # Set gutter ranges
        self.left_gutter.set_diff_ranges(left_diff_ranges, len(old_lines))
        self.right_gutter.set_diff_ranges(right_diff_ranges, len(new_lines))

    def _highlight_line(self, editor: QPlainTextEdit, line_num: int, color: QColor) -> None:
        """Highlight a specific line with a background color."""
        if line_num < 0:
            return

        cursor = QTextCursor(editor.document())
        cursor.movePosition(QTextCursor.MoveOperation.Start)

        for _ in range(line_num):
            cursor.movePosition(QTextCursor.MoveOperation.Down)

        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor)

        fmt = cursor.charFormat()
        fmt.setBackground(color)
        cursor.setCharFormat(fmt)

    def _update_gutter_viewports(self) -> None:
        """Update the gutter viewport indicators based on current scroll position."""
        # Get viewport info from left editor
        block = self.left_editor.firstVisibleBlock()
        top_line = block.blockNumber()

        # Calculate how many lines are visible
        metrics = self.left_editor.fontMetrics()
        height_in_lines = self.left_editor.height() // metrics.lineSpacing()

        self.left_gutter.set_viewport_range(top_line, height_in_lines)
        self.right_gutter.set_viewport_range(top_line, height_in_lines)

    def _scroll_to_ratio(self, ratio: float) -> None:
        """Scroll editors to a relative location (0.0 top, 1.0 bottom)."""
        scroll_bar = self.left_editor.verticalScrollBar()
        target = int(scroll_bar.maximum() * max(0.0, min(1.0, ratio)))
        scroll_bar.setValue(target)


class LocalGitDiffViewerWindow(QDialog):
    """A window showing HEAD vs working-tree diff for one local file."""

    def __init__(
        self,
        repository_path: Path,
        file_path: str,
        local_status: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.repository_path = repository_path
        self.file_path = file_path
        self.local_status = (local_status or "").strip()

        self.setWindowTitle(f"Diff: {file_path} (HEAD vs Working Tree)")
        self.resize(1400, 800)

        self._build_ui()
        self._load_diff()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        status_text = self._status_label(self.local_status)
        status_color = self._status_color(self.local_status)
        header = QLabel(
            f"<b>File:</b> {self.file_path} | "
            f"<b>Status:</b> <span style='color:{status_color};font-weight:600'>{status_text}</span> | "
            f"<b>Comparison:</b> HEAD vs Working Tree"
        )
        layout.addWidget(header)

        content_layout = QHBoxLayout()

        left_layout = QVBoxLayout()
        left_label = QLabel("Before (HEAD)")
        left_layout.addWidget(left_label)

        left_content = QHBoxLayout()
        self.left_editor = SynchronizedPlainTextEdit()
        self.left_gutter = DiffGutter()
        left_content.addWidget(self.left_editor, 1)
        left_content.addWidget(self.left_gutter)
        left_layout.addLayout(left_content)

        left_widget = QWidget()
        left_widget.setLayout(left_layout)
        content_layout.addWidget(left_widget, 1)

        right_layout = QVBoxLayout()
        right_label = QLabel("After (Working Tree)")
        right_layout.addWidget(right_label)

        right_content = QHBoxLayout()
        self.right_editor = SynchronizedPlainTextEdit()
        self.right_gutter = DiffGutter()
        right_content.addWidget(self.right_editor, 1)
        right_content.addWidget(self.right_gutter)
        right_layout.addLayout(right_content)

        right_widget = QWidget()
        right_widget.setLayout(right_layout)
        content_layout.addWidget(right_widget, 1)

        content = QWidget()
        content.setLayout(content_layout)
        layout.addWidget(content, 1)

        self.left_editor.set_sync_partner(self.right_editor)
        self.right_editor.set_sync_partner(self.left_editor)

        self.left_editor.verticalScrollBar().valueChanged.connect(self._update_gutter_viewports)
        self.right_editor.verticalScrollBar().valueChanged.connect(self._update_gutter_viewports)

        self.left_gutter.clicked.connect(self._scroll_to_ratio)
        self.right_gutter.clicked.connect(self._scroll_to_ratio)

    def _load_diff(self) -> None:
        try:
            old_content = self._get_head_file_content()
            new_content = self._get_worktree_file_content()

            self.left_editor.setPlainText(old_content)
            self.right_editor.setPlainText(new_content)

            self._highlight_diffs(old_content, new_content)
            self._update_gutter_viewports()
        except Exception as exc:
            self.left_editor.setPlainText(f"Error loading local diff: {exc}")
            self.right_editor.setPlainText("")

    def _get_head_file_content(self) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repository_path), "show", f"HEAD:{self.file_path}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout
            return ""
        except Exception as exc:
            return f"Error: {exc}"

    def _get_worktree_file_content(self) -> str:
        file_on_disk = self.repository_path / self.file_path
        try:
            if not file_on_disk.exists() or file_on_disk.is_dir():
                return ""
            return file_on_disk.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"Error: {exc}"

    def _status_label(self, status_code: str) -> str:
        normalized = status_code.strip()
        if not normalized:
            return "unknown"

        if normalized == "??":
            return "untracked"
        if normalized.startswith("R"):
            return "renamed"
        if normalized.startswith("D") or normalized.endswith("D"):
            return "deleted"
        if normalized.startswith("A") or normalized.endswith("A"):
            return "added"
        if normalized.startswith("M") or normalized.endswith("M"):
            return "modified"

        return normalized

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

    def _highlight_diffs(self, old_content: str, new_content: str) -> None:
        old_lines = old_content.splitlines(keepends=False) if old_content else []
        new_lines = new_content.splitlines(keepends=False) if new_content else []

        left_diff_ranges = []
        right_diff_ranges = []

        matcher = SequenceMatcher(None, old_lines, new_lines)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "replace":
                left_diff_ranges.append((i1, i2, "modify"))
                right_diff_ranges.append((j1, j2, "modify"))
                for line_num in range(i1, i2):
                    self._highlight_line(self.left_editor, line_num, QColor(255, 200, 200))
                for line_num in range(j1, j2):
                    self._highlight_line(self.right_editor, line_num, QColor(200, 255, 200))
            elif tag == "delete":
                left_diff_ranges.append((i1, i2, "remove"))
                for line_num in range(i1, i2):
                    self._highlight_line(self.left_editor, line_num, QColor(255, 100, 100))
            elif tag == "insert":
                right_diff_ranges.append((j1, j2, "add"))
                for line_num in range(j1, j2):
                    self._highlight_line(self.right_editor, line_num, QColor(100, 255, 100))

        self.left_gutter.set_diff_ranges(left_diff_ranges, len(old_lines))
        self.right_gutter.set_diff_ranges(right_diff_ranges, len(new_lines))

    def _highlight_line(self, editor: QPlainTextEdit, line_num: int, color: QColor) -> None:
        if line_num < 0:
            return

        cursor = QTextCursor(editor.document())
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        for _ in range(line_num):
            cursor.movePosition(QTextCursor.MoveOperation.Down)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor)

        fmt = cursor.charFormat()
        fmt.setBackground(color)
        cursor.setCharFormat(fmt)

    def _update_gutter_viewports(self) -> None:
        block = self.left_editor.firstVisibleBlock()
        top_line = block.blockNumber()
        metrics = self.left_editor.fontMetrics()
        height_in_lines = self.left_editor.height() // metrics.lineSpacing()

        self.left_gutter.set_viewport_range(top_line, height_in_lines)
        self.right_gutter.set_viewport_range(top_line, height_in_lines)

    def _scroll_to_ratio(self, ratio: float) -> None:
        scroll_bar = self.left_editor.verticalScrollBar()
        target = int(scroll_bar.maximum() * max(0.0, min(1.0, ratio)))
        scroll_bar.setValue(target)



