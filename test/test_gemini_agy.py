from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from gemini_comm import is_agy_cli, find_gemini_cmd, _IS_AGY_CACHE
from ccb_start_config import _normalize_providers


def _load_ccb_module() -> object:
    repo_root = Path(__file__).resolve().parents[1]
    ccb_path = repo_root / "ccb"
    loader = SourceFileLoader("ccb_script", str(ccb_path))
    spec = importlib.util.spec_from_loader("ccb_script", loader)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def setup_function():
    _IS_AGY_CACHE.clear()


def test_is_agy_cli_name_matching():
    assert is_agy_cli("agy") is True
    assert is_agy_cli("antigravity") is True
    assert is_agy_cli("/usr/local/bin/agy") is True
    assert is_agy_cli("C:\\bin\\agy.exe") is True


def test_is_agy_cli_resolved_symlink(tmp_path: Path):
    target = tmp_path / "agy"
    target.touch()
    link = tmp_path / "gemini"
    link.symlink_to(target)

    with patch("shutil.which", return_value=str(link)):
        assert is_agy_cli("gemini") is True


def test_is_agy_cli_help_probe_agy():
    mock_res = MagicMock()
    mock_res.stdout = "Usage of gemini:\n  --dangerously-skip-permissions\n  --continue\n"
    mock_res.stderr = ""
    with patch("shutil.which", return_value="/bin/gemini"), \
         patch("pathlib.Path.resolve", return_value=Path("/bin/gemini")), \
         patch("subprocess.run", return_value=mock_res):
        assert is_agy_cli("gemini") is True


def test_is_agy_cli_help_probe_legacy():
    mock_res = MagicMock()
    mock_res.stdout = "Gemini CLI\n  --yolo  Run all commands\n  --resume <id>\n"
    mock_res.stderr = ""
    with patch("shutil.which", return_value="/bin/gemini"), \
         patch("pathlib.Path.resolve", return_value=Path("/bin/gemini")), \
         patch("subprocess.run", return_value=mock_res):
        assert is_agy_cli("gemini") is False


def test_find_gemini_cmd_env(monkeypatch):
    monkeypatch.setenv("GEMINI_START_CMD", "custom-gemini --flag")
    assert find_gemini_cmd() == "custom-gemini --flag"

    monkeypatch.delenv("GEMINI_START_CMD", raising=False)
    monkeypatch.setenv("AGY_START_CMD", "custom-agy")
    assert find_gemini_cmd() == "custom-agy"


def test_build_gemini_start_cmd_agy(tmp_path: Path):
    ccb = _load_ccb_module()
    launcher = ccb.AILauncher(providers=["gemini"], auto=True, resume=False)
    launcher.project_root = tmp_path
    with patch.object(launcher, "_find_gemini_cmd", return_value="gemini"), \
         patch.object(ccb, "is_agy_cli", return_value=True):
        cmd = launcher._build_gemini_start_cmd()
        assert "--dangerously-skip-permissions" in cmd
        assert "--yolo" not in cmd


def test_build_gemini_start_cmd_agy_no_auto(tmp_path: Path):
    ccb = _load_ccb_module()
    launcher = ccb.AILauncher(providers=["gemini"], auto=False, resume=False)
    launcher.project_root = tmp_path
    with patch.object(launcher, "_find_gemini_cmd", return_value="gemini"), \
         patch.object(ccb, "is_agy_cli", return_value=True):
        cmd = launcher._build_gemini_start_cmd()
        assert cmd == "gemini"


def test_build_gemini_start_cmd_agy_resume(tmp_path: Path):
    ccb = _load_ccb_module()
    launcher = ccb.AILauncher(providers=["gemini"], auto=True, resume=True)
    launcher.project_root = tmp_path
    with patch.object(launcher, "_find_gemini_cmd", return_value="gemini"), \
         patch.object(ccb, "is_agy_cli", return_value=True), \
         patch.object(launcher, "_get_latest_gemini_project_hash", return_value=("hash", True, tmp_path)):
        cmd = launcher._build_gemini_start_cmd()
        assert "--continue" in cmd
        assert "--resume latest" not in cmd
        assert "--dangerously-skip-permissions" in cmd


def test_build_gemini_start_cmd_legacy(tmp_path: Path):
    ccb = _load_ccb_module()
    launcher = ccb.AILauncher(providers=["gemini"], auto=True, resume=False)
    launcher.project_root = tmp_path
    with patch.object(launcher, "_find_gemini_cmd", return_value="gemini"), \
         patch.object(ccb, "is_agy_cli", return_value=False):
        cmd = launcher._build_gemini_start_cmd()
        assert "--yolo" in cmd
        assert "--dangerously-skip-permissions" not in cmd


def test_build_gemini_start_cmd_legacy_resume(tmp_path: Path):
    ccb = _load_ccb_module()
    launcher = ccb.AILauncher(providers=["gemini"], auto=True, resume=True)
    launcher.project_root = tmp_path
    with patch.object(launcher, "_find_gemini_cmd", return_value="gemini"), \
         patch.object(ccb, "is_agy_cli", return_value=False), \
         patch.object(launcher, "_get_latest_gemini_project_hash", return_value=("hash", True, tmp_path)):
        cmd = launcher._build_gemini_start_cmd()
        assert "--resume latest" in cmd
        assert "--continue" not in cmd
        assert "--yolo" in cmd


def test_parse_providers_agy_alias():
    ccb = _load_ccb_module()
    assert ccb._parse_providers(["agy", "claude"]) == ["gemini", "claude"]
    assert ccb._parse_providers(["antigravity"]) == ["gemini"]
    assert ccb._parse_providers(["gemini", "agy"]) == ["gemini"]


def test_normalize_providers_agy_alias():
    providers, cmd_enabled = _normalize_providers(["agy", "codex", "cmd"])
    assert providers == ["gemini", "codex"]
    assert cmd_enabled is True


def test_launch_args_translates_yolo_for_agy(tmp_path: Path):
    ccb = _load_ccb_module()
    launcher = ccb.AILauncher(
        providers=["gemini"],
        launch_args={"gemini": "--yolo --other"},
    )
    launcher.project_root = tmp_path
    with patch.object(launcher, "_build_gemini_start_cmd", return_value="gemini"), \
         patch.object(ccb, "is_agy_cli", return_value=True):
        cmd = launcher._get_start_cmd("gemini")
        assert "--dangerously-skip-permissions" in cmd
        assert "--yolo" not in cmd


def test_gaskd_session_sanitizes_start_cmd(tmp_path: Path):
    from gaskd_session import GeminiProjectSession

    session_file = tmp_path / ".gemini-session"
    data = {
        "terminal": "tmux",
        "pane_id": "%1",
        "start_cmd": "gemini --yolo --resume latest",
        "work_dir": str(tmp_path),
        "runtime_dir": str(tmp_path),
    }
    session = GeminiProjectSession(session_file, data)

    mock_backend = MagicMock()
    mock_backend.is_alive.return_value = False

    def fake_respawn(target, cmd, cwd, remain_on_exit):
        assert "--dangerously-skip-permissions" in cmd
        assert "--continue" in cmd
        assert "--yolo" not in cmd
        assert "--resume latest" not in cmd

    mock_backend.respawn_pane.side_effect = fake_respawn

    with patch.object(session, "backend", return_value=mock_backend), \
         patch("gemini_comm.is_agy_cli", return_value=True):
        session.ensure_pane()
