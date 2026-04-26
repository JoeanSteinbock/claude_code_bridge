"""Helpers for the `/mount` Telegram command (clone repo + spawn bot).

The `/mount <git-url>` flow is owned by the manager bot (mate404_bot)
and produces a fully-booted CCB project (tmux session, telegramd,
dedicated bot) from a single user message + one tap on a t.me/newbot
link. State that needs to survive a daemon restart lives in
`<project_root>/.ccb/pending_mounts.json`; per-process state (the
`threading.Event` used to coordinate clone-done with the
`update["managed_bot"]` arrival) lives on the daemon instance.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any

PENDING_FILE = "pending_mounts.json"
TTL_SECONDS = 24 * 3600
_PROJECTS_ROOT = Path(os.environ.get("CCB_PROJECTS_ROOT") or Path.home() / "projects")

# Allow-list of git hosts. Keep narrow on purpose — broader URLs are
# Phase 2. Both https and ssh forms accepted for github (the most
# common case here).
_URL_RE = re.compile(
    r"^(?:"
    r"https://(?P<host_https>github\.com|gitlab\.com|bitbucket\.org)/(?P<owner_h>[\w.-]+)/(?P<repo_h>[\w.-]+?)(?:\.git)?/?"
    r"|"
    r"git@(?P<host_ssh>github\.com|gitlab\.com|bitbucket\.org):(?P<owner_s>[\w.-]+)/(?P<repo_s>[\w.-]+?)(?:\.git)?/?"
    r")$"
)

_USERNAME_BAD = re.compile(r"[^a-z0-9_]")


def parse_repo_url(url: str) -> tuple[str, str] | None:
    """Return (owner, repo) for a supported git URL, else None."""
    if not url:
        return None
    m = _URL_RE.match(url.strip())
    if not m:
        return None
    owner = m.group("owner_h") or m.group("owner_s")
    repo = m.group("repo_h") or m.group("repo_s")
    return (owner, repo) if owner and repo else None


def suggested_username(owner: str, repo: str) -> str:
    """Build a Telegram-legal bot username from owner+repo. Telegram
    requires <=32 chars, alphanumeric + underscore, must end in `_bot`.
    Strategy: lowercase, strip illegal chars, suffix `_ccb_bot`,
    truncate the body to make it fit."""
    raw = f"{owner}_{repo}".lower()
    raw = _USERNAME_BAD.sub("_", raw)
    suffix = "_ccb_bot"
    body_max = 32 - len(suffix)
    return raw[:body_max] + suffix


def target_path(owner: str, repo: str) -> Path:
    """`~/projects/<owner>/<repo>` — matches the existing fleet layout."""
    return _PROJECTS_ROOT / owner / repo


def is_clone_complete(target: Path) -> bool:
    """Cheap restart-recovery check: directory exists and looks like a git repo."""
    return target.is_dir() and (target / ".git").exists()


def clone_repo(url: str, dest: Path) -> tuple[bool, str]:
    """Clone (or fetch if already there). Synchronous; the daemon runs
    this off-thread. Returns (ok, human-readable message)."""
    if is_clone_complete(dest):
        try:
            r = subprocess.run(
                ["git", "fetch", "--all", "--prune"],
                cwd=dest, capture_output=True, text=True, timeout=300,
            )
            if r.returncode != 0:
                return False, f"fetch failed: {(r.stderr or r.stdout).strip()[:300]}"
            return True, "fetched (already cloned)"
        except Exception as exc:
            return False, f"fetch error: {exc}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["git", "clone", url, str(dest)],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            return False, f"clone failed: {(r.stderr or r.stdout).strip()[:300]}"
        return True, "cloned"
    except Exception as exc:
        return False, f"clone error: {exc}"


def _state_path(project_root: Path) -> Path:
    return project_root / ".ccb" / PENDING_FILE


def load_pending(project_root: Path) -> dict[str, dict[str, Any]]:
    p = _state_path(project_root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_pending(project_root: Path, data: dict[str, dict[str, Any]]) -> None:
    p = _state_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(p)


def add_pending(
    project_root: Path,
    suggested: str,
    chat_id: str | int,
    target: str,
    url: str,
) -> None:
    """MUST be called BEFORE spawning the clone thread or sending the
    inline keyboard — otherwise a fast user tap can race the JSON write
    and `_handle_managed_bot` won't find the entry."""
    data = load_pending(project_root)
    data[suggested] = {
        "chat_id": str(chat_id),
        "target": str(target),
        "url": url,
        "created_at": time.time(),
    }
    save_pending(project_root, data)


def pop_pending(project_root: Path, suggested: str) -> dict[str, Any] | None:
    data = load_pending(project_root)
    entry = data.pop(suggested, None)
    if entry is not None:
        save_pending(project_root, data)
    return entry


def gc_pending(project_root: Path, ttl_seconds: int = TTL_SECONDS) -> list[dict[str, Any]]:
    """Remove entries older than TTL. Returns the dropped entries so the
    caller can DM the user that their mount expired."""
    data = load_pending(project_root)
    now = time.time()
    dropped: list[dict[str, Any]] = []
    keep: dict[str, dict[str, Any]] = {}
    for name, entry in data.items():
        age = now - float(entry.get("created_at") or 0)
        if age > ttl_seconds:
            entry["_suggested"] = name
            dropped.append(entry)
        else:
            keep[name] = entry
    if dropped:
        save_pending(project_root, keep)
    return dropped


def recover_pending(daemon, project_root: Path) -> None:
    """Called once at daemon startup, before the polling loop. For each
    surviving pending entry: register a `threading.Event` on `daemon`
    so `_handle_managed_bot` can await clone-done; if the clone already
    finished (target is a git repo), pre-set the event; otherwise
    re-spawn the clone thread."""
    # Drop expired entries first; DM the user for each.
    for entry in gc_pending(project_root):
        try:
            daemon._send_text(
                entry.get("chat_id", ""),
                f"⌛ /mount @{entry.get('_suggested')} expired (24h, never tapped).",
            )
        except Exception:
            pass

    for suggested, entry in load_pending(project_root).items():
        ev = Event()
        # daemon owns the registry; module just writes to it.
        daemon._clone_events[suggested] = ev
        target = Path(entry["target"])
        if is_clone_complete(target):
            ev.set()
            continue
        # Clone never finished (or never started). Re-queue.
        Thread(
            target=daemon._clone_repo_bg,
            args=(entry["url"], target, suggested, ev),
            daemon=True,
            name=f"ccb-mount-clone-{suggested}",
        ).start()
