from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from ccb_start_config import load_start_config
from session_utils import project_config_dir

CONFIG_FILE = "ccb.config"
CURRENT_CONFIG_VERSION = 1

SUPPORTED_PROVIDERS = [
    "claude",
    "codex",
    "gemini",
    "opencode",
    "droid",
    "copilot",
    "codebuddy",
    "qwen",
]


@dataclass
class TelegramConfig:
    version: int = CURRENT_CONFIG_VERSION
    enabled: bool = False
    bot_token: str = ""
    allowed_chat_ids: list[str] = field(default_factory=list)
    default_provider: str = "claude"
    default_work_dir: str = ""
    polling_interval_seconds: int = 2
    long_poll_timeout_seconds: int = 30
    request_timeout_seconds: int = 3600
    send_acknowledgements: bool = True
    broadcast_providers: list[str] = field(
        default_factory=lambda: ["claude", "codex", "gemini", "opencode", "droid"]
    )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TelegramConfig":
        raw_chats = data.get("allowed_chat_ids", [])
        raw_broadcast = data.get("broadcast_providers", [])
        return cls(
            version=int(data.get("version", CURRENT_CONFIG_VERSION) or CURRENT_CONFIG_VERSION),
            enabled=bool(data.get("enabled", False)),
            bot_token=str(data.get("bot_token", "") or "").strip(),
            allowed_chat_ids=[str(x).strip() for x in raw_chats if str(x).strip()],
            default_provider=_normalize_provider(str(data.get("default_provider", "claude") or "claude")),
            default_work_dir=str(data.get("default_work_dir", "") or "").strip(),
            polling_interval_seconds=max(1, int(data.get("polling_interval_seconds", 2) or 2)),
            long_poll_timeout_seconds=max(1, int(data.get("long_poll_timeout_seconds", 30) or 30)),
            request_timeout_seconds=max(1, int(data.get("request_timeout_seconds", 3600) or 3600)),
            send_acknowledgements=bool(data.get("send_acknowledgements", True)),
            broadcast_providers=_normalize_provider_list(raw_broadcast) or ["claude", "codex", "gemini", "opencode", "droid"],
        )


def _normalize_provider(value: str) -> str:
    provider = (value or "").strip().lower()
    if provider in SUPPORTED_PROVIDERS:
        return provider
    return "claude"


def _normalize_provider_list(values: list | tuple | set | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        provider = (str(raw or "")).strip().lower()
        if provider not in SUPPORTED_PROVIDERS or provider in seen:
            continue
        seen.add(provider)
        out.append(provider)
    return out


def get_project_root(work_dir: str | Path | None = None) -> Path:
    if work_dir is None:
        return Path.cwd()
    return Path(work_dir).expanduser()


def get_config_dir(work_dir: str | Path | None = None) -> Path:
    return project_config_dir(get_project_root(work_dir))


def get_config_path(work_dir: str | Path | None = None) -> Path:
    return get_config_dir(work_dir) / CONFIG_FILE


def load_config(work_dir: str | Path | None = None) -> TelegramConfig:
    start = load_start_config(get_project_root(work_dir))
    data = start.data if isinstance(start.data, dict) else {}
    telegram = data.get("telegram")
    if not isinstance(telegram, dict):
        return TelegramConfig()
    return TelegramConfig.from_dict(telegram)


def save_config(config: TelegramConfig, work_dir: str | Path | None = None) -> Path:
    root = get_project_root(work_dir)
    start = load_start_config(root)
    data = dict(start.data) if isinstance(start.data, dict) else {}
    data["telegram"] = config.to_dict()

    path = get_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    providers = data.get("providers")
    if isinstance(providers, tuple):
        data["providers"] = list(providers)
    import json

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def is_configured(config: TelegramConfig | None = None, work_dir: str | Path | None = None) -> bool:
    cfg = config or load_config(work_dir)
    return bool(cfg.bot_token.strip())
