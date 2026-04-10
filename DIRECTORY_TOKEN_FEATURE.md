# QTGit Directory-Token Association Feature

## Overview
Added a new feature that allows users to associate Git tokens with recent directories. When a user selects a directory from the recent directories list, the associated token automatically becomes active.

## Changes Made

### 1. **AppSettings** (`app/services/app_settings.py`)
Added new methods for managing directory-token associations:
- `load_directory_token_associations()` - Load all directory-token associations
- `save_directory_token_association(directory, token_name)` - Associate a token with a directory
- `get_token_for_directory(directory)` - Get the token associated with a specific directory
- `remove_directory_association(directory)` - Remove the association for a directory

These methods store associations as a dictionary mapping directory paths to token names.

### 2. **New Widget** (`app/widgets/directory_token_assoc.py`)
Created `DirectoryTokenAssociationWidget` which displays and manages token associations:
- Shows a table of recent directories and their associated tokens
- Allows users to select/change tokens via dropdown
- Provides a remove button for each association
- Updates associations in real-time as the user makes changes

### 3. **ConfigDialog** (`app/widgets/config_dialog.py`)
Restructured the settings dialog to use tabs:
- **Tab 1: "GitHub Tokens"** - Manage tokens (add, delete, test, set active)
- **Tab 2: "Directory-Token Links"** - Manage directory-token associations

Added new functionality:
- `associations_saved` signal emitted when associations are saved
- Updated `_on_save()` to persist both tokens and associations
- Enhanced initialization to accept directory associations parameter

### 4. **MainWindow** (`app/main_window.py`)
Updated to integrate directory-token associations:
- `_restore_recent_directory(directory)` - Activates the associated token before browsing to the directory
- `_clear_recent_directories()` - Also removes token associations when clearing recent directories
- `_show_settings()` - Passes directory associations to the settings dialog
- `_on_associations_saved(associations)` - Persists updated associations

## User Workflow

1. **Setting Up Associations:**
   - Open Settings dialog
   - Navigate to "Directory-Token Links" tab
   - Select a directory from the list
   - Choose which token to associate with it (or leave empty for no association)
   - Click Save

2. **Using Associations:**
   - Click "Recent" button in toolbar
   - Select a directory
   - The associated token automatically becomes active
   - Status bar shows confirmation

3. **Managing Associations:**
   - View all associations in the "Directory-Token Links" tab
   - Change associated token by selecting from dropdown
   - Remove association by clicking "Remove" button
   - Clear all recent directories also clears their associations

## Technical Details

### Data Storage
- Associations stored in QSettings with key: `tokens/directoryAssociations`
- Format: Dictionary mapping normalized directory paths to token names
- Paths are normalized using `Path.expanduser().resolve()` for consistency

### Signal Flow
1. User opens Settings → ConfigDialog loads associations
2. User modifies associations in DirectoryTokenAssociationWidget
3. User clicks Save → associations_saved signal emitted
4. MainWindow._on_associations_saved() persists the changes

### Directory Selection
1. User clicks recent directory → _restore_recent_directory() called
2. Method retrieves associated token from settings
3. Token is activated via set_active_token()
4. Directory is scanned and displayed

## Benefits

✓ Automatically manage which token is active based on the project you're working on
✓ No manual token switching needed when moving between projects
✓ Token associations are persisted across sessions
✓ Can have different tokens for different directories (e.g., work vs. personal projects)
✓ Easy to update or remove associations from the Settings dialog
✓ Associations cleaned up when recent directories are cleared

## Testing

A comprehensive test file (`test_directory_associations.py`) verifies:
- Saving associations
- Loading associations
- Retrieving tokens for directories
- Updating associations
- Removing associations
- Proper handling of non-existent directories

All tests pass successfully.

