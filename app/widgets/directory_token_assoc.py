"""Widget for managing token associations with directories."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)


class DirectoryTokenAssociationWidget(QWidget):
    """Display and manage token associations for recent directories."""

    def __init__(
        self,
        directory_associations: dict[str, str],
        available_tokens: list[str],
        recent_directories: list[Path] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """
        Args:
            directory_associations: dict of {directory_path_str: token_name}
            available_tokens: list of token names that can be assigned
            recent_directories: list of Path objects for recent directories
        """
        super().__init__(parent)

        self._directory_associations = dict(directory_associations)
        self._available_tokens = available_tokens
        self._working_associations = dict(directory_associations)
        self._recent_directories = recent_directories or []
        self._combo_boxes: dict[str, QComboBox] = {}  # Track combo boxes by directory path

        self._table: QTableWidget | None = None

        self._build_ui()
        self._populate_table()

    def _build_ui(self) -> None:
        """Build the UI for directory-token associations."""
        layout = QVBoxLayout(self)

        header = QLabel("<b>Directory-Token Associations</b>")
        layout.addWidget(header)

        desc = QLabel(
            "Associate Git tokens with your recent directories. "
            "When you browse to a directory, its associated token will automatically become active."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        table_label = QLabel("<b>Recent Directories:</b>")
        layout.addWidget(table_label)

        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["Directory", "Associated Token", "Actions"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table)

        info = QLabel(
            "<span style='color:#666;font-size:11px;'>"
            "Click the token dropdown to change or remove an association (select empty to remove)."
            "</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info)

        layout.addStretch()

    def _populate_table(self) -> None:
        """Populate the table with all recent directories and their associations."""
        if not self._table:
            return

        self._table.setRowCount(0)
        self._combo_boxes.clear()

        if not self._recent_directories:
            # Show a message if no recent directories
            self._table.setRowCount(1)
            item = QTableWidgetItem("No recent directories")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self._table.setItem(0, 0, item)
            return

        row = 0
        # Show all recent directories
        for directory in self._recent_directories:
            dir_path_str = str(directory)
            token_name = self._working_associations.get(dir_path_str, "")

            # Directory column
            dir_item = QTableWidgetItem(directory.name)
            dir_item.setToolTip(dir_path_str)  # Full path in tooltip
            dir_item.setFlags(dir_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.insertRow(row)
            self._table.setItem(row, 0, dir_item)

            # Token dropdown column
            combo = QComboBox()
            combo.addItem("(no token)", userData=None)
            for token in sorted(self._available_tokens):
                combo.addItem(token, userData=token)

            # Set the current selection
            if token_name:
                index = combo.findData(token_name)
                if index >= 0:
                    combo.setCurrentIndex(index)

            # Store reference to this combo box
            self._combo_boxes[dir_path_str] = combo

            # Connect the combo box to update associations when changed
            combo.currentIndexChanged.connect(
                lambda idx, path=dir_path_str: self._on_combo_changed(path)
            )
            self._table.setCellWidget(row, 1, combo)

            # Remove button column
            remove_btn = QPushButton("Clear")
            remove_btn.clicked.connect(
                lambda checked=False, path=dir_path_str: self._on_remove_clicked(path)
            )
            self._table.setCellWidget(row, 2, remove_btn)

            row += 1

    def _on_combo_changed(self, directory_path: str) -> None:
        """Handle combo box selection change."""
        if directory_path not in self._combo_boxes:
            return

        combo = self._combo_boxes[directory_path]
        new_token = combo.currentData()

        if new_token:
            self._working_associations[directory_path] = new_token
        else:
            # Remove association if "(no token)" is selected
            self._working_associations.pop(directory_path, None)

    def _on_remove_clicked(self, directory_path: str) -> None:
        """Handle remove button click."""
        self._working_associations.pop(directory_path, None)
        self._populate_table()

    def get_working_associations(self) -> dict[str, str]:
        """Return the current working associations."""
        return dict(self._working_associations)

