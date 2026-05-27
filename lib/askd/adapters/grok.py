"""
Grok (xAI Grok Build CLI) adapter for the unified ask daemon.

Unlike codex/opencode/qwen which use long-lived TUI panes, Grok Build's
headless `-p` mode gives a clean one-shot subprocess: send prompt, capture
stdout, return. No log readers, no pane plumbing.

The pane (if present) is for the human's interactive use; programmatic asks
go straight to the binary. This trades the conversation-continuation feature
(every ask is a fresh context) for vastly simpler integration.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from askd.adapters.base import BaseProviderAdapter, ProviderRequest, ProviderResult, QueuedTask
from askd_runtime import log_path, write_log
from completion_hook import (
    COMPLETION_STATUS_CANCELLED,
    COMPLETION_STATUS_COMPLETED,
    COMPLETION_STATUS_FAILED,
    COMPLETION_STATUS_INCOMPLETE,
    default_reply_for_status,
    notify_completion,
)
from project_id import compute_ccb_project_id
from providers import RASKD_SPEC


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_log(line: str) -> None:
    write_log(log_path(RASKD_SPEC.log_file_name), line)


def _grok_binary() -> str:
    return (os.environ.get("GROK_BIN") or "grok").strip() or "grok"


class GrokAdapter(BaseProviderAdapter):
    """Headless adapter for xAI's Grok Build CLI."""

    @property
    def key(self) -> str:
        return "grok"

    @property
    def spec(self):
        return RASKD_SPEC

    @property
    def session_filename(self) -> str:
        return ".grok-session"

    def load_session(self, work_dir: Path, instance: Optional[str] = None) -> Optional[Any]:
        # Grok asks are stateless (one subprocess per request). Returning a
        # lightweight sentinel keeps the daemon's session-routing path happy
        # without requiring a live pane.
        return {"work_dir": str(work_dir), "instance": instance}

    def compute_session_key(self, session: Any, instance: Optional[str] = None) -> str:
        work_dir = ""
        try:
            work_dir = str(session.get("work_dir") or "") if session else ""
        except Exception:
            work_dir = ""
        ccb_project_id = ""
        if work_dir:
            try:
                ccb_project_id = compute_ccb_project_id(Path(work_dir))
            except Exception:
                ccb_project_id = ""
        prefix = f"grok:{instance}" if instance else "grok"
        return f"{prefix}:{ccb_project_id}" if ccb_project_id else f"{prefix}:unknown"

    def handle_task(self, task: QueuedTask) -> ProviderResult:
        started_ms = _now_ms()
        req = task.request
        work_dir = Path(req.work_dir)
        _write_log(f"[INFO] start provider=grok req_id={task.req_id} work_dir={req.work_dir}")

        session_key = self.compute_session_key(
            {"work_dir": str(work_dir)}, task.request.instance
        )

        # Async fire-and-forget mode (timeout_s == 0)
        is_async = float(req.timeout_s) == 0.0

        binary = _grok_binary()
        cmd = [binary, "-p", req.message]

        timeout = None if float(req.timeout_s) < 0.0 else float(req.timeout_s)
        if is_async:
            timeout = None

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(work_dir) if work_dir.is_dir() else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            _write_log(f"[WARN] timeout req_id={task.req_id} after {timeout}s")
            return ProviderResult(
                exit_code=2,
                reply=f"Grok request timed out after {timeout}s.",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
                status=COMPLETION_STATUS_INCOMPLETE,
            )
        except FileNotFoundError:
            return ProviderResult(
                exit_code=1,
                reply=f"`{binary}` not on PATH — install Grok Build CLI and authenticate.",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
                status=COMPLETION_STATUS_FAILED,
            )
        except Exception as exc:
            _write_log(f"[ERROR] subprocess failure req_id={task.req_id}: {exc}")
            return ProviderResult(
                exit_code=1,
                reply=f"Grok subprocess failure: {exc}",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
                status=COMPLETION_STATUS_FAILED,
            )

        if task.cancel_event and task.cancel_event.is_set():
            status = COMPLETION_STATUS_CANCELLED
            final_reply = (proc.stdout or "").strip()
            done_seen = False
        elif proc.returncode == 0:
            status = COMPLETION_STATUS_COMPLETED
            final_reply = (proc.stdout or "").strip()
            done_seen = True
        else:
            status = COMPLETION_STATUS_FAILED
            err_tail = (proc.stderr or "").strip()
            out_tail = (proc.stdout or "").strip()
            final_reply = err_tail or out_tail or f"Grok exited with code {proc.returncode}"
            done_seen = False

        done_ms = _now_ms() - started_ms if done_seen else None

        reply_for_hook = final_reply or default_reply_for_status(status, done_seen=done_seen)
        notify_completion(
            provider="grok",
            output_file=req.output_path,
            reply=reply_for_hook,
            req_id=task.req_id,
            done_seen=done_seen,
            status=status,
            caller=req.caller,
            email_req_id=req.email_req_id,
            email_msg_id=req.email_msg_id,
            email_from=req.email_from,
            work_dir=req.work_dir,
            caller_pane_id=req.caller_pane_id,
            caller_terminal=req.caller_terminal,
            telegram_chat_id=("" if req.telegram_sync_reply else req.telegram_chat_id),
        )

        result = ProviderResult(
            exit_code=0 if done_seen else (2 if status == COMPLETION_STATUS_INCOMPLETE else 1),
            reply=final_reply,
            req_id=task.req_id,
            session_key=session_key,
            done_seen=done_seen,
            done_ms=done_ms,
            status=status,
        )
        _write_log(f"[INFO] done provider=grok req_id={task.req_id} exit={result.exit_code} status={status}")
        return result
