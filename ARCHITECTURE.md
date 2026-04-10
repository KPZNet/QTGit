# Directory-Token Association Feature - Architecture Diagram

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         MainWindow (main_window.py)                   │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Recent Directory Menu                                        │  │
│  │ ├─ Directory 1                                               │  │
│  │ ├─ Directory 2  ←─ _restore_recent_directory()              │  │
│  │ └─ Directory 3      │                                        │  │
│  │                     ├─ get_token_for_directory()            │  │
│  │                     ├─ set_active_token()                   │  │
│  │                     └─ set_github_token()                   │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ Settings Button → ConfigDialog                               │  │
│  │                     │                                         │  │
│  │                     ├─ Tab 1: GitHub Tokens                  │  │
│  │                     │   (Add, Delete, Test, Set Active)      │  │
│  │                     │                                         │  │
│  │                     └─ Tab 2: Directory-Token Links ←────┐   │  │
│  │                         (Dropdowns, Remove Buttons)      │   │  │
│  │                         DirectoryTokenAssociation        │   │  │
│  │                         Widget                           │   │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                                │                │
                                │                │
                    ┌───────────┘                └──────────┐
                    │                                        │
                    ▼                                        ▼
    ┌──────────────────────────────────┐    ┌──────────────────────────┐
    │    AppSettings                   │    │  ConfigDialog Signals   │
    │   (app_settings.py)              │    │                         │
    │                                  │    │  tokens_saved ──────────┤
    │  Storage Keys:                   │    │  associations_saved ────┤
    │  ├─ tokens/directoryAssociations │    │                         │
    │  ├─ auth/githubTokens            │    └──────────────────────────┘
    │  └─ auth/activeToken             │
    │                                  │
    │  Methods:                        │
    │  ├─ load_directory_token_        │
    │  │  associations()               │
    │  ├─ save_directory_token_        │
    │  │  association()                │
    │  ├─ get_token_for_               │
    │  │  directory()                  │
    │  └─ remove_directory_            │
    │     association()                │
    │                                  │
    └──────────────────────────────────┘
            │                │
            │                │
            ▼                ▼
    ┌──────────────────────────────────┐
    │      QSettings                   │
    │  (Persistent Storage)            │
    │                                  │
    │  QSettings Format.NativeFormat   │
    │  (macOS: ~/Library/Preferences)  │
    │                                  │
    └──────────────────────────────────┘
```

---

## Data Flow Diagram

### When User Selects a Recent Directory

```
User clicks "Recent" → Selects Directory
          │
          ▼
_restore_recent_directory(path)
          │
          ├─ get_token_for_directory(path)
          │   │
          │   ├─ load_directory_token_associations()
          │   │
          │   └─ Returns: token_name (or "")
          │
          ├─ set_active_token(token_name)
          │   │
          │   └─ Save to QSettings
          │
          ├─ set_github_token(token_value)
          │   │
          │   └─ Apply to git operations
          │
          ├─ Status: "Activated token 'xxx'" (if token set)
          │
          └─ _scan_directory(path)
              │
              └─ Display repositories
```

### When User Manages Associations in Settings

```
User clicks "Settings"
          │
          ▼
ConfigDialog.__init__()
          │
          ├─ load_directory_token_associations()
          │   │
          │   └─ directoryTokenAssociation_widget = DirectoryTokenAssociation
          │       Widget(associations, token_names)
          │
          └─ Show Settings Dialog with Tabs
              │
              └─ Tab 2: Directory-Token Links
                  │
                  ├─ User changes token dropdown
                  │   │
                  │   └─ _on_token_changed() updates working_associations
                  │
                  ├─ User clicks Remove button
                  │   │
                  │   └─ _on_remove_association() removes association
                  │
                  └─ User clicks Save
                      │
                      ├─ associations_saved.emit(working_associations)
                      │
                      └─ _on_associations_saved() saves to AppSettings
```

---

## Class Relationships

```
MainWindow
    │
    ├─ uses: AppSettings
    │          │
    │          └─ stores: {"/path": "token_name"}
    │
    ├─ launches: ConfigDialog
    │              │
    │              ├─ has: DirectoryTokenAssociationWidget
    │              │         │
    │              │         └─ edits: directory_associations dict
    │              │
    │              ├─ emits: tokens_saved signal
    │              └─ emits: associations_saved signal
    │
    └─ connects signals:
         ├─ tokens_saved → _on_tokens_saved()
         └─ associations_saved → _on_associations_saved()
```

---

## File Dependencies

```
main_window.py
    │
    ├─ imports: AppSettings
    ├─ imports: ConfigDialog
    ├─ imports: set_github_token()
    └─ calls: AppSettings methods
         ├─ get_token_for_directory()
         ├─ save_directory_token_association()
         ├─ remove_directory_association()
         └─ load_directory_token_associations()

config_dialog.py
    │
    ├─ imports: DirectoryTokenAssociationWidget
    │
    └─ emits:
         ├─ tokens_saved(dict, str)
         └─ associations_saved(dict)

directory_token_assoc.py
    │
    └─ DirectoryTokenAssociationWidget
         │
         └─ displays: {"/path": "token_name"} dict

app_settings.py
    │
    ├─ stores: {"/path": "token_name"} in QSettings
    │
    └─ provides:
         ├─ load_directory_token_associations()
         ├─ save_directory_token_association()
         ├─ get_token_for_directory()
         └─ remove_directory_association()
```

---

## Signal/Slot Connections

```
ConfigDialog.tokens_saved
    │
    └─> MainWindow._on_tokens_saved()
            │
            └─ Saves tokens to AppSettings

ConfigDialog.associations_saved
    │
    └─> MainWindow._on_associations_saved()
            │
            ├─ Removes old associations
            └─ Saves new associations

MainWindow Recent Directory Menu
    │
    └─> MainWindow._restore_recent_directory()
            │
            ├─ Retrieves token from AppSettings
            ├─ Activates token
            └─ Scans directory
```

---

## Data Storage Structure

```json
{
  "tokens/directoryAssociations": {
    "/Users/dev/work-project": "work-token",
    "/Users/dev/personal-project": "personal-token",
    "/Users/dev/hobby-project": ""
  },
  "auth/githubTokens": ["work-token", "personal-token"],
  "auth/activeToken": "work-token"
}
```

---

## Key Design Decisions

1. **Separation of Concerns**
   - AppSettings handles data persistence
   - DirectoryTokenAssociationWidget handles UI for associations
   - ConfigDialog coordinates the dialog flow
   - MainWindow orchestrates the overall feature

2. **Signal-Driven Updates**
   - Settings dialog emits signals on save
   - MainWindow listens and updates accordingly
   - Decouples components for maintainability

3. **Normalized Paths**
   - All directory paths normalized using Path.expanduser().resolve()
   - Ensures consistency across different path representations

4. **Graceful Degradation**
   - If no token associated, directory selection proceeds normally
   - If token removed from QSettings, empty token silently handled
   - No errors even with partial data

---


