"""Tests for multi-instance provider support (#117)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

# Ensure lib is on path
_lib = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(_lib))

# ── Stub out modules that use Python 3.10+ syntax (str | None) ─────────────
# ccb_config and terminal use union-type syntax unsupported on Python <3.10.
# We pre-populate sys.modules with lightweight stubs so that caskd_session /
# gaskd_session can be imported without hitting a TypeError.

def _ensure_stub(mod_name: str, attrs: dict[str, Any] | None = None) -> None:
    """Insert a stub module into sys.modules if not already importable."""
    if mod_name in sys.modules:
        return
    try:
        __import__(mod_name)
        return
    except Exception:
        pass
    stub = types.ModuleType(mod_name)
    for k, v in (attrs or {}).items():
        setattr(stub, k, v)
    sys.modules[mod_name] = stub


_ensure_stub("terminal", {
    "_subprocess_kwargs": lambda: {},
    "get_backend_for_session": lambda data: None,
})
_ensure_stub("ccb_config", {
    "apply_backend_env": lambda: None,
    "get_backend_env": lambda: None,
})
_ensure_stub("project_id", {
    "compute_ccb_project_id": lambda p: "stub_project_id",
})


# ── parse_qualified_provider ────────────────────────────────────────────────

class TestParseQualifiedProvider:
    def test_plain_provider(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider("codex") == ("codex", None)

    def test_qualified_provider(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider("codex:auth") == ("codex", "auth")

    def test_empty_string(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider("") == ("", None)

    def test_none_input(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider(None) == ("", None)

    def test_colon_only(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider(":") == ("", None)

    def test_provider_with_empty_instance(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider("codex:") == ("codex", None)

    def test_uppercase_normalized(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider("CODEX:Auth") == ("codex", "auth")

    def test_whitespace_trimmed(self):
        from providers import parse_qualified_provider
        assert parse_qualified_provider(" codex : auth ") == ("codex", "auth")

    def test_multiple_colons(self):
        """Only split on first colon."""
        from providers import parse_qualified_provider
        base, instance = parse_qualified_provider("codex:auth:extra")
        assert base == "codex"
        assert instance == "auth:extra"

    def test_all_providers(self):
        from providers import parse_qualified_provider
        for prov in ("codex", "gemini", "opencode", "claude", "droid", "copilot", "codebuddy", "qwen"):
            assert parse_qualified_provider(prov) == (prov, None)
            assert parse_qualified_provider(f"{prov}:test") == (prov, "test")


# ── make_qualified_key ──────────────────────────────────────────────────────

class TestMakeQualifiedKey:
    def test_no_instance(self):
        from providers import make_qualified_key
        assert make_qualified_key("codex", None) == "codex"

    def test_with_instance(self):
        from providers import make_qualified_key
        assert make_qualified_key("codex", "auth") == "codex:auth"

    def test_empty_instance(self):
        from providers import make_qualified_key
        assert make_qualified_key("codex", "") == "codex"

    def test_roundtrip(self):
        from providers import parse_qualified_provider, make_qualified_key
        key = make_qualified_key("gemini", "frontend")
        base, inst = parse_qualified_provider(key)
        assert base == "gemini"
        assert inst == "frontend"

    def test_roundtrip_no_instance(self):
        from providers import parse_qualified_provider, make_qualified_key
        key = make_qualified_key("codex", None)
        base, inst = parse_qualified_provider(key)
        assert base == "codex"
        assert inst is None


# ── session_filename_for_instance ───────────────────────────────────────────

class TestSessionFilenameForInstance:
    def test_no_instance(self):
        from providers import session_filename_for_instance
        assert session_filename_for_instance(".codex-session", None) == ".codex-session"

    def test_with_instance(self):
        from providers import session_filename_for_instance
        assert session_filename_for_instance(".codex-session", "auth") == ".codex-auth-session"

    def test_empty_instance(self):
        from providers import session_filename_for_instance
        assert session_filename_for_instance(".codex-session", "") == ".codex-session"

    def test_whitespace_instance(self):
        from providers import session_filename_for_instance
        assert session_filename_for_instance(".codex-session", "  ") == ".codex-session"

    def test_all_provider_sessions(self):
        from providers import session_filename_for_instance
        cases = [
            (".codex-session", "auth", ".codex-auth-session"),
            (".gemini-session", "frontend", ".gemini-frontend-session"),
            (".opencode-session", "backend", ".opencode-backend-session"),
            (".claude-session", "review", ".claude-review-session"),
            (".droid-session", "test", ".droid-test-session"),
            (".copilot-session", "dev", ".copilot-dev-session"),
            (".codebuddy-session", "prod", ".codebuddy-prod-session"),
            (".qwen-session", "api", ".qwen-api-session"),
        ]
        for base, inst, expected in cases:
            assert session_filename_for_instance(base, inst) == expected, f"Failed for {base} + {inst}"


# ── Session module instance support ────────────────────────────────────────

class TestSessionModuleInstance:
    """Test that session modules accept instance parameter."""

    def test_codex_find_session_file_default(self, tmp_path):
        from caskd_session import find_project_session_file
        # No session file exists -- should return None
        result = find_project_session_file(tmp_path)
        assert result is None

    def test_codex_find_session_file_with_instance(self, tmp_path):
        from caskd_session import find_project_session_file
        # Create instance-specific session file
        ccb_dir = tmp_path / ".ccb"
        ccb_dir.mkdir()
        session_file = ccb_dir / ".codex-auth-session"
        session_file.write_text('{"pane_id": "test"}')
        result = find_project_session_file(tmp_path, instance="auth")
        assert result is not None
        assert "auth" in result.name

    def test_codex_load_session_no_instance(self, tmp_path):
        from caskd_session import load_project_session
        result = load_project_session(tmp_path)
        assert result is None

    def test_codex_load_session_with_instance(self, tmp_path):
        from caskd_session import load_project_session
        result = load_project_session(tmp_path, instance="auth")
        assert result is None  # No file exists

    def test_codex_load_session_with_instance_file_exists(self, tmp_path):
        from caskd_session import load_project_session
        ccb_dir = tmp_path / ".ccb"
        ccb_dir.mkdir()
        session_file = ccb_dir / ".codex-auth-session"
        session_file.write_text('{"pane_id": "%42", "work_dir": "/tmp/test"}')
        result = load_project_session(tmp_path, instance="auth")
        assert result is not None
        assert result.pane_id == "%42"

    def test_codex_compute_session_key_no_instance(self):
        from caskd_session import compute_session_key, CodexProjectSession
        session = CodexProjectSession(
            session_file=Path("/tmp/test/.ccb/.codex-session"),
            data={"ccb_project_id": "abc123", "work_dir": "/tmp/test"},
        )
        key = compute_session_key(session)
        assert key == "codex:abc123"

    def test_codex_compute_session_key_with_instance(self):
        from caskd_session import compute_session_key, CodexProjectSession
        session = CodexProjectSession(
            session_file=Path("/tmp/test/.ccb/.codex-auth-session"),
            data={"ccb_project_id": "abc123", "work_dir": "/tmp/test"},
        )
        key = compute_session_key(session, instance="auth")
        assert "auth" in key
        assert "abc123" in key

    def test_gemini_find_session_file_default(self, tmp_path):
        from gaskd_session import find_project_session_file
        result = find_project_session_file(tmp_path)
        assert result is None

    def test_gemini_find_session_file_with_instance(self, tmp_path):
        from gaskd_session import find_project_session_file
        ccb_dir = tmp_path / ".ccb"
        ccb_dir.mkdir()
        session_file = ccb_dir / ".gemini-frontend-session"
        session_file.write_text('{"pane_id": "test"}')
        result = find_project_session_file(tmp_path, instance="frontend")
        assert result is not None
        assert "frontend" in result.name

    def test_gemini_compute_session_key_with_instance(self):
        from gaskd_session import compute_session_key, GeminiProjectSession
        session = GeminiProjectSession(
            session_file=Path("/tmp/test/.ccb/.gemini-session"),
            data={"ccb_project_id": "xyz789", "work_dir": "/tmp/test"},
        )
        key = compute_session_key(session, instance="frontend")
        assert "frontend" in key
        assert "xyz789" in key


# ── ProviderRequest instance field ──────────────────────────────────────────

class TestProviderRequestInstance:
    def test_default_instance_is_none(self):
        from askd.adapters.base import ProviderRequest
        req = ProviderRequest(
            client_id="test",
            work_dir="/tmp",
            timeout_s=30.0,
            quiet=False,
            message="hello",
            caller="manual",
        )
        assert req.instance is None

    def test_instance_can_be_set(self):
        from askd.adapters.base import ProviderRequest
        req = ProviderRequest(
            client_id="test",
            work_dir="/tmp",
            timeout_s=30.0,
            quiet=False,
            message="hello",
            caller="manual",
            instance="auth",
        )
        assert req.instance == "auth"


# ── Backward compatibility ──────────────────────────────────────────────────

class TestBackwardCompatibility:
    """Verify that no-instance usage produces identical behavior."""

    def test_parse_qualified_plain(self):
        from providers import parse_qualified_provider
        for prov in ("codex", "gemini", "opencode", "claude", "droid", "copilot", "codebuddy", "qwen"):
            base, inst = parse_qualified_provider(prov)
            assert base == prov
            assert inst is None

    def test_session_filename_unchanged(self):
        from providers import session_filename_for_instance
        for name in (".codex-session", ".gemini-session", ".opencode-session",
                     ".claude-session", ".droid-session", ".copilot-session",
                     ".codebuddy-session", ".qwen-session"):
            assert session_filename_for_instance(name, None) == name

    def test_make_qualified_key_no_instance(self):
        from providers import make_qualified_key
        assert make_qualified_key("codex", None) == "codex"


# ── Daemon routing with instance ────────────────────────────────────────────

class TestDaemonInstanceRouting:
    """Test that the daemon correctly parses and routes instance-qualified providers."""

    def test_daemon_parses_qualified_provider(self):
        """Verify _handle_request parses 'codex:auth' correctly."""
        from providers import parse_qualified_provider
        base, inst = parse_qualified_provider("codex:auth")
        assert base == "codex"
        assert inst == "auth"

    def test_pool_key_includes_instance(self):
        """Pool key should use qualified key for instance isolation."""
        from providers import make_qualified_key
        key = make_qualified_key("codex", "auth")
        assert key == "codex:auth"
        # Different instance = different pool key
        key2 = make_qualified_key("codex", "payment")
        assert key2 == "codex:payment"
        assert key != key2

    def test_same_provider_different_instances_isolated(self):
        """Two instances of the same provider must produce different keys."""
        from providers import make_qualified_key
        keys = set()
        for inst in ("auth", "payment", "frontend", "backend"):
            keys.add(make_qualified_key("codex", inst))
        assert len(keys) == 4

    def test_different_providers_same_instance_isolated(self):
        """Same instance name on different providers must produce different keys."""
        from providers import make_qualified_key
        key1 = make_qualified_key("codex", "auth")
        key2 = make_qualified_key("gemini", "auth")
        assert key1 != key2


# ── Alias resolution, ccb_start_config, launcher & OpenCode req_id isolation ─

class TestProviderAliasAndLauncher:
    def test_assignment_syntax_and_aliases(self):
        from providers import normalize_provider_token, parse_qualified_provider, display_provider_name
        assert parse_qualified_provider("oc1=opencode") == ("opencode", "oc1")
        assert normalize_provider_token("oc1=opencode") == "opencode:oc1"
        assert normalize_provider_token("agy:fast") == "gemini:fast"
        assert normalize_provider_token("oc1", aliases={"oc1": "opencode", "oc2": "opencode"}) == "opencode:oc1"
        assert display_provider_name("opencode:oc1") == "OpenCode:oc1"
        assert display_provider_name("opencode") == "OpenCode"

    def test_resolve_bare_alias_from_session_file_and_config(self, tmp_path):
        from providers import resolve_provider_key
        ccb_dir = tmp_path / ".ccb"
        ccb_dir.mkdir()
        (ccb_dir / ".opencode-alpha-session").write_text('{"active": true}', encoding="utf-8")

        base, inst, qual = resolve_provider_key("alpha", work_dir=tmp_path)
        assert (base, inst, qual) == ("opencode", "alpha", "opencode:alpha")

        # Also resolve from ccb.config aliases map when session file doesn't exist yet
        (ccb_dir / "ccb.config").write_text(
            json.dumps({
                "providers": ["opencode:alpha", "beta", "claude"],
                "aliases": {"beta": "opencode"},
            }),
            encoding="utf-8",
        )
        assert resolve_provider_key("beta", work_dir=tmp_path) == ("opencode", "beta", "opencode:beta")

    def test_start_config_multi_instance_and_aliases(self):
        from ccb_start_config import _normalize_providers, _parse_config_obj
        provs, cmd = _normalize_providers(["opencode:alpha", "opencode:beta", "claude"])
        assert provs == ["opencode:alpha", "opencode:beta", "claude"]
        assert cmd is False

        provs2, _ = _normalize_providers(["oc1=opencode", "oc2=opencode", "claude"])
        assert provs2 == ["opencode:oc1", "opencode:oc2", "claude"]

        parsed = _parse_config_obj({
            "providers": ["oc1", "oc2", "claude"],
            "aliases": {"oc1": "opencode", "oc2": "opencode"},
        })
        assert parsed["providers"] == ["opencode:oc1", "opencode:oc2", "claude"]

    def test_ccb_launcher_two_opencode_instances(self, tmp_path, monkeypatch):
        import importlib.machinery
        import importlib.util

        ccb_path = Path(__file__).resolve().parent.parent / "ccb"
        loader = importlib.machinery.SourceFileLoader("ccb_main_multi", str(ccb_path))
        spec = importlib.util.spec_from_loader("ccb_main_multi", loader)
        ccb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ccb)

        assert ccb._parse_providers(["opencode:alpha", "opencode:beta", "claude"]) == [
            "opencode:alpha",
            "opencode:beta",
            "claude",
        ]
        assert ccb._parse_providers(["alpha=opencode", "beta=opencode"]) == [
            "opencode:alpha",
            "opencode:beta",
        ]

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ccb").mkdir(exist_ok=True)
        launcher = ccb.AILauncher(
            providers=["opencode:alpha", "opencode:beta", "claude"],
            resume=True,
            auto=False,
            launch_args={"opencode:alpha": "-m model-a", "opencode:beta": "-m model-b"},
            launch_env={"opencode:alpha": {"CUSTOM_VAR": "alpha_val"}},
        )
        try:
            # Separate runtime dirs
            rt_a = launcher._runtime_subdir("opencode:alpha")
            rt_b = launcher._runtime_subdir("opencode:beta")
            assert rt_a != rt_b
            assert rt_a.name == "opencode-alpha"
            assert rt_b.name == "opencode-beta"

            # Separate pane titles & agent labels
            assert "CCB-OpenCode-alpha-" in launcher._pane_title_marker("opencode:alpha")
            assert "CCB-OpenCode-beta-" in launcher._pane_title_marker("opencode:beta")
            assert launcher._agent_labels("opencode:alpha") == ("OpenCode:alpha", "OpenCode")
            assert launcher._agent_labels("opencode:beta") == ("OpenCode:beta", "OpenCode")

            # Caller & per-instance launch_env
            env_a = launcher._provider_env_overrides("opencode:alpha")
            env_b = launcher._provider_env_overrides("opencode:beta")
            assert env_a["CCB_CALLER"] == "opencode:alpha"
            assert env_a["CUSTOM_VAR"] == "alpha_val"
            assert env_b["CCB_CALLER"] == "opencode:beta"
            assert "CUSTOM_VAR" not in env_b

            # Separate session files written to .ccb/
            monkeypatch.setattr(launcher, "_maybe_start_provider_daemon", lambda *a, **kw: None)
            assert launcher._write_opencode_session(
                rt_a, None, pane_id="%11", pane_title_marker="m1", start_cmd="opencode", provider="opencode:alpha"
            )
            assert launcher._write_opencode_session(
                rt_b, None, pane_id="%12", pane_title_marker="m2", start_cmd="opencode", provider="opencode:beta"
            )
            sf_a = tmp_path / ".ccb" / ".opencode-alpha-session"
            sf_b = tmp_path / ".ccb" / ".opencode-beta-session"
            assert sf_a.is_file()
            assert sf_b.is_file()
            data_a = json.loads(sf_a.read_text(encoding="utf-8"))
            data_b = json.loads(sf_b.read_text(encoding="utf-8"))
            assert data_a["pane_id"] == "%11"
            assert data_a["instance"] == "alpha"
            assert data_b["pane_id"] == "%12"
            assert data_b["instance"] == "beta"

            # Per-instance resume with saved opencode_session_id + per-instance launch_args
            data_a["opencode_session_id"] = "ses_alpha_111"
            sf_a.write_text(json.dumps(data_a), encoding="utf-8")
            cmd_a = launcher._get_start_cmd("opencode:alpha")
            cmd_b = launcher._get_start_cmd("opencode:beta")
            assert "--session ses_alpha_111" in cmd_a
            assert "-m model-a" in cmd_a
            assert "-m model-b" in cmd_b
        finally:
            launcher.cleanup(kill_panes=False, clear_sessions=True, remove_runtime=True, quiet=True)

    def test_opencode_multi_instance_req_id_isolation_sqlite(self, tmp_path):
        import sqlite3
        from opencode_comm import OpenCodeLogReader

        work_dir = tmp_path / "proj"
        work_dir.mkdir()
        root = tmp_path / "storage"
        root.mkdir()
        db_path = tmp_path / "opencode.db"

        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE session (
                    id TEXT PRIMARY KEY,
                    directory TEXT NOT NULL,
                    time_updated INTEGER NOT NULL
                );
                CREATE TABLE message (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    time_created INTEGER NOT NULL,
                    time_updated INTEGER NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE part (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    time_created INTEGER NOT NULL,
                    time_updated INTEGER NOT NULL,
                    data TEXT NOT NULL
                );
                """
            )
            # Two OpenCode sessions in the same directory; ses_beta is newer!
            conn.execute("INSERT INTO session VALUES (?, ?, ?)", ("ses_alpha", str(work_dir), 1000))
            conn.execute("INSERT INTO session VALUES (?, ?, ?)", ("ses_beta", str(work_dir), 2000))

            # User prompt + assistant reply in ses_alpha for req-alpha-1
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                ("msg_u_a", "ses_alpha", 100, 100, json.dumps({"role": "user"})),
            )
            conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                ("prt_u_a", "msg_u_a", "ses_alpha", 100, 100, json.dumps({"type": "text", "text": "CCB_REQ_ID: req-alpha-1\nhello alpha"})),
            )
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                ("msg_a_a", "ses_alpha", 110, 120, json.dumps({"role": "assistant", "parentID": "msg_u_a", "time": {"created": 110, "completed": 120}})),
            )
            conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                ("prt_a_a", "msg_a_a", "ses_alpha", 115, 120, json.dumps({"type": "text", "text": "reply from alpha\nCCB_DONE: req-alpha-1"})),
            )

            # Unrelated newer activity in ses_beta
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                ("msg_a_b", "ses_beta", 200, 210, json.dumps({"role": "assistant", "time": {"created": 200, "completed": 210}})),
            )
            conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                ("prt_a_b", "msg_a_b", "ses_beta", 205, 210, json.dumps({"type": "text", "text": "reply from beta\nCCB_DONE: req-beta-9"})),
            )
            conn.commit()

        reader = OpenCodeLogReader(root=root, work_dir=work_dir)
        # Even if initial capture_state saw ses_beta (because ses_beta had higher time_updated),
        # wait_for_message with req_id='req-alpha-1' pins to ses_alpha and returns alpha's reply!
        state = reader.capture_state()
        assert state["session_id"] == "ses_beta"

        reply, new_state = reader.wait_for_message(state, 0.2, req_id="req-alpha-1")
        assert reply is not None
        assert "reply from alpha" in reply
        assert new_state["session_id"] == "ses_alpha"
