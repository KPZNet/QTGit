from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings

_KEYRING_SERVICE = "QTGit"
_KEYRING_USERNAME_PREFIX = "github_token_"
_GITHUB_TOKEN_KEY = "auth/githubToken"  # QSettings fallback key
_GITHUB_TOKENS_KEY = "auth/githubTokens"  # List of token names
_ACTIVE_TOKEN_KEY = "auth/activeToken"  # Currently active token name
_LEGACY_COMMIT_DIALOG_KEYS = (
    "window/commitDialogSize",
    "window/commitDialogMainSplitterSizes",
    "window/commitDialogTopSplitterSizes",
    "window/commitDialogCommitColumnSizes",
    "window/commitDialogFileColumnSizes",
)


class AppSettings:
    _LAST_DIRECTORY_KEY = "browser/lastDirectory"
    _RECENT_DIRECTORIES_KEY = "browser/recentDirectories"
    _WINDOW_GEOMETRY_KEY = "window/geometry"
    _MAIN_SPLITTER_SIZES_KEY = "window/mainSplitterSizes"
    _RIGHT_SPLITTER_SIZES_KEY = "window/rightSplitterSizes"
    _RIGHT_CONTENT_SPLITTER_SIZES_KEY = "window/rightContentSplitterSizes"
    _RIGHT_COMMIT_COLUMN_SIZES_KEY = "window/rightCommitColumnSizes"
    _RIGHT_FILE_COLUMN_SIZES_KEY = "window/rightFileColumnSizes"
    _MAX_RECENT_DIRECTORIES = 5

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings or QSettings()
        self._cleanup_legacy_settings()

    def _cleanup_legacy_settings(self) -> None:
        removed_any = False
        for key in _LEGACY_COMMIT_DIALOG_KEYS:
            if self._settings.contains(key):
                self._settings.remove(key)
                removed_any = True
        if removed_any:
            self._settings.sync()

    def load_last_directory(self, fallback_directory: Path) -> Path:
        stored_directory = self._settings.value(self._LAST_DIRECTORY_KEY, "", str)
        directory = self._coerce_directory(stored_directory)
        if directory is not None:
            return directory

        return fallback_directory.expanduser().resolve()

    def recent_directories(self) -> list[Path]:
        raw_value = self._settings.value(self._RECENT_DIRECTORIES_KEY, [])

        if isinstance(raw_value, str):
            candidate_paths = [raw_value]
        else:
            candidate_paths = list(raw_value)

        recent_directories: list[Path] = []
        for candidate in candidate_paths:
            directory = self._coerce_directory(candidate)
            if directory is None or directory in recent_directories:
                continue
            recent_directories.append(directory)

        return recent_directories[: self._MAX_RECENT_DIRECTORIES]

    def save_browsed_directory(self, directory: Path) -> list[Path]:
        normalized_directory = directory.expanduser().resolve()

        recent_directories = [
            item for item in self.recent_directories() if item != normalized_directory
        ]
        recent_directories.insert(0, normalized_directory)
        recent_directories = recent_directories[: self._MAX_RECENT_DIRECTORIES]

        self._settings.setValue(self._LAST_DIRECTORY_KEY, str(normalized_directory))
        self._settings.setValue(
            self._RECENT_DIRECTORIES_KEY,
            [str(item) for item in recent_directories],
        )
        self._settings.sync()

        return recent_directories

    def clear_recent_directories(self) -> None:
        self._settings.remove(self._LAST_DIRECTORY_KEY)
        self._settings.remove(self._RECENT_DIRECTORIES_KEY)
        self._settings.sync()

    def load_window_geometry(self) -> QByteArray | None:
        geometry = self._settings.value(
            self._WINDOW_GEOMETRY_KEY,
            QByteArray(),
            QByteArray,
        )
        if geometry.isEmpty():
            return None

        return geometry

    def save_window_geometry(self, geometry: QByteArray) -> None:
        self._settings.setValue(self._WINDOW_GEOMETRY_KEY, geometry)
        self._settings.sync()

    def load_main_splitter_sizes(self) -> list[int] | None:
        return self._load_sizes(self._MAIN_SPLITTER_SIZES_KEY)

    def save_main_splitter_sizes(self, sizes: list[int]) -> None:
        self._save_sizes(self._MAIN_SPLITTER_SIZES_KEY, sizes)

    def load_right_splitter_sizes(self) -> list[int] | None:
        return self._load_sizes(self._RIGHT_SPLITTER_SIZES_KEY)

    def save_right_splitter_sizes(self, sizes: list[int]) -> None:
        self._save_sizes(self._RIGHT_SPLITTER_SIZES_KEY, sizes)

    def load_right_content_splitter_sizes(self) -> list[int] | None:
        return self._load_sizes(self._RIGHT_CONTENT_SPLITTER_SIZES_KEY)

    def save_right_content_splitter_sizes(self, sizes: list[int]) -> None:
        self._save_sizes(self._RIGHT_CONTENT_SPLITTER_SIZES_KEY, sizes)

    def load_right_commit_column_sizes(self) -> list[int] | None:
        return self._load_sizes(self._RIGHT_COMMIT_COLUMN_SIZES_KEY)

    def save_right_commit_column_sizes(self, sizes: list[int]) -> None:
        self._save_sizes(self._RIGHT_COMMIT_COLUMN_SIZES_KEY, sizes)

    def load_right_file_column_sizes(self) -> list[int] | None:
        return self._load_sizes(self._RIGHT_FILE_COLUMN_SIZES_KEY)

    def save_right_file_column_sizes(self, sizes: list[int]) -> None:
        self._save_sizes(self._RIGHT_FILE_COLUMN_SIZES_KEY, sizes)


    def _load_sizes(self, key: str) -> list[int] | None:
        raw_value = self._settings.value(key, [])
        if isinstance(raw_value, str):
            raw_sizes = [raw_value]
        else:
            raw_sizes = list(raw_value)

        sizes: list[int] = []
        for raw_size in raw_sizes:
            try:
                size = int(raw_size)
            except (TypeError, ValueError):
                continue

            if size > 0:
                sizes.append(size)

        return sizes or None

    def _save_sizes(self, key: str, sizes: list[int]) -> None:
        self._settings.setValue(key, [int(size) for size in sizes if int(size) > 0])
        self._settings.sync()

    # ── GitHub tokens (multi-token support) ──────────────────────────────────

    def load_github_tokens(self) -> dict[str, str]:
        """Return a dict of {token_name: token_value} for all stored tokens."""
        token_names_raw = self._settings.value(_GITHUB_TOKENS_KEY, [], list)
        if not token_names_raw:
            # Migrate old single token if it exists
            old_token = self._load_github_token_legacy()
            if old_token:
                self.save_github_token("default", old_token)
                return {"default": old_token}
            return {}

        tokens: dict[str, str] = {}
        for token_name in token_names_raw:
            token = self._load_token_by_name(token_name)
            if token:
                tokens[token_name] = token
        return tokens

    def get_active_github_token(self) -> str:
        """Return the currently active token value, or empty string."""
        active_name = self._settings.value(_ACTIVE_TOKEN_KEY, "", str)
        if not active_name:
            return ""
        return self._load_token_by_name(active_name)

    def get_active_token_name(self) -> str:
        """Return the name of the currently active token."""
        return self._settings.value(_ACTIVE_TOKEN_KEY, "", str)

    def save_github_token(self, token_name: str, token: str) -> None:
        """Store a token with the given name.  If token is empty, delete it."""
        token_name = token_name.strip()
        if not token_name:
            return

        token_names_raw = self._settings.value(_GITHUB_TOKENS_KEY, [], list)
        if not isinstance(token_names_raw, list):
            token_names_raw = []

        if token:
            # Save the token
            self._save_token_by_name(token_name, token)
            # Add to list if not already there
            if token_name not in token_names_raw:
                token_names_raw.append(token_name)
            # Set as active if no other token is active
            if not self._settings.value(_ACTIVE_TOKEN_KEY, "", str):
                self._settings.setValue(_ACTIVE_TOKEN_KEY, token_name)
        else:
            # Delete the token
            self._delete_token_by_name(token_name)
            # Remove from list
            if token_name in token_names_raw:
                token_names_raw.remove(token_name)
            # If this was the active token, pick another one
            if self._settings.value(_ACTIVE_TOKEN_KEY, "", str) == token_name:
                next_active = token_names_raw[0] if token_names_raw else ""
                self._settings.setValue(_ACTIVE_TOKEN_KEY, next_active)

        self._settings.setValue(_GITHUB_TOKENS_KEY, token_names_raw)
        self._settings.sync()

    def set_active_token(self, token_name: str) -> None:
        """Set the active token by name."""
        token_names_raw = self._settings.value(_GITHUB_TOKENS_KEY, [], list)
        if token_name in token_names_raw or token_name == "":
            self._settings.setValue(_ACTIVE_TOKEN_KEY, token_name)
            self._settings.sync()

    # ── Private helpers ──────────────────────────────────────────────────────

    def _load_github_token_legacy(self) -> str:
        """Load the old single-token format for migration."""
        try:
            import keyring
            token = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME_PREFIX + "default")
            if token:
                return token
        except Exception:
            pass
        # Fallback: check old QSettings key
        return self._settings.value(_GITHUB_TOKEN_KEY, "", str)

    def _load_token_by_name(self, token_name: str) -> str:
        """Load a token by name from keyring or QSettings."""
        try:
            import keyring
            token = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME_PREFIX + token_name)
            if token:
                return token
        except Exception:
            pass
        # Fallback: QSettings (for backward compatibility)
        return self._settings.value(f"auth/token_{token_name}", "", str)

    def _save_token_by_name(self, token_name: str, token: str) -> None:
        """Save a token by name to keyring or QSettings."""
        try:
            import keyring
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME_PREFIX + token_name, token)
            # Clear QSettings copy
            self._settings.remove(f"auth/token_{token_name}")
            self._settings.sync()
            return
        except Exception:
            pass
        # Fallback: QSettings
        self._settings.setValue(f"auth/token_{token_name}", token)
        self._settings.sync()

    def _delete_token_by_name(self, token_name: str) -> None:
        """Delete a token by name from keyring and QSettings."""
        try:
            import keyring
            try:
                keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME_PREFIX + token_name)
            except Exception:
                pass
        except Exception:
            pass
        # Also clear QSettings
        self._settings.remove(f"auth/token_{token_name}")
        self._settings.sync()

    # ── Legacy single-token API (deprecated, for backward compatibility) ──────

    def load_github_token(self) -> str:
        """[DEPRECATED] Return the active GitHub token, or empty string."""
        return self.get_active_github_token()

    def save_github_token_legacy(self, token: str) -> None:
        """[DEPRECATED] Save a single token."""
        self.save_github_token("default", token)

    # ── Directory-Token Associations ──────────────────────────────────────

    _DIRECTORY_TOKEN_ASSOC_KEY = "tokens/directoryAssociations"  # dict[str, str]

    def load_directory_token_associations(self) -> dict[str, str]:
        """Load directory-to-token associations.

        Returns a dict of {directory_path_str: token_name}.
        """
        raw_value = self._settings.value(self._DIRECTORY_TOKEN_ASSOC_KEY, {})
        if isinstance(raw_value, dict):
            return dict(raw_value)
        return {}

    def save_directory_token_association(self, directory: Path, token_name: str) -> None:
        """Associate a directory with a token name."""
        normalized_path = str(directory.expanduser().resolve())
        associations = self.load_directory_token_associations()

        if token_name:
            associations[normalized_path] = token_name
        else:
            # Remove association if token_name is empty
            associations.pop(normalized_path, None)

        self._settings.setValue(self._DIRECTORY_TOKEN_ASSOC_KEY, associations)
        self._settings.sync()

    def get_token_for_directory(self, directory: Path) -> str:
        """Get the token name associated with a directory, or empty string."""
        normalized_path = str(directory.expanduser().resolve())
        associations = self.load_directory_token_associations()
        return associations.get(normalized_path, "")

    def remove_directory_association(self, directory: Path) -> None:
        """Remove the token association for a directory."""
        self.save_directory_token_association(directory, "")

    def _coerce_directory(self, value: object) -> Path | None:
        if not value:
            return None

        directory = Path(str(value)).expanduser().resolve()
        if not directory.exists() or not directory.is_dir():
            return None

        return directory