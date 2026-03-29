"""Dialog for displaying and managing remote branches."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.services.repo_scanner import (
    GitRepository,
    RemoteBranch,
    checkout_remote_branch,
    get_remote_branches,
)


class RemotesDialog(QDialog):
    """Dialog to display remote branches and allow checking out as local branches."""

    branch_checked_out = Signal(object, str)  # (repository, branch_name)

    def __init__(self, repository: GitRepository, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Remote Branches - {repository.name}")
        self.resize(800, 500)
        self._repository = repository
        self._remote_branches: list[RemoteBranch] = []

        layout = QVBoxLayout(self)

        # Title label
        title_label = QLabel(f"Remote branches for: {repository.path}")
        title_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(title_label)

        # Table for remote branches
        self._table = QTableWidget(self)
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels([
            "Commit Date",
            "Branch",
            "Author",
        ])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.cellDoubleClicked.connect(lambda _row, _col: self._on_checkout_clicked())
        layout.addWidget(self._table)

        # Buttons
        button_layout = QHBoxLayout()

        checkout_button = QPushButton("Checkout as Local Branch", self)
        checkout_button.clicked.connect(self._on_checkout_clicked)
        button_layout.addWidget(checkout_button)

        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.accept)
        button_layout.addWidget(close_button)

        layout.addLayout(button_layout)

        # Load remote branches
        self._load_remote_branches()

    def _load_remote_branches(self) -> None:
        """Load remote branches from the repository and populate the table."""
        self._remote_branches = get_remote_branches(self._repository)

        if not self._remote_branches:
            self._table.setRowCount(0)
            QMessageBox.information(
                self,
                "No Remote Branches",
                f"No remote branches found for repository: {self._repository.name}",
            )
            return

        self._table.setRowCount(len(self._remote_branches))

        for row, remote_branch in enumerate(self._remote_branches):
            # Commit date
            date_item = QTableWidgetItem(remote_branch.commit_date or "")
            date_item.setFlags(date_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, date_item)

            # Branch name (remote/branch format)
            branch_item = QTableWidgetItem(remote_branch.name)
            branch_item.setFlags(branch_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 1, branch_item)

            # Author
            author_item = QTableWidgetItem(remote_branch.author or "")
            author_item.setFlags(author_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 2, author_item)

        # Auto-resize columns to fit content
        for col in range(self._table.columnCount() - 1):
            self._table.resizeColumnToContents(col)

    def _on_checkout_clicked(self) -> None:
        """Handle the checkout button click."""
        selected_rows = self._table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(
                self,
                "No Selection",
                "Please select a remote branch to check out.",
            )
            return

        row = selected_rows[0].row()
        if row < 0 or row >= len(self._remote_branches):
            return

        remote_branch = self._remote_branches[row]

        # Attempt to check out the remote branch
        result = checkout_remote_branch(self._repository, remote_branch.name)

        if result.success:
            QMessageBox.information(
                self,
                "Checkout Successful",
                f"Successfully checked out '{remote_branch.name}' as a local tracking branch.",
            )
            self.branch_checked_out.emit(self._repository, remote_branch.name)
        else:
            error = result.error or result.output or "Unknown error"
            QMessageBox.warning(
                self,
                "Checkout Failed",
                f"Failed to check out '{remote_branch.name}'.\n\n{error}",
            )
