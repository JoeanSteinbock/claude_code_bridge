from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Optional

from . import _mount
from .bot_api import TelegramApiError, TelegramBotClient, chunk_message
from .config import SUPPORTED_PROVIDERS, TelegramConfig, get_config_dir, get_project_root, is_configured, load_config
from .router import help_text, parse_message
from project_id import compute_ccb_project_id
from session_utils import find_project_session_file

STATE_FILE = "telegramd.json"
PID_FILE = "telegramd.pid"
LOG_FILE = "telegramd.log"
SESSION_FILES = {
    "claude": ".claude-session",
    "codex": ".codex-session",
    "gemini": ".gemini-session",
    "opencode": ".opencode-session",
    "droid": ".droid-session",
    "copilot": ".copilot-session",
    "codebuddy": ".codebuddy-session",
    "qwen": ".qwen-session",
    "grok": ".grok-session",
}

# Matches the `[Provider]` prefix our bot prepends to every reply
# (see _run_request). Used to infer routing when a user taps reply.
_PROVIDER_PREFIX_RE = re.compile(r"^\[([A-Za-z][A-Za-z0-9_-]*)\]")

# Grok writes image_gen / image_edit / video_gen outputs under
# ~/.grok/sessions/<encoded-cwd>/<session>/{images,videos}/N.{ext}
# and prints the path verbatim in its reply. Telegramd reads `ask grok`
# stdout in sync mode (bypassing the completion hook's attachment plumb),
# so we re-extract here and sendPhoto/sendVideo each match. Mirror of
# _GROK_MEDIA_RE in lib/askd/adapters/grok.py — keep in sync if updating.
_GROK_MEDIA_RE = re.compile(
    r"(?P<path>/(?:home/[^/\s]+|root)/\.grok/sessions/[^\s`)]+/(?:images|videos)/[^\s`)]+\.(?:jpg|jpeg|png|webp|gif|mp4|webm|mov))",
    re.IGNORECASE,
)

# Hermes live tool-chain — markers that signal "the agent is doing X right now".
# Strip ANSI escape sequences before matching so terminal colour codes don't
# wreck the regex. Each provider's TUI uses slightly different glyphs:
#   claude     `❯` for tool invocation, `✻` / `⏺` for status
#   opencode   `◆` for actions, `┃ Run` for shell calls
#   codex      `▌`, `❯`, `✻` similar shape to claude
#   qwen       `◆` similar to opencode
# Pattern below captures the LATEST line that looks like agent activity. If no
# marker matches, falls back to last non-empty line (best effort).
_ANSI_RE = re.compile(
    # CSI: `\x1b[...<letter>` — the bulk of colour / cursor sequences.
    r"\x1b\[[0-9;?]*[A-Za-z]"
    # OSC: `\x1b]...<terminator>` — terminal-title sets etc. Terminator is
    # BEL (\x07) or ST (\x1b\\). Without this, set-title escapes like
    # `\x1b]0;⠐ New Claude Code session started\x07` survive ANSI strip
    # and pollute `/tail` + Hermes output.
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    # Lone DCS / PM / APC openers and Ptmux pass-through tokens — these
    # tmux features can leak `\x1b\\` and `Ptmux;` markers into capture
    # output during certain terminal interactions. Catch the obvious ones.
    r"|\x1bP[^\x1b]*\x1b\\"
    r"|\x1b[NOPX^_]"
)
# Strip "Ptmux; ... ESC \\" pass-through wrappers (tmux's escape-passthrough
# feature for nested terminals) — surface as literal `Ptmux;` in capture-pane
# output when ANSI prefix is partially eaten.
_PTMUX_RE = re.compile(r"Ptmux;[^\x1b]*\x1b\\")
_HERMES_MARKERS = re.compile(
    # Claude TUI's "thinking spinner" cycles through many star/asterisk
    # variants — `✶ ✻ ✽ ✺ ✷ ✸ ❅ ✦ ✱` — so we need all of them, not just one.
    # ⏺/⎿ are tool record + tool result subline; ◆ is generic tool call;
    # ▌ is codex step; ● is Claude's bullet; ❯ is the input cursor.
    r"^\s*[◆❯✻✶✽✺✷✸❅✦✱⏺⎿▌●][^\n]{0,200}",
    re.MULTILINE,
)


# Substrings that mean "this line is protocol scaffolding or Claude TUI
# chrome, not real agent activity" — Hermes should never surface them.
# `CCB_REQ_ID` / `CCB_BEGIN` / `CCB_DONE` / `CCB_TASK_COMPLETED` are our
# own protocol markers. `Tip:` lines are Claude TUI's marketing chrome.
_HERMES_SKIP_SUBSTR = (
    "CCB_REQ_ID",
    "CCB_BEGIN",
    "CCB_DONE",
    "CCB_TASK_COMPLETED",
    "Tip:",
    "Share Claude Code",
)


def _parse_latest_activity(text: str, user_prompt: str = "") -> str:
    """Extract the most recent agent-activity line from a pane snapshot.

    Best-effort heuristic. Strips ANSI, hunts for tool-call glyphs, skips
    bare-cursor noise (a lone `❯` with nothing after it is just the input
    prompt, not activity), and skips lines that echo the user's own prompt
    (Claude TUI renders the user message with a `●` bullet too, so the naive
    last-glyph match catches "● <user question>" instead of Claude's reply).

    `user_prompt` — optional first ~80 chars of what we sent in; lines that
    contain it are treated as echo and skipped.

    Glyph priority (newest-first within each tier):
      Tier 1: `✻` (thinking), `⏺` (tool result), `◆` (tool call), `▌` (codex step)
      Tier 2: `●` (Claude bullet — could be user echo or section header)
      Tier 3: `❯` (input cursor)
    Falls back to the last non-empty visible line that's not box-drawing.
    """
    if not text:
        return ""
    plain = _ANSI_RE.sub("", text)
    needle = (user_prompt or "").strip().split("\n", 1)[0][:80].lower()

    # Bucket matches by tier so high-signal glyphs win over `● user echo`.
    tier1: list[str] = []
    tier2: list[str] = []
    tier3: list[str] = []
    for raw in _HERMES_MARKERS.findall(plain):
        line = raw.strip()
        body = line[1:].strip() if line else ""
        if len(body) < 3:
            continue
        if needle and needle in body.lower():
            continue
        if any(skip in line for skip in _HERMES_SKIP_SUBSTR):
            continue
        glyph = line[:1]
        # Tier 1 = real activity. Includes the full Claude spinner family
        # (✶✻✽✺✷✸❅✦✱), tool record/subline (⏺⎿), generic tool call (◆),
        # and codex step (▌). All of these mean "the agent is doing
        # something substantive" — they beat user-echo and section headers.
        if glyph in "✶✻✽✺✷✸❅✦✱⏺⎿◆▌":
            tier1.append(line)
        elif glyph == "●":
            tier2.append(line)
        elif glyph == "❯":
            tier3.append(line)
        else:
            tier2.append(line)
    for bucket in (tier1, tier2, tier3):
        if bucket:
            return bucket[-1][:180]
    # Fallback — last non-empty line that's not box-drawing, user prompt, or scaffolding.
    for line in reversed([ln.strip() for ln in plain.splitlines() if ln.strip()]):
        if len(line) < 4:
            continue
        if needle and needle in line.lower():
            continue
        if all(c in "─━│┃┌┐└┘├┤┬┴┼ " for c in line):
            continue
        if any(skip in line for skip in _HERMES_SKIP_SUBSTR):
            continue
        return line[:180]
    return ""


# Phase C — post-timeout completion watcher.
# Telegramd caps ask at 30 min today. For longer tasks (deep code dives,
# slow tool chains) the ask process exits without seeing the agent's
# CCB_DONE marker, and the partial output gets posted looking just like
# a real final. The watcher fixes that: capture the pane log offset
# BEFORE we run ask; if ask comes back without confirmation, tail the log
# from that offset, find the late CCB_DONE, extract the actual reply,
# and post that as the real final (deleting the timeout warning).
_CCB_DONE_LINE_RE = re.compile(
    r"^\s*CCB_DONE:\s*(\d{8}-\d{6}-\d{3}-\d+-\d+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _resolve_pane_log_path(pane_id: str) -> Optional[Path]:
    """Look up the on-disk pipe-pane log for a tmux pane id like `%266`."""
    pid = (pane_id or "").strip().replace("%", "")
    if not pid:
        return None
    try:
        from terminal import _pane_log_path_for
        return _pane_log_path_for(pane_id, "tmux", None)
    except Exception:
        # Fall back to the standard layout if terminal helpers aren't loadable.
        try:
            from askd_runtime import run_dir
            return run_dir() / "pane-logs" / "tmux" / f"pane-{pid}.log"
        except Exception:
            return Path.home() / ".cache" / "ccb" / "pane-logs" / "tmux" / f"pane-{pid}.log"


def _pane_log_offset(log_path: Optional[Path]) -> int:
    """Current file size — used as a marker to start tailing from later."""
    try:
        return int(log_path.stat().st_size) if log_path and log_path.exists() else 0
    except Exception:
        return 0


def _read_pane_log_from_offset(log_path: Optional[Path], offset: int) -> str:
    """Read appended bytes since `offset` and strip ANSI. Empty on error."""
    if not log_path or not log_path.exists():
        return ""
    try:
        with log_path.open("rb") as f:
            f.seek(max(0, int(offset)))
            data = f.read()
    except Exception:
        return ""
    text = data.decode("utf-8", errors="replace")
    return _PTMUX_RE.sub("", _ANSI_RE.sub("", text))


# Sublines under `⎿` are sometimes real tool output (`⎿ === port 9338 ===`,
# `⎿ Read foo.py (49 lines)`) and sometimes just in-progress spinner-style
# labels Claude TUI shows during a tool run (`⎿ Running…`, `⎿ Waiting…`,
# `⎿ Searching…`). The second kind is animation noise — drop it.
_HERMES_INPROGRESS_RE = re.compile(
    r"^[A-Za-z]+\s*[.…]+$",  # `Running…`, `Waiting...`, `Hashing…`
)


# Suggestion detection — Claude often ends a turn with `Want me to X?`
# (or `Should I X?`, `Shall I X?`). Captured group is the action body.
# Strict on the question mark so we don't snag mid-paragraph musings.
_SUGGESTION_RE = re.compile(
    r"(?im)(?:Want me to|Should I|Shall I|Do you want me to)\s+([^?\n]{3,200})\?",
)
# Hard ceiling on the suggestion dict; FIFO eviction past this so a
# busy chat doesn't bloat the daemon over hours.
_SUGGESTION_CAP = 200


def _extract_suggestion(reply: str) -> str:
    """Pull the latest `Want me to X?` action from a final-reply body.

    Returns the suggested action verbatim (e.g. `rebuild and redeploy
    app.ros.rip`) — without the trailing `?` and without the lead-in
    verb. Empty when no suggestion is detected.
    """
    if not reply:
        return ""
    matches = list(_SUGGESTION_RE.finditer(reply))
    if not matches:
        return ""
    # Last match — agent's most-recent ask.
    action = matches[-1].group(1).strip()
    # Trim trailing punctuation we don't want repeated.
    return action.rstrip(".,;:!")


def _extract_new_bullets(text: str, seen_signatures: set, needle: str = "") -> list[str]:
    """Pull *new* agent-activity bullets out of newly-appended pane content.

    `seen_signatures` is mutated in place with a fingerprint per bullet
    so the next call won't re-emit the same line. `needle` is the user's
    prompt (first 80 chars, lowercased) — lines containing it are skipped
    as echoes. Honours the same skip-list as `_parse_latest_activity`.

    Bullets are lines whose first non-space character is a tool-call
    marker (`●⏺⎿◆▌`). Thinking-spinner glyphs (`✶✻✽✺✷✸❅✦✱`) are
    excluded entirely — Claude TUI animates through them many times per
    second with bodies like `✱ thinking` / `✻ 76 thinking`, which would
    flood the chat with one transient message per frame. The user wants
    only actual tool activity surfaced. Cursor markers `❯` are also
    excluded (never substantive).
    """
    if not text:
        return []
    plain = _ANSI_RE.sub("", text)
    out: list[str] = []
    # Tool-call markers only. Spinner glyphs deliberately omitted; see docstring.
    bullet_glyphs = "⏺⎿◆▌●"
    for raw in plain.splitlines():
        line = raw.strip()
        if len(line) < 4:
            continue
        glyph = line[:1]
        if glyph not in bullet_glyphs:
            continue
        body = line[1:].strip()
        if len(body) < 3:
            continue
        if needle and needle in body.lower():
            continue
        if any(skip in line for skip in _HERMES_SKIP_SUBSTR):
            continue
        # Drop in-progress spinner sublines (`⎿ Running…`, `⎿ Waiting…`).
        # They use the `⎿` glyph (same as legit tool-result sublines) but
        # the body is just a single word + ellipsis. Real result sublines
        # always carry substantive content (paths, line counts, output
        # excerpts) so they pass this check.
        if glyph == "⎿" and _HERMES_INPROGRESS_RE.match(body):
            continue
        # Drop `●` prose lines — Claude TUI uses `●` BOTH for tool calls
        # (`● Bash(...)`, `● Read(...)`, `● Write(...)`) and for agent
        # commentary / section headers (`● Found it — the homepage OG…`,
        # `● The wiring is verified-correct at every level…`). The user
        # only wants the real actions, not the prose summaries. Heuristic:
        # a tool call always has a `(` near the start of the body
        # (`Bash(`, `Read(`, `MultiEdit(`). Prose doesn't. Keep `●` only
        # when body's first 32 chars contain `(`.
        if glyph == "●" and "(" not in body[:32]:
            continue
        # Signature ignores trailing whitespace + collapses inner spaces so
        # the same bullet re-rendered with different padding (Claude TUI
        # re-paints frequently) doesn't double-fire.
        sig = " ".join(line.split())[:140]
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        out.append(line)
    return out


def _capture_pane_tail(pane_id: str, lines: int = 120) -> str:
    """Snapshot the last `lines` lines of a tmux pane. Empty string on failure."""
    try:
        cap = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id, "-S", f"-{int(lines)}"],
            capture_output=True, text=True, timeout=3.0,
        )
        return cap.stdout or ""
    except Exception:
        return ""


def _extract_grok_media(text: str) -> list[str]:
    if not text:
        return []
    seen: dict[str, None] = {}
    for m in _GROK_MEDIA_RE.finditer(text):
        path = m.group("path").rstrip(".,;:!?")
        if path not in seen and Path(path).is_file():
            seen[path] = None
    return list(seen.keys())


def _looks_like_single_emoji(text: str) -> bool:
    """True iff `text` is plausibly just one emoji (single-codepoint or
    multi-codepoint with ZWJ / variation selectors). Used to decide
    whether to deliver a bot reply as a Telegram reaction instead of a
    regular text message."""
    s = (text or "").strip()
    if not s or len(s) > 10:
        return False
    # Any ASCII letter / digit / common punctuation → not a bare emoji.
    for ch in s:
        if ch.isascii() and (ch.isalnum() or ch in "!?.,:;'\"()[]{}<>@#$%^&*_+-=\\|/"):
            return False
    return True


def _match_provider_prefix(body: str) -> str | None:
    if not body:
        return None
    m = _PROVIDER_PREFIX_RE.match(body.lstrip())
    if not m:
        return None
    candidate = m.group(1).lower()
    return candidate if candidate in SUPPORTED_PROVIDERS else None


def _provider_from_replied_to(msg: dict | None) -> str | None:
    """If the user replied to (or quoted) one of our `[Provider]` messages,
    return that provider. Checks `reply_to_message`, `external_reply`
    (cross-chat reply, Telegram 2023+), and `quote` (the user-highlighted
    snippet) — any of which may carry the prefix depending on which
    Telegram client UI the user used."""
    if not isinstance(msg, dict):
        return None

    # Same-chat reply: full original Message object.
    rtm = msg.get("reply_to_message")
    if isinstance(rtm, dict):
        body = str(rtm.get("text", "") or rtm.get("caption", "") or "")
        provider = _match_provider_prefix(body)
        if provider:
            return provider

    # Cross-chat / external reply (newer Telegram feature).
    ext = msg.get("external_reply")
    if isinstance(ext, dict):
        provider = _match_provider_prefix(str(ext.get("text", "") or ""))
        if provider:
            return provider

    # User-highlighted quote snippet (may not include the `[Provider]`
    # prefix if the user only quoted a portion mid-message — that's
    # fine, falls through to None).
    quote = msg.get("quote")
    if isinstance(quote, dict):
        provider = _match_provider_prefix(str(quote.get("text", "") or ""))
        if provider:
            return provider

    return None


def _format_sender(user: dict | None) -> str:
    """Render a Telegram User as `@username` or `First Last`."""
    if not isinstance(user, dict):
        return ""
    username = (user.get("username") or "").strip()
    if username:
        return f"@{username}"
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    return name or ""


def _format_origin(origin: dict | None) -> str:
    """Render a MessageOrigin (forward_origin / external_reply.origin)."""
    if not isinstance(origin, dict):
        return ""
    otype = (origin.get("type") or "").strip()
    if otype == "user":
        return _format_sender(origin.get("sender_user"))
    if otype == "hidden_user":
        return (origin.get("sender_user_name") or "").strip()
    if otype in ("chat", "channel"):
        chat = origin.get("sender_chat") or origin.get("chat") or {}
        username = (chat.get("username") or "").strip()
        if username:
            return f"@{username}"
        return (chat.get("title") or "").strip() or otype
    return ""


def _extract_reply_context(msg: dict) -> str:
    """Build a header describing the reply / quote / forward context so
    the model sees the conversational frame the user is responding to.
    Telegram only puts the user's own text in `msg.text`; the message
    they're replying to or quoting lives in sibling fields and is
    invisible to the provider unless we surface it explicitly."""
    if not isinstance(msg, dict):
        return ""
    parts: list[str] = []

    # Whole-message forward — `msg.text` already carries the body, so
    # we only need to annotate the source.
    fwd = msg.get("forward_origin")
    if isinstance(fwd, dict):
        src = _format_origin(fwd) or "unknown"
        parts.append(f"[forwarded from {src}]")

    # Quote-reply: a snippet from the replied message that the user
    # explicitly highlighted (Telegram 2023+ "reply with quote").
    quote = msg.get("quote")
    quote_text = ""
    if isinstance(quote, dict):
        quote_text = (quote.get("text") or "").strip()
        if quote_text:
            parts.append(f"[quoted snippet] {quote_text}")

    # Same-chat reply. If a quote snippet was provided, the full body
    # would just be noise — keep the attribution but skip the body.
    rtm = msg.get("reply_to_message")
    if isinstance(rtm, dict):
        sender = _format_sender(rtm.get("from")) or "someone"
        body = (rtm.get("text") or rtm.get("caption") or "").strip()
        if quote_text:
            parts.append(f"[replying to {sender}]")
        elif body:
            parts.append(f"[replying to {sender}] {body}")

    # Cross-chat reply (newer Telegram feature). `external_reply` carries
    # an origin + optional content snippet from the other chat.
    ext = msg.get("external_reply")
    if isinstance(ext, dict):
        src = _format_origin(ext.get("origin")) or "another chat"
        body = (ext.get("text") or "").strip()
        if body:
            parts.append(f"[replying to {src}] {body}")
        else:
            parts.append(f"[replying to {src}]")

    return "\n".join(parts)


@dataclass
class DaemonState:
    pid: int
    started_at: float
    status: str = "running"
    bot_username: str = ""
    last_update_id: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DaemonState":
        return cls(
            pid=int(data.get("pid", 0) or 0),
            started_at=float(data.get("started_at", 0.0) or 0.0),
            status=str(data.get("status", "running") or "running"),
            bot_username=str(data.get("bot_username", "") or "").strip(),
            last_update_id=int(data.get("last_update_id", 0) or 0),
        )


def _state_path(work_dir: str | Path | None = None) -> Path:
    return get_config_dir(work_dir) / STATE_FILE


def _pid_path(work_dir: str | Path | None = None) -> Path:
    return get_config_dir(work_dir) / PID_FILE


def _log_path(work_dir: str | Path | None = None) -> Path:
    return get_config_dir(work_dir) / LOG_FILE


def _write_log(line: str, work_dir: str | Path | None = None) -> None:
    path = _log_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")


def read_daemon_state(work_dir: str | Path | None = None) -> Optional[DaemonState]:
    path = _state_path(work_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return DaemonState.from_dict(data)


def write_daemon_state(state: DaemonState, work_dir: str | Path | None = None) -> None:
    path = _state_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    _pid_path(work_dir).write_text(str(state.pid), encoding="utf-8")


def remove_daemon_state(work_dir: str | Path | None = None) -> None:
    for path in (_state_path(work_dir), _pid_path(work_dir)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) == 0:
                    return False
                return code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def is_daemon_running(work_dir: str | Path | None = None) -> bool:
    state = read_daemon_state(work_dir)
    if not state:
        return False
    if _is_process_alive(state.pid):
        return True
    remove_daemon_state(work_dir)
    return False


def get_daemon_status(work_dir: str | Path | None = None) -> dict:
    state = read_daemon_state(work_dir)
    if not state or not _is_process_alive(state.pid):
        remove_daemon_state(work_dir)
        return {"running": False}
    return {
        "running": True,
        "pid": state.pid,
        "started_at": state.started_at,
        "uptime": time.time() - state.started_at,
        "bot_username": state.bot_username,
        "last_update_id": state.last_update_id,
    }


class TelegramDaemon:
    def __init__(self, config: Optional[TelegramConfig] = None, work_dir: str | Path | None = None):
        self.project_root = get_project_root(work_dir)
        self.config = config or load_config(self.project_root)
        self.stop_event = Event()
        self.client = TelegramBotClient(self.config.bot_token)
        self.state = DaemonState(pid=os.getpid(), started_at=time.time())
        # Per-(chat_id, provider) coalescing state. When a request is in flight
        # for a given pair, additional messages queue here and are flushed as a
        # single combined ask request when the worker drains the queue.
        self._chat_queues: dict[tuple[str, str], list[dict]] = {}
        self._chat_busy: dict[tuple[str, str], bool] = {}
        self._chat_state_lock = Lock()
        # /mount: per-process registry of clone-done flags keyed by
        # suggested_username. Persisted state lives in pending_mounts.json;
        # this dict only tracks the threading.Event for the corresponding
        # background clone. Empty until the first /mount or until
        # _mount.recover_pending repopulates it at startup.
        self._clone_events: dict[str, Event] = {}
        # Pending agent suggestions surfaced as inline-keyboard buttons on
        # `[Provider]` replies (`Want me to X?` → tap ✅ to dispatch X
        # verbatim). Keyed by short tokens that fit in Telegram's 64-byte
        # callback_data ceiling. Evicted FIFO past `_SUGGESTION_CAP` so the
        # daemon doesn't bloat across many turns. Lost on restart — taps
        # of expired suggestions answer with a "suggestion expired" toast.
        self._pending_suggestions: dict[str, tuple[str, str]] = {}
        self._pending_suggestions_lock = Lock()

    def start(self) -> None:
        me = self.client.get_me()
        username = ""
        if isinstance(me, dict):
            username = str(me.get("username") or "").strip()
        self.state.bot_username = username
        prior = read_daemon_state(self.project_root)
        if prior and prior.last_update_id:
            self.state.last_update_id = prior.last_update_id
        write_daemon_state(self.state, self.project_root)

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        # Register the `/` autocomplete menu so users get command
        # suggestions in Telegram clients. Best-effort: don't block
        # startup if the network call fails.
        try:
            self.client.set_my_commands([
                {"command": "new", "description": "reset a provider (e.g. /new codex)"},
                {"command": "reset", "description": "alias for /new"},
                {"command": "restart", "description": "alias for /new"},
                {"command": "clear", "description": "alias for /new"},
                {"command": "respawn", "description": "full restart of a provider's CLI"},
                {"command": "relaunch", "description": "alias for /respawn"},
                {"command": "context", "description": "show Claude's context window usage"},
                {"command": "compact", "description": "compact Claude's conversation context"},
                {"command": "status", "description": "show Claude's session status"},
                {"command": "stats", "description": "alias for /status"},
                {"command": "tail", "description": "peek at latest pane output (e.g. /tail codex)"},
                {"command": "usage", "description": "show token/cost usage"},
                {"command": "cost", "description": "show billing breakdown"},
                {"command": "config", "description": "open Claude config dialog"},
                {"command": "model", "description": "show/switch Claude model"},
                {"command": "mcp", "description": "show MCP server status"},
                {"command": "sessions", "description": "list Claude sessions"},
                {"command": "wake", "description": "schedule a future ask (e.g. /wake 5m check BTC price)"},
                {"command": "work", "description": "wake shortcut with work-first imperative (/work codex 30m)"},
                {"command": "mount", "description": "clone a repo + spawn a dedicated bot (manager only)"},
                {"command": "providers", "description": "list available providers"},
                {"command": "help", "description": "show usage"},
            ])
        except Exception as exc:
            _write_log(f"[telegramd] setMyCommands failed: {exc}", self.project_root)

        _write_log(f"[telegramd] started pid={self.state.pid} bot=@{username or 'unknown'}", self.project_root)

        # Re-hydrate /mount in-flight state. Must run before the polling
        # loop so any `update["managed_bot"]` arriving on the first poll
        # finds the threading.Event already registered.
        try:
            _mount.recover_pending(self, self.project_root)
        except Exception as exc:
            _write_log(f"[telegramd] mount recovery failed: {exc}", self.project_root)

        while not self.stop_event.is_set():
            try:
                updates = self.client.get_updates(
                    offset=(self.state.last_update_id + 1) if self.state.last_update_id else None,
                    timeout=self.config.long_poll_timeout_seconds,
                    # Explicit list — Telegram treats `allowed_updates` as
                    # a hard filter, so we must enumerate all currently-
                    # received types plus `managed_bot` (the new opt-in
                    # type from the Managed Bots feature). Daemon today
                    # only dispatches `message` and `managed_bot`, but we
                    # include the others defensively for forward compat.
                    allowed_updates=[
                        "message",
                        "edited_message",
                        "channel_post",
                        "edited_channel_post",
                        "callback_query",
                        "managed_bot",
                    ],
                )
                if not updates:
                    time.sleep(self.config.polling_interval_seconds)
                    continue
                # Dispatch each update in its own daemon thread so a slow
                # synchronous handler (e.g. `/tail` capturing a busy pane,
                # `/respawn` blocking on tmux respawn, a synchronous wake
                # roundtrip) does not stall the polling loop. Ask requests
                # already hand off via `_chat_queue` / `_chat_worker`, so
                # this addition only changes behaviour for the slash-command
                # path. We update `last_update_id` BEFORE spawning so the
                # next long-poll advances even if a handler hangs.
                advanced = False
                for update in updates:
                    try:
                        uid = int(update.get("update_id") or 0)
                        if uid > self.state.last_update_id:
                            self.state.last_update_id = uid
                            advanced = True
                    except Exception:
                        pass
                    Thread(
                        target=self._handle_update_safely,
                        args=(update,),
                        daemon=True,
                        name=f"telegramd-dispatch-{update.get('update_id', '?')}",
                    ).start()
                if advanced:
                    try:
                        write_daemon_state(self.state, self.project_root)
                    except Exception:
                        pass
            except TelegramApiError as exc:
                _write_log(f"[telegramd] api error: {exc}", self.project_root)
                time.sleep(max(2, self.config.polling_interval_seconds))
            except Exception as exc:
                _write_log(f"[telegramd] unexpected error: {exc}", self.project_root)
                time.sleep(max(2, self.config.polling_interval_seconds))

        remove_daemon_state(self.project_root)
        _write_log("[telegramd] stopped", self.project_root)

    def _handle_signal(self, signum, _frame) -> None:
        _write_log(f"[telegramd] received signal {signum}", self.project_root)
        self.stop_event.set()

    def _downloads_dir(self) -> Path:
        """Persistent per-project download dir for Telegram attachments."""
        d = get_config_dir(self.project_root) / "telegram_downloads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # File extensions we consider "voice-like" for auto-transcription.
    _AUDIO_EXTS = {".oga", ".ogg", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".flac"}

    def _download_attachments(self, msg: dict) -> list[tuple[Path, str]]:
        """Download all supported attachments from a Telegram message.

        Returns a list of (local_path, kind) tuples. `kind` is one of
        "photo", "document", "voice", "audio", "video", "video_note",
        "animation" — used downstream to decide whether to transcribe.
        Silently ignores unsupported types (sticker/location/etc.).
        """
        out: list[tuple[Path, str]] = []
        dest = self._downloads_dir()

        # photo: largest variant last
        photos = msg.get("photo")
        if isinstance(photos, list) and photos:
            biggest = max(photos, key=lambda p: int(p.get("file_size") or 0)) if any(isinstance(p, dict) for p in photos) else None
            if isinstance(biggest, dict):
                file_id = str(biggest.get("file_id") or "")
                if file_id:
                    mid = int(msg.get("message_id") or 0)
                    out.append((
                        self.client.download_file(file_id, dest, preferred_name=f"photo-{mid}.jpg"),
                        "photo",
                    ))

        # document, voice, audio, video, video_note, animation — single file each
        for key in ("document", "voice", "audio", "video", "video_note", "animation"):
            media = msg.get(key)
            if not isinstance(media, dict):
                continue
            file_id = str(media.get("file_id") or "")
            if not file_id:
                continue
            name = str(media.get("file_name") or "").strip()
            out.append((
                self.client.download_file(file_id, dest, preferred_name=name),
                key,
            ))

        return out

    def _whisper_model_path(self) -> Path | None:
        """Resolve which GGML model file to use for voice transcription.

        Resolution order: `CCB_WHISPER_MODEL` env, then the default
        `<install>/models/ggml-small.bin`. Returns None if no model
        file is present.
        """
        env_path = os.environ.get("CCB_WHISPER_MODEL", "").strip()
        if env_path:
            p = Path(env_path).expanduser()
            if p.is_file():
                return p
        # Default: prefer `~/.cache/ccb/whisper-models/` (survives
        # `install.sh` which `rm -rf`s the install prefix). Fall back to
        # legacy `<codex-dual>/models/` if someone still keeps it there.
        model_names = ("ggml-medium.bin", "ggml-small.bin", "ggml-base.bin", "ggml-tiny.bin")
        search_dirs = (
            Path.home() / ".cache" / "ccb" / "whisper-models",
            Path.home() / ".local" / "share" / "codex-dual" / "models",
        )
        for d in search_dirs:
            for name in model_names:
                p = d / name
                if p.is_file():
                    return p
        return None

    def _transcribe_voice(self, src: Path) -> str:
        """Transcribe an audio file via whisper-cli; return "" on any failure.

        Works on .oga/.ogg/.opus/etc. directly — whisper-cli handles
        conversion via its bundled ffmpeg reader. Runs with a hard
        wall-clock timeout so a hung model download can't wedge us.
        """
        model = self._whisper_model_path()
        if not model:
            return ""
        whisper_bin = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
        if not Path(whisper_bin).is_file():
            return ""
        # whisper-cli needs 16kHz mono WAV for best results; let ffmpeg
        # convert first so we don't depend on whisper's built-in decoder
        # (which varies by build).
        ffmpeg = shutil.which("ffmpeg")
        wav_path = src.with_suffix(".wav")
        try:
            if ffmpeg:
                subprocess.run(
                    [ffmpeg, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(wav_path)],
                    check=True, capture_output=True, timeout=60,
                )
                input_path = wav_path
            else:
                input_path = src
            result = subprocess.run(
                [whisper_bin, "-m", str(model), "-f", str(input_path),
                 "-l", "auto", "-nt", "-np", "--output-txt", "-of", str(input_path)],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode != 0:
                _write_log(
                    f"[telegramd] whisper-cli exit={result.returncode}: {result.stderr.strip()[:200]}",
                    self.project_root,
                )
                return ""
            # whisper-cli with -of writes <input_path>.txt
            txt_path = Path(f"{input_path}.txt")
            if txt_path.exists():
                return txt_path.read_text(encoding="utf-8", errors="replace").strip()
            # Fallback: parse stdout.
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            _write_log(f"[telegramd] whisper-cli timed out on {src.name}", self.project_root)
            return ""
        except Exception as exc:
            _write_log(f"[telegramd] whisper-cli error on {src.name}: {exc}", self.project_root)
            return ""
        finally:
            # Best-effort cleanup of intermediate wav; leave transcript .txt.
            try:
                if ffmpeg and wav_path.exists():
                    wav_path.unlink()
            except Exception:
                pass

    def _handle_update_safely(self, update: dict) -> None:
        """Thread entrypoint — never let a handler exception kill the worker.

        The polling loop spawns one of these per update so a slow / blocking
        handler (e.g. /tail capturing a stuck pane) does not stall the loop.
        last_update_id and the persisted state are advanced by the polling
        loop BEFORE this fires, so we don't need to redo that here.
        """
        try:
            self._handle_update(update)
        except Exception as exc:
            _write_log(
                f"[telegramd] dispatch worker error: {exc}",
                self.project_root,
            )

    def _handle_update(self, update: dict) -> None:
        update_id = int(update.get("update_id", 0) or 0)
        if update_id > self.state.last_update_id:
            self.state.last_update_id = update_id
            write_daemon_state(self.state, self.project_root)

        # Inline-keyboard tap on a `Want me to X?` suggestion button.
        cb = update.get("callback_query")
        if isinstance(cb, dict):
            try:
                self._handle_callback_query(cb)
            except Exception as exc:
                _write_log(f"[telegramd] callback handler error: {exc}", self.project_root)
            return

        # Managed-bots (the bot creation flow): a `managed_bot` update
        # arrives whenever a user taps a `t.me/newbot/<self>/...` link
        # we sent and completes the 1-tap creation. See `/mount`.
        mb = update.get("managed_bot")
        if isinstance(mb, dict):
            try:
                self._handle_managed_bot(mb)
            except Exception as exc:
                _write_log(f"[telegramd] managed_bot handler error: {exc}", self.project_root)
            return

        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        chat = msg.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id = str(chat.get("id", "") or "").strip()
        if self.config.allowed_chat_ids and chat_id not in self.config.allowed_chat_ids:
            return

        # Telegram chat.type is "private" for 1:1 DMs, "group"/"supergroup" for
        # multi-user. In DMs we skip reply_to_message_id for a natural chat feel;
        # in groups we keep it so threading is obvious when multiple users post.
        chat_type = str(chat.get("type", "") or "").strip().lower()
        is_group = chat_type in {"group", "supergroup", "channel"}
        message_id = int(msg.get("message_id", 0) or 0)
        reply_to = message_id if is_group else 0

        # Text, or caption that accompanies a media attachment.
        text = str(msg.get("text", "") or msg.get("caption", "") or "").strip()

        # Surface reply / quote / forward context. Telegram puts only
        # the user's own typing in `text`; anything they're replying to
        # or quoting lives in sibling fields and is otherwise invisible
        # to the provider. Skip for slash commands so command parsing
        # isn't disturbed.
        if text and not text.startswith("/"):
            reply_ctx = _extract_reply_context(msg)
            if reply_ctx:
                text = f"{reply_ctx}\n\n{text}"

        # Detect and download any attachments. Voice/audio get transcribed
        # via whisper-cli so the provider sees the actual spoken text.
        # Other files (photos, documents, video) are surfaced as
        # `[attachment] <path>` lines so the provider can open them with
        # its own tools. On download failure, we still proceed and
        # annotate the prompt so the bot at least knows the user tried
        # to send something (caption/context isn't lost just because the
        # binary fetch hiccuped).
        attachments: list[tuple[Path, str]] = []
        attachment_error = ""
        try:
            attachments = self._download_attachments(msg)
        except Exception as exc:
            attachment_error = str(exc)
            _write_log(f"[telegramd] attachment error chat={chat_id}: {exc}", self.project_root)
            self._send_text(chat_id, f"⚠️ couldn't fetch attachment: {exc}", reply_to_message_id=reply_to)

        if attachments:
            att_lines: list[str] = []
            for path, kind in attachments:
                is_voice_kind = kind in {"voice", "audio", "video_note"}
                is_audio_ext = path.suffix.lower() in self._AUDIO_EXTS
                if is_voice_kind or is_audio_ext:
                    transcript = self._transcribe_voice(path)
                    if transcript:
                        att_lines.append(f"[voice transcript] {transcript}")
                        _write_log(
                            f"[telegramd] transcribed {path.name}: {transcript[:60]!r}",
                            self.project_root,
                        )
                        continue
                att_lines.append(f"[attachment] {path}")
            atts = "\n".join(att_lines)
            text = f"{text}\n\n{atts}".strip() if text else atts

        if attachment_error:
            # Download failed upstream. Tell the bot the user tried to
            # send something, but don't drop the caption/accompanying
            # text if there was any.
            note = f"[attachment-failed] user tried to send an attachment but download failed: {attachment_error}"
            text = f"{text}\n\n{note}".strip() if text else note

        if not text:
            return

        parsed = parse_message(text, self.config.default_provider)
        if parsed.command == "help":
            self._send_text(chat_id, help_text(self.config.default_provider, self.config.broadcast_providers), reply_to_message_id=reply_to)
            return
        if parsed.command == "providers":
            providers = ", ".join(SUPPORTED_PROVIDERS)
            self._send_text(chat_id, f"Providers: {providers}", reply_to_message_id=reply_to)
            return
        if parsed.command in ("new", "new_all"):
            self._run_new_command(parsed, chat_id, reply_to)
            return
        if parsed.command == "new_usage":
            available = ", ".join(self._available_providers()) or "(none mounted)"
            self._send_text(
                chat_id,
                f"Usage: /new <provider> (or `all`).\nMounted: {available}",
                reply_to_message_id=reply_to,
            )
            return
        if parsed.command in ("respawn", "respawn_all"):
            self._run_respawn_command(parsed, chat_id, reply_to)
            return
        if parsed.command == "respawn_usage":
            available = ", ".join(self._available_providers()) or "(none mounted)"
            self._send_text(
                chat_id,
                f"Usage: /respawn <provider> (or `all`). Full CLI restart.\nMounted: {available}",
                reply_to_message_id=reply_to,
            )
            return
        if parsed.command in ("context", "compact", "status", "usage", "cost",
                              "config", "model", "mcp", "sessions"):
            self._run_slash_passthrough(
                parsed.command, parsed.provider or "claude",
                chat_id, reply_to,
                arg=(parsed.message or "").strip(),
            )
            return
        if parsed.command == "tail":
            self._run_tail_command(parsed.provider or "claude", chat_id, reply_to)
            return
        if parsed.command in ("wake_add", "wake_list", "wake_cancel", "wake_usage"):
            self._run_wake_command(parsed, chat_id, reply_to)
            return
        if parsed.command == "work_add":
            self._run_wake_command(parsed, chat_id, reply_to, work=True)
            return
        if parsed.command in ("mount_add", "mount_usage"):
            self._run_mount_command(parsed, chat_id, reply_to)
            return
        if parsed.command == "work_usage":
            self._send_text(
                chat_id,
                "Usage:\n"
                "  /work <duration> [hint]             — agent defaults to claude\n"
                "  /work <agent> <duration> [hint]     — explicit agent\n\n"
                "Like /wake but with a work-first imperative: the agent is told to make "
                "real edits/tool calls/commits this turn, THEN report, THEN self-schedule "
                "if not done. Prevents report-only lazy replies.\n\n"
                "Example: /work codex 30m PR #29 mobile DOM",
                reply_to_message_id=reply_to,
            )
            return
        if not parsed.message:
            self._send_text(chat_id, "Empty message.", reply_to_message_id=reply_to)
            return

        if parsed.targets:
            # Multi-mention: "@claude ... @codex ..." — deliver the full
            # message to each mentioned provider. They see each other's
            # mentions so they can reason about who else is addressed.
            providers = list(parsed.targets)
            # Silently skip mentioned providers that aren't mounted in
            # this project — user shouldn't get N error messages just
            # because they addressed a model that isn't running.
            mounted_set = set(self._available_providers())
            providers = [p for p in providers if p in mounted_set]
        elif parsed.broadcast:
            # Filter out unmounted providers for the same reason.
            mounted_set = set(self._available_providers())
            providers = [p for p in self.config.broadcast_providers if p in mounted_set]
        else:
            # Precedence: explicit prefix > reply_to target > default_provider.
            inferred = _provider_from_replied_to(msg)
            if not inferred:
                # Diagnostic: log what we saw so a mis-routed reply is
                # debuggable. Truncated to 80 chars per field.
                rtm_body = str((msg.get("reply_to_message") or {}).get("text", "") or
                               (msg.get("reply_to_message") or {}).get("caption", "") or "")[:80]
                ext_body = str((msg.get("external_reply") or {}).get("text", "") or "")[:80]
                qte_body = str((msg.get("quote") or {}).get("text", "") or "")[:80]
                if rtm_body or ext_body or qte_body:
                    _write_log(
                        f"[telegramd] reply-route miss: rtm={rtm_body!r} ext={ext_body!r} quote={qte_body!r}",
                        self.project_root,
                    )
            chosen = parsed.provider or inferred or self.config.default_provider
            providers = [chosen]

        if not providers:
            # Broadcast / multi-mention landed on zero mounted targets.
            # Tell the user once instead of going silent.
            available = ", ".join(self._available_providers()) or "(none)"
            self._send_text(
                chat_id,
                f"No mounted providers matched. Available: {available}",
                reply_to_message_id=reply_to,
            )
            return
        for provider in providers:
            self._enqueue_message(
                provider=provider,
                message=parsed.message,
                chat_id=chat_id,
                is_group=is_group,
                message_id=message_id,
            )

    def _enqueue_message(
        self,
        *,
        provider: str,
        message: str,
        chat_id: str,
        is_group: bool,
        message_id: int,
    ) -> None:
        """Queue a message for (chat_id, provider); start a worker if idle."""
        key = (chat_id, provider)
        item = {"message": message, "message_id": message_id, "is_group": is_group}
        with self._chat_state_lock:
            self._chat_queues.setdefault(key, []).append(item)
            if self._chat_busy.get(key):
                # Worker already running; it will drain this on its next loop.
                return
            self._chat_busy[key] = True
        Thread(
            target=self._chat_worker,
            args=(chat_id, provider),
            daemon=True,
            name=f"telegramd-worker-{chat_id}-{provider}",
        ).start()

    def _chat_worker(self, chat_id: str, provider: str) -> None:
        """Drain the queue for (chat_id, provider), batching while busy."""
        key = (chat_id, provider)
        try:
            while True:
                with self._chat_state_lock:
                    batch = self._chat_queues.get(key) or []
                    self._chat_queues[key] = []
                    if not batch:
                        self._chat_busy[key] = False
                        return
                self._run_batch(provider, chat_id, batch)
        except Exception as exc:
            _write_log(f"[telegramd] worker error chat={chat_id} provider={provider}: {exc}", self.project_root)
            with self._chat_state_lock:
                self._chat_busy[key] = False

    def _run_batch(self, provider: str, chat_id: str, batch: list[dict]) -> None:
        """Combine queued messages into one ask request."""
        if not batch:
            return
        is_group = any(item.get("is_group") for item in batch)
        # In group chats we anchor the reply at the first queued message so users
        # can trace which burst triggered the reply. In DMs we send plain.
        anchor_id = int(batch[0].get("message_id") or 0) if is_group else 0
        # Track the original message id even in DMs so short emoji replies
        # can be delivered as reactions on that message (only meaningful
        # for single-message batches; a coalesced batch of >1 doesn't have
        # one natural "target" message).
        source_message_id = int(batch[0].get("message_id") or 0) if len(batch) == 1 else 0
        if len(batch) == 1:
            combined = str(batch[0].get("message") or "")
        else:
            lines = [
                f"The following {len(batch)} messages arrived back-to-back from the same user. "
                "Treat them as one combined turn and give a single consolidated reply.",
                "",
            ]
            for i, item in enumerate(batch, 1):
                lines.append(f"{i}. {item.get('message') or ''}")
            combined = "\n".join(lines)
        self._run_request(provider, combined, chat_id, anchor_id, source_message_id=source_message_id)

    def _run_request(self, provider: str, message: str, chat_id: str, reply_to_message_id: int, *, source_message_id: int = 0) -> None:
        mounted = self._available_providers()
        if not mounted:
            self._send_text(
                chat_id,
                "CCB is currently offline for this project. No models are mounted right now.",
                reply_to_message_id=reply_to_message_id,
            )
            return
        if provider not in mounted:
            available = ", ".join(mounted)
            self._send_text(
                chat_id,
                f"[{provider.capitalize()}] is not currently mounted for this project.\nAvailable models: {available}",
                reply_to_message_id=reply_to_message_id,
            )
            return

        # Typing indicator loop: Telegram's "... is typing" status expires after
        # ~5s, so keep re-sending sendChatAction every 4s until the reply is
        # ready. This replaces the old "[Provider] processing..." text message.
        typing_stop = Event()

        def _typing_loop() -> None:
            while not typing_stop.is_set():
                try:
                    self.client.send_chat_action(chat_id, "typing")
                except Exception:
                    pass
                # Re-send slightly before the 5s expiry so the indicator stays continuous.
                typing_stop.wait(4.0)

        typing_thread: Optional[Thread] = None
        if self.config.send_acknowledgements:
            typing_thread = Thread(target=_typing_loop, name="telegramd-typing", daemon=True)
            typing_thread.start()

        # Hermes live tool-chain: stream each NEW agent bullet (`● Read …`,
        # `● Bash …`, `✶ Thinking …`, etc.) as it lands in the pane, so the
        # user sees the actual progress flow — not just the latest line.
        # All transient messages are deleted on completion so only the
        # final reply remains in the channel. Skipped for headless
        # providers (no tmux pane).
        hermes_stop = Event()
        hermes_message_ids: list[int] = []  # all transient ids, for cleanup
        # Race guard: held by the hermes worker while sending a bullet AND
        # appending its message_id to `hermes_message_ids`. `_stop_hermes`
        # acquires the same lock before draining for delete, so a bullet
        # mid-send during stop completes + registers BEFORE we try to clean
        # up — no orphan transients survive the final reply.
        hermes_send_lock = Lock()

        def _hermes_loop(pane_id: str) -> None:
            log_path = _resolve_pane_log_path(pane_id)
            if not log_path:
                return
            # Initial dwell — don't fire for sub-3s tasks.
            if hermes_stop.wait(3.0):
                return
            offset = _pane_log_offset(log_path)
            seen_signatures: set[str] = set()
            needle = (message or "").strip().split("\n", 1)[0][:80].lower()
            # Backfill protection: when the pane log is fresh after a bounce
            # (or `claude --resume` replays history into it AFTER offset
            # capture), the first poll can surface dozens of stale bullets
            # and flood the chat. Detect that and skip-but-record-signatures
            # so future polls only emit truly-new lines. Also enforce hard
            # ceilings so a runaway never overruns the chat.
            first_poll = True
            MAX_TRANSIENT_PER_ASK = 20
            MAX_BULLETS_PER_CYCLE = 3
            FIRST_POLL_BACKFILL_THRESHOLD = 5
            while not hermes_stop.is_set():
                try:
                    new_text = _read_pane_log_from_offset(log_path, offset)
                    if new_text:
                        # Advance offset by raw byte length (pre-ANSI strip).
                        try:
                            offset = log_path.stat().st_size
                        except Exception:
                            pass
                        bullets = _extract_new_bullets(new_text, seen_signatures, needle)
                        # First poll backfill suppression — sigs were already
                        # added to `seen_signatures` by _extract_new_bullets,
                        # so we won't re-emit them on subsequent polls.
                        if first_poll and len(bullets) > FIRST_POLL_BACKFILL_THRESHOLD:
                            _write_log(
                                f"[telegramd] hermes backfill suppressed "
                                f"chat={chat_id} provider={provider} count={len(bullets)}",
                                self.project_root,
                            )
                            bullets = []
                        first_poll = False
                        # Per-cycle cap — if real activity is still emitting
                        # faster than we can post, keep only the latest.
                        if len(bullets) > MAX_BULLETS_PER_CYCLE:
                            bullets = bullets[-MAX_BULLETS_PER_CYCLE:]
                        # Hard ceiling on total transient messages per ask.
                        remaining = MAX_TRANSIENT_PER_ASK - len(hermes_message_ids)
                        if remaining <= 0:
                            # Already at ceiling — stop spamming, but keep
                            # polling so we update seen_signatures and don't
                            # backlog when the ask eventually completes.
                            bullets = []
                        else:
                            bullets = bullets[:remaining]
                        for bullet in bullets:
                            if hermes_stop.is_set():
                                break
                            text = f"[{provider.capitalize()}] ⏳ {bullet[:140]}"
                            # Hold the send-lock around send+append so a
                            # concurrent `_stop_hermes` can't race past the
                            # in-flight bullet and leave it un-tracked (and
                            # therefore un-deletable) after the final reply.
                            with hermes_send_lock:
                                if hermes_stop.is_set():
                                    break
                                try:
                                    resp = self.client.send_message(chat_id, text)
                                    mid = int((resp or {}).get("message_id") or 0) if isinstance(resp, dict) else 0
                                    if mid:
                                        hermes_message_ids.append(mid)
                                except Exception:
                                    pass
                except Exception as exc:
                    _write_log(
                        f"[telegramd] hermes loop error chat={chat_id}: {exc}",
                        self.project_root,
                    )
                hermes_stop.wait(4.0)

        hermes_thread: Optional[Thread] = None
        hermes_pane = self._lookup_pane_id_for_provider(provider)
        if hermes_pane and self.config.send_acknowledgements:
            hermes_thread = Thread(target=_hermes_loop, args=(hermes_pane,), name="telegramd-hermes", daemon=True)
            hermes_thread.start()

        def _stop_hermes() -> None:
            if hermes_thread is not None:
                hermes_stop.set()
                # Allow up to 10s for an in-flight HTTP send_message to
                # complete (Telegram timeouts dominate this number). Without
                # the bump we'd have routinely returned with a still-sending
                # bullet and missed its delete.
                hermes_thread.join(timeout=10.0)
            # Acquire the send-lock so any send_message that finished AFTER
            # the join but BEFORE we delete has its mid in
            # `hermes_message_ids` before we drain. This closes the last
            # window where a transient could survive the final reply.
            with hermes_send_lock:
                for mid in list(hermes_message_ids):
                    try:
                        self.client.delete_message(chat_id, mid)
                    except Exception:
                        pass
                hermes_message_ids.clear()

        def _stop_typing() -> None:
            if typing_thread is not None:
                typing_stop.set()
                typing_thread.join(timeout=1.0)
            _stop_hermes()

        ask_cmd = self._find_ask_command()
        if not ask_cmd:
            _stop_typing()
            self._send_text(chat_id, f"[{provider.capitalize()}] ask command not found", reply_to_message_id=reply_to_message_id)
            return

        work_dir = self._work_dir()
        env = os.environ.copy()
        env["CCB_CALLER"] = "telegram"
        env["CCB_WORK_DIR"] = work_dir
        env["CCB_ASK_EMIT_GUARDRAIL"] = "0"
        env["CCB_UNIFIED_ASKD"] = "1"
        env["CCB_ASKD_AUTOSTART"] = "0"
        # Our own ask invocation captures stdout and posts the reply; the
        # completion hook should NOT also post for this call. Flag it.
        # Nested asks from Claude's pane don't inherit this env var (pane
        # env is set at ccb startup), so their hook post still fires.
        env["CCB_TELEGRAM_SYNC_REPLY"] = "1"
        env.pop("CCB_PARENT_PID", None)
        env.pop("CCB_MANAGED", None)

        # Drop the chat_id in a project-scoped file. Nested asks that
        # Claude fires from inside its pane (the `ask @codex …` delegation
        # pattern) can't read telegramd's env, but the `ask` CLI falls
        # back to this file so the completion hook has somewhere to send
        # the eventual reply. The file is overwritten each turn, so the
        # "active chat" is always the last Telegram sender — fine for the
        # single-user DM setup we have.
        if chat_id:
            try:
                active = self.project_root / ".ccb" / ".active_chat_id"
                active.parent.mkdir(parents=True, exist_ok=True)
                active.write_text(str(chat_id).strip() + "\n")
            except Exception:
                pass
        # Telegramd is a daemon, not a terminal pane. Stripping these prevents
        # `ask` from populating caller_pane_id with whatever pane happened to
        # be in the shell that launched telegramd — which would misroute
        # completion-hook notifications to an unrelated Claude session.
        for _pane_var in ("TMUX_PANE", "WEZTERM_PANE", "CCB_CALLER_PANE_ID", "CCB_CALLER_TERMINAL"):
            env.pop(_pane_var, None)
        try:
            project_id = compute_ccb_project_id(self.project_root)
            if project_id:
                env["CCB_RUN_DIR"] = str(Path.home() / ".cache" / "ccb" / "projects" / project_id[:16])
        except Exception:
            pass

        # Cap per-Telegram-message timeout regardless of config: a stuck
        # ask (provider drift-off-format, pane hang, etc.) should fail fast
        # so the bot's chat worker doesn't head-of-line-block for an hour.
        # 5-min cap was too aggressive — even a single tool call from
        # Claude (e.g. `git show <large commit>`, deep playwright runs)
        # can exceed 2 min. Bumped to 30 min — still meaningfully shorter
        # than the 1h `request_timeout_seconds` config default but won't
        # cut off normal multi-step investigations mid-tool.
        # Override via `CCB_TELEGRAM_ASK_TIMEOUT_S` env if a specific bot
        # really needs longer-running tasks.
        default_cap = 1800
        try:
            cap = int(os.environ.get("CCB_TELEGRAM_ASK_TIMEOUT_S", "") or default_cap)
        except Exception:
            cap = default_cap
        ask_timeout_s = min(self.config.request_timeout_seconds, cap)

        # Phase C: snapshot the pane log offset BEFORE ask runs so we have a
        # marker to start late-completion tailing from if ask exits without
        # seeing CCB_DONE. `hermes_pane` was resolved above for the live tail;
        # we reuse it here when present.
        late_log_path: Optional[Path] = None
        late_log_offset: int = 0
        if hermes_pane:
            late_log_path = _resolve_pane_log_path(hermes_pane)
            late_log_offset = _pane_log_offset(late_log_path)

        ask_started_at = time.time()
        try:
            result = subprocess.run(
                [ask_cmd, provider, "--foreground", "--timeout", str(ask_timeout_s)],
                cwd=work_dir,
                env=env,
                input=message,
                capture_output=True,
                text=True,
                timeout=ask_timeout_s + 30,
            )
            ask_returned_at = time.time()
            _write_log(
                f"[TIMING] ask subprocess returned chat={chat_id} provider={provider} "
                f"elapsed={(ask_returned_at - ask_started_at):.2f}s rc={result.returncode} "
                f"stdout_len={len(result.stdout or '')}",
                self.project_root,
            )
        except subprocess.TimeoutExpired:
            _stop_typing()
            self._send_text(chat_id, f"[{provider.capitalize()}] timed out", reply_to_message_id=reply_to_message_id)
            return
        except Exception as exc:
            _stop_typing()
            self._send_text(chat_id, f"[{provider.capitalize()}] failed to start ask: {exc}", reply_to_message_id=reply_to_message_id)
            return

        _stop_typing()

        reply = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        # Safety strip: ask is supposed to extract just the text BETWEEN
        # `CCB_BEGIN: <id>` and `CCB_DONE: <id>`, but when Claude prefixes
        # the protocol opener with a `●` bullet glyph (which the TUI does
        # for its own assistant rendering), the anchored extraction misses
        # the begin line and the whole wrapped block falls through. Trim
        # any stray protocol markers + leading bullet here so they never
        # reach the user even when ask's regex misfires.
        if reply:
            reply = re.sub(
                r"^\s*●?\s*CCB_BEGIN:\s*[0-9-]+\s*\n?", "", reply,
                flags=re.IGNORECASE,
            )
            reply = re.sub(
                r"\n?\s*CCB_DONE:\s*[0-9-]+\s*$", "", reply,
                flags=re.IGNORECASE,
            )
            reply = reply.strip()

        # Phase C: non-zero exit means ask ended without seeing CCB_DONE.
        # The user-visible text might be a *partial*, not the real conclusion.
        # Instead of posting it as if it were final (and losing the genuine
        # answer when Claude finally emits CCB_DONE in the pane), post a
        # transient warning and spawn a watcher that tails the pane log for
        # the late completion marker. Skipped when we have no pane (headless
        # providers) — those use the old post-and-return behaviour.
        if result.returncode != 0 and late_log_path is not None:
            # Stop typing + Hermes BEFORE posting warning so the live
            # transients get deleted and the warning becomes the only
            # in-chat indicator until the watcher fires. Without this,
            # Hermes kept emitting bullets after the timeout — the loop
            # was alive until `_run_request` returned, but the closure's
            # `hermes_message_ids` were never drained for delete.
            _stop_typing()
            warning_msg_id = 0
            try:
                resp = self.client.send_message(
                    chat_id,
                    f"[{provider.capitalize()}] ⏱️ Hit the ask timeout — agent still working in the pane. "
                    f"I'll post the real reply when CCB_DONE arrives (giving it up to 60 min).",
                    reply_to_message_id=reply_to_message_id,
                )
                if isinstance(resp, dict):
                    warning_msg_id = int(resp.get("message_id") or 0)
            except Exception as exc:
                _write_log(
                    f"[telegramd] late-completion warning post failed chat={chat_id}: {exc}",
                    self.project_root,
                )
            Thread(
                target=self._watch_for_late_completion,
                kwargs=dict(
                    provider=provider,
                    chat_id=chat_id,
                    reply_to_message_id=reply_to_message_id,
                    log_path=late_log_path,
                    start_offset=late_log_offset,
                    warning_msg_id=warning_msg_id,
                    timeout_at=time.time() + 60 * 60,
                ),
                daemon=True,
                name=f"telegramd-late-{chat_id}-{provider}",
            ).start()
            return

        if reply:
            # If the reply is essentially just an emoji, try to attach it
            # as a Telegram reaction on the source message instead of
            # sending a whole new message. Falls back to text on any
            # failure (e.g. emoji not in Telegram's allowed set).
            if source_message_id and _looks_like_single_emoji(reply):
                try:
                    self.client.set_message_reaction(chat_id, source_message_id, reply)
                    return
                except Exception as exc:
                    _write_log(
                        f"[telegramd] reaction fallback → text (chat={chat_id}, emoji={reply!r}): {exc}",
                        self.project_root,
                    )
            # Grok image_gen / video_gen path relay: the GrokAdapter writes
            # the absolute file path into the reply text. Sync mode bypasses
            # the completion hook's attachment plumb, so we re-extract here
            # and sendPhoto/sendVideo first, then post the text.
            attachments = _extract_grok_media(reply) if provider == "grok" else []
            for idx, file_path in enumerate(attachments):
                caption = f"[{provider.capitalize()}]" if idx == 0 else ""
                try:
                    path_obj = Path(file_path)
                    ext = path_obj.suffix.lower().lstrip(".")
                    if ext in {"jpg", "jpeg", "png", "webp", "gif"}:
                        self.client.send_photo(chat_id, path_obj, caption=caption, reply_to_message_id=reply_to_message_id if idx == 0 else None)
                    elif ext in {"mp4", "webm", "mov"}:
                        self.client.send_video(chat_id, path_obj, caption=caption, reply_to_message_id=reply_to_message_id if idx == 0 else None)
                    else:
                        self.client.send_document(chat_id, path_obj, caption=caption, reply_to_message_id=reply_to_message_id if idx == 0 else None)
                except Exception as exc:
                    _write_log(
                        f"[telegramd] grok media send failed chat={chat_id} path={file_path}: {exc}",
                        self.project_root,
                    )
            # Detect a `Want me to X?` suggestion in the reply and attach
            # a ✅ inline button so the user can dispatch the action with
            # one tap instead of typing it back. Callback returns through
            # `_handle_callback_query` → enqueues the action verbatim.
            suggestion = _extract_suggestion(reply)
            reply_markup: dict | None = None
            if suggestion:
                token = self._register_suggestion(provider, suggestion)
                # Just `✅ Yes`. The action text is already visible in the
                # reply body (Claude said "Want me to X?"), so the button
                # only needs to confirm acceptance — single short word
                # keeps it tap-friendly. Ignoring = no tap = no follow-up.
                reply_markup = {
                    "inline_keyboard": [[
                        {"text": "✅ Yes", "callback_data": f"sug:{token}"},
                    ]]
                }
            self._send_text(
                chat_id, f"[{provider.capitalize()}]\n{reply}",
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup,
            )
            _write_log(
                f"[TIMING] final reply posted chat={chat_id} provider={provider} "
                f"total_elapsed={(time.time() - ask_started_at):.2f}s "
                f"send_gap={(time.time() - ask_returned_at):.2f}s "
                f"suggestion={'yes' if suggestion else 'no'}",
                self.project_root,
            )
            return
        if result.returncode != 0:
            msg = err or f"ask exited with code {result.returncode}"
            self._send_text(chat_id, f"[{provider.capitalize()}] {msg}", reply_to_message_id=reply_to_message_id)
            return
        self._send_text(chat_id, f"[{provider.capitalize()}] (empty reply)", reply_to_message_id=reply_to_message_id)

    def _send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> None:
        try:
            chunks = list(chunk_message(text))
            for idx, chunk in enumerate(chunks):
                # Inline-keyboard reply_markup only attaches to the LAST
                # chunk so the button is anchored under the full reply,
                # not in the middle of a chunked message.
                last = (idx == len(chunks) - 1)
                self.client.send_message(
                    chat_id, chunk,
                    reply_to_message_id=reply_to_message_id,
                    reply_markup=(reply_markup if last else None),
                )
                reply_to_message_id = None
        except Exception as exc:
            _write_log(f"[telegramd] send error chat={chat_id}: {exc}", self.project_root)

    def _register_suggestion(self, provider: str, action: str) -> str:
        """Stash a pending agent suggestion; return its short callback-data token."""
        import secrets
        token = secrets.token_hex(6)  # 12 chars → fits in 64-byte callback_data
        with self._pending_suggestions_lock:
            if len(self._pending_suggestions) >= _SUGGESTION_CAP:
                # FIFO eviction — drop the oldest key.
                oldest = next(iter(self._pending_suggestions))
                self._pending_suggestions.pop(oldest, None)
            self._pending_suggestions[token] = (provider, action)
        return token

    def _handle_callback_query(self, cb: dict) -> None:
        """Process an inline-keyboard tap.

        Looks up the suggestion by callback_data token, acks the tap, and
        dispatches the suggested action through the normal chat-queue path
        so it goes through the same ask flow as a typed message.
        """
        cb_id = str(cb.get("id") or "")
        data = str(cb.get("data") or "")
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        msg_id = int(msg.get("message_id") or 0)
        if not (cb_id and data.startswith("sug:") and chat_id):
            try:
                self.client.answer_callback_query(cb_id)
            except Exception:
                pass
            return
        if self.config.allowed_chat_ids and chat_id not in self.config.allowed_chat_ids:
            try:
                self.client.answer_callback_query(cb_id, text="Not authorised")
            except Exception:
                pass
            return
        token = data[4:]
        with self._pending_suggestions_lock:
            entry = self._pending_suggestions.pop(token, None)
        if not entry:
            try:
                self.client.answer_callback_query(cb_id, text="Suggestion expired")
            except Exception:
                pass
            return
        provider, action = entry
        try:
            self.client.answer_callback_query(cb_id, text=f"Dispatching: {action[:60]}")
        except Exception:
            pass
        # Strip the button from the message so the user knows it was taken.
        try:
            self.client.edit_message_reply_markup(chat_id, msg_id, None)
        except Exception:
            pass
        self._enqueue_for_provider(
            chat_id=chat_id,
            provider=provider,
            message=action,
            message_id=msg_id,
            is_group=False,
        )

    def _work_dir(self) -> str:
        if self.config.default_work_dir:
            candidate = Path(self.config.default_work_dir).expanduser()
            if not candidate.is_absolute():
                candidate = self.project_root / candidate
            return str(candidate)
        return str(self.project_root)

    def _available_providers(self) -> list[str]:
        work_dir = self.project_root
        mounted: list[str] = []
        for provider in SUPPORTED_PROVIDERS:
            session_name = SESSION_FILES.get(provider)
            if not session_name:
                continue
            session_file = find_project_session_file(work_dir, session_name)
            if not session_file or not session_file.exists():
                continue
            try:
                data = json.loads(session_file.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("active") is False:
                continue
            mounted.append(provider)
        return mounted

    def _find_ask_command(self) -> str | None:
        project_root = Path(__file__).resolve().parents[2]
        candidates = [
            project_root / "bin" / "ask",
            Path.home() / ".local" / "bin" / "ask",
            Path.home() / ".local" / "share" / "codex-dual" / "bin" / "ask",
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        return None

    def _find_autonew_command(self) -> str | None:
        project_root = Path(__file__).resolve().parents[2]
        candidates = [
            project_root / "bin" / "autonew",
            Path.home() / ".local" / "bin" / "autonew",
            Path.home() / ".local" / "share" / "codex-dual" / "bin" / "autonew",
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        return None

    def _run_new_command(self, parsed, chat_id: str, reply_to_message_id: int) -> None:
        """Handle `/new <provider>` and `/new all` — reset provider sessions.

        Runs `autonew <provider>` directly (no AI round-trip). Results are
        delivered as a short Telegram reply.
        """
        autonew_cmd = self._find_autonew_command()
        if not autonew_cmd:
            self._send_text(chat_id, "autonew command not found on this host.",
                            reply_to_message_id=reply_to_message_id)
            return

        if parsed.command == "new_all":
            targets = list(self._available_providers())
            if not targets:
                self._send_text(chat_id, "No providers are mounted for this project.",
                                reply_to_message_id=reply_to_message_id)
                return
        else:
            target = (parsed.provider or "").strip().lower()
            if not target or target not in SUPPORTED_PROVIDERS:
                rest = (parsed.message or "").strip() or "(empty)"
                self._send_text(
                    chat_id,
                    f"Unknown provider for /new: {rest!r}. Try one of: "
                    f"{', '.join(SUPPORTED_PROVIDERS)} or 'all'.",
                    reply_to_message_id=reply_to_message_id,
                )
                return
            targets = [target]

        work_dir = self._work_dir()
        env = os.environ.copy()
        env["CCB_WORK_DIR"] = work_dir
        # Don't let inherited pane vars confuse autonew's pane resolver.
        for v in ("TMUX_PANE", "WEZTERM_PANE", "CCB_CALLER_PANE_ID", "CCB_CALLER_TERMINAL"):
            env.pop(v, None)

        results: list[str] = []
        for target in targets:
            try:
                rc = subprocess.run(
                    [autonew_cmd, target],
                    cwd=work_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if rc.returncode == 0:
                    results.append(f"✓ {target}")
                else:
                    msg = (rc.stderr or rc.stdout or "").strip().splitlines()
                    tail = msg[-1] if msg else f"exit {rc.returncode}"
                    results.append(f"✗ {target}: {tail[:120]}")
            except subprocess.TimeoutExpired:
                results.append(f"✗ {target}: timed out")
            except Exception as exc:
                results.append(f"✗ {target}: {exc}")

        self._send_text(chat_id, "Reset:\n" + "\n".join(results),
                        reply_to_message_id=reply_to_message_id)

    def _run_respawn_command(self, parsed, chat_id: str, reply_to_message_id: int) -> None:
        """Kill + relaunch a provider's CLI in its tmux/wezterm pane.

        Full process restart (new PID), not just a `/new` inside the CLI.
        Reads pane_id + start_cmd from the provider's session file and
        calls the terminal backend's `respawn_pane`.
        """
        if parsed.command == "respawn_all":
            targets = list(self._available_providers())
            if not targets:
                self._send_text(chat_id, "No providers are mounted for this project.",
                                reply_to_message_id=reply_to_message_id)
                return
        else:
            target = (parsed.provider or "").strip().lower()
            if not target or target not in SUPPORTED_PROVIDERS:
                rest = (parsed.message or "").strip() or "(empty)"
                self._send_text(
                    chat_id,
                    f"Unknown provider for /respawn: {rest!r}. Try one of: "
                    f"{', '.join(SUPPORTED_PROVIDERS)} or 'all'.",
                    reply_to_message_id=reply_to_message_id,
                )
                return
            targets = [target]

        work_dir = self._work_dir()
        # Import lazily to avoid pulling terminal backends on startup.
        try:
            from terminal import get_backend_for_session
        except Exception as exc:
            self._send_text(chat_id, f"Could not load terminal backend: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        results: list[str] = []
        for target in targets:
            session_name = SESSION_FILES.get(target)
            if not session_name:
                results.append(f"✗ {target}: no session file mapping")
                continue
            session_file = find_project_session_file(self.project_root, session_name)
            if not session_file or not session_file.exists():
                results.append(f"✗ {target}: no active session")
                continue
            try:
                data = json.loads(session_file.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                results.append(f"✗ {target}: unreadable session ({exc})")
                continue
            pane_id = str(data.get("pane_id") or "").strip()
            start_cmd = str(data.get("start_cmd") or "").strip()
            if not (pane_id and start_cmd):
                results.append(f"✗ {target}: missing pane_id or start_cmd")
                continue
            try:
                backend = get_backend_for_session(data)
                if not backend or not hasattr(backend, "respawn_pane"):
                    results.append(f"✗ {target}: backend lacks respawn_pane")
                    continue
                backend.respawn_pane(pane_id, cmd=start_cmd, cwd=work_dir, remain_on_exit=True)
                results.append(f"✓ {target} (pane {pane_id})")
            except Exception as exc:
                results.append(f"✗ {target}: {exc}")

        self._send_text(chat_id, "Respawn:\n" + "\n".join(results),
                        reply_to_message_id=reply_to_message_id)

    def _run_wake_command(self, parsed, chat_id: str, reply_to_message_id: int, *, work: bool = False) -> None:
        """Schedule / list / cancel wakes from Telegram.

        Shells out to the existing `wake` CLI instead of reimplementing
        the queue format — keeps parity with what models and the terminal
        shell already do. CCB_WORK_DIR is pinned to this project so the
        queue file ends up at <project>/.ccb/wake_queue.json.
        """
        wake_cmd = shutil.which("wake") or str(
            Path.home() / ".local" / "share" / "codex-dual" / "bin" / "wake"
        )
        if not Path(wake_cmd).is_file():
            self._send_text(chat_id, "wake command not found on this host.",
                            reply_to_message_id=reply_to_message_id)
            return

        env = os.environ.copy()
        env["CCB_WORK_DIR"] = self._work_dir()

        if parsed.command == "wake_usage":
            self._send_text(
                chat_id,
                "Usage:\n"
                "  /wake <duration> <message>             — agent defaults to claude\n"
                "  /wake <agent> <duration> <message>     — explicit agent\n"
                "  /wake list                             — show pending\n"
                "  /wake cancel <wake_id>                 — remove one\n\n"
                "Duration: 30s | 5m | 1h | 1h30m\n"
                "Example: /wake 15m check BTC price + summarize",
                reply_to_message_id=reply_to_message_id,
            )
            return

        if parsed.command == "wake_list":
            try:
                out = subprocess.run(
                    [wake_cmd, "list"], cwd=self._work_dir(), env=env,
                    capture_output=True, text=True, timeout=10,
                )
            except Exception as exc:
                self._send_text(chat_id, f"wake list failed: {exc}",
                                reply_to_message_id=reply_to_message_id)
                return
            body = (out.stdout or "").strip() or "(no pending wakes)"
            self._send_text(chat_id, f"```\n{body}\n```",
                            reply_to_message_id=reply_to_message_id)
            return

        if parsed.command == "wake_cancel":
            wake_id = (parsed.message or "").strip().split()[0] if parsed.message else ""
            if not wake_id:
                self._send_text(chat_id, "Usage: /wake cancel <wake_id>",
                                reply_to_message_id=reply_to_message_id)
                return
            try:
                out = subprocess.run(
                    [wake_cmd, "cancel", wake_id], cwd=self._work_dir(), env=env,
                    capture_output=True, text=True, timeout=10,
                )
            except Exception as exc:
                self._send_text(chat_id, f"wake cancel failed: {exc}",
                                reply_to_message_id=reply_to_message_id)
                return
            body = (out.stdout or out.stderr or "").strip() or f"rc={out.returncode}"
            self._send_text(chat_id, body, reply_to_message_id=reply_to_message_id)
            return

        # wake_add
        agent = (parsed.provider or "claude").strip().lower()
        # parsed.message is "<duration> <message>"; split off duration.
        parts = (parsed.message or "").split(None, 1)
        if len(parts) < 2:
            self._send_text(chat_id, "Usage: /wake <duration> <message>",
                            reply_to_message_id=reply_to_message_id)
            return
        duration, message = parts[0].strip(), parts[1].strip()
        if not message:
            self._send_text(chat_id, "Message required.",
                            reply_to_message_id=reply_to_message_id)
            return
        try:
            cmd_args = [wake_cmd, "add", agent, "--in", duration,
                        "--caller", "telegram", "--chat-id", str(chat_id)]
            if work:
                cmd_args.append("--work")
            cmd_args.append(message)
            out = subprocess.run(
                cmd_args,
                cwd=self._work_dir(), env=env,
                capture_output=True, text=True, timeout=15,
            )
        except Exception as exc:
            self._send_text(chat_id, f"wake add failed: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return
        if out.returncode != 0:
            err = (out.stderr or out.stdout or "").strip() or f"rc={out.returncode}"
            self._send_text(chat_id, f"wake: {err}",
                            reply_to_message_id=reply_to_message_id)
            return
        wake_id = (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
        self._send_text(
            chat_id,
            f"⏰ Wake scheduled: `{wake_id}`\n"
            f"{agent} in {duration} → will reply here when it fires.",
            reply_to_message_id=reply_to_message_id,
        )

    # ------------------------------------------------------------------
    # /mount — clone repo + spawn dedicated bot.
    # See `_mount.py` for helpers and the recovery dance on daemon
    # restart. The slash handler MUST persist pending state and register
    # the in-memory Event BEFORE starting the clone thread or sending
    # the inline keyboard, otherwise a fast user tap can race the JSON
    # write and `_handle_managed_bot` fails to find the entry.
    # ------------------------------------------------------------------

    def _run_mount_command(self, parsed, chat_id: str, reply_to_message_id: int) -> None:
        if parsed.command == "mount_usage":
            self._send_text(
                chat_id,
                "Usage: `/mount <git-url>`\n"
                "Supported: github.com / gitlab.com / bitbucket.org (https or ssh).",
                reply_to_message_id=reply_to_message_id,
            )
            return

        url = (parsed.message or "").strip()
        parsed_url = _mount.parse_repo_url(url)
        if not parsed_url:
            self._send_text(
                chat_id,
                "URL must be github.com / gitlab.com / bitbucket.org (https or ssh).",
                reply_to_message_id=reply_to_message_id,
            )
            return
        owner, repo = parsed_url
        target = _mount.target_path(owner, repo)
        suggested = _mount.suggested_username(owner, repo)
        manager_username = (self.state.bot_username or "").strip()
        if not manager_username:
            self._send_text(
                chat_id,
                "Manager bot username unknown — can't build deep link. Try again in a moment.",
                reply_to_message_id=reply_to_message_id,
            )
            return

        # ORDER MATTERS: persist pending + register Event BEFORE clone
        # thread spawn and BEFORE sending the keyboard, so a fast tap
        # can never race ahead of the JSON write.
        _mount.add_pending(self.project_root, suggested, chat_id, str(target), url)
        done_event = Event()
        self._clone_events[suggested] = done_event

        Thread(
            target=self._clone_repo_bg,
            args=(url, target, suggested, done_event),
            daemon=True,
            name=f"ccb-mount-clone-{suggested}",
        ).start()

        button_url = (
            f"https://t.me/newbot/{manager_username}/{suggested}"
            f"?name={owner}/{repo}"
        )
        # Telegram's Markdown can't render `@` plus underscore-heavy
        # bot names cleanly inside a link, so the body stays terse and
        # the URL lives in the inline button.
        self.client.send_message_with_url_button(
            chat_id,
            f"📦 Cloning `{owner}/{repo}` → `{target}`.\n"
            f"Tap below to create `@{suggested}`. Once it's live, "
            "I'll wire it up automatically.",
            button_text=f"Create @{suggested}",
            button_url=button_url,
            reply_to_message_id=reply_to_message_id,
        )

    def _clone_repo_bg(
        self,
        url: str,
        target: Path,
        suggested: str,
        done_event: Event,
    ) -> None:
        """Run `git clone` (or fetch) off the polling thread. Always
        sets `done_event` so `_handle_managed_bot` unblocks even on
        clone failure — error reporting happens at handoff time."""
        try:
            ok, msg = _mount.clone_repo(url, target)
            _write_log(
                f"[telegramd] mount clone {suggested}: {'ok' if ok else 'FAIL'} {msg}",
                self.project_root,
            )
        finally:
            done_event.set()

    def _handle_managed_bot(self, mb: dict) -> None:
        bot = mb.get("bot") or {}
        bot_id = int(bot.get("id") or 0)
        bot_username = (bot.get("username") or "").lower()
        if not (bot_id and bot_username):
            _write_log(
                f"[telegramd] managed_bot missing bot.id or bot.username: {mb!r}",
                self.project_root,
            )
            return

        pending = _mount.pop_pending(self.project_root, bot_username)
        if not pending:
            # Could be a bot we already processed, or the user picked a
            # different username than we suggested. Log + bail.
            _write_log(
                f"[telegramd] managed_bot for unknown @{bot_username} (id={bot_id}); ignoring",
                self.project_root,
            )
            return

        chat_id = pending.get("chat_id", "")
        target = pending.get("target", "")

        ev = self._clone_events.get(bot_username)
        if ev is not None and not ev.wait(timeout=60):
            self._send_text(
                chat_id,
                f"⏳ Clone for @{bot_username} still running after 60s — "
                "will keep going in the background. Re-tap the button "
                "to retry once it's done.",
            )
            # Re-add so the next tap finds the entry.
            _mount.add_pending(
                self.project_root, bot_username, chat_id, target, pending.get("url", "")
            )
            return

        try:
            token = self.client.get_managed_bot_token(user_id=bot_id)
        except TelegramApiError as exc:
            # Log bot.id explicitly so a future /mount-recover can use it
            # without scraping the Telegram side.
            _write_log(
                f"[telegramd] getManagedBotToken failed for @{bot_username} "
                f"(bot_id={bot_id}, chat={chat_id}): {exc}",
                self.project_root,
            )
            self._send_text(
                chat_id,
                f"❌ Token fetch for @{bot_username} failed: {exc}\n"
                f"Bot exists on Telegram (id={bot_id}); manual recovery in Phase 2.",
            )
            return

        if not token:
            self._send_text(
                chat_id,
                f"❌ getManagedBotToken returned empty for @{bot_username} (id={bot_id}).",
            )
            return

        try:
            rc = subprocess.run(
                ["ccb", "mount", target, "--bot-token", token, "--chat-id", str(chat_id)],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as exc:
            self._send_text(chat_id, f"❌ `ccb mount` spawn failed: {exc}")
            return

        if rc.returncode != 0:
            err = (rc.stderr or rc.stdout or "").strip() or f"rc={rc.returncode}"
            self._send_text(chat_id, f"❌ `ccb mount` failed:\n```\n{err[:1500]}\n```")
            return

        self._send_text(
            chat_id,
            f"✅ @{bot_username} live at `{target}`.\n"
            f"DM it to start working in that project.",
        )

    def _watch_for_late_completion(
        self,
        provider: str,
        chat_id: str,
        reply_to_message_id: int,
        log_path: Path,
        start_offset: int,
        warning_msg_id: int,
        timeout_at: float,
    ) -> None:
        """Tail the pane log for a late CCB_DONE marker.

        Background thread spawned by `_run_request` when ask returned
        without seeing the protocol completion marker. Polls every 15s,
        gives up after `timeout_at`. On success: extracts the reply
        between the matching CCB_BEGIN/CCB_DONE pair, posts as final,
        deletes the warning.
        """
        try:
            from laskd_protocol import extract_reply_for_req
        except Exception:
            extract_reply_for_req = None  # type: ignore
        seen_offset = start_offset
        while time.time() < timeout_at and not self.stop_event.is_set():
            try:
                new_text = _read_pane_log_from_offset(log_path, seen_offset)
                if new_text:
                    matches = _CCB_DONE_LINE_RE.findall(new_text)
                    if matches:
                        req_id = matches[-1]
                        reply = ""
                        if extract_reply_for_req:
                            try:
                                reply = extract_reply_for_req(new_text, req_id).strip()
                            except Exception:
                                reply = ""
                        if not reply:
                            # Fallback — best-effort split.
                            done_re = re.compile(
                                rf"CCB_BEGIN:\s*{re.escape(req_id)}\s*\n(.*?)\nCCB_DONE:\s*{re.escape(req_id)}",
                                re.DOTALL | re.IGNORECASE,
                            )
                            m = done_re.search(new_text)
                            reply = m.group(1).strip() if m else ""
                        if reply:
                            try:
                                if warning_msg_id:
                                    self.client.delete_message(chat_id, warning_msg_id)
                            except Exception:
                                pass
                            self._send_text(
                                chat_id,
                                f"[{provider.capitalize()}]\n{reply}",
                                reply_to_message_id=reply_to_message_id,
                            )
                            return
            except Exception as exc:
                _write_log(
                    f"[telegramd] late-completion watcher error chat={chat_id}: {exc}",
                    self.project_root,
                )
            time.sleep(15.0)
        # Timed out without seeing CCB_DONE — give up quietly. User can /tail
        # to see what's happening. Warning message stays in chat as the
        # last-known status.

    def _lookup_pane_id_for_provider(self, provider: str) -> str:
        """Return the tmux pane id for `provider` in this project, or ''.

        Shared lookup used by `/tail` and the Hermes live tool-chain. Reads the
        provider's session file from project config dir; ignores wezterm panes
        (capture-pane is tmux-only).
        """
        try:
            session_filename = SESSION_FILES.get(provider)
            if not session_filename:
                return ""
            session_file = find_project_session_file(self.project_root, session_filename)
            if not session_file or not session_file.exists():
                return ""
            data = json.loads(session_file.read_text(encoding="utf-8-sig"))
        except Exception:
            return ""
        if (data.get("terminal") or "").strip().lower() != "tmux":
            return ""
        pane_id = str(data.get("pane_id") or "").strip()
        return pane_id if pane_id.startswith("%") else ""

    def _run_tail_command(
        self,
        provider: str,
        chat_id: str,
        reply_to_message_id: int,
    ) -> None:
        """Snapshot the provider's tmux pane and post the tail back.

        Read-only — doesn't type anything into the pane. Useful for
        checking on a long-running turn remotely without interrupting it.
        """
        provider = (provider or "claude").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            self._send_text(chat_id, f"Unknown provider: {provider}",
                            reply_to_message_id=reply_to_message_id)
            return
        session_filename = SESSION_FILES.get(provider)
        if not session_filename:
            self._send_text(chat_id, f"No session-file mapping for provider {provider}.",
                            reply_to_message_id=reply_to_message_id)
            return
        session_file = find_project_session_file(self.project_root, session_filename)
        if not session_file or not session_file.exists():
            self._send_text(chat_id, f"{provider} is not mounted for this project.",
                            reply_to_message_id=reply_to_message_id)
            return

        try:
            data = json.loads(session_file.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            self._send_text(chat_id, f"Couldn't read {provider} session: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        terminal = (data.get("terminal") or "tmux").strip().lower()
        pane_id = str(data.get("pane_id") or data.get("tmux_session") or "").strip()
        if not pane_id:
            self._send_text(chat_id, f"{provider} session has no pane_id.",
                            reply_to_message_id=reply_to_message_id)
            return
        if terminal != "tmux":
            self._send_text(
                chat_id,
                f"/tail is tmux-only for now (this pane is {terminal}).",
                reply_to_message_id=reply_to_message_id,
            )
            return

        try:
            cap = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", pane_id, "-S", "-80"],
                check=True, capture_output=True, text=True,
            )
            tail = (cap.stdout or "").rstrip()
        except Exception as exc:
            self._send_text(chat_id, f"tmux capture-pane failed: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        # Strip ANSI / OSC / Ptmux escape noise. `capture-pane -p` usually
        # gives clean text but some pipe-pane configs leak set-title
        # sequences (`\x1b]0;…\x07`) that pollute the output.
        tail = _PTMUX_RE.sub("", _ANSI_RE.sub("", tail))

        # Collapse trailing blank lines; Telegram markdown hates huge blobs,
        # so cap at ~3500 chars (covers both message limit + code-fence
        # overhead). Preserve the MOST RECENT content by trimming head.
        lines = [ln.rstrip() for ln in tail.split("\n")]
        while lines and not lines[-1].strip():
            lines.pop()
        body = "\n".join(lines)
        if len(body) > 3500:
            body = "…(older output truncated)\n" + body[-3400:]

        if not body:
            body = "(pane is empty)"

        self._send_text(
            chat_id,
            f"[{provider.capitalize()} tail]\n```\n{body}\n```",
            reply_to_message_id=reply_to_message_id,
        )

    def _run_slash_passthrough(
        self,
        command: str,
        provider: str,
        chat_id: str,
        reply_to_message_id: int,
        arg: str = "",
    ) -> None:
        """Type a provider-native slash command (`/context`, `/compact`) into
        the pane and return the output tail.

        Bypasses the ask/CCB_DONE protocol because these commands don't
        follow our reply convention — they produce UI output inside the
        provider's CLI. We snapshot the pane before sending, wait a few
        seconds, capture the post-send tail, and post the diff.
        """
        provider = (provider or "claude").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            self._send_text(chat_id, f"Unknown provider: {provider}",
                            reply_to_message_id=reply_to_message_id)
            return

        session_filename = SESSION_FILES.get(provider)
        if not session_filename:
            self._send_text(chat_id, f"No session-file mapping for provider {provider}.",
                            reply_to_message_id=reply_to_message_id)
            return
        session_file = find_project_session_file(self.project_root, session_filename)
        if not session_file or not session_file.exists():
            self._send_text(chat_id, f"{provider} is not mounted for this project.",
                            reply_to_message_id=reply_to_message_id)
            return

        try:
            data = json.loads(session_file.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            self._send_text(chat_id, f"Couldn't read {provider} session: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        terminal = (data.get("terminal") or "tmux").strip().lower()
        pane_id = str(data.get("pane_id") or data.get("tmux_session") or "").strip()
        if not pane_id:
            self._send_text(chat_id, f"{provider} session has no pane_id.",
                            reply_to_message_id=reply_to_message_id)
            return

        if terminal != "tmux":
            self._send_text(
                chat_id,
                f"/{command} passthrough is tmux-only for now "
                f"(this pane is {terminal}).",
                reply_to_message_id=reply_to_message_id,
            )
            return

        # /compact can take a while (Claude summarizes the whole session);
        # /context is instant. Tune the wait accordingly so we catch the
        # output without blocking the telegramd worker for too long.
        wait_s = 15 if command == "compact" else 4

        try:
            # Send the slash command + Enter into the Claude pane.
            full_cmd = f"/{command} {arg}".strip() if arg else f"/{command}"
            subprocess.run(["tmux", "send-keys", "-t", pane_id, full_cmd],
                           check=True, capture_output=True)
            # Small delay so the CLI registers the line before we press Enter.
            time.sleep(0.2)
            subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"],
                           check=True, capture_output=True)
        except Exception as exc:
            self._send_text(chat_id, f"tmux send-keys failed: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        time.sleep(wait_s)

        try:
            cap = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", pane_id, "-S", "-80"],
                check=True, capture_output=True, text=True,
            )
            tail = (cap.stdout or "").rstrip()
        except Exception as exc:
            self._send_text(chat_id, f"tmux capture-pane failed: {exc}",
                            reply_to_message_id=reply_to_message_id)
            return

        # Dismiss modal dialogs that Claude Code's /status, /context, /compact
        # pop into the pane. They block the prompt until the user presses
        # Escape. Without this the next ask() would send its text INTO the
        # modal instead of the prompt field. Send Escape a couple times to
        # be safe (covers multi-level nested modals).
        try:
            for _ in range(2):
                subprocess.run(["tmux", "send-keys", "-t", pane_id, "Escape"],
                               check=False, capture_output=True)
                time.sleep(0.1)
        except Exception:
            pass

        # Trim leading box-drawing / UI chrome noise: try to find the last
        # occurrence of `/command` we just typed and keep everything below.
        marker = f"/{command}"
        idx = tail.rfind(marker)
        body = tail[idx:].strip() if idx >= 0 else tail
        if len(body) > 3500:
            body = body[:3500] + "\n…(truncated)"

        self._send_text(
            chat_id,
            f"[{provider.capitalize()} /{command}]\n```\n{body}\n```",
            reply_to_message_id=reply_to_message_id,
        )


def start_daemon(foreground: bool = False, work_dir: str | Path | None = None) -> None:
    project_root = get_project_root(work_dir)
    config = load_config(project_root)
    if not config.enabled:
        print("Telegram bridge is disabled. Configure it and enable it first.")
        sys.exit(1)
    if not is_configured(config, project_root):
        print("Telegram bridge is not configured. Set a bot token first.")
        sys.exit(1)
    if is_daemon_running(project_root):
        print("Telegram daemon is already running.")
        return

    daemon = TelegramDaemon(config, project_root)
    if foreground:
        daemon.start()
        return

    code_root = Path(__file__).resolve().parents[2]
    log_path = _log_path(project_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    py_path = [str(code_root), str(code_root / "lib")]
    if env.get("PYTHONPATH"):
        py_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(py_path)
    env["CCB_TELEGRAM_PROJECT_ROOT"] = str(project_root)

    # Avoid raw fork() on macOS. Objective-C / SystemConfiguration work inside the
    # child (for example proxy lookup during urllib calls) can abort with
    # "crashed on child side of fork pre-exec". Launch a detached subprocess instead.
    if os.name == "nt" or sys.platform == "darwin":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        with log_path.open("a", buffering=1) as log_file:
            kwargs = {
                "args": [sys.executable, "-m", "telegram_bridge.daemon", "run"],
                "stdin": subprocess.DEVNULL,
                "stdout": log_file,
                "stderr": log_file,
                "cwd": str(code_root),
                "close_fds": True,
                "env": env,
            }
            if os.name == "nt":
                kwargs["creationflags"] = creationflags
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(**kwargs)
        print(f"[telegramd] Started in background (PID: {proc.pid})")
        return

    pid = os.fork()
    if pid > 0:
        print(f"[telegramd] Started in background (PID: {pid})")
        return
    os.setsid()
    os.umask(0)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)
    sys.stdout = os.fdopen(1, "w", buffering=1)
    sys.stderr = sys.stdout
    daemon.start()


def stop_daemon(work_dir: str | Path | None = None) -> bool:
    state = read_daemon_state(work_dir)
    if not state:
        print("Telegram daemon is not running")
        return False
    if not _is_process_alive(state.pid):
        print("Telegram daemon is not running")
        remove_daemon_state(work_dir)
        return False
    try:
        os.kill(state.pid, signal.SIGTERM)
        for _ in range(20):
            if not _is_process_alive(state.pid):
                remove_daemon_state(work_dir)
                print("Telegram daemon stopped")
                return True
            time.sleep(0.25)
        print("Warning: Telegram daemon did not stop gracefully")
        return False
    except Exception:
        remove_daemon_state(work_dir)
        return False


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    project_root = os.environ.get("CCB_TELEGRAM_PROJECT_ROOT") or os.getcwd()
    cmd = argv[0] if argv else "run"
    if cmd == "run":
        TelegramDaemon(load_config(project_root), project_root).start()
        return 0
    if cmd == "start":
        start_daemon(foreground="--foreground" in argv[1:], work_dir=project_root)
        return 0
    if cmd == "stop":
        return 0 if stop_daemon(project_root) else 1
    if cmd == "status":
        print(json.dumps(get_daemon_status(project_root), ensure_ascii=False, indent=2))
        return 0
    print("Usage: python -m telegram_bridge.daemon [run|start|stop|status]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
