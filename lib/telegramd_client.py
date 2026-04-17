from __future__ import annotations

from pathlib import Path

from telegram_bridge.bot_api import TelegramApiError, TelegramBotClient
from telegram_bridge.config import TelegramConfig, is_configured, load_config, save_config
from telegram_bridge.daemon import get_daemon_status, is_daemon_running, start_daemon, stop_daemon


def get_telegram_status(work_dir: str | Path | None = None) -> dict:
    config = load_config(work_dir)
    daemon = get_daemon_status(work_dir)
    return {
        "configured": is_configured(config, work_dir),
        "enabled": config.enabled,
        "default_provider": config.default_provider,
        "default_work_dir": config.default_work_dir,
        "allowed_chat_ids": list(config.allowed_chat_ids),
        "bot_token_set": bool(config.bot_token),
        "daemon": daemon,
    }


def update_telegram_config(
    *,
    work_dir: str | Path | None = None,
    token: str | None = None,
    chat_ids: list[str] | None = None,
    clear_chat_ids: bool = False,
    default_provider: str | None = None,
    default_work_dir: str | None = None,
    polling_interval_seconds: int | None = None,
    long_poll_timeout_seconds: int | None = None,
    request_timeout_seconds: int | None = None,
    send_acknowledgements: bool | None = None,
    enabled: bool | None = None,
    broadcast_providers: list[str] | None = None,
) -> TelegramConfig:
    config = load_config(work_dir)
    if token is not None:
        config.bot_token = token.strip()
    if clear_chat_ids:
        config.allowed_chat_ids = []
    if chat_ids:
        config.allowed_chat_ids = [str(x).strip() for x in chat_ids if str(x).strip()]
    if default_provider is not None:
        config.default_provider = default_provider.strip().lower() or config.default_provider
    if default_work_dir is not None:
        config.default_work_dir = default_work_dir.strip()
    if polling_interval_seconds is not None:
        config.polling_interval_seconds = max(1, int(polling_interval_seconds))
    if long_poll_timeout_seconds is not None:
        config.long_poll_timeout_seconds = max(1, int(long_poll_timeout_seconds))
    if request_timeout_seconds is not None:
        config.request_timeout_seconds = max(1, int(request_timeout_seconds))
    if send_acknowledgements is not None:
        config.send_acknowledgements = bool(send_acknowledgements)
    if enabled is not None:
        config.enabled = bool(enabled)
    if broadcast_providers is not None:
        config.broadcast_providers = [str(x).strip().lower() for x in broadcast_providers if str(x).strip()]
    save_config(config, work_dir)
    return config


def start_telegram_service(foreground: bool = False, work_dir: str | Path | None = None) -> bool:
    config = load_config(work_dir)
    if not is_configured(config, work_dir):
        print("Telegram bridge not configured. Run `ccb telegram config --token ...` first.")
        return False
    if not config.enabled:
        config.enabled = True
        save_config(config, work_dir)
    start_daemon(foreground=foreground, work_dir=work_dir)
    return True


def stop_telegram_service(work_dir: str | Path | None = None) -> bool:
    return stop_daemon(work_dir)


def test_telegram_connection(work_dir: str | Path | None = None) -> dict:
    config = load_config(work_dir)
    if not is_configured(config, work_dir):
        return {"success": False, "error": "Telegram bridge not configured"}
    try:
        client = TelegramBotClient(config.bot_token)
        me = client.get_me()
        return {
            "success": True,
            "username": me.get("username") if isinstance(me, dict) else "",
            "id": me.get("id") if isinstance(me, dict) else None,
        }
    except TelegramApiError as exc:
        return {"success": False, "error": str(exc)}


def send_telegram_message(
    text: str,
    *,
    work_dir: str | Path | None = None,
    chat_ids: list[str] | None = None,
    http_timeout: float = 65.0,
) -> dict:
    config = load_config(work_dir)
    if not config.enabled:
        return {"success": False, "error": "Telegram bridge disabled"}
    if not is_configured(config, work_dir):
        return {"success": False, "error": "Telegram bridge not configured"}

    targets = [str(x).strip() for x in (chat_ids or config.allowed_chat_ids) if str(x).strip()]
    if not targets:
        return {"success": False, "error": "No allowed chat IDs configured"}

    try:
        client = TelegramBotClient(config.bot_token, http_timeout=http_timeout)
        delivered = 0
        for chat_id in targets:
            client.send_message(chat_id, text)
            delivered += 1
        return {"success": True, "delivered": delivered}
    except TelegramApiError as exc:
        return {"success": False, "error": str(exc)}
