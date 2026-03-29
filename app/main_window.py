from __future__ import annotations

from pathlib import Path
import threading

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QFileDialog,
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
    delete_branch,
    find_git_repositories,
    get_github_token,
    pull_repository,
    push_branch_commits,
    push_repository,
    scan_repositories_live,
    set_github_token,
    sync_active_branch_to_remote,
)
from app.widgets.config_dialog import ConfigDialog
from app.widgets.git_diff_viewer import GitDiffViewerWindow
from app.widgets.remotes_dialog import RemotesDialog
from app.widgets.repo_tree import RepoTreeWidget
from app.widgets.split_pane import RightSplitPane


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


class MainWindow(QMainWindow):
    def __init__(self, start_directory: Path) -> None:
        super().__init__()
        self._settings = AppSettings()
        self._current_directory = self._settings.load_last_directory(start_directory)
        self._recent_directories: list[Path] = self._settings.recent_directories()
        self._repo_tree = RepoTreeWidget()
        self._right_pane = RightSplitPane()
        self._recent_menu = QMenu("Recent", self)
        self._main_splitter: QSplitter | None = None
        self._directory_display: QLineEdit | None = None
        self._refresh_action: QAction | None = None
        self._pull_all_action: QAction | None = None
        self._clean_action: QAction | None = None
        self._latest_repositories: list[GitRepository] = []
        self._selected_repository: GitRepository | None = None
        self._selected_branch: GitBranch | None = None
        self._pull_signals = _PullSignals()
        self._pull_results: list[PullResult] = []
        self._pull_results_lock = threading.Lock()
        self._refresh_signals = _RefreshSignals()
        self._push_signals = _PushSignals()
        self._pull_branch_signals = _PullBranchSignals()
        self._commit_signals = _CommitSignals()
        self._pull_signals.progress.connect(self._repo_tree.set_pull_status)
        self._pull_signals.all_done.connect(self._on_pull_all_complete)
        self._refresh_signals.repo_scanned.connect(self._on_repo_scanned)
        self._refresh_signals.scan_complete.connect(self._on_scan_complete)
        self._push_signals.progress.connect(self._on_push_progress)
        self._push_signals.done.connect(self._on_push_done)
        self._pull_branch_signals.progress.connect(self._repo_tree.set_pull_status)
        self._pull_branch_signals.done.connect(self._on_pull_branch_complete)
        self._commit_signals.done.connect(self._on_commit_done)
        self._repo_tree.selection_changed.connect(self._handle_tree_selection)
        self._repo_tree.branch_double_clicked.connect(self._handle_branch_double_click)
        self._repo_tree.select_all_branches_requested.connect(
            self._handle_select_all_branches
        )
        self._repo_tree.branch_delete_requested.connect(self._handle_branch_delete_requested)
        self._repo_tree.branch_sync_to_remote_requested.connect(self._handle_branch_sync_to_remote_requested)
        self._repo_tree.remotes_requested.connect(self._handle_remotes_requested)
        self._repo_tree.clean_branches_requested.connect(self._handle_clean_branches_requested)
        self._repo_tree.pull_branch_requested.connect(self._handle_pull_branch_requested)
        self._right_pane.file_double_clicked.connect(self._handle_file_double_clicked)
        self._right_pane.commit_requested.connect(self._handle_commit_requested)
        self._right_pane.push_requested.connect(self._handle_push_requested)

        self.setWindowTitle("QTGit")
        self.resize(1280, 780)

        self._build_toolbar()
        self._build_layout()
        self._restore_window_state()
        self._refresh_recent_menu()

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

    def _build_toolbar(self) -> None:
        toolbar = self.addToolBar("Main")
        toolbar.setMovable(False)

        browse_action = QAction("Browse", self)
        browse_action.triggered.connect(self._browse_for_directory)
        toolbar.addAction(browse_action)

        recent_button = QToolButton(self)
        recent_button.setText("Recent")
        recent_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        recent_button.setMenu(self._recent_menu)
        toolbar.addWidget(recent_button)

        self._refresh_action = QAction("Refresh", self)
        self._refresh_action.setToolTip("Re-scan repositories and update sync status")
        self._refresh_action.triggered.connect(self._refresh_repositories)
        toolbar.addAction(self._refresh_action)

        self._pull_all_action = QAction("Pull All", self)
        self._pull_all_action.setToolTip("Pull latest for every repository's active branch (parallel)")
        self._pull_all_action.triggered.connect(self._pull_all)
        toolbar.addAction(self._pull_all_action)


        self._clean_action = QAction("Clean", self)
        self._clean_action.setToolTip(
            "Run Clean Branches for each repository (local only; keeps active and develop)"
        )
        self._clean_action.triggered.connect(self._clean_all_repositories)
        toolbar.addAction(self._clean_action)

        self._directory_display = QLineEdit(self)
        self._directory_display.setReadOnly(True)
        self._directory_display.setMinimumWidth(360)
        self._directory_display.setToolTip("Current browse directory")
        toolbar.addWidget(self._directory_display)

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

    def _refresh_repositories(self) -> None:
        if self._refresh_action is not None:
            self._refresh_action.setEnabled(False)
        if self._pull_all_action is not None:
            self._pull_all_action.setEnabled(False)
        if self._clean_action is not None:
            self._clean_action.setEnabled(False)
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
        self._repo_tree.clear_selection()

        if self._refresh_action is not None:
            self._refresh_action.setEnabled(True)
        if self._pull_all_action is not None:
            self._pull_all_action.setEnabled(True)
        if self._clean_action is not None:
            self._clean_action.setEnabled(True)

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
        self._scan_directory(directory, remember_directory=True)

    def _scan_directory(self, directory: Path, remember_directory: bool) -> None:
        normalized_directory = directory.expanduser().resolve()
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
        self._repo_tree.clear_selection()
        self._right_pane.update_context(normalized_directory, result)

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
            if matching_branch is None:
                continue

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

        if matching_repositories == 0:
            self.statusBar().showMessage(
                f"No repositories contain branch '{target_branch}'."
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

    def _handle_commit_requested(
        self,
        repository: GitRepository | None,
        branch: GitBranch | None,
    ) -> None:
        """Show commit dialog and perform a local commit in the background."""
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

        import datetime
        default_message = f"Update {branch.name} - {datetime.date.today().isoformat()}"

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
        cancel_btn = button_box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(button_box)

        commit_btn.setDefault(True)

        def _append_status(text: str) -> None:
            status_output.append(text)
            status_output.verticalScrollBar().setValue(
                status_output.verticalScrollBar().maximum()
            )

        def _on_commit_btn() -> None:
            commit_msg = msg_edit.toPlainText().strip()
            if not commit_msg:
                QMessageBox.warning(dialog, "Commit", "Please enter a commit message.")
                return

            commit_btn.setEnabled(False)
            msg_edit.setEnabled(False)
            cancel_btn.setText("Close")
            status_label.setVisible(True)
            status_output.setVisible(True)
            status_output.clear()
            _append_status(f"Starting local commit for {repository.name} / {branch.name}...")

            def worker() -> None:
                result = commit_local_changes(repository, branch, commit_msg)
                self._commit_signals.done.emit(result)

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

            cancel_btn.setText("Close")
            commit_btn.setEnabled(False)

        self._commit_signals.done.connect(_on_done)

        def _cleanup() -> None:
            try:
                self._commit_signals.done.disconnect(_on_done)
            except RuntimeError:
                pass

        dialog.finished.connect(lambda _: _cleanup())

        button_box.accepted.connect(_on_commit_btn)
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
        if self._pull_branch_action is not None:
            self._pull_branch_action.setEnabled(False)
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
        """Open the Settings dialog with all stored tokens."""
        stored_tokens = self._settings.load_github_tokens()
        active_token_name = self._settings.get_active_token_name()

        dialog = ConfigDialog(
            stored_tokens=stored_tokens,
            active_token_name=active_token_name,
            parent=self,
        )
        dialog.tokens_saved.connect(self._on_tokens_saved)
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

        if active_token_name:
            self.statusBar().showMessage(
                f"Token '{active_token_name}' is now active for all git operations."
            )
        else:
            self.statusBar().showMessage(
                "No active token — git operations will use local credentials."
            )

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "About QTGit",
            "QTGit is a PySide6 desktop shell for browsing directories, restoring recent locations, and listing Git repositories.",
        )