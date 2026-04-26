"""Unit tests for `lib/telegram_bridge/_mount.py`.

Covers the pure helpers (URL parsing, username sanitization, target
path derivation) and the JSON pending-state lifecycle (add/pop/gc with
atomic write + TTL). Network-touching helpers (clone_repo, full
recover_pending) are exercised via subprocess monkeypatching so the
suite stays fast and offline.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

import pytest

from telegram_bridge import _mount


# ---------------------------------------------------------------------------
# parse_repo_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/anthropics/claude-code-bridge", ("anthropics", "claude-code-bridge")),
    ("https://github.com/anthropics/claude-code-bridge.git", ("anthropics", "claude-code-bridge")),
    ("https://github.com/anthropics/claude-code-bridge/", ("anthropics", "claude-code-bridge")),
    ("git@github.com:anthropics/claude-code-bridge.git", ("anthropics", "claude-code-bridge")),
    ("git@github.com:anthropics/claude-code-bridge", ("anthropics", "claude-code-bridge")),
    ("https://gitlab.com/foo/bar", ("foo", "bar")),
    ("https://gitlab.com/foo/bar.git", ("foo", "bar")),
    ("git@gitlab.com:foo/bar", ("foo", "bar")),
    ("https://bitbucket.org/team/proj", ("team", "proj")),
    ("https://bitbucket.org/team/proj.git", ("team", "proj")),
    ("git@bitbucket.org:team/proj", ("team", "proj")),
    # Owner / repo with dots, dashes, underscores.
    ("https://github.com/owner.sub/repo-name_v2", ("owner.sub", "repo-name_v2")),
])
def test_parse_repo_url_accepts_supported_hosts(url, expected):
    assert _mount.parse_repo_url(url) == expected


@pytest.mark.parametrize("url", [
    "",
    "   ",
    "https://example.com/foo/bar",
    "https://github.io/foo/bar",                # github.io is not github.com
    "https://github.com/owner",                  # missing repo
    "https://github.com/",                       # missing both
    "git@github.com:owner",                      # missing repo
    "ssh://git@github.com/owner/repo",           # ssh:// scheme not in allow-list
    "github.com/owner/repo",                     # no scheme
    "https://github.com/owner/repo extra",       # trailing junk
    "javascript:alert(1)",                       # injection attempt
    None,                                        # type: ignore[arg-type]
])
def test_parse_repo_url_rejects_unsupported(url):
    # type: ignore[arg-type] — we want to verify None doesn't crash.
    assert _mount.parse_repo_url(url) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# suggested_username
# ---------------------------------------------------------------------------

def test_suggested_username_short_owner_repo():
    assert _mount.suggested_username("anthropics", "ccb") == "anthropics_ccb_ccb_bot"


def test_suggested_username_truncates_to_32_chars_total():
    name = _mount.suggested_username("verylongorganizationname", "verylongrepositoryname")
    assert len(name) == 32
    assert name.endswith("_ccb_bot")


def test_suggested_username_sanitizes_bad_chars():
    # Dots, dashes, uppercase all collapse to underscore + lowercase.
    name = _mount.suggested_username("Owner.Sub", "Repo-Name.git")
    assert all(ch.isalnum() or ch == "_" for ch in name)
    assert name.endswith("_ccb_bot")


def test_suggested_username_lowercases():
    assert _mount.suggested_username("ABC", "DEF").startswith("abc_def")


# ---------------------------------------------------------------------------
# target_path
# ---------------------------------------------------------------------------

def test_target_path_under_projects_root(monkeypatch, tmp_path):
    monkeypatch.setattr(_mount, "_PROJECTS_ROOT", tmp_path / "projects")
    assert _mount.target_path("acme", "widget") == tmp_path / "projects" / "acme" / "widget"


# ---------------------------------------------------------------------------
# is_clone_complete
# ---------------------------------------------------------------------------

def test_is_clone_complete_true_when_git_dir_exists(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    assert _mount.is_clone_complete(repo) is True


def test_is_clone_complete_false_when_no_git_dir(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    assert _mount.is_clone_complete(repo) is False


def test_is_clone_complete_false_when_dir_missing(tmp_path):
    assert _mount.is_clone_complete(tmp_path / "nope") is False


# ---------------------------------------------------------------------------
# Pending-state JSON lifecycle (add / pop / gc / load / save)
# ---------------------------------------------------------------------------

def _make_project_root(tmp_path: Path) -> Path:
    """Create a `<tmp>/.ccb/` skeleton matching real layout."""
    root = tmp_path / "proj"
    (root / ".ccb").mkdir(parents=True)
    return root


def test_load_pending_returns_empty_when_file_missing(tmp_path):
    root = _make_project_root(tmp_path)
    assert _mount.load_pending(root) == {}


def test_load_pending_handles_corrupt_json(tmp_path):
    root = _make_project_root(tmp_path)
    (root / ".ccb" / _mount.PENDING_FILE).write_text("{not json}")
    # Corrupt file should not raise — just return empty.
    assert _mount.load_pending(root) == {}


def test_add_pending_then_load_round_trip(tmp_path):
    root = _make_project_root(tmp_path)
    _mount.add_pending(root, "acme_widget_ccb_bot", "12345", "/tmp/acme/widget", "https://github.com/acme/widget")
    data = _mount.load_pending(root)
    assert "acme_widget_ccb_bot" in data
    entry = data["acme_widget_ccb_bot"]
    assert entry["chat_id"] == "12345"
    assert entry["target"] == "/tmp/acme/widget"
    assert entry["url"] == "https://github.com/acme/widget"
    assert isinstance(entry["created_at"], (int, float))


def test_add_pending_writes_atomically(tmp_path):
    """`.tmp + replace` should leave no `.tmp` artifact behind."""
    root = _make_project_root(tmp_path)
    _mount.add_pending(root, "x_ccb_bot", "1", "/t", "u")
    leftover = list((root / ".ccb").glob("*.tmp"))
    assert leftover == []


def test_pop_pending_removes_entry(tmp_path):
    root = _make_project_root(tmp_path)
    _mount.add_pending(root, "x_ccb_bot", "1", "/t", "u")
    entry = _mount.pop_pending(root, "x_ccb_bot")
    assert entry is not None and entry["chat_id"] == "1"
    assert _mount.load_pending(root) == {}


def test_pop_pending_returns_none_for_missing(tmp_path):
    root = _make_project_root(tmp_path)
    assert _mount.pop_pending(root, "ghost_bot") is None


def test_gc_pending_drops_expired_entries(tmp_path):
    root = _make_project_root(tmp_path)
    # Hand-craft an entry with a far-past created_at to trigger expiry.
    expired = {
        "old_bot": {
            "chat_id": "1", "target": "/t", "url": "u",
            "created_at": time.time() - 86400 * 10,   # 10 days old
        },
        "fresh_bot": {
            "chat_id": "2", "target": "/t2", "url": "u2",
            "created_at": time.time(),
        },
    }
    _mount.save_pending(root, expired)
    dropped = _mount.gc_pending(root, ttl_seconds=86400)
    assert len(dropped) == 1
    assert dropped[0]["_suggested"] == "old_bot"
    remaining = _mount.load_pending(root)
    assert "fresh_bot" in remaining and "old_bot" not in remaining


def test_gc_pending_no_op_when_all_fresh(tmp_path):
    root = _make_project_root(tmp_path)
    _mount.add_pending(root, "fresh", "1", "/t", "u")
    assert _mount.gc_pending(root, ttl_seconds=86400) == []
    assert "fresh" in _mount.load_pending(root)


def test_pop_then_re_add_keeps_state_consistent(tmp_path):
    """Mirrors the `_handle_managed_bot` clone-timeout path: pop, then
    re-add when the user needs to re-tap. The entry should be back."""
    root = _make_project_root(tmp_path)
    _mount.add_pending(root, "x_bot", "1", "/t", "u")
    entry = _mount.pop_pending(root, "x_bot")
    assert entry is not None
    _mount.add_pending(root, "x_bot", entry["chat_id"], entry["target"], entry["url"])
    assert "x_bot" in _mount.load_pending(root)


# ---------------------------------------------------------------------------
# clone_repo (subprocess monkeypatched to keep tests offline)
# ---------------------------------------------------------------------------

class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_clone_repo_runs_git_clone_when_target_missing(tmp_path):
    target = tmp_path / "out" / "repo"
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # Pretend git made the dir + .git
        target.mkdir(parents=True, exist_ok=True)
        (target / ".git").mkdir()
        return _FakeCompleted(returncode=0, stdout="Cloning into ...")

    with patch.object(_mount.subprocess, "run", side_effect=fake_run):
        ok, msg = _mount.clone_repo("https://github.com/x/y", target)

    assert ok is True
    assert "cloned" in msg.lower()
    assert captured["cmd"][:2] == ["git", "clone"]
    assert captured["cmd"][2] == "https://github.com/x/y"
    assert captured["cmd"][3] == str(target)


def test_clone_repo_runs_git_fetch_when_target_already_clone(tmp_path):
    target = tmp_path / "repo"
    (target / ".git").mkdir(parents=True)
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return _FakeCompleted(returncode=0, stdout="Fetching origin")

    with patch.object(_mount.subprocess, "run", side_effect=fake_run):
        ok, msg = _mount.clone_repo("https://github.com/x/y", target)

    assert ok is True
    assert "fetch" in msg.lower()
    assert captured["cmd"][:2] == ["git", "fetch"]
    assert captured["cwd"] == target


def test_clone_repo_reports_clone_failure(tmp_path):
    target = tmp_path / "repo"

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(returncode=128, stderr="fatal: repo not found")

    with patch.object(_mount.subprocess, "run", side_effect=fake_run):
        ok, msg = _mount.clone_repo("https://github.com/x/missing", target)

    assert ok is False
    assert "clone failed" in msg
    assert "repo not found" in msg


def test_clone_repo_reports_fetch_failure(tmp_path):
    target = tmp_path / "repo"
    (target / ".git").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(returncode=1, stderr="fatal: bad remote")

    with patch.object(_mount.subprocess, "run", side_effect=fake_run):
        ok, msg = _mount.clone_repo("https://github.com/x/y", target)

    assert ok is False
    assert "fetch failed" in msg


def test_clone_repo_handles_subprocess_exception(tmp_path):
    target = tmp_path / "repo"

    with patch.object(_mount.subprocess, "run", side_effect=OSError("no git")):
        ok, msg = _mount.clone_repo("https://github.com/x/y", target)

    assert ok is False
    assert "clone error" in msg


# ---------------------------------------------------------------------------
# recover_pending — the daemon-restart recovery contract
# ---------------------------------------------------------------------------

class _StubDaemon:
    """Minimal stand-in for TelegramDaemon used by recover_pending. Only
    needs the surface that recover_pending touches: _clone_events,
    _clone_repo_bg, and _send_text."""
    def __init__(self):
        self._clone_events: dict[str, Event] = {}
        self.cloned: list[tuple[str, Path, str]] = []
        self.sent: list[tuple[str, str]] = []

    def _clone_repo_bg(self, url: str, target: Path, suggested: str, ev: Event):
        self.cloned.append((url, target, suggested))
        ev.set()

    def _send_text(self, chat_id: str, text: str, **_kwargs):
        self.sent.append((chat_id, text))


def test_recover_pending_presets_event_for_already_cloned(tmp_path, monkeypatch):
    monkeypatch.setattr(_mount, "_PROJECTS_ROOT", tmp_path / "projects")
    root = _make_project_root(tmp_path)
    cloned = tmp_path / "projects" / "acme" / "widget"
    (cloned / ".git").mkdir(parents=True)
    _mount.add_pending(root, "acme_widget_ccb_bot", "1", str(cloned), "https://github.com/acme/widget")

    daemon = _StubDaemon()
    _mount.recover_pending(daemon, root)

    ev = daemon._clone_events.get("acme_widget_ccb_bot")
    assert ev is not None and ev.is_set()
    assert daemon.cloned == []   # no re-clone triggered


def test_recover_pending_requeues_clone_when_target_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(_mount, "_PROJECTS_ROOT", tmp_path / "projects")
    root = _make_project_root(tmp_path)
    target = tmp_path / "projects" / "acme" / "widget"
    _mount.add_pending(root, "acme_widget_ccb_bot", "1", str(target), "https://github.com/acme/widget")

    daemon = _StubDaemon()
    _mount.recover_pending(daemon, root)

    # The stub _clone_repo_bg runs synchronously (we ignore the Thread
    # wrapping recover_pending uses) — instead we just verify the
    # daemon's event is registered. The real clone runs in a thread;
    # for unit purposes, asserting registration is enough.
    assert "acme_widget_ccb_bot" in daemon._clone_events


def test_recover_pending_dms_user_when_entry_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(_mount, "_PROJECTS_ROOT", tmp_path / "projects")
    root = _make_project_root(tmp_path)
    # Pre-populate with an expired entry (TTL is 24h by default).
    _mount.save_pending(root, {
        "old_bot": {
            "chat_id": "777",
            "target": str(tmp_path / "old"),
            "url": "https://github.com/old/old",
            "created_at": time.time() - 86400 * 5,
        },
    })

    daemon = _StubDaemon()
    _mount.recover_pending(daemon, root)

    # Expired entry should have been DM'd and dropped.
    assert any("777" in chat_id and "expired" in text.lower() for chat_id, text in daemon.sent)
    assert "old_bot" not in _mount.load_pending(root)
    assert "old_bot" not in daemon._clone_events
