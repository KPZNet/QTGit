from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable

# ---------------------------------------------------------------------------
# GitHub token — set once at startup (and whenever the user changes it in the
# Settings dialog) so every git subprocess inherits the right credentials.
# ---------------------------------------------------------------------------
_github_token: str = ""


def set_github_token(token: str) -> None:
    """Store *token* globally so all subsequent git calls authenticate with it."""
    global _github_token
    _github_token = token.strip()


def get_github_token() -> str:
    """Return the currently configured GitHub token (may be empty)."""
    return _github_token


def _git_env() -> dict[str, str] | None:
    """Return an env dict that injects the GitHub token via GIT_ASKPASS.

    When a token is set we write a tiny shell script that is used as the
    GIT_ASKPASS helper.  Git calls the helper once for the username prompt and
    once for the password prompt.  We return "x-token" for username and the
    actual PAT for password, which is the correct pattern for GitHub HTTPS.

    Returns *None* when no token is configured so callers can pass
    ``env=None`` and inherit the process environment unchanged.
    """
    if not _github_token:
        return None

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"   # never block on an interactive prompt

    # Build an ASKPASS helper that returns:
    #   "x-token"        when git asks for a username
    #   the PAT          when git asks for a password
    # Git passes the prompt string as $1 to the helper.
    # We must escape single-quotes in the token for the shell here-doc.
    safe_token = _github_token.replace("'", "'\\''")
    helper_content = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  *Username*) echo "x-token" ;;\n'
        f"  *Password*) echo '{safe_token}' ;;\n"
        "esac\n"
    )

    global _askpass_file
    if _askpass_file is None or not os.path.exists(_askpass_file.name):
        _askpass_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", delete=False, prefix="qtgit_askpass_"
        )
        _askpass_file.write(helper_content)
        _askpass_file.flush()
        os.chmod(_askpass_file.name, 0o700)
    else:
        # Update token in place when it changes
        with open(_askpass_file.name, "w") as fh:
            fh.write(helper_content)

    env["GIT_ASKPASS"] = _askpass_file.name
    env["SSH_ASKPASS"] = _askpass_file.name   # in case remote uses SSH askpass path

    # Override any system credential helper that might shadow our ASKPASS.
    # git respects GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n
    # to inject config without touching any config file.
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "credential.helper"
    env["GIT_CONFIG_VALUE_0"] = ""   # empty string disables all stored helpers

    return env


_askpass_file: tempfile.NamedTemporaryFile | None = None  # type: ignore[type-arg]


@dataclass(frozen=True)
class GitBranch:
    name: str
    is_current: bool
    upstream: str | None
    commit_sha: str | None
    commit_subject: str | None
    sync_status: str | None = None
    behind_count: int = 0
    ahead_count: int = 0


@dataclass(frozen=True)
class GitRepository:
    name: str
    path: Path
    local_branches: list[GitBranch]
    has_uncommitted_changes: bool = False


@dataclass(frozen=True)
class PullResult:
    repository: GitRepository
    success: bool
    output: str
    error: str


@dataclass(frozen=True)
class CheckoutResult:
    repository: GitRepository
    branch_name: str
    success: bool
    output: str
    error: str


@dataclass(frozen=True)
class DeleteBranchResult:
    repository: GitRepository
    branch_name: str
    success: bool
    output: str
    error: str


@dataclass(frozen=True)
class PushResult:
    repository: GitRepository
    branch_name: str
    success: bool
    output: str
    error: str


@dataclass(frozen=True)
class CommitResult:
    repository: GitRepository
    branch_name: str
    success: bool
    output: str
    error: str
    created_commit: bool


@dataclass(frozen=True)
class SyncToRemoteResult:
    repository: GitRepository
    branch_name: str
    success: bool
    output: str
    error: str


@dataclass(frozen=True)
class RemoteBranch:
    """Represents a remote branch with information about the most recent commit."""
    name: str
    commit_sha: str | None
    commit_subject: str | None
    commit_date: str | None
    author: str | None


@dataclass(frozen=True)
class RepoScanResult:
    root_directory: Path
    repositories: list[GitRepository]
    scanned_directories: int
    error_message: str | None = None


def find_git_repositories(root_directory: Path) -> RepoScanResult:
    directory = root_directory.expanduser().resolve()
    repositories: list[GitRepository] = []
    scanned_directories = 0

    if not directory.exists():
        return RepoScanResult(
            root_directory=directory,
            repositories=[],
            scanned_directories=0,
            error_message=f"Directory does not exist: {directory}",
        )

    if not directory.is_dir():
        return RepoScanResult(
            root_directory=directory,
            repositories=[],
            scanned_directories=0,
            error_message=f"Path is not a directory: {directory}",
        )

    try:
        stack = [directory]

        while stack:
            current_directory = stack.pop()
            scanned_directories += 1

            git_dir = current_directory / ".git"
            if git_dir.is_dir():
                local_branches = _read_branches(current_directory)
                repositories.append(
                    GitRepository(
                        name=current_directory.name or str(current_directory),
                        path=current_directory,
                        local_branches=local_branches,
                        has_uncommitted_changes=_has_uncommitted_changes(current_directory),
                    )
                )
                continue

            child_directories = []
            for child in current_directory.iterdir():
                if child.is_dir() and child.name not in {".git", ".venv", "__pycache__"}:
                    child_directories.append(child)

            stack.extend(sorted(child_directories, reverse=True))
    except OSError as exc:
        return RepoScanResult(
            root_directory=directory,
            repositories=sorted(repositories, key=lambda repo: repo.name.lower()),
            scanned_directories=scanned_directories,
            error_message=f"Unable to scan {directory}: {exc}",
        )

    repositories.sort(key=lambda repo: (repo.name.lower(), str(repo.path).lower()))
    return RepoScanResult(
        root_directory=directory,
        repositories=repositories,
        scanned_directories=scanned_directories,
    )


def _fetch_repository(repo_path: Path) -> None:
    """Run ``git fetch --prune`` for *repo_path* to update remote-tracking refs.

    Errors are silently swallowed so a single unreachable remote cannot abort
    the whole refresh scan.
    """
    if shutil.which("git") is None:
        return
    try:
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "--prune", "--quiet"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        pass


def _collect_repo_paths(directory: Path) -> tuple[list[Path], int]:
    """Walk *directory* and return (repo_paths, scanned_directories).

    Does no git I/O — just finds every subdirectory that contains a ``.git``
    folder so we can hand them off to parallel workers.
    """
    repo_paths: list[Path] = []
    scanned_directories = 0
    stack = [directory]

    while stack:
        current = stack.pop()
        scanned_directories += 1

        if (current / ".git").is_dir():
            repo_paths.append(current)
            continue

        try:
            children = [
                child for child in current.iterdir()
                if child.is_dir() and child.name not in {".git", ".venv", "__pycache__"}
            ]
        except OSError:
            continue

        stack.extend(sorted(children, reverse=True))

    return repo_paths, scanned_directories


def _fetch_and_read(repo_path: Path) -> GitRepository:
    """Fetch from remote then read local branches. Designed for parallel use."""
    _fetch_repository(repo_path)
    local_branches = _read_branches(repo_path)
    return GitRepository(
        name=repo_path.name or str(repo_path),
        path=repo_path,
        local_branches=local_branches,
        has_uncommitted_changes=_has_uncommitted_changes(repo_path),
    )


def _has_uncommitted_changes(repo_path: Path) -> bool:
    """Return True when git reports any staged/unstaged/untracked change."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    return bool(completed.stdout.strip())


def scan_repositories_live(
    root_directory: Path,
    on_repo_scanned: Callable[[GitRepository], None],
    on_complete: Callable[[RepoScanResult], None],
) -> None:
    """Scan *root_directory* for git repos, fetching all remotes in parallel.

    Phase 1 – fast directory walk to collect every repo path (no network I/O).
    Phase 2 – ``git fetch --prune`` + branch read for every repo concurrently
               via a thread-pool; *on_repo_scanned* is called as each finishes.
    Phase 3 – *on_complete* is called with the full sorted result.

    Designed to run in a background thread so the UI stays responsive.
    """
    directory = root_directory.expanduser().resolve()

    if not directory.exists():
        on_complete(RepoScanResult(
            root_directory=directory,
            repositories=[],
            scanned_directories=0,
            error_message=f"Directory does not exist: {directory}",
        ))
        return

    if not directory.is_dir():
        on_complete(RepoScanResult(
            root_directory=directory,
            repositories=[],
            scanned_directories=0,
            error_message=f"Path is not a directory: {directory}",
        ))
        return

    try:
        repo_paths, scanned_directories = _collect_repo_paths(directory)
    except OSError as exc:
        on_complete(RepoScanResult(
            root_directory=directory,
            repositories=[],
            scanned_directories=0,
            error_message=f"Unable to scan {directory}: {exc}",
        ))
        return

    repositories: list[GitRepository] = []

    # Use min(32, repo_count) workers — each worker is mostly waiting on
    # network / subprocess I/O, so a generous pool is fine.
    max_workers = max(1, min(32, len(repo_paths)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {
            executor.submit(_fetch_and_read, path): path
            for path in repo_paths
        }
        for future in as_completed(future_to_path):
            try:
                repo = future.result()
            except Exception:
                # If a single repo fails for any reason, skip it gracefully.
                continue
            repositories.append(repo)
            on_repo_scanned(repo)

    sorted_repos = sorted(repositories, key=lambda r: (r.name.lower(), str(r.path).lower()))
    on_complete(RepoScanResult(
        root_directory=directory,
        repositories=sorted_repos,
        scanned_directories=scanned_directories,
    ))


def _read_branches(repo_path: Path) -> list[GitBranch]:
    if shutil.which("git") is None:
        return []

    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "for-each-ref",
                "--format=%(refname:short)\t%(HEAD)\t%(upstream:short)\t%(objectname:short)\t%(subject)",
                "refs/heads",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    local_branches: list[GitBranch] = []
    for line in completed.stdout.splitlines():
        branch = _parse_branch_line(line)
        if branch is None:
            continue

        if branch.is_current and branch.upstream:
            sync_status, behind_count, ahead_count = _read_branch_sync_status(
                repo_path,
                branch.name,
                branch.upstream,
            )
            branch = replace(
                branch,
                sync_status=sync_status,
                behind_count=behind_count,
                ahead_count=ahead_count,
            )

        local_branches.append(branch)

    local_branches.sort(key=lambda branch: (not branch.is_current, branch.name.lower()))
    return local_branches


def _parse_branch_line(line: str) -> GitBranch | None:
    if not line.strip():
        return None

    parts = line.split("\t", maxsplit=4)
    if len(parts) != 5:
        return None

    short_name, head_marker, upstream, commit_sha, commit_subject = parts
    branch_name = short_name.strip()
    if not branch_name:
        return None

    return GitBranch(
        name=branch_name,
        is_current=head_marker.strip() == "*",
        upstream=upstream.strip() or None,
        commit_sha=commit_sha.strip() or None,
        commit_subject=commit_subject.strip() or None,
    )


def _read_branch_sync_status(
    repo_path: Path,
    branch_name: str,
    upstream: str,
) -> tuple[str | None, int, int]:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "rev-list",
                "--left-right",
                "--count",
                f"{upstream}...{branch_name}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None, 0, 0

    counts = completed.stdout.strip().split()
    if len(counts) != 2:
        return None, 0, 0

    try:
        behind_count = int(counts[0])
        ahead_count = int(counts[1])
    except ValueError:
        return None, 0, 0

    if behind_count == 0 and ahead_count == 0:
        return "in_sync", behind_count, ahead_count

    if behind_count > 0 and ahead_count > 0:
        return "diverged", behind_count, ahead_count

    if behind_count > 0:
        return "behind", behind_count, ahead_count

    return "ahead", behind_count, ahead_count


def pull_repository(
    repository: GitRepository,
    on_progress: Callable[[GitRepository, str], None],
) -> PullResult:
    """Run ``git pull`` for the active branch of *repository*.

    Calls *on_progress* with a human-readable status string at each stage so
    callers can update the UI while the pull is in progress.  This function is
    designed to be called from a background thread.
    """
    if shutil.which("git") is None:
        message = "git not found on PATH"
        on_progress(repository, message)
        return PullResult(
            repository=repository,
            success=False,
            output="",
            error=message,
        )

    on_progress(repository, "Pulling…")

    try:
        completed = subprocess.run(
            ["git", "-C", str(repository.path), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            env=_git_env(),
        )
    except OSError as exc:
        on_progress(repository, f"Error: {exc}")
        return PullResult(
            repository=repository,
            success=False,
            output="",
            error=str(exc),
        )

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()

    if completed.returncode == 0:
        summary = stdout.splitlines()[0] if stdout else "Already up to date."
        on_progress(repository, f"Done: {summary}")
        return PullResult(
            repository=repository,
            success=True,
            output=stdout,
            error=stderr,
        )

    error_line = stderr.splitlines()[0] if stderr else f"Exit code {completed.returncode}"
    on_progress(repository, f"Failed: {error_line}")
    return PullResult(
        repository=repository,
        success=False,
        output=stdout,
        error=stderr,
    )


def checkout_branch(repository: GitRepository, branch_name: str) -> CheckoutResult:
    if shutil.which("git") is None:
        return CheckoutResult(
            repository=repository,
            branch_name=branch_name,
            success=False,
            output="",
            error="git not found on PATH",
        )

    try:
        completed = subprocess.run(
            ["git", "-C", str(repository.path), "checkout", branch_name],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return CheckoutResult(
            repository=repository,
            branch_name=branch_name,
            success=False,
            output="",
            error=str(exc),
        )

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    return CheckoutResult(
        repository=repository,
        branch_name=branch_name,
        success=completed.returncode == 0,
        output=stdout,
        error=stderr,
    )


def get_remote_branches(repository: GitRepository) -> list[RemoteBranch]:
    """Fetch all remote branches from a repository, sorted by most recent commit date.

    Returns a list of RemoteBranch objects sorted with the most recently committed
    branches first.
    """
    if shutil.which("git") is None:
        return []

    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repository.path),
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)\t%(objectname:short)\t%(subject)\t%(committerdate:short)\t%(authorname)",
                "refs/remotes",
            ],
            capture_output=True,
            text=True,
            check=True,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return []

    remote_branches: list[RemoteBranch] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue

        parts = line.split("\t", maxsplit=4)
        if len(parts) < 4:
            continue

        ref_name = parts[0].strip()
        # Filter out HEAD pointers (origin/HEAD, etc.)
        if ref_name.endswith("/HEAD"):
            continue

        # Extract just the remote branch name (e.g., "origin/main" from "refs/remotes/origin/main")
        commit_sha = parts[1].strip() if len(parts) > 1 else None
        commit_subject = parts[2].strip() if len(parts) > 2 else None
        commit_date = parts[3].strip() if len(parts) > 3 else None
        author = parts[4].strip() if len(parts) > 4 else None

        remote_branches.append(
            RemoteBranch(
                name=ref_name,
                commit_sha=commit_sha,
                commit_subject=commit_subject,
                commit_date=commit_date,
                author=author,
            )
        )

    return remote_branches


def checkout_remote_branch(repository: GitRepository, remote_branch_name: str) -> CheckoutResult:
    """Check out a remote branch as a new local branch.

    Given a remote branch name like 'origin/main', creates a local tracking branch
    named 'main' that tracks the remote branch.
    """
    if shutil.which("git") is None:
        return CheckoutResult(
            repository=repository,
            branch_name=remote_branch_name,
            success=False,
            output="",
            error="git not found on PATH",
        )

    # Extract the local branch name from the remote branch name (e.g., "main" from "origin/main")
    local_branch_name = remote_branch_name.split("/", 1)[-1] if "/" in remote_branch_name else remote_branch_name

    try:
        # Try to checkout with --track, which creates a local branch tracking the remote
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repository.path),
                "checkout",
                "--track",
                remote_branch_name,
            ],
            capture_output=True,
            text=True,
            env=_git_env(),
        )
    except OSError as exc:
        return CheckoutResult(
            repository=repository,
            branch_name=remote_branch_name,
            success=False,
            output="",
            error=str(exc),
        )

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    return CheckoutResult(
        repository=repository,
        branch_name=remote_branch_name,
        success=completed.returncode == 0,
        output=stdout,
        error=stderr,
    )


def delete_branch(
    repository: GitRepository,
    branch_name: str,
    force: bool = False,
) -> DeleteBranchResult:
    """Delete a local branch in the repository.

    Deletes only the local branch, not any remote branches.
    If force=True, uses 'git branch -D' to force delete even if not fully merged.
    If force=False, uses 'git branch -d' which only deletes if fully merged.
    """
    if shutil.which("git") is None:
        return DeleteBranchResult(
            repository=repository,
            branch_name=branch_name,
            success=False,
            output="",
            error="git not found on PATH",
        )

    try:
        flag = "-D" if force else "-d"
        completed = subprocess.run(
            ["git", "-C", str(repository.path), "branch", flag, branch_name],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return DeleteBranchResult(
            repository=repository,
            branch_name=branch_name,
            success=False,
            output="",
            error=str(exc),
        )

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    return DeleteBranchResult(
        repository=repository,
        branch_name=branch_name,
        success=completed.returncode == 0,
        output=stdout,
        error=stderr,
    )


def _run_git_command(
    repo_path: Path,
    args: list[str],
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    """Run a git command and return (completed_process, os_error_message)."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        return completed, None
    except OSError as exc:
        return None, str(exc)


def _validate_sync_preconditions(branch: GitBranch) -> str | None:
    if not branch.is_current:
        return "Sync to Remote is only supported for the active branch."
    if not branch.upstream:
        return f"Branch '{branch.name}' does not track an upstream branch."
    return None


def _execute_sync_commands(
    repo_path: Path,
    upstream: str,
) -> tuple[bool, list[str], str]:
    logs: list[str] = []
    commands = [
        ("fetch", ["fetch", "--prune"]),
        ("reset", ["reset", "--hard", upstream]),
        ("clean", ["clean", "-fd"]),
    ]

    for action, args in commands:
        completed, os_error = _run_git_command(repo_path, args)
        if os_error:
            return False, logs, os_error

        if completed is None:
            return False, logs, f"git {action} failed unexpectedly"

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        if stdout:
            logs.append(stdout)
        if stderr:
            logs.append(stderr)

        if completed.returncode != 0:
            message = stderr or f"git {action} failed with exit code {completed.returncode}"
            return False, logs, message

    return True, logs, ""


def sync_active_branch_to_remote(
    repository: GitRepository,
    branch: GitBranch,
) -> SyncToRemoteResult:
    """Reset an active local branch to its tracked remote branch.

    This operation is destructive for local state: it discards staged/unstaged
    changes, removes untracked files/directories, and drops local commits not
    present on upstream. It never pushes and does not modify the remote branch.
    """
    if shutil.which("git") is None:
        return SyncToRemoteResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="",
            error="git not found on PATH",
        )

    precondition_error = _validate_sync_preconditions(branch)
    if precondition_error is not None:
        return SyncToRemoteResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="",
            error=precondition_error,
        )

    success, logs, error = _execute_sync_commands(repository.path, branch.upstream)
    if not success:
        return SyncToRemoteResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="\n".join(logs),
            error=error,
        )

    return SyncToRemoteResult(
        repository=repository,
        branch_name=branch.name,
        success=True,
        output="\n".join(logs),
        error="",
    )


def push_repository(
    repository: GitRepository,
    branch: GitBranch,
    commit_message: str,
    on_progress: Callable[[GitRepository, str], None],
) -> PushResult:
    """Stage all changes, commit with *commit_message*, then push to the remote.

    Steps performed:
      1. ``git add -A``                – stage all changes (modified, new, deleted)
      2. ``git commit -m message``     – commit; skipped if nothing to commit
      3. ``git push``                  – push committed changes to upstream

    *on_progress* is called with status strings so the UI can update in real time.
    Designed to run in a background thread.
    """
    if shutil.which("git") is None:
        msg = "git not found on PATH"
        on_progress(repository, msg)
        return PushResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="",
            error=msg,
        )

    repo_path = repository.path

    # ── Stage all changes ─────────────────────────────────────────────────────
    on_progress(repository, "Staging changes…")
    try:
        add_result = subprocess.run(
            ["git", "-C", str(repo_path), "add", "-A"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        on_progress(repository, f"Stage failed: {exc}")
        return PushResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="",
            error=str(exc),
        )

    if add_result.returncode != 0:
        err = add_result.stderr.strip() or f"git add exited {add_result.returncode}"
        on_progress(repository, f"Stage failed: {err}")
        return PushResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output=add_result.stdout.strip(),
            error=err,
        )

    # ── Commit (skip if nothing staged) ──────────────────────────────────────
    on_progress(repository, "Committing…")
    try:
        commit_result = subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-m", commit_message],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        on_progress(repository, f"Commit failed: {exc}")
        return PushResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="",
            error=str(exc),
        )

    commit_stdout = commit_result.stdout.strip()
    commit_stderr = commit_result.stderr.strip()
    # Exit code 1 with "nothing to commit" is not a real failure
    nothing_to_commit = (
        commit_result.returncode == 1
        and ("nothing to commit" in commit_stdout or "nothing to commit" in commit_stderr)
    )
    if not nothing_to_commit and commit_result.returncode != 0:
        err = commit_stderr or f"git commit exited {commit_result.returncode}"
        on_progress(repository, f"Commit failed: {err}")
        return PushResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output=commit_stdout,
            error=err,
        )

    if nothing_to_commit:
        on_progress(repository, "Nothing to commit — pushing existing commits…")
    else:
        summary = commit_stdout.splitlines()[0] if commit_stdout else "Committed"
        on_progress(repository, f"Committed — {summary}")

    # ── Push ──────────────────────────────────────────────────────────────────
    on_progress(repository, "Pushing…")

    # Resolve the remote name and refspec from the upstream tracking branch.
    remote_name = "origin"
    refspec: str | None = None
    if branch.upstream and "/" in branch.upstream:
        remote_name, remote_branch = branch.upstream.split("/", 1)
        refspec = f"{branch.name}:{remote_branch}"

    # Get the remote URL so we can inject the token directly into it.
    # This is the most reliable approach for GitHub HTTPS — avoids any
    # askpass timing / credential-helper interaction.
    authed_url: str | None = None
    if _github_token:
        try:
            url_result = subprocess.run(
                ["git", "-C", str(repo_path), "remote", "get-url", remote_name],
                capture_output=True,
                text=True,
            )
            raw_url = url_result.stdout.strip()
            if raw_url.startswith("https://"):
                # Insert "x-token:<PAT>@" after "https://"
                safe_token = _github_token.replace("@", "%40")
                authed_url = raw_url.replace(
                    "https://",
                    f"https://x-token:{safe_token}@",
                    1,
                )
                # Strip any existing user:pass that may already be in the URL
                # (keep only the first occurrence we just inserted)
        except Exception:
            pass

    if authed_url:
        push_args = ["git", "-C", str(repo_path), "push", authed_url]
        if refspec:
            push_args.append(refspec)
    else:
        push_args = ["git", "-C", str(repo_path), "push", remote_name]
        if refspec:
            push_args.append(refspec)

    try:
        push_result = subprocess.run(
            push_args,
            capture_output=True,
            text=True,
            env=_git_env(),
        )
    except OSError as exc:
        on_progress(repository, f"Push failed: {exc}")
        return PushResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="",
            error=str(exc),
        )

    push_stdout = push_result.stdout.strip()
    push_stderr = push_result.stderr.strip()

    if push_result.returncode == 0:
        summary = push_stderr.splitlines()[-1] if push_stderr else push_stdout.splitlines()[-1] if push_stdout else "Push successful"
        on_progress(repository, f"✓ Done: {summary}")
        return PushResult(
            repository=repository,
            branch_name=branch.name,
            success=True,
            output=push_stdout or push_stderr,
            error="",
        )

    err_line = push_stderr.splitlines()[0] if push_stderr else f"git push exited {push_result.returncode}"
    on_progress(repository, f"✗ Push failed: {err_line}")
    return PushResult(
        repository=repository,
        branch_name=branch.name,
        success=False,
        output=push_stdout,
        error=push_stderr or err_line,
    )


def commit_overview_files(
    repository: GitRepository,
    branch: GitBranch,
    limit: int = 30,
) -> list[tuple[str, str, str]]:
    """Return file rows for the commit popup.

    Rows include local worktree changes and files from recent committed changes.
    Each row is (source, status, path) where source is either "Local" or
    a short commit SHA.
    """
    if shutil.which("git") is None:
        return []

    local_rows = _local_change_file_rows(repository.path)
    committed_rows = _committed_change_file_rows(repository.path, branch.name, limit)
    return local_rows + committed_rows


def _local_change_file_rows(repo_path: Path) -> list[tuple[str, str, str]]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    rows: list[tuple[str, str, str]] = []
    for raw_line in completed.stdout.splitlines():
        parsed = _parse_porcelain_line(raw_line)
        if parsed is None:
            continue
        status, path = parsed
        rows.append(("Local", status, path))

    return rows


def _committed_change_file_rows(
    repo_path: Path,
    branch_name: str,
    limit: int,
) -> list[tuple[str, str, str]]:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "log",
                branch_name,
                "-n",
                str(limit),
                "--name-status",
                "--pretty=format:__COMMIT__%h",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    rows: list[tuple[str, str, str]] = []
    commit_sha = ""
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("__COMMIT__"):
            commit_sha = line.replace("__COMMIT__", "", 1).strip() or "-"
            continue

        parsed = _parse_name_status_line(line)
        if parsed is None:
            continue
        status, path = parsed
        rows.append((commit_sha or "-", status, path))

    return rows


def _parse_porcelain_line(raw_line: str) -> tuple[str, str] | None:
    if len(raw_line) < 3:
        return None

    status_code = raw_line[:2]
    path_part = raw_line[3:].strip()
    if not path_part:
        return None

    display_path = path_part.split(" -> ")[-1].strip() or path_part
    status = status_code.strip() or "M"
    return status, display_path


def _parse_name_status_line(raw_line: str) -> tuple[str, str] | None:
    parts = raw_line.split("\t")
    if len(parts) < 2:
        return None

    status = parts[0].strip() or "-"
    if len(parts) == 2:
        display_path = parts[1].strip() or "-"
    else:
        display_path = parts[-1].strip() or "-"

    return status, display_path


def commit_local_changes(
    repository: GitRepository,
    branch: GitBranch,
    commit_message: str,
) -> CommitResult:
    """Stage and commit local changes for the active branch."""
    if shutil.which("git") is None:
        return CommitResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="",
            error="git not found on PATH",
            created_commit=False,
        )

    try:
        add_result = subprocess.run(
            ["git", "-C", str(repository.path), "add", "-A"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return CommitResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="",
            error=str(exc),
            created_commit=False,
        )

    if add_result.returncode != 0:
        return CommitResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output=add_result.stdout.strip(),
            error=add_result.stderr.strip() or "Failed to stage changes.",
            created_commit=False,
        )

    try:
        commit_result = subprocess.run(
            ["git", "-C", str(repository.path), "commit", "-m", commit_message],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return CommitResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="",
            error=str(exc),
            created_commit=False,
        )

    stdout = commit_result.stdout.strip()
    stderr = commit_result.stderr.strip()
    nothing_to_commit = (
        commit_result.returncode == 1
        and ("nothing to commit" in stdout or "nothing to commit" in stderr)
    )

    if nothing_to_commit:
        return CommitResult(
            repository=repository,
            branch_name=branch.name,
            success=True,
            output=stdout or stderr,
            error="",
            created_commit=False,
        )

    if commit_result.returncode != 0:
        return CommitResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output=stdout,
            error=stderr or f"git commit exited {commit_result.returncode}",
            created_commit=False,
        )

    return CommitResult(
        repository=repository,
        branch_name=branch.name,
        success=True,
        output=stdout,
        error="",
        created_commit=True,
    )


def push_branch_commits(
    repository: GitRepository,
    branch: GitBranch,
) -> PushResult:
    """Push all local commits for a branch without creating a new commit."""
    if shutil.which("git") is None:
        return PushResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="",
            error="git not found on PATH",
        )

    remote_name = "origin"
    refspec: str | None = None
    if branch.upstream and "/" in branch.upstream:
        remote_name, remote_branch = branch.upstream.split("/", 1)
        refspec = f"{branch.name}:{remote_branch}"

    authed_url: str | None = None
    if _github_token:
        try:
            url_result = subprocess.run(
                ["git", "-C", str(repository.path), "remote", "get-url", remote_name],
                capture_output=True,
                text=True,
            )
            raw_url = url_result.stdout.strip()
            if raw_url.startswith("https://"):
                safe_token = _github_token.replace("@", "%40")
                authed_url = raw_url.replace(
                    "https://",
                    f"https://x-token:{safe_token}@",
                    1,
                )
        except Exception:
            authed_url = None

    if authed_url:
        push_args = ["git", "-C", str(repository.path), "push", authed_url]
    else:
        push_args = ["git", "-C", str(repository.path), "push", remote_name]

    if refspec:
        push_args.append(refspec)

    try:
        push_result = subprocess.run(
            push_args,
            capture_output=True,
            text=True,
            env=_git_env(),
        )
    except OSError as exc:
        return PushResult(
            repository=repository,
            branch_name=branch.name,
            success=False,
            output="",
            error=str(exc),
        )

    stdout = push_result.stdout.strip()
    stderr = push_result.stderr.strip()
    if push_result.returncode == 0:
        return PushResult(
            repository=repository,
            branch_name=branch.name,
            success=True,
            output=stdout or stderr,
            error="",
        )

    return PushResult(
        repository=repository,
        branch_name=branch.name,
        success=False,
        output=stdout,
        error=stderr or f"git push exited {push_result.returncode}",
    )

