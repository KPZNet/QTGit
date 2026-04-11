from __future__ import annotations

from datetime import date, datetime
from importlib import metadata
from pathlib import Path
import os
import shutil
import stat
import threading
import tomllib

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QAction, QColor, QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHeaderView,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QMenu,
    QSplitter,
    QToolButton,
    QCheckBox,
    QVBoxLayout,
    QHBoxLayout,
    QDialog,
    QDialogButtonBox,
    QPushButton,
    QLabel,
    QPlainTextEdit,
    QTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QSizePolicy,
    QWidget,
)

from app.services.app_settings import AppSettings
from app.services.repo_scanner import (
    GitBranch,
    GitRepository,
    CommitResult,
    PullResult,
    PushResult,
    RepoScanResult,
    CheckoutResult,
    DeleteBranchResult,
    commit_local_changes,
    checkout_branch,
    checkout_remote_branch,
    delete_branch,
    find_git_repositories,
    get_remote_branches,
    get_github_token,
    pull_repository,
    push_branch_commits,
    push_repository,
    scan_repositories_live,
    set_github_token,
    sync_active_branch_to_remote,
)
from app.widgets.config_dialog import ConfigDialog
from app.widgets.clone_dialog import CloneDialog
from app.widgets.git_diff_viewer import GitDiffViewerWindow
from app.widgets.remotes_dialog import RemotesDialog, BranchesDialog
from app.widgets.repo_tree import RepoTreeWidget
from app.widgets.split_pane import RightSplitPane


def _resolve_app_version() -> str:
    """Return app version from installed metadata or local pyproject fallback."""
    try:
        return metadata.version("qtgit")
    except metadata.PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        if pyproject_path.exists():
            try:
                data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
                version = data.get("project", {}).get("version")
                if isinstance(version, str) and version.strip():
                    return version.strip()
            except (OSError, tomllib.TOMLDecodeError):
                pass
    return "unknown"


def _default_commit_timestamp() -> str:
    """Return a local timestamp with timezone for default commit messages."""
    local_now = datetime.now().astimezone()
    base_timestamp = local_now.strftime("%Y-%m-%d %H:%M:%S")
    timezone_name = local_now.tzname()
    timezone_offset = local_now.strftime("%z")

    if timezone_name and timezone_offset and timezone_name != timezone_offset:
        return f"{base_timestamp} {timezone_name} {timezone_offset}"
    if timezone_name:
        return f"{base_timestamp} {timezone_name}"
    if timezone_offset:
        return f"{base_timestamp} {timezone_offset}"
    return base_timestamp


class _PullSignals(QObject):
    """Carries cross-thread signals for the Pull All operation."""
    progress = Signal(object, str)   # (repository_path: Path, status: str)
    all_done = Signal()


class _RefreshSignals(QObject):
    """Carries cross-thread signals for the live Refresh scan."""
    repo_scanned = Signal(object)   # GitRepository
    scan_complete = Signal(object)  # RepoScanResult


class _PushSignals(QObject):
    """Carries cross-thread signals for a Push operation."""
    progress = Signal(object, str, str)  # (repository_path: Path, branch_name: str, status: str)
    done = Signal(object)               # PushResult


class _PullBranchSignals(QObject):
    """Carries cross-thread signals for the Pull Branch operation."""
    progress = Signal(object, str)  # (repository_path: Path, status: str)
    done = Signal(object)           # PullResult


class _CommitSignals(QObject):
    """Carries cross-thread signals for local Commit operations."""
    done = Signal(object)  # CommitResult


class _PushAllSignals(QObject):
    """Carries cross-thread signals for Push All operations."""
    progress = Signal(str)
    done = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self, start_directory: Path) -> None:
        super().__init__()
        self._app_version = _resolve_app_version()
        self._settings = AppSettings()
        self._current_directory = self._settings.load_last_directory(start_directory)
        self._recent_directories: list[Path] = self._settings.recent_directories()
        self._repo_tree = RepoTreeWidget()
        self._right_pane = RightSplitPane()
        self._recent_menu = QMenu("Recent", self)
        self._main_splitter: QSplitter | None = None
        self._directory_display: QLineEdit | None = None
        self._token_display: QPushButton | None = None
        self._refresh_action: QAction | None = None
        self._clone_action: QAction | None = None
        self._pull_all_action: QAction | None = None
        self._push_all_action: QAction | None = None
        self._clean_action: QAction | None = None
        self._branches_action: QAction | None = None
        self._latest_repositories: list[GitRepository] = []
        self._selected_repository: GitRepository | None = None
        self._selected_branch: GitBranch | None = None
        self._pending_selection_repo_path: Path | None = None
        self._pending_selection_branch_name: str | None = None
        self._pull_signals = _PullSignals()
        self._pull_results: list[PullResult] = []
        self._pull_results_lock = threading.Lock()
        self._refresh_signals = _RefreshSignals()
        self._push_signals = _PushSignals()
        self._pull_branch_signals = _PullBranchSignals()
        self._commit_signals = _CommitSignals()
        self._push_all_signals = _PushAllSignals()
        self._pull_signals.progress.connect(self._repo_tree.set_pull_status)
        self._pull_signals.all_done.connect(self._on_pull_all_complete)
        self._refresh_signals.repo_scanned.connect(self._on_repo_scanned)
        self._refresh_signals.scan_complete.connect(self._on_scan_complete)
        self._push_signals.progress.connect(self._on_push_progress)
        self._push_signals.done.connect(self._on_push_done)
        self._pull_branch_signals.progress.connect(self._repo_tree.set_pull_status)
        self._pull_branch_signals.done.connect(self._on_pull_branch_complete)
        self._commit_signals.done.connect(self._on_commit_done)
        self._push_all_signals.progress.connect(self._on_push_all_progress)
        self._push_all_signals.done.connect(self._on_push_all_done)
        self._repo_tree.selection_changed.connect(self._handle_tree_selection)
        self._repo_tree.branch_double_clicked.connect(self._handle_branch_double_click)
        self._repo_tree.select_all_branches_requested.connect(
            self._handle_select_all_branches
        )
        self._repo_tree.branch_delete_requested.connect(self._handle_branch_delete_requested)
        self._repo_tree.remove_all_local_branches_requested.connect(
            self._handle_remove_all_local_branches_requested
        )
        self._repo_tree.branch_sync_to_remote_requested.connect(self._handle_branch_sync_to_remote_requested)
        self._repo_tree.branch_select_active_requested.connect(self._handle_branch_double_click)
        self._repo_tree.remotes_requested.connect(self._handle_remotes_requested)
        self._repo_tree.clean_branches_requested.connect(self._handle_clean_branches_requested)
        self._repo_tree.pull_branch_requested.connect(self._handle_pull_branch_requested)
        self._repo_tree.delete_local_repository_requested.connect(self._handle_delete_local_repository_requested)
        self._right_pane.file_double_clicked.connect(self._handle_file_double_clicked)
        self._right_pane.commit_requested.connect(self._handle_commit_requested)
        self._right_pane.push_requested.connect(self._handle_push_requested)

        self.setWindowTitle("QTGit")
        self.resize(1280, 780)

        self._build_toolbar()
        self._build_layout()
        self._restore_window_state()
        self._refresh_recent_menu()
        self._update_active_token_display()

        # Apply any previously saved GitHub token so git calls are authenticated
        # from the very first scan.
        self._apply_saved_token()

        self._scan_directory(self._current_directory, remember_directory=False)
        # Trigger a full refresh on startup so sync status reflects current remote state
        self._refresh_repositories()

    def _apply_saved_token(self) -> None:
        """Load the active stored token on app startup."""
        token = self._settings.get_active_github_token()
        if token:
            set_github_token(token)
        self._update_active_token_display()

    def _build_toolbar(self) -> None:
        toolbar = self.addToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setStyleSheet(
            """
            QToolBar QToolButton,
            QToolBar QPushButton {
                background-color: #f5f5f5;
                border: 1px solid #dcdcdc;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QToolBar QToolButton:hover,
            QToolBar QPushButton:hover {
                background-color: #f0f0f0;
            }
            QToolBar QToolButton:pressed,
            QToolBar QPushButton:pressed {
                background-color: #e8e8e8;
            }
            """
        )

        browse_action = QAction("Browse", self)
        browse_action.triggered.connect(self._browse_for_directory)
        toolbar.addAction(browse_action)

        recent_button = QToolButton(self)
        recent_button.setText("Recent")
        recent_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        recent_button.setMenu(self._recent_menu)
        toolbar.addWidget(recent_button)

        self._clone_action = QAction("Clone", self)
        self._clone_action.setToolTip("Clone a remote repository into the current directory")
        self._clone_action.triggered.connect(self._handle_clone_requested)
        toolbar.addAction(self._clone_action)

        self._refresh_action = QAction("Refresh", self)
        self._refresh_action.setToolTip("Re-scan repositories and update sync status")
        self._refresh_action.triggered.connect(self._refresh_repositories)
        toolbar.addAction(self._refresh_action)

        self._pull_all_action = QAction("Pull All", self)
        self._pull_all_action.setToolTip("Pull latest for every repository's active branch (parallel)")
        self._pull_all_action.triggered.connect(self._pull_all)
        toolbar.addAction(self._pull_all_action)

        self._push_all_action = QAction("Push All", self)
        self._push_all_action.setToolTip(
            "Commit outstanding local changes with one message, then push all repositories"
        )
        self._push_all_action.triggered.connect(self._push_all)
        toolbar.addAction(self._push_all_action)


        self._clean_action = QAction("Clean", self)
        self._clean_action.setToolTip(
            "Run Clean Branches for each repository (local only; keeps active and develop)"
        )
        self._clean_action.triggered.connect(self._clean_all_repositories)
        toolbar.addAction(self._clean_action)

        self._branches_action = QAction("Branches", self)
        self._branches_action.setToolTip(
            "Show remote branches present in 2+ repositories and switch all repositories to the selected branch"
        )
        self._branches_action.triggered.connect(self._handle_branches_requested)
        toolbar.addAction(self._branches_action)

        self._directory_display = QLineEdit(self)
        self._directory_display.setReadOnly(True)
        self._directory_display.setMinimumWidth(360)
        self._directory_display.setToolTip("Current browse directory")
        toolbar.addWidget(self._directory_display)

        self._token_display = QPushButton(self)
        self._token_display.setFlat(True)
        self._token_display.setMinimumWidth(220)
        self._token_display.setToolTip("Currently selected GitHub token")
        self._token_display.clicked.connect(self._show_settings)
        toolbar.addWidget(self._token_display)

        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        settings_action = QAction("Settings", self)
        settings_action.setToolTip("Configure GitHub token and other preferences")
        settings_action.triggered.connect(self._show_settings)
        toolbar.addAction(settings_action)

        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        toolbar.addAction(about_action)

    def _build_layout(self) -> None:
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._main_splitter.addWidget(self._repo_tree)
        self._main_splitter.addWidget(self._right_pane)
        self._main_splitter.setCollapsible(0, False)
        self._main_splitter.setCollapsible(1, False)
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setSizes([340, 940])

        self.setCentralWidget(self._main_splitter)

        self.statusBar().showMessage("Select a directory to browse Git repositories")

    def _browse_for_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Choose Directory",
            str(self._current_directory),
        )
        if not directory:
            return

        self._scan_directory(Path(directory), remember_directory=True)

    def _handle_clone_requested(self) -> None:
        self._activate_associated_token_for_directory(self._current_directory)

        if not self._current_directory.exists() or not self._current_directory.is_dir():
            QMessageBox.warning(
                self,
                "Clone",
                f"The current directory is not available:\n{self._current_directory}",
            )
            return

        dialog = CloneDialog(target_directory=self._current_directory, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        cloned_repo_path = dialog.cloned_repository_path()
        if cloned_repo_path is None:
            return

        self._pending_selection_repo_path = cloned_repo_path
        self._pending_selection_branch_name = None
        self.statusBar().showMessage(f"Cloned repository: {cloned_repo_path.name}")
        self._scan_directory(self._current_directory, remember_directory=False)

    def _refresh_repositories(self) -> None:
        self._queue_current_selection_for_restore()
        if self._refresh_action is not None:
            self._refresh_action.setEnabled(False)
        if self._pull_all_action is not None:
            self._pull_all_action.setEnabled(False)
        if self._push_all_action is not None:
            self._push_all_action.setEnabled(False)
        if self._clean_action is not None:
            self._clean_action.setEnabled(False)
        if self._branches_action is not None:
            self._branches_action.setEnabled(False)
        self.statusBar().showMessage("Fetching from remotes and refreshing repositories\u2026")
        self._repo_tree.begin_live_scan(self._current_directory)

        def on_repo_scanned(repo: GitRepository) -> None:
            self._refresh_signals.repo_scanned.emit(repo)

        def on_complete(result: RepoScanResult) -> None:
            self._refresh_signals.scan_complete.emit(result)

        thread = threading.Thread(
            target=scan_repositories_live,
            args=(self._current_directory, on_repo_scanned, on_complete),
            daemon=True,
        )
        thread.start()

    def _on_repo_scanned(self, repo: GitRepository) -> None:
        self._repo_tree.add_repository(repo)

    def _on_scan_complete(self, result: RepoScanResult) -> None:
        self._latest_repositories = result.repositories
        self._right_pane.update_context(self._current_directory, result)
        self._restore_or_clear_tree_selection()

        if self._refresh_action is not None:
            self._refresh_action.setEnabled(True)
        if self._pull_all_action is not None:
            self._pull_all_action.setEnabled(True)
        if self._push_all_action is not None:
            self._push_all_action.setEnabled(True)
        if self._clean_action is not None:
            self._clean_action.setEnabled(True)
        if self._branches_action is not None:
            self._branches_action.setEnabled(True)

        if result.error_message:
            self.statusBar().showMessage(result.error_message)
            return

        repo_count = len(result.repositories)
        in_sync = sum(
            1 for repo in result.repositories
            if any(b.is_current and b.sync_status == "in_sync" for b in repo.local_branches)
        )
        behind = sum(
            1 for repo in result.repositories
            if any(b.is_current and b.sync_status in {"behind", "diverged"} for b in repo.local_branches)
        )
        self.statusBar().showMessage(
            f"{repo_count} repositories \u2014 {in_sync} in sync, {behind} behind upstream"
        )

    def _restore_recent_directory(self, directory: Path) -> None:
        """Browse to a recent directory and activate its associated token if any."""
        token_name = self._activate_associated_token_for_directory(directory)
        if token_name:
            self.statusBar().showMessage(
                f"Activated token '{token_name}' for directory {directory.name}"
            )

        # Now scan the directory
        self._scan_directory(directory, remember_directory=True)

    def _activate_associated_token_for_directory(self, directory: Path) -> str:
        """Activate the token associated with *directory* and return its name."""
        token_name = self._settings.get_token_for_directory(directory)
        if not token_name:
            return ""

        self._settings.set_active_token(token_name)
        active_token_name = self._settings.get_active_token_name()
        if active_token_name != token_name:
            return ""

        set_github_token(self._settings.get_active_github_token())
        self._update_active_token_display()
        return token_name

    def _update_active_token_display(self) -> None:
        if self._token_display is None:
            return

        active_token_name = self._settings.get_active_token_name().strip()
        label_text = f"Token: {active_token_name}" if active_token_name else "Token: (none)"
        self._token_display.setText(label_text)
        self._token_display.setToolTip(
            "Currently selected GitHub token" if active_token_name else "No active GitHub token selected"
        )

    def _scan_directory(self, directory: Path, remember_directory: bool) -> None:
        normalized_directory = directory.expanduser().resolve()
        if not remember_directory:
            self._queue_current_selection_for_restore()
        self._current_directory = normalized_directory
        self._update_directory_display()

        if remember_directory:
            self._recent_directories = self._settings.save_browsed_directory(
                normalized_directory
            )
            self._refresh_recent_menu()

        result = find_git_repositories(normalized_directory)
        self._latest_repositories = result.repositories
        self._repo_tree.set_root_directory(normalized_directory)
        self._repo_tree.clear_pull_statuses()
        self._repo_tree.set_repositories(result.repositories)
        self._right_pane.update_context(normalized_directory, result)
        self._restore_or_clear_tree_selection()

        if result.error_message:
            self.statusBar().showMessage(result.error_message)
            return

        repo_count = len(result.repositories)
        in_sync = sum(
            1 for repo in result.repositories
            if any(b.is_current and b.sync_status == "in_sync" for b in repo.local_branches)
        )
        behind = sum(
            1 for repo in result.repositories
            if any(b.is_current and b.sync_status in {"behind", "diverged"} for b in repo.local_branches)
        )
        self.statusBar().showMessage(
            f"{repo_count} repositories \u2014 {in_sync} in sync, {behind} behind upstream"
        )

    def _queue_current_selection_for_restore(self) -> None:
        """Capture current tree selection so the next scan can restore it."""
        if self._pending_selection_repo_path is not None:
            return

        if self._selected_repository is None:
            return

        self._pending_selection_repo_path = self._selected_repository.path
        self._pending_selection_branch_name = (
            self._selected_branch.name if self._selected_branch is not None else None
        )

    def _restore_or_clear_tree_selection(self) -> None:
        """Restore queued selection after a scan, or clear when none is queued."""
        if self._pending_selection_repo_path is not None:
            self._repo_tree.select_repo_branch(
                self._pending_selection_repo_path,
                self._pending_selection_branch_name,
            )
            self._pending_selection_repo_path = None
            self._pending_selection_branch_name = None
            return

        self._repo_tree.clear_selection()

    def _handle_tree_selection(
        self,
        repository: GitRepository | None,
        branch: GitBranch | None,
    ) -> None:
        self._selected_repository = repository
        self._selected_branch = branch
        self._right_pane.show_selection(repository, branch)


    def _handle_pull_branch_requested(self, repository: GitRepository) -> None:
        self._selected_repository = repository
        self._pull_branch()

    def _handle_branch_double_click(
        self,
        repository: GitRepository | None,
        branch: GitBranch | None,
    ) -> None:
        if repository is None or branch is None:
            return

        if branch.is_current:
            self.statusBar().showMessage(f"{repository.name}: {branch.name} is already active.")
            return

        result = checkout_branch(repository, branch.name)
        if result.success:
            self.statusBar().showMessage(
                f"{repository.name}: switched to {branch.name}."
            )
            self._scan_directory(self._current_directory, remember_directory=False)
            return

        error = result.error or result.output or "Unknown checkout error"
        self.statusBar().showMessage(
            f"{repository.name}: failed to switch to {branch.name}."
        )
        QMessageBox.warning(
            self,
            "Branch Switch Failed",
            f"Could not switch to branch '{branch.name}' in {repository.path}.\n\n{error}",
        )

    def _handle_select_all_branches(
        self,
        repository: GitRepository | None,
        branch: GitBranch | None,
    ) -> None:
        if repository is None or branch is None:
            return

        target_branch = branch.name
        matching_repositories = 0
        switched_count = 0
        already_active_count = 0
        failures: list[str] = []

        self.statusBar().showMessage(f"Switching all repositories to {target_branch}...")

        for candidate_repo in self._latest_repositories:
            matching_branch = next(
                (candidate for candidate in candidate_repo.local_branches if candidate.name == target_branch),
                None,
            )
            if matching_branch is not None:
                matching_repositories += 1
                if matching_branch.is_current:
                    already_active_count += 1
                    continue

                result = checkout_branch(candidate_repo, target_branch)
                if result.success:
                    switched_count += 1
                    continue

                error = result.error or result.output or "Unknown checkout error"
                failures.append(
                    f"{candidate_repo.name} ({candidate_repo.path}): {error}"
                )
                continue

            # Branch is not local; attempt to find and checkout a remote branch.
            remote_branches = get_remote_branches(candidate_repo)
            remote_match = next(
                (rb for rb in remote_branches if rb.name == f"origin/{target_branch}"),
                None,
            )
            if remote_match is None:
                remote_match = next(
                    (rb for rb in remote_branches if rb.name.endswith(f"/{target_branch}")),
                    None,
                )
            if remote_match is None:
                continue

            matching_repositories += 1
            remote_result = checkout_remote_branch(candidate_repo, remote_match.name)
            if remote_result.success:
                switched_count += 1
                continue

            error = remote_result.error or remote_result.output or "Unknown remote checkout error"
            failures.append(
                f"{candidate_repo.name} ({candidate_repo.path}): {error}"
            )

        if matching_repositories == 0:
            self.statusBar().showMessage(
                f"No repositories contain local or remote branch '{target_branch}'."
            )
            return

        if failures:
            self.statusBar().showMessage(
                f"Select All Branches complete: {switched_count} switched, {already_active_count} already active, {len(failures)} failed."
            )
            QMessageBox.warning(
                self,
                "Select All Branches - Some Checkouts Failed",
                "Could not switch one or more repositories:\n\n" + "\n".join(failures),
            )
        else:
            self.statusBar().showMessage(
                f"Select All Branches complete: {switched_count} switched, {already_active_count} already active."
            )

        if switched_count > 0 or failures:
            self._scan_directory(self._current_directory, remember_directory=False)

    def _handle_branch_delete_requested(
        self,
        repository: GitRepository | None,
        branch: GitBranch | None,
        force: bool,
    ) -> None:
        """Handle the Delete Local Branch context menu action."""
        if repository is None or branch is None:
            return

        # If deleting the current branch, attempt to switch to a safe fallback first
        if branch.is_current:
            fallback_order = ["develop", "main", "master"]
            fallback_branch = next(
                (
                    candidate
                    for candidate in fallback_order
                    if candidate != branch.name
                    and any(
                        local_branch.name == candidate
                        for local_branch in repository.local_branches
                    )
                ),
                None,
            )

            if fallback_branch is None:
                QMessageBox.warning(
                    self,
                    "Cannot Delete Active Branch",
                    f"Cannot delete '{branch.name}' as it is the currently active branch in {repository.name}.\n"
                    f"No fallback branch found to switch to automatically (tried: develop, main, master).\n"
                    f"Please checkout a different branch first.",
                )
                return

            # Attempt to switch to the first available fallback branch
            checkout_result = checkout_branch(repository, fallback_branch)
            if not checkout_result.success:
                error = checkout_result.error or checkout_result.output or "Unknown checkout error"
                QMessageBox.warning(
                    self,
                    "Cannot Delete Active Branch",
                    f"Cannot delete '{branch.name}' as it is the currently active branch in {repository.name}.\n"
                    f"Failed to automatically switch to '{fallback_branch}' branch.\n\n{error}",
                )
                return

            # Successfully switched to a fallback branch
            self.statusBar().showMessage(
                f"{repository.name}: automatically switched to {fallback_branch} branch."
            )
            # Continue with deletion dialog below
        
        # Create custom dialog with force delete checkbox
        dialog = QDialog(self)
        dialog.setWindowTitle("Delete Local Branch")
        dialog.resize(450, 180)

        layout = QVBoxLayout(dialog)

        msg_label = QLabel(
            f"Are you sure you want to delete the local branch '{branch.name}'?\n\n"
            f"This will only delete the local branch, not any remote branch."
        )
        layout.addWidget(msg_label)

        force_checkbox = QCheckBox("Force delete (ignore merge status)")
        force_checkbox.setChecked(False)
        layout.addWidget(force_checkbox)

        # Button layout
        button_layout = QHBoxLayout()
        delete_button = QPushButton("Delete")
        cancel_button = QPushButton("Cancel")
        button_layout.addStretch()
        button_layout.addWidget(delete_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)

        user_confirmed = False

        def on_delete():
            nonlocal user_confirmed
            user_confirmed = True
            dialog.accept()

        def on_cancel():
            dialog.reject()

        delete_button.clicked.connect(on_delete)
        cancel_button.clicked.connect(on_cancel)

        dialog.exec()

        if not user_confirmed:
            return

        # Perform the delete operation
        force_delete = force_checkbox.isChecked()
        delete_result = delete_branch(repository, branch.name, force=force_delete)

        if delete_result.success:
            self.statusBar().showMessage(
                f"{repository.name}: deleted local branch '{branch.name}'."
            )
            # Refresh the repository tree to remove the deleted branch
            self._scan_directory(self._current_directory, remember_directory=False)
        else:
            error = delete_result.error or delete_result.output or "Unknown deletion error"
            self.statusBar().showMessage(
                f"{repository.name}: failed to delete branch '{branch.name}'."
            )
            QMessageBox.warning(
                self,
                "Branch Deletion Failed",
                f"Could not delete branch '{branch.name}' in {repository.path}.\n\n{error}",
            )

    def _handle_remove_all_local_branches_requested(
        self,
        repository: GitRepository | None,
        branch: GitBranch | None,
    ) -> None:
        """Remove a local branch name from all repositories where it exists."""
        if repository is None or branch is None:
            return

        target_branch = branch.name
        confirmation = QMessageBox.question(
            self,
            "Remove All Local Branches",
            (
                f"Remove local branch '{target_branch}' from all repositories where it exists?\n\n"
                "This uses force delete for each matching repository.\n"
                "Remote branches are not affected."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        matching_repositories = 0
        deleted_count = 0
        failures: list[str] = []

        for candidate_repo in self._latest_repositories:
            target = next(
                (candidate for candidate in candidate_repo.local_branches if candidate.name == target_branch),
                None,
            )
            if target is None:
                continue

            matching_repositories += 1

            if target.is_current:
                fallback_order = ["develop", "main", "master"]
                fallback_branch = next(
                    (
                        candidate
                        for candidate in fallback_order
                        if candidate != target_branch
                        and any(local_branch.name == candidate for local_branch in candidate_repo.local_branches)
                    ),
                    None,
                )
                if fallback_branch is None:
                    fallback_branch = next(
                        (local_branch.name for local_branch in candidate_repo.local_branches if local_branch.name != target_branch),
                        None,
                    )

                if fallback_branch is None:
                    failures.append(
                        f"{candidate_repo.name} ({candidate_repo.path}): no fallback branch available to switch away from active '{target_branch}'."
                    )
                    continue

                checkout_result = checkout_branch(candidate_repo, fallback_branch)
                if not checkout_result.success:
                    error = checkout_result.error or checkout_result.output or "Unknown checkout error"
                    failures.append(
                        f"{candidate_repo.name} ({candidate_repo.path}): failed to switch to '{fallback_branch}' before deletion ({error})."
                    )
                    continue

            delete_result = delete_branch(candidate_repo, target_branch, force=True)
            if delete_result.success:
                deleted_count += 1
                continue

            error = delete_result.error or delete_result.output or "Unknown deletion error"
            failures.append(
                f"{candidate_repo.name} ({candidate_repo.path}): {error}"
            )

        if matching_repositories == 0:
            self.statusBar().showMessage(
                f"No repositories contain local branch '{target_branch}'."
            )
            return

        if failures:
            self.statusBar().showMessage(
                f"Remove All Local Branches complete: {deleted_count} deleted, {len(failures)} failed."
            )
            QMessageBox.warning(
                self,
                "Remove All Local Branches - Some Deletes Failed",
                "Could not delete one or more local branches:\n\n" + "\n".join(failures),
            )
        else:
            self.statusBar().showMessage(
                f"Remove All Local Branches complete: deleted '{target_branch}' from {deleted_count} repositories."
            )

        self._scan_directory(self._current_directory, remember_directory=False)

    def _handle_branch_sync_to_remote_requested(
        self,
        repository: GitRepository | None,
        branch: GitBranch | None,
    ) -> None:
        """Hard reset the active branch to its upstream remote branch."""
        dialog_title = "Sync to Remote"

        if repository is None or branch is None:
            return

        if not branch.is_current:
            QMessageBox.warning(
                self,
                dialog_title,
                "Sync to Remote is only available for the active branch.",
            )
            return

        if not branch.upstream:
            QMessageBox.warning(
                self,
                dialog_title,
                f"Branch '{branch.name}' does not track an upstream branch.",
            )
            return

        confirmation = QMessageBox.question(
            self,
            dialog_title,
            (
                f"Reset active branch '{branch.name}' to '{branch.upstream}' in {repository.name}?\n\n"
                "This will discard all local commits not on the remote branch.\n"
                "This will discard staged and unstaged local changes.\n"
                "This will remove untracked files and directories.\n\n"
                "The remote branch is not modified.\n\n"
                "Continue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        result = sync_active_branch_to_remote(repository, branch)
        if result.success:
            self.statusBar().showMessage(
                f"{repository.name}: synced '{branch.name}' to '{branch.upstream}'."
            )
            self._scan_directory(self._current_directory, remember_directory=False)
            return

        error = result.error or result.output or "Unknown sync error"
        self.statusBar().showMessage(
            f"{repository.name}: failed to sync '{branch.name}' to remote."
        )
        QMessageBox.warning(
            self,
            "Sync to Remote Failed",
            f"Could not sync branch '{branch.name}' in {repository.path}.\n\n{error}",
        )

    def _handle_remotes_requested(self, repository: GitRepository | None) -> None:
        """Handle the Remotes context menu action."""
        if repository is None:
            return

        dialog = RemotesDialog(repository, parent=self)
        dialog.branch_checked_out.connect(self._on_remote_branch_checked_out)
        dialog.exec()

    def _handle_delete_local_repository_requested(self, repository: GitRepository | None) -> None:
        """Delete the selected local repository directory after explicit confirmation."""
        if repository is None:
            return

        repo_path = repository.path
        repo_name = repository.name

        confirmation = QMessageBox.question(
            self,
            "Delete Local Repository",
            (
                f"Delete local repository '{repo_name}'?\n\n"
                f"This will permanently remove all local files in:\n{repo_path}\n\n"
                "This does NOT delete any remote repository."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        if not repo_path.exists():
            self.statusBar().showMessage(f"{repo_name}: local directory not found.")
            self._scan_directory(self._current_directory, remember_directory=False)
            return

        if not repo_path.is_dir():
            QMessageBox.warning(
                self,
                "Delete Local Repository Failed",
                f"Expected a directory, but found:\n{repo_path}",
            )
            return

        try:
            self._delete_local_repository_files(repo_path)
        except OSError as error:
            self.statusBar().showMessage(f"{repo_name}: failed to delete local files.")
            QMessageBox.warning(
                self,
                "Delete Local Repository Failed",
                f"Could not delete local repository files in:\n{repo_path}\n\n{error}",
            )
            return

        self.statusBar().showMessage(f"{repo_name}: deleted local repository files.")
        self._scan_directory(self._current_directory, remember_directory=False)

    def _delete_local_repository_files(self, repo_path: Path) -> None:
        """Delete a local repository folder, clearing read-only attributes when needed."""

        def _handle_remove_readonly(function, path, exc_info) -> None:
            del exc_info
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(repo_path, onerror=_handle_remove_readonly)

    def _handle_branches_requested(self) -> None:
        """Show shared remote branches and switch all repositories to one selection."""
        if not self._latest_repositories:
            self.statusBar().showMessage("No repositories loaded.")
            return

        if len(self._latest_repositories) < 2:
            QMessageBox.information(
                self,
                "Branches",
                "At least two repositories are required to show shared branches.",
            )
            self.statusBar().showMessage("Need at least two repositories for shared branches.")
            return

        shared_branches = self._get_shared_remote_branch_names(self._latest_repositories)
        if not shared_branches:
            QMessageBox.information(
                self,
                "Branches",
                "No remote branches were found in two or more repositories.",
            )
            self.statusBar().showMessage("No shared remote branches available.")
            return

        dialog = BranchesDialog(shared_branches, len(self._latest_repositories), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        target_branch = dialog.selected_branch_name()
        if not target_branch:
            return

        confirmation = QMessageBox.question(
            self,
            "Switch All Repositories",
            (
                f"Selected branch: '{target_branch}'\n\n"
                "Check out Common Branch across all repositories?\n\n"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        self._checkout_common_branch_across_repositories(target_branch)

    def _get_shared_remote_branch_names(self, repositories: list[GitRepository]) -> list[str]:
        """Return branch names present in 2+ repos, newest activity first."""
        branch_counts: dict[str, int] = {}
        branch_latest_date: dict[str, date | None] = {}
        for repository in repositories:
            # Count each branch at most once per repository.
            repo_latest_by_branch: dict[str, date | None] = {}
            for remote_branch in get_remote_branches(repository):
                normalized_name = self._normalize_remote_branch_name(remote_branch.name)
                if not normalized_name:
                    continue
                commit_date = self._parse_remote_commit_date(remote_branch.commit_date)
                current_latest = repo_latest_by_branch.get(normalized_name)
                if current_latest is None or (commit_date is not None and commit_date > current_latest):
                    repo_latest_by_branch[normalized_name] = commit_date

            for branch_name, latest_date in repo_latest_by_branch.items():
                branch_counts[branch_name] = branch_counts.get(branch_name, 0) + 1
                known_latest = branch_latest_date.get(branch_name)
                if known_latest is None or (latest_date is not None and latest_date > known_latest):
                    branch_latest_date[branch_name] = latest_date

        shared_branches = [
            branch_name
            for branch_name, repo_count in branch_counts.items()
            if repo_count >= 2
        ]
        shared_branches.sort(
            key=lambda name: (
                branch_latest_date.get(name) is None,
                -(branch_latest_date[name].toordinal() if branch_latest_date.get(name) is not None else 0),
                name.lower(),
            )
        )
        return shared_branches

    def _parse_remote_commit_date(self, raw_date: str | None) -> date | None:
        if not raw_date:
            return None
        try:
            return date.fromisoformat(raw_date.strip())
        except ValueError:
            return None

    def _normalize_remote_branch_name(self, remote_branch_name: str) -> str | None:
        """Convert 'origin/feature/x' to 'feature/x'."""
        if not remote_branch_name or "/" not in remote_branch_name:
            return None
        return remote_branch_name.split("/", 1)[1].strip() or None

    def _find_remote_branch_ref(self, repository: GitRepository, branch_name: str) -> str | None:
        """Pick the best matching remote ref for a branch in a repository."""
        remote_branches = get_remote_branches(repository)
        preferred_name = f"origin/{branch_name}"

        if any(rb.name == preferred_name for rb in remote_branches):
            return preferred_name

        for remote_branch in remote_branches:
            if self._normalize_remote_branch_name(remote_branch.name) == branch_name:
                return remote_branch.name

        return None

    def _checkout_common_branch_across_repositories(self, target_branch: str) -> None:
        switched_count = 0
        already_active_count = 0
        failures_count = 0
        result_rows: list[tuple[str, str, str]] = []

        self.statusBar().showMessage(f"Switching all repositories to {target_branch}...")

        for repository in self._latest_repositories:
            local_branch = next(
                (candidate for candidate in repository.local_branches if candidate.name == target_branch),
                None,
            )
            if local_branch is not None and local_branch.is_current:
                already_active_count += 1
                result_rows.append((repository.name, "Already Active", "Branch is already active."))
                continue

            if local_branch is not None:
                local_result = checkout_branch(repository, target_branch)
                if local_result.success:
                    switched_count += 1
                    result_rows.append((repository.name, "Switched", "Switched local branch."))
                    continue

                error = local_result.error or local_result.output or "Unknown checkout error"
                failures_count += 1
                result_rows.append((repository.name, "Failed", error))
                continue

            remote_branch_ref = self._find_remote_branch_ref(repository, target_branch)
            if remote_branch_ref is None:
                failures_count += 1
                result_rows.append(
                    (
                        repository.name,
                        "Not Found",
                        f"No matching remote branch found for '{target_branch}'.",
                    )
                )
                continue

            remote_result = checkout_remote_branch(repository, remote_branch_ref)
            if remote_result.success:
                switched_count += 1
                result_rows.append((repository.name, "Switched", f"Checked out from '{remote_branch_ref}'."))
                continue

            error = remote_result.error or remote_result.output or "Unknown checkout error"
            failures_count += 1
            result_rows.append((repository.name, "Failed", error))

        if failures_count:
            self.statusBar().showMessage(
                f"Branches complete: {switched_count} switched, {already_active_count} already active, {failures_count} not switched."
            )
        else:
            self.statusBar().showMessage(
                f"Branches complete: {switched_count} switched, {already_active_count} already active."
            )

        self._show_branch_checkout_status_dialog(target_branch, result_rows)

        self._scan_directory(self._current_directory, remember_directory=False)

    def _show_branch_checkout_status_dialog(
        self,
        target_branch: str,
        rows: list[tuple[str, str, str]],
    ) -> None:
        """Show a tabular per-repository summary for the branch checkout operation."""
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Branches Results - {target_branch}")
        dialog.resize(900, 520)

        layout = QVBoxLayout(dialog)
        summary_label = QLabel(
            f"Branch checkout results for '{target_branch}' across {len(rows)} repositories:"
        )
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        table = QTableWidget(dialog)
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["Repository", "Status", "Details"])
        table.setRowCount(len(rows))
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        status_colors: dict[str, QColor] = {
            "Switched": QColor("#dcfce7"),
            "Already Active": QColor("#dbeafe"),
            "Not Found": QColor("#fef3c7"),
            "Failed": QColor("#fee2e2"),
        }

        for row_index, (repo_name, status, details) in enumerate(rows):
            repo_item = QTableWidgetItem(repo_name)
            status_item = QTableWidgetItem(status)
            status_item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
            )
            details_item = QTableWidgetItem(details)

            row_color = status_colors.get(status)
            if row_color is not None:
                repo_item.setBackground(row_color)
                status_item.setBackground(row_color)
                details_item.setBackground(row_color)

            table.setItem(row_index, 0, repo_item)
            table.setItem(row_index, 1, status_item)
            table.setItem(row_index, 2, details_item)

        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)

        dialog.exec()

    def _handle_clean_branches_requested(self, repository: GitRepository | None) -> None:
        """Delete local branches except active and 'develop'."""
        if repository is None:
            return

        deleted_count, failures, skipped = self._clean_repository_local_branches(
            repository,
            require_confirmation=True,
        )

        if skipped:
            return

        if failures:
            self.statusBar().showMessage(
                f"{repository.name}: deleted {deleted_count} branch(es), {len(failures)} failed."
            )
            QMessageBox.warning(
                self,
                "Clean Branches - Some Deletes Failed",
                "Could not delete one or more local branches:\n\n" + "\n".join(failures),
            )
        else:
            self.statusBar().showMessage(
                f"{repository.name}: deleted {deleted_count} local branch(es)."
            )

        if deleted_count > 0 or failures:
            self._scan_directory(self._current_directory, remember_directory=False)

    def _clean_all_repositories(self) -> None:
        """Run local-branch cleanup across all discovered repositories."""
        if not self._latest_repositories:
            self.statusBar().showMessage("No repositories to clean.")
            return

        confirmation = QMessageBox.question(
            self,
            "Clean All Repositories",
            (
                f"Run Clean Branches for all {len(self._latest_repositories)} repositories?\n\n"
                "This deletes local branches except the active branch and any branch named 'develop', 'main', or 'master'.\n"
                "Remote branches are never deleted."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        total_deleted = 0
        total_failed = 0
        touched_repos = 0
        all_failures: list[str] = []

        for repository in list(self._latest_repositories):
            deleted_count, failures, skipped = self._clean_repository_local_branches(
                repository,
                require_confirmation=False,
            )
            if skipped:
                continue

            if deleted_count > 0 or failures:
                touched_repos += 1
            total_deleted += deleted_count
            total_failed += len(failures)
            all_failures.extend(f"{repository.name}: {failure}" for failure in failures)

        if total_failed:
            self.statusBar().showMessage(
                f"Clean complete: {total_deleted} branch(es) deleted, {total_failed} failed across {touched_repos} repos."
            )
            QMessageBox.warning(
                self,
                "Clean - Some Deletes Failed",
                "Could not delete one or more local branches:\n\n" + "\n".join(all_failures),
            )
        else:
            self.statusBar().showMessage(
                f"Clean complete: {total_deleted} branch(es) deleted across {touched_repos} repos."
            )

        self._scan_directory(self._current_directory, remember_directory=False)

    def _clean_repository_local_branches(
        self,
        repository: GitRepository,
        require_confirmation: bool,
    ) -> tuple[int, list[str], bool]:
        """Delete local branches except active and 'develop'.

        Returns (deleted_count, failures, skipped).
        """
        active_branch = next(
            (branch for branch in repository.local_branches if branch.is_current),
            None,
        )
        if active_branch is None:
            if require_confirmation:
                QMessageBox.warning(
                    self,
                    "Clean Branches Failed",
                    f"Could not determine the active branch for {repository.name}.",
                )
            return 0, ["Could not determine active branch."], True

        _PROTECTED_BRANCHES = {"develop", "main", "master"}

        branches_to_delete = [
            branch
            for branch in repository.local_branches
            if not branch.is_current and branch.name not in _PROTECTED_BRANCHES
        ]

        if not branches_to_delete:
            if require_confirmation:
                self.statusBar().showMessage(
                    f"{repository.name}: no local branches to clean."
                )
            return 0, [], True

        if require_confirmation:
            branch_names = "\n".join(f"- {branch.name}" for branch in branches_to_delete)
            confirmation = QMessageBox.question(
                self,
                "Clean Branches",
                (
                    f"This will delete {len(branches_to_delete)} local branch(es) in {repository.name}.\n\n"
                    f"Branches to delete:\n{branch_names}\n\n"
                    "The active branch and any branch named 'develop', 'main', or 'master' will be kept.\n"
                    "Remote branches are not affected.\n\n"
                    "Continue?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirmation != QMessageBox.StandardButton.Yes:
                return 0, [], True

        deleted_count = 0
        failures: list[str] = []

        for branch in branches_to_delete:
            result = delete_branch(repository, branch.name, force=True)
            if result.success:
                deleted_count += 1
                continue

            error = result.error or result.output or "Unknown deletion error"
            failures.append(f"{branch.name}: {error}")

        return deleted_count, failures, False

    def _on_remote_branch_checked_out(
        self,
        repository: GitRepository | None,
        branch_name: str,
    ) -> None:
        """Handle when a remote branch is checked out from the RemotesDialog."""
        if repository is None:
            return

        self.statusBar().showMessage(
            f"{repository.name}: checked out remote branch {branch_name} as local tracking branch."
        )
        # Refresh the repository tree to show the new local branch
        self._scan_directory(self._current_directory, remember_directory=False)

    def _handle_file_double_clicked(self, commit_sha: str, file_path: str) -> None:
        """Open the diff viewer when a file is double-clicked."""
        if not self._latest_repositories:
            return

        # Find the repository that matches the currently selected one
        repository_path = self._right_pane._selected_repository_path
        if repository_path is None:
            return

        # Open the diff viewer window
        diff_viewer = GitDiffViewerWindow(
            repository_path=repository_path,
            commit_sha=commit_sha,
            file_path=file_path,
            parent=self,
        )
        diff_viewer.show()


    def _handle_push_requested(
        self,
        repository: GitRepository | None,
        branch: GitBranch | None,
    ) -> None:
        """Push all local commits for the active branch and show a final status dialog."""
        if repository is None or branch is None:
            return

        if not branch.is_current:
            QMessageBox.warning(
                self,
                "Push",
                f"Push is only available for the active branch.\n\n"
                f"'{branch.name}' is not the currently checked-out branch in {repository.name}.",
            )
            return

        confirmation = QMessageBox.question(
            self,
            "Push Local Commits",
            (
                f"Push all local commits from '{branch.name}' in {repository.name} to remote?\n\n"
                "This does not create a new commit."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        self._repo_tree.set_push_status(repository.path, branch.name, "Pushing...")
        self.statusBar().showMessage(f"{repository.name}: pushing local commits on '{branch.name}'...")

        def _show_push_result_dialog(result: PushResult) -> None:
            details = result.output or result.error or "No output returned from git push."
            if result.success:
                QMessageBox.information(
                    self,
                    "Push Complete",
                    f"{result.repository.name}: pushed '{result.branch_name}' successfully.\n\n{details}",
                )
            else:
                QMessageBox.warning(
                    self,
                    "Push Failed",
                    f"{result.repository.name}: failed to push '{result.branch_name}'.\n\n{details}",
                )

        def _on_done(result: PushResult) -> None:
            if result.repository.path != repository.path or result.branch_name != branch.name:
                return
            _cleanup()
            _show_push_result_dialog(result)

        self._push_signals.done.connect(_on_done)

        def _cleanup() -> None:
            try:
                self._push_signals.done.disconnect(_on_done)
            except RuntimeError:
                pass

        def worker() -> None:
            result = push_branch_commits(repository, branch)
            self._push_signals.done.emit(result)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _show_push_all_commit_message_dialog(self, dirty_repo_count: int) -> str | None:
        """Prompt for a single commit message used for all dirty repositories."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Push All - Commit Message")
        dialog.resize(560, 280)

        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(
                (
                    f"{dirty_repo_count} repositor{'y has' if dirty_repo_count == 1 else 'ies have'} "
                    "local changes that must be committed before push.\n\n"
                    "Enter one commit message to apply to all changed repositories:"
                )
            )
        )

        msg_edit = QPlainTextEdit(dialog)
        msg_edit.setPlainText(
            f"Update all repositories - {_default_commit_timestamp()}"
        )
        msg_edit.setFixedHeight(120)
        layout.addWidget(msg_edit)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            dialog,
        )
        layout.addWidget(button_box)

        selected_message: str | None = None

        def _accept() -> None:
            nonlocal selected_message
            message = msg_edit.toPlainText().strip()
            if not message:
                QMessageBox.warning(dialog, "Push All", "Please enter a commit message.")
                return
            selected_message = message
            dialog.accept()

        button_box.accepted.connect(_accept)
        button_box.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        return selected_message

    def _show_push_all_results_dialog(
        self,
        rows: list[tuple[str, str, str, str]],
    ) -> None:
        """Show a table summarizing Push All per repository."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Push All Results")
        dialog.resize(980, 560)

        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"Processed {len(rows)} repositories:"))

        table = QTableWidget(dialog)
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["Repository", "Status", "Commit Activity", "Details"])
        table.setRowCount(len(rows))
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        status_colors: dict[str, QColor] = {
            "Pushed (Commits Transferred)": QColor("#dcfce7"),
            "Up To Date (No Commits)": QColor("#dbeafe"),
            "Commit Failed": QColor("#fee2e2"),
            "Push Failed": QColor("#fee2e2"),
            "Skipped": QColor("#fef3c7"),
        }

        for row_index, (repo_name, status, commit_activity, details) in enumerate(rows):
            repo_item = QTableWidgetItem(repo_name)
            status_item = QTableWidgetItem(status)
            status_item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
            )
            activity_item = QTableWidgetItem(commit_activity)
            activity_item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
            )
            details_item = QTableWidgetItem(details)

            row_color = status_colors.get(status)
            if row_color is not None:
                repo_item.setBackground(row_color)
                status_item.setBackground(row_color)
                activity_item.setBackground(row_color)
                details_item.setBackground(row_color)

            table.setItem(row_index, 0, repo_item)
            table.setItem(row_index, 1, status_item)
            table.setItem(row_index, 2, activity_item)
            table.setItem(row_index, 3, details_item)

        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)

        dialog.exec()

    def _confirm_push_all_summary(
        self,
        total_count: int,
        commit_count: int,
        push_only_count: int,
    ) -> bool:
        """Confirm Push All using a readable summary table."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Push All")
        dialog.resize(620, 320)

        layout = QVBoxLayout(dialog)

        summary_label = QLabel("Review the repositories that will be processed before continuing:")
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        table = QTableWidget(dialog)
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Status", "Count"])
        table.setRowCount(3)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)

        summary_rows = [
            ("Repositories with active branches", str(total_count)),
            ("Will commit then push", str(commit_count)),
            ("Will push only (ahead of remote)", str(push_only_count)),
        ]

        for row_index, (label, count) in enumerate(summary_rows):
            label_item = QTableWidgetItem(label)
            count_item = QTableWidgetItem(count)
            count_item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
            )
            table.setItem(row_index, 0, label_item)
            table.setItem(row_index, 1, count_item)

        layout.addWidget(table)

        prompt_label = QLabel("Start Push All?")
        prompt_label.setWordWrap(True)
        layout.addWidget(prompt_label)

        button_box = QDialogButtonBox(dialog)
        start_button = button_box.addButton("Start", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = button_box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        start_button.setDefault(True)
        layout.addWidget(button_box)

        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        start_button.clicked.connect(dialog.accept)
        cancel_button.clicked.connect(dialog.reject)

        return dialog.exec() == QDialog.DialogCode.Accepted

    def _push_output_indicates_up_to_date(self, output: str) -> bool:
        normalized = output.lower()
        return (
            "everything up-to-date" in normalized
            or "everything up to date" in normalized
            or "up to date" in normalized
        )

    def _push_all(self) -> None:
        """Commit dirty repos with one message, then push all active branches."""
        if not self._latest_repositories:
            self.statusBar().showMessage("No repositories to push.")
            return

        repositories_with_active: list[tuple[GitRepository, GitBranch]] = []
        for repository in self._latest_repositories:
            active_branch = next(
                (branch for branch in repository.local_branches if branch.is_current),
                None,
            )
            if active_branch is not None:
                repositories_with_active.append((repository, active_branch))

        if not repositories_with_active:
            self.statusBar().showMessage("No repositories have an active branch to push.")
            return

        dirty_repositories = [
            repository
            for repository, _ in repositories_with_active
            if repository.has_uncommitted_changes
        ]

        commit_count = len(dirty_repositories)
        push_only_count = sum(
            1
            for repository, active_branch in repositories_with_active
            if not repository.has_uncommitted_changes and active_branch.ahead_count > 0
        )
        if not self._confirm_push_all_summary(
            len(repositories_with_active),
            commit_count,
            push_only_count,
        ):
            self.statusBar().showMessage("Push All canceled.")
            return

        commit_message: str | None = None
        if dirty_repositories:
            commit_message = self._show_push_all_commit_message_dialog(len(dirty_repositories))
            if commit_message is None:
                self.statusBar().showMessage("Push All canceled.")
                return

        if self._refresh_action is not None:
            self._refresh_action.setEnabled(False)
        if self._pull_all_action is not None:
            self._pull_all_action.setEnabled(False)
        if self._push_all_action is not None:
            self._push_all_action.setEnabled(False)
        if self._clean_action is not None:
            self._clean_action.setEnabled(False)
        if self._branches_action is not None:
            self._branches_action.setEnabled(False)

        self.statusBar().showMessage(
            f"Push All: processing {len(repositories_with_active)} repositories..."
        )

        def worker() -> None:
            rows: list[tuple[str, str, str, str]] = []
            pushed_count = 0
            commit_failed_count = 0
            push_failed_count = 0
            skipped_count = 0
            no_commits_count = 0

            for repository, active_branch in repositories_with_active:
                self._push_all_signals.progress.emit(
                    f"Push All: {repository.name} ({active_branch.name})..."
                )

                if repository.has_uncommitted_changes:
                    if commit_message is None:
                        skipped_count += 1
                        rows.append(
                            (
                                repository.name,
                                "Skipped",
                                "Not Run",
                                "Local changes detected but no commit message was provided.",
                            )
                        )
                        continue

                    commit_result = commit_local_changes(repository, active_branch, commit_message)
                    if not commit_result.success:
                        commit_failed_count += 1
                        details = commit_result.error or commit_result.output or "Commit failed."
                        rows.append((repository.name, "Commit Failed", "Commit Failed", details))
                        continue
                    created_commit = commit_result.created_commit
                else:
                    created_commit = False

                push_result = push_branch_commits(repository, active_branch)
                if push_result.success:
                    pushed_count += 1
                    push_output = push_result.output or ""
                    if created_commit:
                        status = "Pushed (Commits Transferred)"
                        commit_activity = "Created Commit"
                        details = "Committed local changes and pushed successfully."
                    elif self._push_output_indicates_up_to_date(push_output):
                        no_commits_count += 1
                        status = "Up To Date (No Commits)"
                        commit_activity = "No Commits To Push"
                        details = "No commits to push for this repository."
                    else:
                        status = "Pushed (Commits Transferred)"
                        commit_activity = "Existing Commits Pushed"
                        details = "Pushed existing local commits successfully."
                    rows.append((repository.name, status, commit_activity, details))
                else:
                    push_failed_count += 1
                    details = push_result.error or push_result.output or "Push failed."
                    commit_activity = "Created Commit" if created_commit else "Push Attempt Failed"
                    rows.append((repository.name, "Push Failed", commit_activity, details))

            self._push_all_signals.done.emit(
                {
                    "rows": rows,
                    "pushed_count": pushed_count,
                    "commit_failed_count": commit_failed_count,
                    "push_failed_count": push_failed_count,
                    "skipped_count": skipped_count,
                    "no_commits_count": no_commits_count,
                    "total_count": len(repositories_with_active),
                }
            )

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _on_push_all_progress(self, status: str) -> None:
        self.statusBar().showMessage(status)

    def _on_push_all_done(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return

        rows = payload.get("rows", [])
        pushed_count = int(payload.get("pushed_count", 0))
        commit_failed_count = int(payload.get("commit_failed_count", 0))
        push_failed_count = int(payload.get("push_failed_count", 0))
        skipped_count = int(payload.get("skipped_count", 0))
        no_commits_count = int(payload.get("no_commits_count", 0))
        total_count = int(payload.get("total_count", 0))

        if self._refresh_action is not None:
            self._refresh_action.setEnabled(True)
        if self._pull_all_action is not None:
            self._pull_all_action.setEnabled(True)
        if self._push_all_action is not None:
            self._push_all_action.setEnabled(True)
        if self._clean_action is not None:
            self._clean_action.setEnabled(True)
        if self._branches_action is not None:
            self._branches_action.setEnabled(True)

        self.statusBar().showMessage(
            (
                f"Push All complete: {pushed_count}/{total_count} pushed, "
                f"{no_commits_count} had no commits to push, "
                f"{commit_failed_count} commit failed, "
                f"{push_failed_count} push failed, "
                f"{skipped_count} skipped."
            )
        )

        if isinstance(rows, list) and rows:
            self._show_push_all_results_dialog(rows)

        # Refresh after bulk push so branch sync states stay accurate.
        self._refresh_repositories()

    def _handle_commit_requested(
        self,
        repository: GitRepository | None,
        branch: GitBranch | None,
    ) -> None:
        """Show commit dialog and run commit-only or commit-then-push in the background."""
        if repository is None or branch is None:
            return

        if not branch.is_current:
            QMessageBox.warning(
                self,
                "Commit",
                f"Commit is only available for the active branch.\n\n"
                f"'{branch.name}' is not the currently checked-out branch in {repository.name}.",
            )
            return

        default_message = f"Update {branch.name} - {_default_commit_timestamp()}"

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Commit - {repository.name} / {branch.name}")
        dialog.resize(520, 320)

        layout = QVBoxLayout(dialog)
        info_label = QLabel(
            f"<b>Repository:</b> {repository.name}<br>"
            f"<b>Branch:</b> {branch.name}"
        )
        info_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info_label)

        layout.addWidget(QLabel("Commit message:"))

        msg_edit = QPlainTextEdit(dialog)
        msg_edit.setPlainText(default_message)
        msg_edit.setFixedHeight(80)
        layout.addWidget(msg_edit)

        status_label = QLabel("Commit status:")
        status_label.setVisible(False)
        layout.addWidget(status_label)

        status_output = QTextEdit(dialog)
        status_output.setReadOnly(True)
        status_output.setFixedHeight(100)
        status_output.setVisible(False)
        layout.addWidget(status_output)

        button_box = QDialogButtonBox(dialog)
        commit_btn = button_box.addButton("Commit", QDialogButtonBox.ButtonRole.AcceptRole)
        commit_push_btn = button_box.addButton(
            "Commit and Push",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        cancel_btn = button_box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(button_box)

        has_upstream = bool(branch.upstream and branch.upstream.strip())
        commit_push_btn.setEnabled(has_upstream)
        if not has_upstream:
            commit_push_btn.setToolTip("This branch has no upstream configured.")

        # Prefer commit+push as Enter-key default when the branch tracks an upstream.
        if has_upstream:
            commit_push_btn.setDefault(True)
        else:
            commit_btn.setDefault(True)
        commit_mode: str | None = None

        def _append_status(text: str) -> None:
            status_output.append(text)
            status_output.verticalScrollBar().setValue(
                status_output.verticalScrollBar().maximum()
            )

        def _on_commit_btn(commit_then_push: bool) -> None:
            nonlocal commit_mode
            commit_msg = msg_edit.toPlainText().strip()
            if not commit_msg:
                QMessageBox.warning(dialog, "Commit", "Please enter a commit message.")
                return

            commit_mode = "commit_then_push" if commit_then_push else "commit_only"
            commit_btn.setEnabled(False)
            commit_push_btn.setEnabled(False)
            msg_edit.setEnabled(False)
            cancel_btn.setText("Close")
            status_label.setVisible(True)
            status_output.setVisible(True)
            status_output.clear()
            _append_status(f"Starting local commit for {repository.name} / {branch.name}...")

            def worker() -> None:
                commit_result = commit_local_changes(repository, branch, commit_msg)
                self._commit_signals.done.emit(commit_result)
                if commit_then_push and commit_result.success:
                    push_result = push_branch_commits(repository, branch)
                    self._push_signals.done.emit(push_result)

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()

        def _on_done(result: CommitResult) -> None:
            if result.repository.path != repository.path or result.branch_name != branch.name:
                return

            if result.success and result.created_commit:
                _append_status("\nCommit completed successfully.")
            elif result.success:
                _append_status("\nNothing to commit.")
            else:
                err = result.error or result.output or "Unknown error"
                _append_status(f"\nCommit failed:\n{err}")

            output = result.output.strip()
            if output:
                _append_status(f"\nDetails:\n{output}")

            if commit_mode == "commit_then_push" and result.success:
                if result.created_commit:
                    _append_status("\nStarting push...")
                else:
                    _append_status("\nNothing to commit; attempting push of existing local commits...")
                return

            cancel_btn.setText("Close")
            commit_btn.setEnabled(False)
            commit_push_btn.setEnabled(False)

        def _on_push_done(result: PushResult) -> None:
            if result.repository.path != repository.path or result.branch_name != branch.name:
                return
            if commit_mode != "commit_then_push":
                return

            if result.success:
                _append_status("\nPush completed successfully.")
            else:
                err = result.error or result.output or "Unknown error"
                _append_status(f"\nPush failed:\n{err}")

            output = result.output.strip()
            if output:
                _append_status(f"\nPush details:\n{output}")

            cancel_btn.setText("Close")
            commit_btn.setEnabled(False)
            commit_push_btn.setEnabled(False)

        self._commit_signals.done.connect(_on_done)
        self._push_signals.done.connect(_on_push_done)

        def _cleanup() -> None:
            try:
                self._commit_signals.done.disconnect(_on_done)
            except RuntimeError:
                pass
            try:
                self._push_signals.done.disconnect(_on_push_done)
            except RuntimeError:
                pass

        dialog.finished.connect(lambda _: _cleanup())

        commit_btn.clicked.connect(lambda: _on_commit_btn(False))
        commit_push_btn.clicked.connect(lambda: _on_commit_btn(True))
        button_box.rejected.connect(dialog.reject)

        dialog.exec()

    def _on_push_progress(self, repo_path: Path, branch_name: str, status: str) -> None:
        """Update the tree item with real-time push status (called on GUI thread via signal)."""
        self._repo_tree.set_push_status(repo_path, branch_name, status)
        self.statusBar().showMessage(f"Push: {status}")

    def _on_push_done(self, result: PushResult) -> None:
        """Handle push completion: update status bar and refresh tree."""
        repo = result.repository
        branch_name = result.branch_name
        if result.success:
            final_status = "✓ Pushed"
            self.statusBar().showMessage(
                f"{repo.name}: push of '{branch_name}' completed successfully."
            )
        else:
            err_summary = (result.error or result.output or "unknown error").splitlines()[0]
            final_status = f"✗ Failed"
            self.statusBar().showMessage(
                f"{repo.name}: push of '{branch_name}' failed — {err_summary}"
            )
        self._repo_tree.set_push_status(repo.path, branch_name, final_status)
        # Always perform a full refresh after push completion so branch status
        # reflects current remote state.
        self._pending_selection_repo_path = repo.path
        self._pending_selection_branch_name = branch_name
        self._refresh_repositories()

    def _on_commit_done(self, result: CommitResult) -> None:
        """Handle local commit completion by reporting status and refreshing the tree."""
        repo = result.repository
        branch_name = result.branch_name
        if result.success and result.created_commit:
            summary = (result.output or "Commit created.").splitlines()[0]
            self.statusBar().showMessage(
                f"{repo.name}: commit on '{branch_name}' created - {summary}"
            )
            self._repo_tree.set_push_status(repo.path, branch_name, "Committed")
        elif result.success:
            self.statusBar().showMessage(
                f"{repo.name}: nothing to commit on '{branch_name}'."
            )
            self._repo_tree.set_push_status(repo.path, branch_name, "No changes")
        else:
            err_summary = (result.error or result.output or "unknown error").splitlines()[0]
            self.statusBar().showMessage(
                f"{repo.name}: commit on '{branch_name}' failed - {err_summary}"
            )
            self._repo_tree.set_push_status(repo.path, branch_name, "Commit failed")

        self._scan_directory(self._current_directory, remember_directory=False)
        self._repo_tree.select_repo_branch(repo.path, branch_name)

    def _pull_all(self) -> None:
        if not self._latest_repositories:
            self.statusBar().showMessage("No repositories to pull.")
            return

        repos_with_upstream = [
            repo for repo in self._latest_repositories
            if any(b.is_current and b.upstream for b in repo.local_branches)
        ]

        if not repos_with_upstream:
            self.statusBar().showMessage("No repositories have an active branch tracking an upstream.")
            return

        if self._pull_all_action is not None:
            self._pull_all_action.setEnabled(False)
        if self._push_all_action is not None:
            self._push_all_action.setEnabled(False)
        if self._clean_action is not None:
            self._clean_action.setEnabled(False)

        with self._pull_results_lock:
            self._pull_results.clear()

        remaining = [len(repos_with_upstream)]
        remaining_lock = threading.Lock()

        def on_progress(repo: GitRepository, status: str) -> None:
            self._pull_signals.progress.emit(repo.path, status)

        def worker(repo: GitRepository) -> None:
            result = pull_repository(repo, on_progress)
            with self._pull_results_lock:
                self._pull_results.append(result)
            with remaining_lock:
                remaining[0] -= 1
                done = remaining[0] == 0
            if done:
                self._pull_signals.all_done.emit()

        for repository in repos_with_upstream:
            thread = threading.Thread(
                target=worker,
                args=(repository,),
                daemon=True,
            )
            thread.start()

    def _show_pull_error_dialog(self, title: str, message: str, detail: str) -> None:
        """Show a fixed-size pull-error dialog with a scrollable detail area."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(660, 420)
        dlg.resize(660, 420)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # Icon + message row
        icon_label = QLabel()
        icon_label.setPixmap(
            self.style().standardIcon(
                self.style().StandardPixmap.SP_MessageBoxWarning
            ).pixmap(32, 32)
        )
        msg_label = QLabel(message)
        msg_label.setWordWrap(True)
        msg_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(msg_label, 1)
        layout.addLayout(header)

        # Scrollable detail area
        text_edit = QPlainTextEdit(detail)
        text_edit.setReadOnly(True)
        text_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(text_edit, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)

        dlg.exec()

    def _on_pull_all_complete(self) -> None:
        if self._pull_all_action is not None:
            self._pull_all_action.setEnabled(True)
        if self._push_all_action is not None:
            self._push_all_action.setEnabled(True)
        if self._clean_action is not None:
            self._clean_action.setEnabled(True)

        with self._pull_results_lock:
            results = list(self._pull_results)

        success_count = sum(1 for r in results if r.success)
        failures = [r for r in results if not r.success]
        fail_count = len(failures)
        if fail_count == 0:
            self.statusBar().showMessage(
                f"Pull All complete: {success_count} repositories updated successfully."
            )
        else:
            self.statusBar().showMessage(
                f"Pull All complete: {success_count} succeeded, {fail_count} failed."
            )
            failure_lines = [
                f"{r.repository.name}: {(r.error or r.output or 'unknown error').splitlines()[0]}"
                for r in failures
            ]
            full_details = "\n\n".join(
                f"{r.repository.name}:\n{r.error or r.output or 'unknown error'}"
                for r in failures
            )
            self._show_pull_error_dialog(
                title="Pull All \u2014 Some Repositories Failed",
                message=f"{fail_count} repositor{'y' if fail_count == 1 else 'ies'} failed to pull:\n"
                        + "\n".join(failure_lines),
                detail=full_details,
            )

        # Refresh tree so sync indicators are up-to-date after pulls.
        self._scan_directory(self._current_directory, remember_directory=False)

    def _pull_branch(self) -> None:
        repo = self._selected_repository
        if repo is None:
            return

        current_branch = next(
            (b for b in repo.local_branches if b.is_current and b.upstream), None
        )
        if current_branch is None:
            self.statusBar().showMessage(
                f"{repo.name}: active branch has no upstream configured."
            )
            return

        if self._pull_all_action is not None:
            self._pull_all_action.setEnabled(False)
        if self._push_all_action is not None:
            self._push_all_action.setEnabled(False)
        if self._clean_action is not None:
            self._clean_action.setEnabled(False)

        self._repo_tree.clear_pull_statuses()
        self.statusBar().showMessage(
            f"Pulling {repo.name} ({current_branch.name})…"
        )

        def on_progress(r: GitRepository, status: str) -> None:
            self._pull_branch_signals.progress.emit(r.path, status)

        def worker() -> None:
            result = pull_repository(repo, on_progress)
            self._pull_branch_signals.done.emit(result)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _on_pull_branch_complete(self, result: PullResult) -> None:
        if result.success:
            summary = (result.output or "Already up to date.").splitlines()[0]
            self.statusBar().showMessage(
                f"{result.repository.name}: pull complete — {summary}"
            )
            self._repo_tree.set_pull_status(result.repository.path, "✓ Pulled")
        else:
            error = result.error or result.output or "Unknown pull error"
            err_summary = error.splitlines()[0]
            self.statusBar().showMessage(
                f"{result.repository.name}: pull failed — {err_summary}"
            )
            self._repo_tree.set_pull_status(result.repository.path, "\u2717 Failed")
            self._show_pull_error_dialog(
                title="Pull Failed",
                message=f"Could not pull '{result.repository.name}'.",
                detail=error,
            )

        if self._pull_all_action is not None:
            self._pull_all_action.setEnabled(True)
        if self._push_all_action is not None:
            self._push_all_action.setEnabled(True)
        if self._clean_action is not None:
            self._clean_action.setEnabled(True)

         # Refresh tree so sync indicators (ahead/behind counts) are up-to-date.
        self._scan_directory(self._current_directory, remember_directory=False)
        # Re-select the pulled repo and its active branch so the user lands back on it.
        current_branch = next(
            (b for b in result.repository.local_branches if b.is_current), None
        )
        self._repo_tree.select_repo_branch(
            result.repository.path,
            current_branch.name if current_branch else None,
        )

    def _refresh_recent_menu(self) -> None:
        self._recent_menu.clear()

        if not self._recent_directories:
            empty_action = self._recent_menu.addAction("No saved locations")
            empty_action.setEnabled(False)
            clear_action = self._recent_menu.addAction("Clear Recent")
            clear_action.setEnabled(False)
            return

        for directory in self._recent_directories:
            action = self._recent_menu.addAction(str(directory))
            action.triggered.connect(
                lambda checked=False, path=directory: self._restore_recent_directory(path)
            )

        self._recent_menu.addSeparator()
        clear_action = self._recent_menu.addAction("Clear Recent")
        clear_action.triggered.connect(self._clear_recent_directories)

    def _clear_recent_directories(self) -> None:
        """Clear recent directories and remove their token associations."""
        # Get and remove associations for all recent directories
        for directory in self._recent_directories:
            self._settings.remove_directory_association(directory)

        # Clear the recent directories list
        self._settings.clear_recent_directories()
        self._recent_directories = []
        self._refresh_recent_menu()
        self.statusBar().showMessage("Saved recent directories cleared")

    def _update_directory_display(self) -> None:
        if self._directory_display is None:
            return

        self._directory_display.setText(str(self._current_directory))
        self._directory_display.setCursorPosition(0)

    def _restore_window_state(self) -> None:
        geometry = self._settings.load_window_geometry()
        if geometry is not None:
            self.restoreGeometry(geometry)

        if self._main_splitter is not None:
            main_splitter_sizes = self._settings.load_main_splitter_sizes()
            if main_splitter_sizes is not None:
                self._main_splitter.setSizes(main_splitter_sizes)

        right_splitter_sizes = self._settings.load_right_splitter_sizes()
        if right_splitter_sizes is not None:
            self._right_pane.setSizes(right_splitter_sizes)

        right_content_splitter_sizes = self._settings.load_right_content_splitter_sizes()
        if right_content_splitter_sizes is not None:
            self._right_pane.set_content_splitter_sizes(right_content_splitter_sizes)

        right_commit_column_sizes = self._settings.load_right_commit_column_sizes()
        if right_commit_column_sizes is not None:
            self._right_pane.set_commit_column_sizes(right_commit_column_sizes)

        right_file_column_sizes = self._settings.load_right_file_column_sizes()
        if right_file_column_sizes is not None:
            self._right_pane.set_file_column_sizes(right_file_column_sizes)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._settings.save_window_geometry(self.saveGeometry())
        if self._main_splitter is not None:
            self._settings.save_main_splitter_sizes(self._main_splitter.sizes())
        self._settings.save_right_splitter_sizes(self._right_pane.sizes())
        self._settings.save_right_content_splitter_sizes(self._right_pane.content_splitter_sizes())
        self._settings.save_right_commit_column_sizes(self._right_pane.commit_column_sizes())
        self._settings.save_right_file_column_sizes(self._right_pane.file_column_sizes())
        super().closeEvent(event)

    def _show_settings(self) -> None:
        """Open the Settings dialog with all stored tokens and directory associations."""
        stored_tokens = self._settings.load_github_tokens()
        active_token_name = self._settings.get_active_token_name()
        directory_associations = self._settings.load_directory_token_associations()

        dialog = ConfigDialog(
            stored_tokens=stored_tokens,
            active_token_name=active_token_name,
            directory_associations=directory_associations,
            recent_directories=self._recent_directories,
            parent=self,
        )
        dialog.tokens_saved.connect(self._on_tokens_saved)
        dialog.associations_saved.connect(self._on_associations_saved)
        dialog.exec()

    def _on_tokens_saved(self, tokens_dict: dict[str, str], active_token_name: str) -> None:
        """Persist and apply the updated token configuration."""
        # Remove all old tokens
        old_tokens = self._settings.load_github_tokens()
        for token_name in old_tokens:
            self._settings.save_github_token(token_name, "")  # empty = delete

        # Save all new tokens
        for token_name, token_value in tokens_dict.items():
            self._settings.save_github_token(token_name, token_value)

        # Set active token
        self._settings.set_active_token(active_token_name)

        # Apply to git operations
        active_token = self._settings.get_active_github_token()
        set_github_token(active_token)
        self._update_active_token_display()

        if active_token_name:
            self.statusBar().showMessage(
                f"Token '{active_token_name}' is now active for all git operations."
            )
        else:
            self.statusBar().showMessage(
                "No active token — git operations will use local credentials."
            )

    def _on_associations_saved(self, associations: dict[str, str]) -> None:
        """Persist the updated directory-token associations."""
        # Clear old associations
        old_associations = self._settings.load_directory_token_associations()
        for directory_path in old_associations:
            self._settings.remove_directory_association(Path(directory_path))

        # Save new associations
        for directory_path_str, token_name in associations.items():
            self._settings.save_directory_token_association(
                Path(directory_path_str),
                token_name
            )

        # Immediately apply association for the currently browsed directory,
        # so Clone and remote operations use the expected account context.
        token_name = self._activate_associated_token_for_directory(self._current_directory)
        self._update_active_token_display()
        if token_name:
            self.statusBar().showMessage(
                f"Applied token '{token_name}' for current directory; refreshing repositories..."
            )
            self._refresh_repositories()

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "About QTGit",
            (
                f"QTGit v{self._app_version}\n\n"
                "QTGit is a PySide6 desktop shell for browsing directories, "
                "restoring recent locations, and listing Git repositories."
            ),
        )
