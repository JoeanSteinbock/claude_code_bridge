from __future__ import annotations

from dataclasses import dataclass

from ccb_protocol import (
    DONE_PREFIX,
    REQ_ID_PREFIX,
    channel_reply_instruction,
    is_done_text,
    make_req_id,
    reply_language_instruction,
    strip_done_text,
)


def wrap_opencode_prompt(message: str, req_id: str, caller: str = "") -> str:
    message = (message or "").rstrip()
    channel = channel_reply_instruction(caller)
    channel_line = f"- {channel}\n" if channel else ""
    return (
        f"{REQ_ID_PREFIX} {req_id}\n\n"
        f"{message}\n\n"
        "IMPORTANT:\n"
        f"- {reply_language_instruction(message)}\n"
        f"{channel_line}"
        "- End your reply with this exact final line (verbatim, on its own line):\n"
        f"{DONE_PREFIX} {req_id}\n"
    )


@dataclass(frozen=True)
class OaskdRequest:
    client_id: str
    work_dir: str
    timeout_s: float
    quiet: bool
    message: str
    output_path: str | None = None
    req_id: str | None = None
    caller: str = "claude"


@dataclass(frozen=True)
class OaskdResult:
    exit_code: int
    reply: str
    req_id: str
    session_key: str
    done_seen: bool
    done_ms: int | None = None
