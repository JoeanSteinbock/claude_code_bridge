"""
Pane Output Monitor for CCB Mail System.

Monitors AI pane output and triggers email notifications when tasks complete.
"""

import os
import re
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Callable, List
from threading import Thread, Event

from .config import MailConfig, PaneHookConfig, SUPPORTED_PROVIDERS


# Completion signal patterns
COMPLETION_PATTERNS = [
    r"CCB_DONE",
    r"^\s*>\s*$",  # Claude prompt
    r"^\s*codex>\s*$",  # Codex prompt
    r"^\s*(?:gemini|agy)>\s*$",  # Gemini/AGY prompt
    r"^\s*claude>\s*$",  # Claude prompt variant
]


@dataclass
class PaneWatcher:
    """Watcher for a single pane's output."""
    provider: str
    log_path: Path
    last_position: int = 0
    last_output: str = ""
    last_check_time: float = 0.0
    is_complete: bool = False


@dataclass
class OutputEvent:
    """Event representing new pane output."""
    provider: str
    output: str
    is_complete: bool
    timestamp: float = field(default_factory=time.time)


class PaneOutputMonitor:
    """Monitor pane output and trigger email notifications."""

    def __init__(
        self,
        config: MailConfig,
        on_output: Optional[Callable[[OutputEvent], None]] = None,
    ):
        self.config = config
        self.on_output = on_output
        self.watchers: Dict[str, PaneWatcher] = {}
        self._stop_event = Event()
        self._thread: Optional[Thread] = None
        self._lock = threading.Lock()
        self._completion_patterns = [re.compile(p, re.MULTILINE) for p in COMPLETION_PATTERNS]

    def _get_pane_log_path(self, provider: str) -> Optional[Path]:
        """Get the log file path for a provider's pane."""
        # Try askd_runtime first
        try:
            from askd_runtime import run_dir
            log_root = run_dir() / "pane-logs"
        except Exception:
            log_root = Path.home() / ".cache" / "ccb" / "pane-logs"

        # Check tmux logs
        tmux_log_dir = log_root / "tmux"
        if tmux_log_dir.exists():
            # Find the most recent log file for this provider
            for log_file in sorted(tmux_log_dir.glob("pane-*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
                return log_file

        # Check provider-specific log
        provider_log = log_root / provider / "output.log"
        if provider_log.exists():
            return provider_log

        return None

    def _detect_completion(self, text: str) -> bool:
        """Detect if the output indicates task completion."""
        for pattern in self._completion_patterns:
            if pattern.search(text):
                return True
        return False

    def start_watching(self, provider: str, log_path: Optional[Path] = None) -> bool:
        """Start watching a provider's pane output."""
        if provider not in SUPPORTED_PROVIDERS:
            return False

        with self._lock:
            if provider in self.watchers:
                return True  # Already watching

            path = log_path or self._get_pane_log_path(provider)
            if not path:
                return False

            watcher = PaneWatcher(
                provider=provider,
                log_path=path,
                last_position=path.stat().st_size if path.exists() else 0,
                last_check_time=time.time(),
            )
            self.watchers[provider] = watcher
            return True

    def stop_watching(self, provider: str) -> None:
        """Stop watching a provider's pane output."""
        with self._lock:
            self.watchers.pop(provider, None)

    def _check_watcher(self, watcher: PaneWatcher) -> Optional[OutputEvent]:
        """Check a watcher for new output."""
        if not watcher.log_path.exists():
            return None

        try:
            current_size = watcher.log_path.stat().st_size
            if current_size <= watcher.last_position:
                return None

            # Read new content
            with open(watcher.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(watcher.last_position)
                new_content = f.read()

            watcher.last_position = current_size
            watcher.last_check_time = time.time()

            if not new_content.strip():
                return None

            # Detect completion
            is_complete = self._detect_completion(new_content)
            watcher.is_complete = is_complete
            watcher.last_output = new_content

            return OutputEvent(
                provider=watcher.provider,
                output=new_content,
                is_complete=is_complete,
            )

        except Exception:
            return None

    def check_all(self) -> List[OutputEvent]:
        """Check all watchers for new output."""
        events = []
        with self._lock:
            for watcher in self.watchers.values():
                event = self._check_watcher(watcher)
                if event:
                    events.append(event)
        return events

    def start(self, interval: float = 1.0) -> None:
        """Start the monitoring loop."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = Thread(target=self._monitor_loop, args=(interval,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the monitoring loop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def is_running(self) -> bool:
        """Check if monitor is running."""
        return self._thread is not None and self._thread.is_alive()

    def _monitor_loop(self, interval: float) -> None:
        """Main monitoring loop."""
        while not self._stop_event.is_set():
            try:
                events = self.check_all()
                for event in events:
                    if self.on_output:
                        try:
                            self.on_output(event)
                        except Exception as e:
                            print(f"Error in output callback: {e}")
            except Exception as e:
                print(f"Monitor loop error: {e}")

            self._stop_event.wait(interval)

    def get_recent_output(self, provider: str, lines: int = 50) -> Optional[str]:
        """Get recent output from a provider's pane."""
        with self._lock:
            watcher = self.watchers.get(provider)
            if not watcher or not watcher.log_path.exists():
                return None

            try:
                with open(watcher.log_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                    all_lines = content.splitlines()
                    return "\n".join(all_lines[-lines:])
            except Exception:
                return None
