from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _setup_project(tmp_path: Path) -> tuple[Path, Path]:
    """Project with claude + grok session files and a fake tmux that logs its args."""
    project = tmp_path / "proj"
    ccb_dir = project / ".ccb"
    ccb_dir.mkdir(parents=True)
    for name, pane in (("claude", "%10"), ("grok", "%20")):
        (ccb_dir / f".{name}-session").write_text(
            json.dumps({"pane_id": pane, "terminal": "tmux", "work_dir": str(project)}),
            encoding="utf-8",
        )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux_log = tmp_path / "tmux.log"
    fake_tmux = bin_dir / "tmux"
    fake_tmux.write_text(f'#!/bin/sh\necho "$@" >> {tmux_log}\ncat >/dev/null 2>&1\nexit 0\n', encoding="utf-8")
    fake_tmux.chmod(0o755)
    return project, tmux_log


def _run_hook(tmp_path: Path, project: Path, env_extra: dict[str, str]) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CCB_")}
    env.pop("TMUX_PANE", None)
    env.pop("WEZTERM_PANE", None)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["CCB_WORK_DIR"] = str(project)
    env["CCB_TMUX_ENTER_DELAY"] = "0"
    env["CCB_COMPLETION_HOOK_DEBUG_LOG"] = str(tmp_path / "hook-debug.log")
    env.update(env_extra)
    subprocess.run(
        [sys.executable, str(_repo_root() / "bin" / "ccb-completion-hook"), "--provider", "opencode", "--req-id", "r1"],
        cwd=str(project),
        env=env,
        input="review done",
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )


def _pasted_panes(tmux_log: Path) -> set[str]:
    panes = set()
    for line in tmux_log.read_text(encoding="utf-8").splitlines():
        if line.startswith("paste-buffer"):
            panes.update(re.findall(r"-t (%\d+)", line))
    return panes


@pytest.mark.skipif(os.name == "nt", reason="fake tmux shim is POSIX-only")
def test_reply_routes_to_sender_session_without_pane_id(tmp_path: Path) -> None:
    # grok's shell tool may not expose TMUX_PANE; caller identity alone must route to grok.
    project, tmux_log = _setup_project(tmp_path)
    _run_hook(tmp_path, project, {"CCB_CALLER": "grok"})
    assert _pasted_panes(tmux_log) == {"%20"}


@pytest.mark.skipif(os.name == "nt", reason="fake tmux shim is POSIX-only")
def test_reply_prefers_captured_caller_pane(tmp_path: Path) -> None:
    project, tmux_log = _setup_project(tmp_path)
    _run_hook(tmp_path, project, {"CCB_CALLER": "claude", "CCB_CALLER_PANE_ID": "%30", "CCB_CALLER_TERMINAL": "tmux"})
    assert _pasted_panes(tmux_log) == {"%30"}


def test_claude_ask_skill_preserves_sender_caller() -> None:
    # The Claude skill is also loaded by other agents (grok's Claude-skill compat);
    # it must not force CCB_CALLER=claude over the pane's own CCB_CALLER.
    for rel in (
        "claude_skills/ask/SKILL.md",
        "claude_skills/file-op/references/usage.md",
        "claude_skills/tr/references/flow.md",
    ):
        text = (_repo_root() / rel).read_text(encoding="utf-8")
        assert "CCB_CALLER=claude ask" not in text, rel

    skill = (_repo_root() / "claude_skills/ask/SKILL.md").read_text(encoding="utf-8")
    cmd = re.search(r'Bash\((CCB_CALLER="\$\{CCB_CALLER:-claude\}") ask', skill)
    assert cmd, "ask skill must default CCB_CALLER from the environment"
    for env_caller, expected in (("grok", "grok"), ("", "claude")):
        out = subprocess.run(
            ["sh", "-c", f'{cmd.group(1)} sh -c \'printf %s "$CCB_CALLER"\''],
            env={**os.environ, "CCB_CALLER": env_caller},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert out == expected
