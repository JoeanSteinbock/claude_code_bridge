"""
process_lock.py - Per-provider, per-directory file lock to serialize request-response cycles.

Each provider (codex, gemini, opencode) has its own lock file per working directory,
allowing concurrent use across different directories while ensuring serial access
within the same directory.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Optional


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return True  # Assume alive if we can't check
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


class ProviderLock:
    """Per-provider, per-directory file lock to serialize request-response cycles.

    Lock files are stored in ~/.ccb/run/{provider}-{cwd_hash}.lock
    """

    def __init__(self, provider: str, timeout: float = 60.0, cwd: str = None):
        """Initialize lock for a specific provider and working directory.

        Args:
            provider: One of "codex", "gemini", "opencode"
            timeout: Max seconds to wait for lock acquisition
            cwd: Working directory for lock scope (defaults to current directory)
        """
        self.provider = provider
        self.timeout = timeout
        self.lock_dir = Path.home() / ".ccb" / "run"

        # Use working directory hash for per-directory locking
        if cwd is None:
            cwd = os.getcwd()
        cwd_hash = hashlib.md5(cwd.encode()).hexdigest()[:8]
        self.lock_file = self.lock_dir / f"{provider}-{cwd_hash}.lock"
        self._fd: Optional[int] = None
        self._acquired = False

    def _try_acquire_once(self) -> bool:
        """Attempt to acquire lock once without blocking."""
        try:
            if os.name == "nt":
                import msvcrt
                # Ensure the file has at least 1 byte so region locking is reliable on Windows.
                try:
                    st = os.fstat(self._fd)
                    if getattr(st, "st_size", 0) < 1:
                        os.lseek(self._fd, 0, os.SEEK_SET)
                        os.write(self._fd, b"\0")
                except Exception:
                    pass
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Write PID for debugging and stale lock detection
            pid_bytes = f"{os.getpid()}\n".encode()
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, pid_bytes)
            # Keep file length >= 1 on Windows to avoid invalidating the locked region.
            if os.name == "nt":
                try:
                    os.ftruncate(self._fd, max(1, len(pid_bytes)))
                except Exception:
                    pass
            else:
                os.ftruncate(self._fd, len(pid_bytes))
            self._acquired = True
            return True
        except (OSError, IOError):
            return False

    def _check_stale_lock(self) -> bool:
        """Check if current lock holder is dead, allowing us to take over.

        Uses rename-then-unlink to close the race where two callers both
        decide the lock is stale at the same time. Only one of them wins the
        rename; the loser sees ENOENT and bails out.
        """
        try:
            with open(self.lock_file, "r") as f:
                content = f.read().strip()
            if not content:
                return False
            try:
                pid = int(content)
            except ValueError:
                return False
            if _is_pid_alive(pid):
                return False

            # Atomic rename: only one concurrent caller can take ownership of
            # the stale file. Losers get FileNotFoundError on the rename and
            # return False so they retry acquire on the fresh inode the winner
            # will create.
            stash = self.lock_file.with_name(
                f"{self.lock_file.name}.stale.{os.getpid()}.{int(time.time() * 1000)}"
            )
            try:
                os.rename(str(self.lock_file), str(stash))
            except OSError:
                return False
            try:
                stash.unlink()
            except OSError:
                pass
            return True
        except OSError:
            return False

    def try_acquire(self) -> bool:
        """Try to acquire lock without blocking. Returns immediately.

        Returns:
            True if lock acquired, False if lock is held by another process
        """
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0))

        if self._try_acquire_once():
            return True

        # Check for stale lock
        if self._check_stale_lock():
            os.close(self._fd)
            self._fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
            if self._try_acquire_once():
                return True

        # Failed - close fd
        os.close(self._fd)
        self._fd = None
        return False

    def acquire(self) -> bool:
        """Acquire the lock, waiting up to timeout seconds.

        Returns:
            True if lock acquired, False if timeout
        """
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0))

        deadline = time.time() + self.timeout
        stale_checked = False

        while time.time() < deadline:
            if self._try_acquire_once():
                return True

            # Check for stale lock once after first failure
            if not stale_checked:
                stale_checked = True
                if self._check_stale_lock():
                    # Lock file was stale, reopen and retry
                    os.close(self._fd)
                    self._fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
                    if self._try_acquire_once():
                        return True

            time.sleep(0.1)

        # Timeout - close fd
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        return False

    def release(self) -> None:
        """Release the lock."""
        if self._fd is not None:
            try:
                if self._acquired:
                    if os.name == "nt":
                        import msvcrt
                        try:
                            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
                    else:
                        import fcntl
                        try:
                            fcntl.flock(self._fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
                self._acquired = False

    def __enter__(self) -> "ProviderLock":
        if not self.acquire():
            raise TimeoutError(f"Failed to acquire {self.provider} lock after {self.timeout}s")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
