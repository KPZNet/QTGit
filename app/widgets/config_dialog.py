from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QTabWidget,
)

from app.widgets.directory_token_assoc import DirectoryTokenAssociationWidget



class ConfigDialog(QDialog):
    """Application settings dialog with multi-token support and directory associations.

    Users can:
    - Add new GitHub tokens with custom names
    - Select which token is "active" (used for all git operations)
    - Test tokens to verify they work
    - Delete tokens
    - Show/hide token values for security
    - Associate tokens with recent directories
    """

    #: Emitted when tokens are modified and user clicks Save.
    #: Carries dict of {token_name: token_value} for all stored tokens,
    #: and the name of the active token.
    tokens_saved = Signal(dict, str)  # (tokens_dict, active_token_name)

    #: Emitted when directory associations are modified.
    #: Carries dict of {directory_path_str: token_name}
    associations_saved = Signal(dict)  # (directory_associations)

    def __init__(
        self,
        stored_tokens: dict[str, str] | None = None,
        active_token_name: str = "",
        directory_associations: dict[str, str] | None = None,
        recent_directories: list[Path] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(720)
        self.setMinimumHeight(550)
        self.setModal(True)

        self._stored_tokens: dict[str, str] = stored_tokens or {}
        self._active_token_name: str = active_token_name or ""
        self._working_tokens: dict[str, str] = dict(self._stored_tokens)
        self._working_active_token: str = self._active_token_name

        self._directory_associations: dict[str, str] = directory_associations or {}
        self._working_associations: dict[str, str] = dict(self._directory_associations)
        self._recent_directories: list[Path] = recent_directories or []

        self._token_list: QListWidget
        self._token_input: QLineEdit
        self._token_name_input: QLineEdit
        self._status_label: QLabel
        self._detail_box: QTextEdit
        self._show_btn: QPushButton
        self._assoc_widget: DirectoryTokenAssociationWidget | None = None

        self._build_ui()
        self._refresh_token_list()


    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        # Create tab widget
        tabs = QTabWidget()

        # Tab 1: GitHub Tokens
        tokens_tab = self._build_tokens_tab()
        tabs.addTab(tokens_tab, "GitHub Tokens")

        # Tab 2: Directory Associations
        assoc_tab = self._build_associations_tab()
        tabs.addTab(assoc_tab, "Directory-Token Links")

        root.addWidget(tabs)

        # Buttons at bottom
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_save)
        button_box.rejected.connect(self.reject)
        root.addWidget(button_box)

    def _build_tokens_tab(self) -> QWidget:
        """Build the GitHub tokens management tab."""
        widget = QWidget()
        root = QVBoxLayout(widget)
        root.setSpacing(12)

        header = QLabel("<b>GitHub Tokens</b>")
        root.addWidget(header)

        desc = QLabel(
            "Store multiple GitHub Personal Access Tokens and select which one to use. "
            "Tokens are stored securely in your system keychain.<br>"
            "The <b>active token</b> is used for all git operations (fetch, pull, push, etc.)."
        )
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(desc)

        list_label = QLabel("<b>Stored Tokens:</b>")
        root.addWidget(list_label)

        self._token_list = QListWidget()
        self._token_list.itemSelectionChanged.connect(self._on_token_selected)
        self._token_list.itemDoubleClicked.connect(self._on_token_double_clicked)
        self._token_list.setMinimumHeight(120)
        root.addWidget(self._token_list)

        mgmt_row = QHBoxLayout()
        mgmt_row.setSpacing(6)

        set_active_btn = QPushButton("Set Active")
        set_active_btn.setToolTip("Use the selected token for all git operations")
        set_active_btn.clicked.connect(self._on_set_active)
        mgmt_row.addWidget(set_active_btn)

        test_btn = QPushButton("Test Selected")
        test_btn.setToolTip("Verify the selected token against GitHub API")
        test_btn.clicked.connect(self._on_test_selected)
        mgmt_row.addWidget(test_btn)

        delete_btn = QPushButton("Delete")
        delete_btn.setToolTip("Delete the selected token")
        delete_btn.clicked.connect(self._on_delete_selected)
        mgmt_row.addWidget(delete_btn)

        mgmt_row.addStretch()
        root.addLayout(mgmt_row)

        add_label = QLabel("<b>Add New Token:</b>")
        root.addWidget(add_label)

        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name_row.addWidget(QLabel("Name:"))
        self._token_name_input = QLineEdit()
        self._token_name_input.setPlaceholderText("e.g., 'work', 'personal', 'github-bot'")
        self._token_name_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        name_row.addWidget(self._token_name_input)
        root.addLayout(name_row)

        token_row = QHBoxLayout()
        token_row.setSpacing(6)
        token_row.addWidget(QLabel("Token:"))
        self._token_input = QLineEdit()
        self._token_input.setPlaceholderText("ghp_xxxxxxxxxxxxxxxxxxxx  or  github_pat_…")
        self._token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        token_row.addWidget(self._token_input)

        self._show_btn = QPushButton("Show")
        self._show_btn.setFixedWidth(52)
        self._show_btn.setCheckable(True)
        self._show_btn.toggled.connect(self._toggle_visibility)
        token_row.addWidget(self._show_btn)

        root.addLayout(token_row)

        scope_hint = QLabel(
            "<span style='color:#555;font-size:11px;'>"
            "Required scopes: <b>repo</b> (read &amp; write) — or use a fine-grained "
            "token with <i>Contents: Read and Write</i>."
            "</span>"
        )
        scope_hint.setTextFormat(Qt.TextFormat.RichText)
        scope_hint.setWordWrap(True)
        root.addWidget(scope_hint)

        add_btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Token")
        add_btn.clicked.connect(self._on_add_token)
        add_btn_row.addStretch()
        add_btn_row.addWidget(add_btn)
        root.addLayout(add_btn_row)

        self._status_label = QLabel("")
        self._status_label.setTextFormat(Qt.TextFormat.RichText)
        self._status_label.setWordWrap(True)
        self._status_label.setMinimumHeight(30)
        root.addWidget(self._status_label)

        self._detail_box = QTextEdit()
        self._detail_box.setReadOnly(True)
        self._detail_box.setFixedHeight(60)
        self._detail_box.setVisible(False)
        root.addWidget(self._detail_box)

        return widget

    def _build_associations_tab(self) -> QWidget:
        """Build the directory-token associations tab."""
        self._assoc_widget = DirectoryTokenAssociationWidget(
            directory_associations=self._working_associations,
            available_tokens=sorted(self._working_tokens.keys()),
            recent_directories=self._recent_directories,
        )
        return self._assoc_widget


    # ── Token management ──────────────────────────────────────────────────────

    def _refresh_token_list(self) -> None:
        """Rebuild the token list widget to show all tokens."""
        self._token_list.clear()
        for token_name in sorted(self._working_tokens.keys()):
            is_active = token_name == self._working_active_token
            marker = "🔑 " if is_active else "  "
            masked_token = self._mask_token(self._working_tokens[token_name])
            label = f"{marker}{token_name}: {masked_token}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, token_name)
            self._token_list.addItem(item)

    def _mask_token(self, token: str) -> str:
        """Return a masked version of the token for display."""
        if len(token) <= 8:
            return "••••••••"
        return token[:4] + "•" * (len(token) - 8) + token[-4:]

    def _on_token_selected(self) -> None:
        """Clear status when user selects a different token."""
        self._set_status("", detail="")

    def _toggle_visibility(self, checked: bool) -> None:
        """Toggle between password and plain text for new token input."""
        if checked:
            self._token_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_btn.setText("Hide")
        else:
            self._token_input.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_btn.setText("Show")

    def _on_add_token(self) -> None:
        """Add a new token to the working set."""
        name = self._token_name_input.text().strip()
        token = self._token_input.text().strip()

        if not name:
            self._set_status(
                "<span style='color:#b71c1c;'>⚠ Please enter a token name.</span>",
                detail="",
            )
            return

        if not token:
            self._set_status(
                "<span style='color:#b71c1c;'>⚠ Please enter a token value.</span>",
                detail="",
            )
            return

        if name in self._working_tokens:
            self._set_status(
                f"<span style='color:#b71c1c;'>⚠ Token '{name}' already exists.</span>",
                detail="",
            )
            return

        self._working_tokens[name] = token
        if not self._working_active_token:
            self._working_active_token = name
        self._token_name_input.clear()
        self._token_input.clear()
        self._show_btn.setChecked(False)
        self._refresh_token_list()
        self._set_status(
            f"<span style='color:#1b5e20;'>✅ Token '{name}' added.</span>",
            detail="",
        )

    def _on_set_active(self) -> None:
        """Mark the selected token as active."""
        selected = self._token_list.selectedItems()
        if not selected:
            self._set_status(
                "<span style='color:#b71c1c;'>⚠ Please select a token first.</span>",
                detail="",
            )
            return

        token_name = selected[0].data(Qt.ItemDataRole.UserRole)
        self._working_active_token = token_name
        self._refresh_token_list()
        self._set_status(
            f"<span style='color:#1b5e20;'>✅ Token '{token_name}' is now active.</span>",
            detail="",
        )

    def _on_token_double_clicked(self, item: QListWidgetItem) -> None:
        """Set the double-clicked token as active."""
        token_name = item.data(Qt.ItemDataRole.UserRole)
        if not token_name:
            return

        self._working_active_token = token_name
        self._refresh_token_list()
        self._set_status(
            f"<span style='color:#1b5e20;'>✅ Token '{token_name}' is now active.</span>",
            detail="",
        )

    def _on_test_selected(self) -> None:
        """Test the selected token."""
        selected = self._token_list.selectedItems()
        if not selected:
            self._set_status(
                "<span style='color:#b71c1c;'>⚠ Please select a token first.</span>",
                detail="",
            )
            return

        token_name = selected[0].data(Qt.ItemDataRole.UserRole)
        token = self._working_tokens[token_name]
        self._set_status("Testing…", detail="")

        if shutil.which("curl"):
            self._test_with_curl(token)
        else:
            self._test_with_git(token)

    def _on_delete_selected(self) -> None:
        """Delete the selected token."""
        selected = self._token_list.selectedItems()
        if not selected:
            self._set_status(
                "<span style='color:#b71c1c;'>⚠ Please select a token first.</span>",
                detail="",
            )
            return

        token_name = selected[0].data(Qt.ItemDataRole.UserRole)
        del self._working_tokens[token_name]

        if self._working_active_token == token_name:
            self._working_active_token = next(iter(self._working_tokens.keys())) if self._working_tokens else ""

        self._refresh_token_list()
        self._set_status(
            f"<span style='color:#1b5e20;'>✅ Token '{token_name}' deleted.</span>",
            detail="",
        )

    # ── Connection testing ───────────────────────────────────────────────────

    def _test_with_curl(self, token: str) -> None:
        """Call GitHub /user API to test the token."""
        try:
            result = subprocess.run(
                [
                    "curl",
                    "--silent",
                    "--max-time", "10",
                    "--header", f"Authorization: Bearer {token}",
                    "--header", "Accept: application/vnd.github+json",
                    "--header", "X-GitHub-Api-Version: 2022-11-28",
                    "https://api.github.com/user",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._set_status(
                "<span style='color:#b71c1c;'>❌ Connection failed.</span>",
                detail=str(exc),
            )
            return

        import json
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            self._set_status(
                "<span style='color:#b71c1c;'>❌ Unexpected response from GitHub.</span>",
                detail=result.stdout[:500],
            )
            return

        if "login" in data:
            login = data["login"]
            name = data.get("name") or login
            self._set_status(
                f"<span style='color:#1b5e20;'>✅ Authenticated as <b>{name}</b> (@{login})</span>",
                detail="",
            )
        elif "message" in data:
            self._set_status(
                f"<span style='color:#b71c1c;'>❌ GitHub error: {data['message']}</span>",
                detail=result.stdout[:500],
            )
        else:
            self._set_status(
                "<span style='color:#b71c1c;'>❌ Unknown response.</span>",
                detail=result.stdout[:500],
            )

    def _test_with_git(self, token: str) -> None:
        """Fallback: try git ls-remote with the token."""
        import os
        import tempfile

        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        safe_token = token.replace("'", "'\\''")
        helper_content = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*) echo "x-token" ;;\n'
            f"  *Password*) echo '{safe_token}' ;;\n"
            "esac\n"
        )
        tf_name: str | None = None
        result: subprocess.CompletedProcess[str] | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, prefix="qtgit_test_"
            ) as tf:
                tf.write(helper_content)
                tf_name = tf.name
            os.chmod(tf_name, 0o700)
            env["GIT_ASKPASS"] = tf_name
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "credential.helper"
            env["GIT_CONFIG_VALUE_0"] = ""

            result = subprocess.run(
                ["git", "ls-remote", "https://github.com/github/gitignore.git", "HEAD"],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._set_status(
                "<span style='color:#b71c1c;'>❌ Connection failed.</span>",
                detail=str(exc),
            )
            return
        finally:
            if tf_name:
                try:
                    os.unlink(tf_name)
                except Exception:
                    pass

        if result is None:
            self._set_status(
                "<span style='color:#b71c1c;'>❌ git command did not run.</span>",
                detail="",
            )
            return

        if result.returncode == 0:
            self._set_status(
                "<span style='color:#1b5e20;'>✅ Token accepted (git connectivity OK).</span>",
                detail="",
            )
        else:
            err = result.stderr.strip() or f"Exit code {result.returncode}"
            self._set_status(
                "<span style='color:#b71c1c;'>❌ Token rejected or connection failed.</span>",
                detail=err,
            )

    # ── Status display ───────────────────────────────────────────────────────

    def _set_status(self, html: str, detail: str) -> None:
        """Update status and optional detail display."""
        self._status_label.setText(html)
        if detail:
            self._detail_box.setPlainText(detail)
            self._detail_box.setVisible(True)
        else:
            self._detail_box.setVisible(False)

    def _on_save(self) -> None:
        """Save and emit the multi-token configuration and directory associations."""
        # Save tokens
        self.tokens_saved.emit(self._working_tokens, self._working_active_token)

        # Save associations if the widget exists
        if self._assoc_widget:
            associations = self._assoc_widget.get_working_associations()
            self.associations_saved.emit(associations)

        self.accept()

    # ── Public helpers ────────────────────────────────────────────────────────

    def current_token(self) -> str:
        """Return the token currently entered in the field."""
        return self._token_input.text().strip()
