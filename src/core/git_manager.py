"""
Git Integration for Cortex AI Agent IDE
"""

import os
import subprocess
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

# Suppress console windows for subprocess calls in frozen exe on Windows
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
from PyQt6.QtCore import QObject, pyqtSignal
from src.utils.logger import get_logger

log = get_logger("git_manager")


class GitStatus(Enum):
    MODIFIED = "M"
    ADDED = "A"
    DELETED = "D"
    RENAMED = "R"
    COPIED = "C"
    UPDATED = "U"
    UNTRACKED = "??"
    IGNORED = "!!"


@dataclass
class GitFile:
    """Represents a file with git status."""
    path: str
    status: GitStatus
    staged: bool
    old_path: Optional[str] = None  # For renamed files


@dataclass
class GitCommit:
    """Represents a git commit."""
    hash: str
    short_hash: str
    message: str
    author: str
    date: str


class GitManager(QObject):
    """Manages Git operations for the project."""
    
    status_changed = pyqtSignal()  # Emitted when git status changes
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._repo_path: Optional[str] = None
        self._available = self._check_git_available()
        
    def _check_git_available(self) -> bool:
        """Check if git is available on the system."""
        try:
            result = subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=5,
                creationflags=_NO_WINDOW
            )
            return result.returncode == 0
        except:
            return False
            
    def set_repository(self, path: str) -> bool:
        """Set the repository path."""
        if not self._available:
            return False
            
        # Check if it's a git repository
        git_dir = Path(path) / ".git"
        if git_dir.exists():
            self._repo_path = path
            return True
            
        # Check parent directories
        current = Path(path)
        while current.parent != current:
            git_dir = current / ".git"
            if git_dir.exists():
                self._repo_path = str(current)
                return True
            current = current.parent
            
        return False
        
    def is_repo(self) -> bool:
        """Check if current path is a git repository."""
        return self._repo_path is not None
        
    def _run_git(self, args: List[str], timeout: int = 30) -> Tuple[bool, str, str]:
        """Run a git command."""
        if not self._available or not self._repo_path:
            return False, "", "Git not available"
            
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=self._repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding='utf-8',
                errors='replace',
                creationflags=_NO_WINDOW
            )
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", "Command timed out"
        except Exception as e:
            return False, "", str(e)
            
    def get_status(self) -> List[GitFile]:
        """Get the current git status."""
        files = []
        
        success, stdout, stderr = self._run_git([
            "status", "--porcelain=v1", "-z", "-u"
        ])
        
        if not success:
            log.error(f"Git status failed: {stderr}")
            return files

        entries = stdout.split('\0')
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if not entry or len(entry) < 3:
                continue

            x = entry[0]  # Staged status
            y = entry[1]  # Unstaged status
            path_part = entry[3:]
            old_path = None

            # In -z porcelain output, renames/copies are emitted as:
            # "XY old_path\0new_path\0"
            if (x in ('R', 'C') or y in ('R', 'C')) and i < len(entries) and entries[i]:
                old_path = path_part
                path_part = entries[i]
                i += 1

            status_code = x if x not in (' ', '?') else y
            status = self._parse_status(status_code)
            staged = x not in (' ', '?')

            files.append(GitFile(
                path=path_part,
                status=status,
                staged=staged,
                old_path=old_path
            ))
            
        return files
        
    def _parse_status(self, code: str) -> GitStatus:
        """Parse status code."""
        status_map = {
            'M': GitStatus.MODIFIED,
            'A': GitStatus.ADDED,
            'D': GitStatus.DELETED,
            'R': GitStatus.RENAMED,
            'C': GitStatus.COPIED,
            'U': GitStatus.UPDATED,
            '?': GitStatus.UNTRACKED,
            '!': GitStatus.IGNORED,
        }
        return status_map.get(code, GitStatus.UNTRACKED)
        
    def stage_file(self, file_path: str) -> bool:
        """Stage a file."""
        success, _, stderr = self._run_git(["add", file_path])
        if success:
            self.status_changed.emit()
        else:
            log.error(f"Git add failed: {stderr}")
        return success
        
    def unstage_file(self, file_path: str) -> bool:
        """Unstage a file."""
        success, _, stderr = self._run_git(["reset", "HEAD", file_path])
        if success:
            self.status_changed.emit()
        else:
            log.error(f"Git unstage failed: {stderr}")
        return success
        
    def discard_changes(self, file_path: str) -> bool:
        """Discard changes in a file."""
        success, _, stderr = self._run_git(["checkout", "--", file_path])
        if success:
            self.status_changed.emit()
        else:
            log.error(f"Git checkout failed: {stderr}")
        return success
        
    def commit(self, message: str, amend: bool = False) -> Tuple[bool, str]:
        """Commit staged changes. Returns (success, error_message)."""
        args = ["commit", "-m", message]
        if amend:
            args.append("--amend")
        success, stdout, stderr = self._run_git(args)
        if success:
            self.status_changed.emit()
            return True, ""
        else:
            error_msg = stderr.strip() or stdout.strip() or "Unknown error"
            log.error(f"Git commit failed: {error_msg}")
            return False, error_msg
        
    def get_commits(self, count: int = 20) -> List[GitCommit]:
        """Get recent commits."""
        commits = []
        
        success, stdout, stderr = self._run_git([
            "log", f"-{count}",
            "--pretty=format:%H|%h|%s|%an|%ad",
            "--date=short"
        ])
        
        if not success:
            return commits
            
        for line in stdout.strip().split('\n'):
            if not line:
                continue
                
            parts = line.split('|')
            if len(parts) >= 5:
                commits.append(GitCommit(
                    hash=parts[0],
                    short_hash=parts[1],
                    message=parts[2],
                    author=parts[3],
                    date=parts[4]
                ))
                
        return commits
        
    def get_diff(self, file_path: str = None, staged: bool = False) -> str:
        """Get diff for a file or all changes."""
        args = ["diff"]
        if staged:
            args.append("--cached")
        if file_path:
            args.append(file_path)
            
        success, stdout, stderr = self._run_git(args)
        return stdout if success else ""
        
    def get_branch(self) -> str:
        """Get current branch name."""
        success, stdout, stderr = self._run_git([
            "rev-parse", "--abbrev-ref", "HEAD"
        ])
        return stdout.strip() if success else ""
        
    def get_branches(self) -> List[str]:
        """Get list of branches."""
        success, stdout, stderr = self._run_git([
            "branch", "-a"
        ])
        
        branches = []
        if success:
            for line in stdout.strip().split('\n'):
                line = line.strip()
                if line.startswith('*'):
                    line = line[2:]  # Remove '* '
                if line:
                    branches.append(line)
                    
        return branches
        
    def checkout_branch(self, branch: str) -> bool:
        """Checkout a branch."""
        success, _, stderr = self._run_git(["checkout", branch])
        if success:
            self.status_changed.emit()
        return success
        
    def create_branch(self, branch: str, checkout: bool = False) -> bool:
        """Create a new branch."""
        args = ["branch", branch]
        success, _, stderr = self._run_git(args)
        
        if success and checkout:
            return self.checkout_branch(branch)
            
        return success
        
    def pull(self, remote: str = "origin", branch: str = None) -> Tuple[bool, str]:
        """Pull from remote."""
        args = ["pull", remote]
        if branch:
            args.append(branch)
            
        success, stdout, stderr = self._run_git(args, timeout=60)
        if success:
            self.status_changed.emit()
        return success, stderr if not success else stdout
        
    def push(self, remote: str = "origin", branch: str = None) -> Tuple[bool, str]:
        """Push to remote."""
        args = ["push", remote]
        if branch:
            args.append(branch)
            
        success, stdout, stderr = self._run_git(args, timeout=60)
        if success:
            self.status_changed.emit()
        return success, stderr if not success else stdout
        
    def get_file_status_icon(self, file_path: str) -> str:
        """Get status icon for a file."""
        files = self.get_status()
        for f in files:
            if f.path == file_path or f.path.endswith(file_path):
                if f.status == GitStatus.MODIFIED:
                    return "📝"
                elif f.status == GitStatus.ADDED or f.staged:
                    return "✅"
                elif f.status == GitStatus.UNTRACKED:
                    return "❓"
                elif f.status == GitStatus.DELETED:
                    return "🗑️"
        return ""
