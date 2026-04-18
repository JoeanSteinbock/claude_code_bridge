"""
Wake queue — per-project persistent schedule of future agent turns.

Entries are JSON objects stored in `.ccb/wake_queue.json`:
    {
      "wake_id": "20260418-153012-001-12345-1",
      "agent":   "claude",
      "message": "check positions",
      "fire_at": 1776500000.0,   # unix seconds
      "created_at": 1776499700.0,
      "caller":  "telegram",
      "chat_id": "7815518351"
    }

Readers/writers coordinate via atomic rewrite of the whole file. Races
between concurrent enqueues are possible but rare in practice; if they
become a problem, upgrade to an fcntl advisory lock.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def queue_path(project_root: str | Path) -> Path:
    return Path(project_root) / ".ccb" / "wake_queue.json"


def _atomic_write(path: Path, queue: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load(project_root: str | Path) -> list[dict]:
    p = queue_path(project_root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save(project_root: str | Path, queue: list[dict]) -> None:
    _atomic_write(queue_path(project_root), list(queue))


def enqueue(project_root: str | Path, entry: dict) -> None:
    q = load(project_root)
    q.append(dict(entry))
    save(project_root, q)


def cancel(project_root: str | Path, wake_id: str) -> bool:
    q = load(project_root)
    new_q = [e for e in q if str(e.get("wake_id")) != str(wake_id)]
    if len(new_q) == len(q):
        return False
    save(project_root, new_q)
    return True


def pop_due(project_root: str | Path, now_unix: float) -> list[dict]:
    """Return all entries whose fire_at <= now_unix, removing them from the
    on-disk queue. Remaining (future) entries are re-saved in place."""
    q = load(project_root)
    if not q:
        return []
    due = [e for e in q if float(e.get("fire_at") or 0) <= now_unix]
    if not due:
        return []
    remaining = [e for e in q if float(e.get("fire_at") or 0) > now_unix]
    save(project_root, remaining)
    return due
