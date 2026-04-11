from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QPoint, Signal, QModelIndex
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygon,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import QMenu, QStyle, QTreeView, QVBoxLayout, QWidget

from app.services.repo_scanner import GitBranch, GitRepository


ITEM_KIND_ROLE = Qt.ItemDataRole.UserRole + 1
REPOSITORY_ROLE = Qt.ItemDataRole.UserRole + 2
BRANCH_ROLE = Qt.ItemDataRole.UserRole + 3
PULL_STATUS_ROLE = Qt.ItemDataRole.UserRole + 4
PUSH_STATUS_ROLE = Qt.ItemDataRole.UserRole + 5


class RepoTreeWidget(QWidget):
    selection_changed = Signal(object, object)
    branch_double_clicked = Signal(object, object)
    select_all_branches_requested = Signal(object, object)
    branch_delete_requested = Signal(object, object, bool)  # (repository, branch, force)
    remove_all_local_branches_requested = Signal(object, object)  # (repository, branch)
    branch_sync_to_remote_requested = Signal(object, object)  # (repository, branch)
    branch_select_active_requested = Signal(object, object)  # (repository, branch)
    remotes_requested = Signal(object)  # (repository)
    clean_branches_requested = Signal(object)  # (repository)
    pull_branch_requested = Signal(object)  # (repository)
    delete_local_repository_requested = Signal(object)  # (repository)

    def __init__(self) -> None:
        super().__init__()
        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(["Git Repositories"])
        self._root_directory: Path | None = None
        self._dimmed_icon_cache: dict[tuple[int, float], QIcon] = {}

        self._tree = QTreeView(self)
        self._tree.setModel(self._model)
        self._tree.setHeaderHidden(False)
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(False)
        self._tree.setMinimumWidth(260)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.selectionModel().currentChanged.connect(self._handle_current_changed)
        self._tree.doubleClicked.connect(self._handle_double_clicked)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)

        self._repo_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self._active_branch_dirty_icon = self._create_left_arrow_icon("#1565c0")  # Blue left arrow
        self._active_branch_dirty_ahead_icon = self._create_left_up_arrows_icon("#1565c0")  # Blue left arrow + blue up arrow
        self._active_branch_dirty_behind_icon = self._create_left_down_arrows_icon("#1565c0", "#c62828")  # Blue left arrow + red down arrow
        self._active_branch_dirty_diverged_icon = self._create_left_up_down_arrows_icon("#1565c0", "#c62828")  # Blue left + blue up + red down
        self._active_branch_in_sync_icon = self._create_circle_icon("#2e7d32")  # Green circle
        self._active_branch_behind_icon = self._create_down_arrow_icon("#c62828")  # Red down arrow
        self._active_branch_ahead_icon = self._create_up_arrow_icon("#1565c0")  # Blue up arrow
        self._active_branch_diverged_icon = self._create_both_arrows_icon("#1565c0", "#c62828")  # Both arrows
        self._active_branch_unknown_icon = self._create_circle_icon("#616161")  # Gray circle
        self._local_branch_icon = self._create_circle_icon("#bdbdbd")  # Light gray circle

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._tree)

        self._show_empty_state("Browse for a directory to scan for Git repositories.")

    def set_root_directory(self, directory: Path) -> None:
        self._root_directory = directory

    def clear_selection(self) -> None:
        """Clear any tree selection so no repository/branch is active."""
        selection_model = self._tree.selectionModel()
        if selection_model is None:
            return

        selection_model.clearSelection()
        selection_model.setCurrentIndex(
            QModelIndex(),
            selection_model.SelectionFlag.NoUpdate,
        )
        self.selection_changed.emit(None, None)

    def begin_live_scan(self, directory: Path) -> None:
        """Clear the tree and show a placeholder while a background scan runs."""
        self._root_directory = directory
        self._model.removeRows(0, self._model.rowCount())
        item = QStandardItem("Scanning repositories\u2026")
        item.setEditable(False)
        item.setSelectable(False)
        item.setData("placeholder", ITEM_KIND_ROLE)
        self._model.appendRow(item)
        self.clear_selection()

    def add_repository(self, repository: GitRepository) -> None:
        """Append one repository in sorted order; called live during a background scan."""
        # Remove the initial 'Scanning…' placeholder on first real result.
        if self._model.rowCount() == 1:
            first = self._model.item(0)
            if first is not None and first.data(ITEM_KIND_ROLE) == "placeholder":
                self._model.removeRow(0)

        repo_item = QStandardItem(repository.name)
        repo_item.setEditable(False)
        repo_item.setToolTip(str(repository.path))
        repo_item.setIcon(self._repo_icon)
        repo_item.setData("repo", ITEM_KIND_ROLE)
        repo_item.setData(repository, REPOSITORY_ROLE)

        if not repository.local_branches:
            empty_item = QStandardItem("No local branches")
            empty_item.setEditable(False)
            empty_item.setSelectable(False)
            empty_item.setToolTip(str(repository.path))
            empty_item.setData("placeholder", ITEM_KIND_ROLE)
            repo_item.appendRow(empty_item)
        else:
            repo_branch_icon = self._repository_branch_icon(repository)
            for branch in repository.local_branches:
                repo_item.appendRow(self._build_branch_item(branch, repository, repo_branch_icon))

        # Insert at the correct alphabetical position.
        key = (repository.name.lower(), str(repository.path).lower())
        insert_row = self._model.rowCount()
        for row in range(self._model.rowCount()):
            existing = self._model.item(row)
            if existing is None:
                continue
            existing_repo: GitRepository | None = existing.data(REPOSITORY_ROLE)
            if existing_repo is None:
                continue
            if key < (existing_repo.name.lower(), str(existing_repo.path).lower()):
                insert_row = row
                break

        self._model.insertRow(insert_row, repo_item)
        self._tree.expand(self._model.indexFromItem(repo_item))
        self._tree.resizeColumnToContents(0)

    def set_pull_status(self, repository_path: Path, status: str) -> None:
        """Update the pull-status label for the matching repo row (thread-safe via Qt signal)."""
        for row in range(self._model.rowCount()):
            item = self._model.item(row)
            if item is None:
                continue
            repo: GitRepository | None = item.data(REPOSITORY_ROLE)
            if repo is not None and repo.path == repository_path:
                item.setData(status, PULL_STATUS_ROLE)
                self._refresh_repo_label(item)
                break

    def clear_pull_statuses(self) -> None:
        for row in range(self._model.rowCount()):
            item = self._model.item(row)
            if item is None:
                continue
            item.setData(None, PULL_STATUS_ROLE)
            self._refresh_repo_label(item)

    def select_repo_branch(self, repo_path: Path, branch_name: str | None) -> None:
        """Select and scroll to the item for repo_path, highlighting branch_name if given."""
        for row in range(self._model.rowCount()):
            repo_item = self._model.item(row)
            if repo_item is None:
                continue
            repo: GitRepository | None = repo_item.data(REPOSITORY_ROLE)
            if repo is None or repo.path != repo_path:
                continue
            # Found the repo — try to select the specific branch child first
            if branch_name is not None:
                for child_row in range(repo_item.rowCount()):
                    branch_item = repo_item.child(child_row)
                    if branch_item is None:
                        continue
                    branch: GitBranch | None = branch_item.data(BRANCH_ROLE)
                    if branch is not None and branch.name == branch_name:
                        index = self._model.indexFromItem(branch_item)
                        self._tree.setCurrentIndex(index)
                        self._tree.scrollTo(index)
                        return
            # Fall back to selecting the repo row itself
            index = self._model.indexFromItem(repo_item)
            self._tree.setCurrentIndex(index)
            self._tree.scrollTo(index)
            return

    def set_push_status(self, repository_path: Path, branch_name: str, status: str) -> None:
        """Update the push-status label for the matching branch row (thread-safe via Qt signal)."""
        for row in range(self._model.rowCount()):
            repo_item = self._model.item(row)
            if repo_item is None:
                continue
            repo: GitRepository | None = repo_item.data(REPOSITORY_ROLE)
            if repo is None or repo.path != repository_path:
                continue
            # Update repo-level label too so status is always visible
            repo_item.setData(status, PUSH_STATUS_ROLE)
            self._refresh_repo_label(repo_item)
            # Also update the specific branch child item
            for child_row in range(repo_item.rowCount()):
                branch_item = repo_item.child(child_row)
                if branch_item is None:
                    continue
                branch: GitBranch | None = branch_item.data(BRANCH_ROLE)
                if branch is not None and branch.name == branch_name:
                    branch_item.setData(status, PUSH_STATUS_ROLE)
                    self._refresh_branch_label(branch_item)
            break

    def clear_push_statuses(self) -> None:
        """Clear push-status labels from all repo and branch items."""
        for row in range(self._model.rowCount()):
            repo_item = self._model.item(row)
            if repo_item is None:
                continue
            repo_item.setData(None, PUSH_STATUS_ROLE)
            self._refresh_repo_label(repo_item)
            for child_row in range(repo_item.rowCount()):
                branch_item = repo_item.child(child_row)
                if branch_item is not None:
                    branch_item.setData(None, PUSH_STATUS_ROLE)
                    self._refresh_branch_label(branch_item)

    def _refresh_repo_label(self, item: QStandardItem) -> None:
        repo: GitRepository | None = item.data(REPOSITORY_ROLE)
        if repo is None:
            return
        pull_status: str | None = item.data(PULL_STATUS_ROLE)
        push_status: str | None = item.data(PUSH_STATUS_ROLE)
        parts = [repo.name]
        if pull_status:
            parts.append(f"[{pull_status}]")
        if push_status:
            parts.append(f"[Push: {push_status}]")
        item.setText("  ".join(parts))
        self._tree.resizeColumnToContents(0)

    def _refresh_branch_label(self, item: QStandardItem) -> None:
        branch: GitBranch | None = item.data(BRANCH_ROLE)
        if branch is None:
            return
        push_status: str | None = item.data(PUSH_STATUS_ROLE)
        label = branch.name if not push_status else f"{branch.name}  [Push: {push_status}]"
        item.setText(label)
        self._tree.resizeColumnToContents(0)

    def set_repositories(self, repositories: list[GitRepository]) -> None:
        self._model.removeRows(0, self._model.rowCount())

        if not repositories:
            if self._root_directory is None:
                self._show_empty_state("Browse for a directory to scan for Git repositories.")
            else:
                self._show_empty_state(f"No Git repositories found in {self._root_directory}")
            return

        for repository in repositories:
            repo_item = QStandardItem(repository.name)
            repo_item.setEditable(False)
            repo_item.setToolTip(str(repository.path))
            repo_item.setIcon(self._repo_icon)
            repo_item.setData("repo", ITEM_KIND_ROLE)
            repo_item.setData(repository, REPOSITORY_ROLE)

            if not repository.local_branches:
                empty_item = QStandardItem("No local branches")
                empty_item.setEditable(False)
                empty_item.setSelectable(False)
                empty_item.setToolTip(str(repository.path))
                empty_item.setData("placeholder", ITEM_KIND_ROLE)
                repo_item.appendRow(empty_item)
            else:
                repo_branch_icon = self._repository_branch_icon(repository)
                for branch in repository.local_branches:
                    repo_item.appendRow(self._build_branch_item(branch, repository, repo_branch_icon))

            self._model.appendRow(repo_item)

        self._tree.expandAll()
        self._tree.resizeColumnToContents(0)
        self.clear_selection()

    def _show_empty_state(self, message: str) -> None:
        self._model.removeRows(0, self._model.rowCount())
        item = QStandardItem(message)
        item.setEditable(False)
        item.setSelectable(False)
        item.setData("placeholder", ITEM_KIND_ROLE)
        self._model.appendRow(item)

    def _build_branch_item(
        self,
        branch: GitBranch,
        repository: GitRepository,
        icon: QIcon,
    ) -> QStandardItem:
        branch_label = branch.name

        item = QStandardItem(branch_label)
        item.setEditable(False)
        item.setToolTip(f"{repository.path} :: {branch.name}")
        item.setData("branch", ITEM_KIND_ROLE)
        item.setData(repository, REPOSITORY_ROLE)
        item.setData(branch, BRANCH_ROLE)

        # Active branches use the sync status icon; non-active branches use light gray circle
        if branch.is_current:
            item.setIcon(icon)
        else:
            item.setIcon(self._local_branch_icon)

        if branch.is_current:
            font = QFont(item.font())
            font.setBold(True)
            item.setFont(font)
            item.setForeground(QBrush(QColor("#0b6e4f")))

        return item

    def _handle_current_changed(self, current, previous) -> None:
        del previous

        if not current.isValid():
            self.selection_changed.emit(None, None)
            return

        item = self._model.itemFromIndex(current)
        item_kind = item.data(ITEM_KIND_ROLE)
        if item_kind == "repo":
            self.selection_changed.emit(item.data(REPOSITORY_ROLE), None)
            return

        if item_kind == "branch":
            self.selection_changed.emit(
                item.data(REPOSITORY_ROLE),
                item.data(BRANCH_ROLE),
            )
            return

        self.selection_changed.emit(None, None)

    def _handle_double_clicked(self, index) -> None:
        if not index.isValid():
            return

        item = self._model.itemFromIndex(index)
        if item.data(ITEM_KIND_ROLE) != "branch":
            return

        self.branch_double_clicked.emit(
            item.data(REPOSITORY_ROLE),
            item.data(BRANCH_ROLE),
        )

    def _show_context_menu(self, position) -> None:
        index = self._tree.indexAt(position)
        if not index.isValid():
            return

        item = self._model.itemFromIndex(index)
        item_kind = item.data(ITEM_KIND_ROLE)

        # Handle context menu for repositories
        if item_kind == "repo":
            repository: GitRepository | None = item.data(REPOSITORY_ROLE)
            if repository is None:
                return

            menu = QMenu(self)

            # ...existing code...
            active_branch = next(
                (b for b in repository.local_branches if b.is_current), None
            )
            if active_branch is not None and active_branch.upstream:
                pull_action = menu.addAction("Pull Branch")
                pull_action.triggered.connect(
                    lambda checked=False, repo=repository: self.pull_branch_requested.emit(repo)
                )
                menu.addSeparator()

            action = menu.addAction("Remotes")
            action.triggered.connect(
                lambda checked=False, repo=repository: self.remotes_requested.emit(repo)
            )
            menu.addSeparator()
            clean_action = menu.addAction("Clean Branches")
            clean_action.triggered.connect(
                lambda checked=False, repo=repository: self.clean_branches_requested.emit(repo)
            )
            menu.addSeparator()
            delete_repo_action = menu.addAction("Delete Local Repository")
            delete_repo_action.triggered.connect(
                lambda checked=False, repo=repository: self.delete_local_repository_requested.emit(repo)
            )
            menu.exec(self._tree.viewport().mapToGlobal(position))
            return

        # Handle context menu for branches
        if item_kind != "branch":
            return

        repository: GitRepository | None = item.data(REPOSITORY_ROLE)
        branch: GitBranch | None = item.data(BRANCH_ROLE)
        if repository is None or branch is None:
            return

        menu = QMenu(self)
        
        action = menu.addAction("Select All Branches")
        action.triggered.connect(
            lambda checked=False, repo=repository, selected_branch=branch: self.select_all_branches_requested.emit(
                repo,
                selected_branch,
            )
        )

        if not branch.is_current:
            select_active_action = menu.addAction("Select Active")
            select_active_action.triggered.connect(
                lambda checked=False, repo=repository, selected_branch=branch: self.branch_select_active_requested.emit(
                    repo,
                    selected_branch,
                )
            )

        menu.addSeparator()

        if branch.is_current:


            if branch.upstream:
                pull_action = menu.addAction("Pull Branch")
                pull_action.triggered.connect(
                    lambda checked=False, repo=repository: self.pull_branch_requested.emit(repo)
                )

            sync_action = menu.addAction("Sync to Remote")
            sync_action.triggered.connect(
                lambda checked=False, repo=repository, selected_branch=branch: self.branch_sync_to_remote_requested.emit(
                    repo,
                    selected_branch,
                )
            )
            menu.addSeparator()

        delete_action = menu.addAction("Delete Local Branch")
        delete_action.triggered.connect(
            lambda checked=False, repo=repository, selected_branch=branch: self.branch_delete_requested.emit(
                repo,
                selected_branch,
                False,
            )
        )

        remove_all_action = menu.addAction("Remove All Local Branches")
        remove_all_action.triggered.connect(
            lambda checked=False, repo=repository, selected_branch=branch: self.remove_all_local_branches_requested.emit(
                repo,
                selected_branch,
            )
        )
        
        menu.exec(self._tree.viewport().mapToGlobal(position))

    def _branch_icon(self, sync_status: str | None) -> QIcon:
        if sync_status == "in_sync":
            return self._active_branch_in_sync_icon

        if sync_status == "behind":
            return self._active_branch_behind_icon

        if sync_status == "ahead":
            return self._active_branch_ahead_icon

        if sync_status == "diverged":
            return self._active_branch_diverged_icon

        return self._active_branch_unknown_icon

    def _repository_branch_icon(self, repository: GitRepository) -> QIcon:
        active_branch = next(
            (branch for branch in repository.local_branches if branch.is_current),
            None,
        )
        if active_branch is None:
            return self._local_branch_icon

        if repository.has_uncommitted_changes and active_branch.sync_status == "ahead":
            return self._active_branch_dirty_ahead_icon

        if repository.has_uncommitted_changes and active_branch.sync_status == "behind":
            return self._active_branch_dirty_behind_icon

        if repository.has_uncommitted_changes and active_branch.sync_status == "diverged":
            return self._active_branch_dirty_diverged_icon

        if repository.has_uncommitted_changes:
            return self._active_branch_dirty_icon

        return self._branch_icon(active_branch.sync_status)

    def _cached_icon_with_opacity(self, icon: QIcon, opacity: float) -> QIcon:
        key = (icon.cacheKey(), opacity)
        cached = self._dimmed_icon_cache.get(key)
        if cached is not None:
            return cached

        cached = self._icon_with_opacity(icon, opacity)
        self._dimmed_icon_cache[key] = cached
        return cached

    def _icon_with_opacity(self, icon: QIcon, opacity: float) -> QIcon:
        base = icon.pixmap(14, 14)
        dimmed = QPixmap(base.size())
        dimmed.fill(Qt.GlobalColor.transparent)

        painter = QPainter(dimmed)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(opacity)
        painter.drawPixmap(0, 0, base)
        painter.end()

        return QIcon(dimmed)

    def _create_circle_icon(self, color: str) -> QIcon:
        icon_size = 14
        circle_diameter = 10
        offset = (icon_size - circle_diameter) // 2

        pixmap = QPixmap(icon_size, icon_size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(QColor(color)))
        painter.drawEllipse(offset, offset, circle_diameter, circle_diameter)
        painter.end()

        return QIcon(pixmap)

    def _create_left_arrow_icon(self, color: str) -> QIcon:
        """Create a left-pointing line with triangle arrow tip icon."""
        icon_size = 14
        pixmap = QPixmap(icon_size, icon_size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(color), 1.5))
        painter.setBrush(QBrush(QColor(color)))

        center_y = icon_size // 2
        tip_x = 2        # leftmost point of arrowhead
        base_x = 6       # base of arrowhead
        arrow_half = 3   # half-height of arrowhead

        # Horizontal line (shaft) from right edge to base of arrowhead
        painter.drawLine(11, center_y, base_x, center_y)

        # Triangle arrowhead pointing left
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        triangle = QPolygon([
            QPoint(tip_x, center_y),
            QPoint(base_x, center_y - arrow_half),
            QPoint(base_x, center_y + arrow_half),
        ])
        painter.drawPolygon(triangle)
        painter.end()

        return QIcon(pixmap)

    def _create_left_up_arrows_icon(self, color: str) -> QIcon:
        """Create a left arrow + up arrow icon (uncommitted changes + ahead).

        Left side: horizontal left arrow for local uncommitted changes.
        Right side: vertical up arrow for local commits ahead of upstream.
        """
        icon_size = 14
        pixmap = QPixmap(icon_size, icon_size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(color), 1.5))
        painter.setBrush(QBrush(QColor(color)))

        center_y = icon_size // 2
        left_tip_x = 1
        left_base_x = 5
        left_shaft_end = 6
        left_arrow_half = 2

        painter.drawLine(left_shaft_end, center_y, left_base_x, center_y)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(QPolygon([
            QPoint(left_tip_x, center_y),
            QPoint(left_base_x, center_y - left_arrow_half),
            QPoint(left_base_x, center_y + left_arrow_half),
        ]))

        shaft_top = 5
        shaft_bottom = 11
        tip_y = 2
        arrow_half = 2
        right_x = 10
        painter.setPen(QPen(QColor(color), 1.5))
        painter.drawLine(right_x, shaft_bottom, right_x, shaft_top)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(QPolygon([
            QPoint(right_x, tip_y),
            QPoint(right_x - arrow_half, shaft_top),
            QPoint(right_x + arrow_half, shaft_top),
        ]))
        painter.end()

        return QIcon(pixmap)

    def _create_left_down_arrows_icon(self, left_color: str, down_color: str) -> QIcon:
        """Create a left arrow + down arrow icon (uncommitted local changes + behind remote).

        Left side: horizontal left arrow (blue) for local uncommitted changes.
        Right side: vertical down arrow (red) for behind remote.
        """
        icon_size = 14
        pixmap = QPixmap(icon_size, icon_size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # --- Left arrow on the left half (local uncommitted changes) ---
        center_y = icon_size // 2
        left_tip_x = 1      # leftmost tip of arrowhead
        left_base_x = 5     # base of arrowhead / end of shaft
        left_shaft_end = 6  # right end of shaft
        left_arrow_half = 2

        painter.setPen(QPen(QColor(left_color), 1.5))
        painter.setBrush(QBrush(QColor(left_color)))
        painter.drawLine(left_shaft_end, center_y, left_base_x, center_y)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(QPolygon([
            QPoint(left_tip_x, center_y),
            QPoint(left_base_x, center_y - left_arrow_half),
            QPoint(left_base_x, center_y + left_arrow_half),
        ]))

        # --- Down arrow on the right half (behind remote) ---
        right_x = 10
        down_shaft_top = 2
        down_shaft_bottom = 8   # shaft ends at base of arrowhead
        down_tip_y = 12         # bottommost point of arrowhead
        down_arrow_half = 2

        painter.setPen(QPen(QColor(down_color), 1.5))
        painter.setBrush(QBrush(QColor(down_color)))
        painter.drawLine(right_x, down_shaft_top, right_x, down_shaft_bottom)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(QPolygon([
            QPoint(right_x, down_tip_y),
            QPoint(right_x - down_arrow_half, down_shaft_bottom),
            QPoint(right_x + down_arrow_half, down_shaft_bottom),
        ]))
        painter.end()

        return QIcon(pixmap)

    def _create_left_up_down_arrows_icon(self, blue_color: str, red_color: str) -> QIcon:
        """Create a left arrow + up arrow + down arrow icon.

        Local uncommitted changes + local committed commits (ahead) + behind remote (diverged+dirty).
        Left column:   blue horizontal left arrow  (uncommitted changes)
        Middle column: blue vertical up arrow       (local commits ahead)
        Right column:  red vertical down arrow      (behind remote)
        """
        icon_size = 14
        pixmap = QPixmap(icon_size, icon_size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # --- Blue left arrow (leftmost, x≈2) ---
        center_y = icon_size // 2
        l_tip_x   = 1
        l_base_x  = 4
        l_shaft_x = 5
        l_half    = 2

        painter.setPen(QPen(QColor(blue_color), 1.5))
        painter.setBrush(QBrush(QColor(blue_color)))
        painter.drawLine(l_shaft_x, center_y, l_base_x, center_y)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(QPolygon([
            QPoint(l_tip_x,       center_y),
            QPoint(l_base_x, center_y - l_half),
            QPoint(l_base_x, center_y + l_half),
        ]))

        # --- Blue up arrow (middle, x=8) ---
        mid_x          = 8
        up_shaft_bottom = 11
        up_shaft_top    = 6
        up_tip_y        = 2
        up_half         = 2

        painter.setPen(QPen(QColor(blue_color), 1.5))
        painter.setBrush(QBrush(QColor(blue_color)))
        painter.drawLine(mid_x, up_shaft_bottom, mid_x, up_shaft_top)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(QPolygon([
            QPoint(mid_x,              up_tip_y),
            QPoint(mid_x - up_half, up_shaft_top),
            QPoint(mid_x + up_half, up_shaft_top),
        ]))

        # --- Red down arrow (rightmost, x=12) ---
        right_x         = 12
        dn_shaft_top    = 2
        dn_shaft_bottom = 8
        dn_tip_y        = 12
        dn_half         = 2

        painter.setPen(QPen(QColor(red_color), 1.5))
        painter.setBrush(QBrush(QColor(red_color)))
        painter.drawLine(right_x, dn_shaft_top, right_x, dn_shaft_bottom)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(QPolygon([
            QPoint(right_x,             dn_tip_y),
            QPoint(right_x - dn_half, dn_shaft_bottom),
            QPoint(right_x + dn_half, dn_shaft_bottom),
        ]))
        painter.end()

        return QIcon(pixmap)

    def _create_down_arrow_icon(self, color: str) -> QIcon:
        """Create a downward line with triangle arrow tip icon (red for behind)."""
        icon_size = 14
        pixmap = QPixmap(icon_size, icon_size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(color), 1.5))
        painter.setBrush(QBrush(QColor(color)))

        center_x = icon_size // 2
        shaft_top = 2
        shaft_bottom = 9  # shaft ends at base of arrowhead
        tip_y = 12        # bottommost point of arrowhead
        arrow_half = 3    # half-width of arrowhead

        # Vertical line (shaft)
        painter.drawLine(center_x, shaft_top, center_x, shaft_bottom)

        # Triangle arrowhead pointing down
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(QPolygon([
            QPoint(center_x, tip_y),
            QPoint(center_x - arrow_half, shaft_bottom),
            QPoint(center_x + arrow_half, shaft_bottom),
        ]))
        painter.end()

        return QIcon(pixmap)

    def _create_up_arrow_icon(self, color: str) -> QIcon:
        """Create an upward line with triangle arrow tip icon (blue for ahead)."""
        icon_size = 14
        pixmap = QPixmap(icon_size, icon_size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(color), 1.5))
        painter.setBrush(QBrush(QColor(color)))

        center_x = icon_size // 2
        shaft_bottom = 11
        shaft_top = 5    # shaft ends at base of arrowhead
        tip_y = 2        # topmost point of arrowhead
        arrow_half = 3   # half-width of arrowhead

        # Vertical line (shaft)
        painter.drawLine(center_x, shaft_bottom, center_x, shaft_top)

        # Triangle arrowhead pointing up
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(QPolygon([
            QPoint(center_x, tip_y),
            QPoint(center_x - arrow_half, shaft_top),
            QPoint(center_x + arrow_half, shaft_top),
        ]))
        painter.end()

        return QIcon(pixmap)

    def _create_both_arrows_icon(self, up_color: str, down_color: str) -> QIcon:
        """Create a both-arrows icon (up and down for diverged).

        Displays vertical lines with triangle tips on the left (blue up) and right (red down)
        for clear visual distinction of diverged state.
        """
        icon_size = 14
        pixmap = QPixmap(icon_size, icon_size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        arrow_half = 2

        # Position left and right lines
        left_x = 3
        right_x = 11

        # Up arrow on left (blue)
        painter.setPen(QPen(QColor(up_color), 1.5))
        painter.setBrush(QBrush(QColor(up_color)))
        painter.drawLine(left_x, 11, left_x, 6)
        # Triangle arrowhead pointing up
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(QPolygon([
            QPoint(left_x, 2),
            QPoint(left_x - arrow_half, 6),
            QPoint(left_x + arrow_half, 6),
        ]))

        # Down arrow on right (red)
        painter.setPen(QPen(QColor(down_color), 1.5))
        painter.setBrush(QBrush(QColor(down_color)))
        painter.drawLine(right_x, 3, right_x, 8)
        # Triangle arrowhead pointing down
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawPolygon(QPolygon([
            QPoint(right_x, 12),
            QPoint(right_x - arrow_half, 8),
            QPoint(right_x + arrow_half, 8),
        ]))
        painter.end()

        return QIcon(pixmap)

    def _create_split_circle_icon(self, left_color: str, right_color: str) -> QIcon:
        """Deprecated: use _create_both_arrows_icon instead."""
        return self._create_both_arrows_icon(left_color, right_color)

