"""
Unified Ask Daemon - Single daemon for all AI providers.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

from askd.adapters.base import BaseProviderAdapter, ProviderRequest, ProviderResult, QueuedTask
from askd.registry import ProviderRegistry
from askd_runtime import log_path, random_token, state_file_path, write_log
from ccb_protocol import make_req_id
from providers import ProviderDaemonSpec, make_qualified_key, parse_qualified_provider
from worker_pool import BaseSessionWorker, PerSessionWorkerPool
import subprocess
import shutil
import wake_queue


ASKD_SPEC = ProviderDaemonSpec(
    daemon_key="askd",
    protocol_prefix="ask",
    state_file_name="askd.json",
    log_file_name="askd.log",
    idle_timeout_env="CCB_ASKD_IDLE_TIMEOUT_S",
    lock_name="askd",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_log(line: str) -> None:
    write_log(log_path(ASKD_SPEC.log_file_name), line)


class _SessionWorker(BaseSessionWorker[QueuedTask, ProviderResult]):
    """Worker thread for processing tasks for a specific session."""

    def __init__(self, session_key: str, adapter: BaseProviderAdapter):
        super().__init__(session_key)
        self.adapter = adapter

    def _handle_task(self, task: QueuedTask) -> ProviderResult:
        return self.adapter.handle_task(task)

    def _handle_exception(self, exc: Exception, task: QueuedTask) -> ProviderResult:
        _write_log(f"[ERROR] provider={self.adapter.key} session={self.session_key} req_id={task.req_id} {exc}")
        return self.adapter.handle_exception(exc, task)


class _UnifiedWorkerPool:
    """Worker pool that routes tasks to provider-specific workers."""

    def __init__(self, registry: ProviderRegistry):
        self._registry = registry
        self._pools: Dict[str, PerSessionWorkerPool[_SessionWorker]] = {}
        self._lock = threading.Lock()

    def _get_pool(self, provider_key: str) -> PerSessionWorkerPool[_SessionWorker]:
        with self._lock:
            if provider_key not in self._pools:
                self._pools[provider_key] = PerSessionWorkerPool[_SessionWorker]()
            return self._pools[provider_key]

    def submit(self, pool_key: str, request: ProviderRequest) -> Optional[QueuedTask]:
        base_provider, instance = parse_qualified_provider(pool_key)
        adapter = self._registry.get(base_provider)
        if not adapter:
            return None

        req_id = request.req_id or make_req_id()
        cancel_event = threading.Event()
        task = QueuedTask(
            request=request,
            created_ms=_now_ms(),
            req_id=req_id,
            done_event=threading.Event(),
            cancelled=False,
            cancel_event=cancel_event,
        )

        session = adapter.load_session(Path(request.work_dir), instance=instance)
        session_key = adapter.compute_session_key(session, instance=instance) if session else f"{pool_key}:unknown"

        pool = self._get_pool(pool_key)
        worker = pool.get_or_create(
            session_key,
            lambda sk: _SessionWorker(sk, adapter),
        )
        worker.enqueue(task)
        return task


class UnifiedAskDaemon:
    """
    Unified daemon server for all AI providers.

    Handles requests for codex, gemini, opencode, droid, and claude
    in a single process with per-provider worker pools.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        state_file: Optional[Path] = None,
        registry: Optional[ProviderRegistry] = None,
        work_dir: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.state_file = state_file or state_file_path(ASKD_SPEC.state_file_name)
        self.token = random_token()
        self.registry = registry or ProviderRegistry()
        self.pool = _UnifiedWorkerPool(self.registry)
        self.work_dir = work_dir
        # Keyed by id(task) because QueuedTask is an unfrozen dataclass and thus unhashable.
        self._active_tasks: dict = {}
        self._active_tasks_lock = threading.Lock()
        self._wake_stop = threading.Event()

    def _handle_request(self, msg: dict) -> dict:
        """Handle an incoming request."""
        provider = str(msg.get("provider") or "").strip().lower()
        if not provider:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": "Missing 'provider' field",
            }

        base_provider, instance = parse_qualified_provider(provider)

        adapter = self.registry.get(base_provider)
        if not adapter:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": f"Unknown provider: {base_provider}",
            }

        caller = str(msg.get("caller") or "").strip()
        if not caller:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": "Missing 'caller' field (required).",
            }

        try:
            request = ProviderRequest(
                client_id=str(msg.get("id") or ""),
                work_dir=str(msg.get("work_dir") or ""),
                timeout_s=float(msg.get("timeout_s") or 300.0),
                quiet=bool(msg.get("quiet") or False),
                message=str(msg.get("message") or ""),
                caller=caller,
                output_path=str(msg.get("output_path")) if msg.get("output_path") else None,
                req_id=str(msg.get("req_id")) if msg.get("req_id") else None,
                no_wrap=bool(msg.get("no_wrap") or False),
                email_req_id=str(msg.get("email_req_id") or ""),
                email_msg_id=str(msg.get("email_msg_id") or ""),
                email_from=str(msg.get("email_from") or ""),
                caller_pane_id=str(msg.get("caller_pane_id") or ""),
                caller_terminal=str(msg.get("caller_terminal") or ""),
            )
        except Exception as exc:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": f"Bad request: {exc}",
            }

        request.instance = instance
        pool_key = make_qualified_key(base_provider, instance)
        task = self.pool.submit(pool_key, request)
        if not task:
            return {
                "type": "ask.response",
                "v": 1,
                "id": msg.get("id"),
                "exit_code": 1,
                "reply": f"Failed to submit task for provider: {provider}",
            }

        task_key = id(task)
        with self._active_tasks_lock:
            self._active_tasks[task_key] = task
        try:
            wait_timeout = None if float(request.timeout_s) < 0.0 else (float(request.timeout_s) + 5.0)
            task.done_event.wait(timeout=wait_timeout)
            result = task.result

            # If timeout occurred and task is still running, mark it as cancelled
            if not result and not task.done_event.is_set():
                _write_log(f"[WARN] Task timeout, marking as cancelled: provider={provider} req_id={task.req_id}")
                task.cancelled = True
                if task.cancel_event:
                    task.cancel_event.set()
        finally:
            with self._active_tasks_lock:
                self._active_tasks.pop(task_key, None)

        if not result:
            return {
                "type": "ask.response",
                "v": 1,
                "id": request.client_id,
                "exit_code": 2,
                "reply": "",
            }

        return {
            "type": "ask.response",
            "v": 1,
            "id": request.client_id,
            "req_id": result.req_id,
            "exit_code": result.exit_code,
            "reply": result.reply,
            "provider": provider,
            "meta": {
                "session_key": result.session_key,
                "status": result.status,
                "done_seen": result.done_seen,
                "done_ms": result.done_ms,
                "anchor_seen": result.anchor_seen,
                "anchor_ms": result.anchor_ms,
                "fallback_scan": result.fallback_scan,
                "log_path": result.log_path,
            },
        }

    def drain(self) -> None:
        """Cancel all in-flight tasks so handler threads can exit promptly.

        Called before httpd.shutdown() so ThreadingTCPServer.server_close doesn't
        block for up to timeout_s joining handlers that are still waiting for a
        provider reply (e.g. a Claude pane that's already gone).
        """
        with self._active_tasks_lock:
            tasks = list(self._active_tasks.values())
        for task in tasks:
            try:
                task.cancelled = True
                if task.cancel_event:
                    task.cancel_event.set()
                task.done_event.set()
            except Exception:
                pass

    def _wake_scheduler_loop(self) -> None:
        """Poll the project's wake queue and dispatch due entries.

        Lives in askd (not telegramd) so every project — including
        terminal-only ones with no Telegram configured — gets wake support.
        Dispatch is a subprocess spawn of `ask <agent>`; the existing
        ask → askd → completion-hook chain handles routing based on
        the entry's `caller` field (terminal / telegram / email).
        """
        if not self.work_dir:
            return
        project_root = Path(self.work_dir)
        poll_s = 2.0
        while not self._wake_stop.is_set():
            try:
                due = wake_queue.pop_due(project_root, time.time())
            except Exception as exc:
                _write_log(f"[ERROR] wake load error: {exc}")
                due = []
            for entry in due:
                try:
                    self._fire_wake(project_root, entry)
                except Exception as exc:
                    _write_log(
                        f"[ERROR] wake fire id={entry.get('wake_id')}: {exc}"
                    )
            self._wake_stop.wait(poll_s)

    def _fire_wake(self, project_root: Path, entry: dict) -> None:
        """Dispatch a single due wake by spawning `ask <agent>` async.

        Environment is populated from the entry's caller metadata so the
        downstream completion-hook routes the reply correctly (pane for
        terminal, chat_id for telegram, etc.).
        """
        agent = str(entry.get("agent") or "").strip().lower()
        message = str(entry.get("message") or "")
        caller = str(entry.get("caller") or "terminal").strip().lower()
        wake_id = str(entry.get("wake_id") or "")
        if not (agent and message):
            _write_log(f"[WARN] wake {wake_id} skipped: missing agent/message")
            return

        env = os.environ.copy()
        env["CCB_CALLER"] = caller
        env["CCB_WORK_DIR"] = str(project_root)
        env["CCB_UNIFIED_ASKD"] = "1"
        env["CCB_ASKD_AUTOSTART"] = "0"
        # Strip any pane env the askd process itself inherited — we'll set
        # explicit routing values from the entry below if applicable.
        for v in ("TMUX_PANE", "WEZTERM_PANE", "CCB_CALLER_PANE_ID", "CCB_CALLER_TERMINAL"):
            env.pop(v, None)

        if caller == "telegram":
            chat_id = str(entry.get("chat_id") or "").strip()
            if chat_id:
                env["CCB_TELEGRAM_CHAT_ID"] = chat_id
        elif caller == "terminal":
            pane_id = str(entry.get("pane_id") or "").strip()
            terminal = str(entry.get("terminal") or "tmux").strip()
            if pane_id:
                env["CCB_CALLER_PANE_ID"] = pane_id
                env["CCB_CALLER_TERMINAL"] = terminal

        ask_cmd = shutil.which("ask") or str(
            Path.home() / ".local" / "share" / "codex-dual" / "bin" / "ask"
        )
        if not Path(ask_cmd).is_file():
            _write_log(f"[ERROR] wake {wake_id}: ask command not found")
            return

        _write_log(
            f"[INFO] wake fired id={wake_id} agent={agent} caller={caller}"
        )
        # Fire-and-forget in a background thread so the scheduler loop
        # doesn't block on a slow provider turn.
        def _run() -> None:
            try:
                subprocess.run(
                    [ask_cmd, agent, "--foreground", "--timeout", "300"],
                    cwd=str(project_root),
                    env=env,
                    input=message,
                    capture_output=True,
                    text=True,
                    timeout=330,
                )
            except Exception as exc:
                _write_log(f"[WARN] wake {wake_id} subprocess: {exc}")

        threading.Thread(target=_run, name=f"wake-{wake_id}", daemon=True).start()

    def serve_forever(self) -> int:
        """Start the daemon and serve requests."""
        from askd_server import AskDaemonServer
        import askd_rpc

        self.registry.start_all()

        threading.Thread(
            target=self._wake_scheduler_loop,
            name="askd-wake-scheduler",
            daemon=True,
        ).start()

        def _on_stop() -> None:
            self._wake_stop.set()
            self.registry.stop_all()
            self._cleanup_state_file()

        server = AskDaemonServer(
            spec=ASKD_SPEC,
            host=self.host,
            port=self.port,
            token=self.token,
            state_file=self.state_file,
            request_handler=self._handle_request,
            request_queue_size=128,
            on_stop=_on_stop,
            on_shutdown_requested=self.drain,
            work_dir=self.work_dir,
        )
        return server.serve_forever()

    def _cleanup_state_file(self) -> None:
        import askd_rpc
        try:
            st = askd_rpc.read_state(self.state_file)
        except Exception:
            st = None
        try:
            if isinstance(st, dict) and int(st.get("pid") or 0) == os.getpid():
                self.state_file.unlink(missing_ok=True)
        except TypeError:
            try:
                if isinstance(st, dict) and int(st.get("pid") or 0) == os.getpid():
                    if self.state_file.exists():
                        self.state_file.unlink()
            except Exception:
                pass
        except Exception:
            pass


def read_state(state_file: Optional[Path] = None) -> Optional[dict]:
    import askd_rpc
    state_file = state_file or state_file_path(ASKD_SPEC.state_file_name)
    return askd_rpc.read_state(state_file)


def ping_daemon(timeout_s: float = 0.5, state_file: Optional[Path] = None) -> bool:
    import askd_rpc
    state_file = state_file or state_file_path(ASKD_SPEC.state_file_name)
    return askd_rpc.ping_daemon("ask", timeout_s, state_file)


def shutdown_daemon(timeout_s: float = 1.0, state_file: Optional[Path] = None) -> bool:
    import askd_rpc
    state_file = state_file or state_file_path(ASKD_SPEC.state_file_name)
    return askd_rpc.shutdown_daemon("ask", timeout_s, state_file)
