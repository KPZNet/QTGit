from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.services.repo_scanner import (
    CloneResult,
    RemoteRepository,
    clone_remote_repository,
    list_remote_repositories,
)


class _CloneDialogSignals(QObject):
    repositories_loaded = Signal(object)
    repositories_failed = Signal(str)
    clone_finished = Signal(object)


class CloneDialog(QDialog):
    """Pick a remote GitHub repository and clone it into a selected local directory."""

    def __init__(self, target_directory: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Clone Repository")
        self.resize(900, 520)

        self._target_directory = target_directory.expanduser().resolve()
        self._signals = _CloneDialogSignals()
        self._signals.repositories_loaded.connect(self._on_repositories_loaded)
        self._signals.repositories_failed.connect(self._on_repositories_failed)
        self._signals.clone_finished.connect(self._on_clone_finished)

        self._all_repositories: list[RemoteRepository] = []
        self._filtered_repositories: list[RemoteRepository] = []
        self._cloned_repository_path: Path | None = None

        layout = QVBoxLayout(self)

        location_label = QLabel(f"Clone destination: {self._target_directory}")
        location_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(location_label)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self._filter_input = QLineEdit(self)
        self._filter_input.setPlaceholderText("Type to filter repositories...")
        self._filter_input.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_input)
        layout.addLayout(filter_row)

        self._table = QTableWidget(self)
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["Updated", "Repository", "Visibility"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.cellDoubleClicked.connect(lambda _row, _col: self._start_clone())
        layout.addWidget(self._table)

        self._status_label = QLabel("Loading remote repositories...")
        layout.addWidget(self._status_label)

        self._progress_bar = QProgressBar(self)
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        self._clone_button = QPushButton("Clone", self)
        self._clone_button.clicked.connect(self._start_clone)
        button_box.addButton(self._clone_button, QDialogButtonBox.ButtonRole.AcceptRole)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self._set_controls_enabled(False)
        self._load_repositories_async()

    def cloned_repository_path(self) -> Path | None:
        return self._cloned_repository_path

    def _load_repositories_async(self) -> None:
        def worker() -> None:
            try:
                repositories = list_remote_repositories()
            except RuntimeError as exc:
                self._signals.repositories_failed.emit(str(exc))
                return
            self._signals.repositories_loaded.emit(repositories)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _on_repositories_loaded(self, payload: object) -> None:
        repositories = payload if isinstance(payload, list) else []
        self._all_repositories = [repo for repo in repositories if isinstance(repo, RemoteRepository)]
        self._apply_filter()
        self._set_controls_enabled(True)

        if self._all_repositories:
            self._status_label.setText(f"Loaded {len(self._all_repositories)} repositories.")
        else:
            self._status_label.setText(
                "No repositories found. Confirm the active GitHub token has repository access."
            )

    def _on_repositories_failed(self, message: str) -> None:
        self._all_repositories = []
        self._apply_filter()
        self._set_controls_enabled(True)
        self._status_label.setText("Failed to load repositories.")
        QMessageBox.warning(self, "Clone", message)

    def _apply_filter(self) -> None:
        filter_text = self._filter_input.text().strip().lower()
        if not filter_text:
            self._filtered_repositories = list(self._all_repositories)
        else:
            self._filtered_repositories = [
                repo
                for repo in self._all_repositories
                if filter_text in repo.full_name.lower()
                or filter_text in repo.name.lower()
                or filter_text in (repo.visibility or "").lower()
            ]

        self._table.setRowCount(len(self._filtered_repositories))
        for row, repository in enumerate(self._filtered_repositories):
            updated_item = QTableWidgetItem(self._format_timestamp(repository.updated_at))
            repo_item = QTableWidgetItem(repository.full_name)
            visibility_item = QTableWidgetItem(repository.visibility or "")
            self._table.setItem(row, 0, updated_item)
            self._table.setItem(row, 1, repo_item)
            self._table.setItem(row, 2, visibility_item)

        self._table.resizeColumnToContents(0)
        self._table.resizeColumnToContents(1)
        self._clone_button.setEnabled(bool(self._filtered_repositories))

    def _selected_repository(self) -> RemoteRepository | None:
        selected_rows = self._table.selectionModel().selectedRows()
        if not selected_rows:
            return None
        row = selected_rows[0].row()
        if row < 0 or row >= len(self._filtered_repositories):
            return None
        return self._filtered_repositories[row]

    def _start_clone(self) -> None:
        repository = self._selected_repository()
        if repository is None:
            QMessageBox.warning(self, "Clone", "Please select a repository to clone.")
            return

        if not self._target_directory.exists() or not self._target_directory.is_dir():
            QMessageBox.warning(
                self,
                "Clone",
                f"The selected directory is not available:\n{self._target_directory}",
            )
            return

        self._status_label.setText(f"Cloning {repository.full_name}...")
        self._progress_bar.setVisible(True)
        self._set_controls_enabled(False)

        def worker() -> None:
            result = clone_remote_repository(repository.clone_url, self._target_directory)
            self._signals.clone_finished.emit(result)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _on_clone_finished(self, payload: object) -> None:
        self._progress_bar.setVisible(False)
        self._set_controls_enabled(True)

        if not isinstance(payload, CloneResult):
            self._status_label.setText("Clone failed.")
            QMessageBox.warning(self, "Clone", "Clone failed with an unexpected result.")
            return

        if payload.success:
            self._cloned_repository_path = payload.destination_path
            self._status_label.setText(f"Clone complete: {payload.destination_path.name}")
            QMessageBox.information(
                self,
                "Clone Complete",
                f"Cloned into:\n{payload.destination_path}",
            )
            self.accept()
            return

        details = payload.error or payload.output or "Unknown clone error."
        self._status_label.setText("Clone failed.")
        QMessageBox.warning(self, "Clone Failed", details)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._filter_input.setEnabled(enabled)
        self._table.setEnabled(enabled)
        if enabled:
            self._clone_button.setEnabled(bool(self._filtered_repositories))
        else:
            self._clone_button.setEnabled(False)

    def _format_timestamp(self, raw_value: str | None) -> str:
        if not raw_value:
            return ""
        normalized = raw_value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return raw_value

