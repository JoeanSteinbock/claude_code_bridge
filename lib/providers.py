from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProviderDaemonSpec:
    daemon_key: str
    protocol_prefix: str
    state_file_name: str
    log_file_name: str
    idle_timeout_env: str
    lock_name: str


@dataclass
class ProviderClientSpec:
    protocol_prefix: str
    enabled_env: str
    autostart_env_primary: str
    autostart_env_legacy: str
    state_file_env: str
    session_filename: str
    daemon_bin_name: str
    daemon_module: str


CASKD_SPEC = ProviderDaemonSpec(
    daemon_key="caskd",
    protocol_prefix="cask",
    state_file_name="caskd.json",
    log_file_name="caskd.log",
    idle_timeout_env="CCB_CASKD_IDLE_TIMEOUT_S",
    lock_name="caskd",
)


GASKD_SPEC = ProviderDaemonSpec(
    daemon_key="gaskd",
    protocol_prefix="gask",
    state_file_name="gaskd.json",
    log_file_name="gaskd.log",
    idle_timeout_env="CCB_GASKD_IDLE_TIMEOUT_S",
    lock_name="gaskd",
)


OASKD_SPEC = ProviderDaemonSpec(
    daemon_key="oaskd",
    protocol_prefix="oask",
    state_file_name="oaskd.json",
    log_file_name="oaskd.log",
    idle_timeout_env="CCB_OASKD_IDLE_TIMEOUT_S",
    lock_name="oaskd",
)


LASKD_SPEC = ProviderDaemonSpec(
    daemon_key="laskd",
    protocol_prefix="lask",
    state_file_name="laskd.json",
    log_file_name="laskd.log",
    idle_timeout_env="CCB_LASKD_IDLE_TIMEOUT_S",
    lock_name="laskd",
)


DASKD_SPEC = ProviderDaemonSpec(
    daemon_key="daskd",
    protocol_prefix="dask",
    state_file_name="daskd.json",
    log_file_name="daskd.log",
    idle_timeout_env="CCB_DASKD_IDLE_TIMEOUT_S",
    lock_name="daskd",
)


CASK_CLIENT_SPEC = ProviderClientSpec(
    protocol_prefix="cask",
    enabled_env="CCB_CASKD",
    autostart_env_primary="CCB_CASKD_AUTOSTART",
    autostart_env_legacy="CCB_AUTO_CASKD",
    state_file_env="CCB_CASKD_STATE_FILE",
    session_filename=".codex-session",
    daemon_bin_name="askd",
    daemon_module="askd.daemon",
)


GASK_CLIENT_SPEC = ProviderClientSpec(
    protocol_prefix="gask",
    enabled_env="CCB_GASKD",
    autostart_env_primary="CCB_GASKD_AUTOSTART",
    autostart_env_legacy="CCB_AUTO_GASKD",
    state_file_env="CCB_GASKD_STATE_FILE",
    session_filename=".gemini-session",
    daemon_bin_name="askd",
    daemon_module="askd.daemon",
)


OASK_CLIENT_SPEC = ProviderClientSpec(
    protocol_prefix="oask",
    enabled_env="CCB_OASKD",
    autostart_env_primary="CCB_OASKD_AUTOSTART",
    autostart_env_legacy="CCB_AUTO_OASKD",
    state_file_env="CCB_OASKD_STATE_FILE",
    session_filename=".opencode-session",
    daemon_bin_name="askd",
    daemon_module="askd.daemon",
)


LASK_CLIENT_SPEC = ProviderClientSpec(
    protocol_prefix="lask",
    enabled_env="CCB_LASKD",
    autostart_env_primary="CCB_LASKD_AUTOSTART",
    autostart_env_legacy="CCB_AUTO_LASKD",
    state_file_env="CCB_LASKD_STATE_FILE",
    session_filename=".claude-session",
    daemon_bin_name="askd",
    daemon_module="askd.daemon",
)


DASK_CLIENT_SPEC = ProviderClientSpec(
    protocol_prefix="dask",
    enabled_env="CCB_DASKD",
    autostart_env_primary="CCB_DASKD_AUTOSTART",
    autostart_env_legacy="CCB_AUTO_DASKD",
    state_file_env="CCB_DASKD_STATE_FILE",
    session_filename=".droid-session",
    daemon_bin_name="askd",
    daemon_module="askd.daemon",
)


# Copilot (GitHub Copilot CLI)
HASKD_SPEC = ProviderDaemonSpec(
    daemon_key="haskd",
    protocol_prefix="hask",
    state_file_name="haskd.json",
    log_file_name="haskd.log",
    idle_timeout_env="CCB_HASKD_IDLE_TIMEOUT_S",
    lock_name="haskd",
)


HASK_CLIENT_SPEC = ProviderClientSpec(
    protocol_prefix="hask",
    enabled_env="CCB_HASKD",
    autostart_env_primary="CCB_HASKD_AUTOSTART",
    autostart_env_legacy="CCB_AUTO_HASKD",
    state_file_env="CCB_HASKD_STATE_FILE",
    session_filename=".copilot-session",
    daemon_bin_name="askd",
    daemon_module="askd.daemon",
)


# CodeBuddy (Tencent CodeBuddy CLI)
BASKD_SPEC = ProviderDaemonSpec(
    daemon_key="baskd",
    protocol_prefix="bask",
    state_file_name="baskd.json",
    log_file_name="baskd.log",
    idle_timeout_env="CCB_BASKD_IDLE_TIMEOUT_S",
    lock_name="baskd",
)


BASK_CLIENT_SPEC = ProviderClientSpec(
    protocol_prefix="bask",
    enabled_env="CCB_BASKD",
    autostart_env_primary="CCB_BASKD_AUTOSTART",
    autostart_env_legacy="CCB_AUTO_BASKD",
    state_file_env="CCB_BASKD_STATE_FILE",
    session_filename=".codebuddy-session",
    daemon_bin_name="askd",
    daemon_module="askd.daemon",
)


# ── Qwen (qwen-code CLI) ──────────────────────────────────────────────────────
QASKD_SPEC = ProviderDaemonSpec(
    daemon_key="qaskd",
    protocol_prefix="qask",
    state_file_name="qaskd.json",
    log_file_name="qaskd.log",
    idle_timeout_env="CCB_QASKD_IDLE_TIMEOUT_S",
    lock_name="qaskd",
)

QASK_CLIENT_SPEC = ProviderClientSpec(
    protocol_prefix="qask",
    enabled_env="CCB_QASKD",
    autostart_env_primary="CCB_QASKD_AUTOSTART",
    autostart_env_legacy="CCB_AUTO_QASKD",
    state_file_env="CCB_QASKD_STATE_FILE",
    session_filename=".qwen-session",
    daemon_bin_name="askd",
    daemon_module="askd.daemon",
)


# ── Grok (xAI Grok Build CLI) ────────────────────────────────────────────────
RASKD_SPEC = ProviderDaemonSpec(
    daemon_key="raskd",
    protocol_prefix="rask",
    state_file_name="raskd.json",
    log_file_name="raskd.log",
    idle_timeout_env="CCB_RASKD_IDLE_TIMEOUT_S",
    lock_name="raskd",
)

RASK_CLIENT_SPEC = ProviderClientSpec(
    protocol_prefix="rask",
    enabled_env="CCB_RASKD",
    autostart_env_primary="CCB_RASKD_AUTOSTART",
    autostart_env_legacy="CCB_AUTO_RASKD",
    state_file_env="CCB_RASKD_STATE_FILE",
    session_filename=".grok-session",
    daemon_bin_name="askd",
    daemon_module="askd.daemon",
)


# ── Multi-instance provider utilities ────────────────────────────────────────

KNOWN_PROVIDERS = (
    "codex",
    "gemini",
    "opencode",
    "claude",
    "droid",
    "copilot",
    "codebuddy",
    "qwen",
    "grok",
)

PROVIDER_DISPLAY_NAMES = {
    "codex": "Codex",
    "gemini": "Gemini",
    "opencode": "OpenCode",
    "claude": "Claude",
    "droid": "Droid",
    "copilot": "Copilot",
    "codebuddy": "CodeBuddy",
    "qwen": "Qwen",
    "grok": "Grok",
}

SESSION_BASE_FILENAMES = {
    "codex": ".codex-session",
    "gemini": ".gemini-session",
    "opencode": ".opencode-session",
    "claude": ".claude-session",
    "droid": ".droid-session",
    "copilot": ".copilot-session",
    "codebuddy": ".codebuddy-session",
    "qwen": ".qwen-session",
    "grok": ".grok-session",
}


def _canonical_base_provider(name: str) -> str:
    low = (name or "").strip().lower()
    if low in ("agy", "antigravity"):
        return "gemini"
    return low


def parse_qualified_provider(key: str) -> tuple[str, str | None]:
    """Parse 'codex:auth' -> ('codex', 'auth'); 'codex' -> ('codex', None)."""
    key = (key or "").strip().lower()
    if not key:
        return ("", None)
    # Support 'alias=provider' shorthand -> ('provider', 'alias')
    if "=" in key and ":" not in key:
        lhs, rhs = key.split("=", 1)
        lhs = lhs.strip()
        rhs = _canonical_base_provider(rhs)
        if lhs and rhs in KNOWN_PROVIDERS:
            return (rhs, lhs)
    # Only split on first colon to separate provider from instance
    parts = key.split(":", 1)
    base = _canonical_base_provider(parts[0])
    instance = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return (base, instance)


def make_qualified_key(base: str, instance: str | None) -> str:
    """Combine base provider and instance: 'codex' + 'auth' -> 'codex:auth'."""
    base = _canonical_base_provider(base)
    if instance and instance.strip():
        return f"{base}:{instance.strip().lower()}"
    return base


def normalize_provider_token(raw: str, aliases: dict[str, str] | None = None) -> str:
    """Normalize a provider token or alias into canonical 'base' or 'base:instance'.

    Supports:
    - 'opencode:oc1' -> 'opencode:oc1'
    - 'oc1=opencode' -> 'opencode:oc1'
    - 'agy:oc1'      -> 'gemini:oc1'
    - 'oc1' with aliases={'oc1': 'opencode'} -> 'opencode:oc1'
    """
    token = (raw or "").strip().lower()
    if not token or token == "cmd":
        return token

    if aliases and isinstance(aliases, dict):
        mapped = aliases.get(token) or aliases.get(raw.strip())
        if isinstance(mapped, str) and mapped.strip():
            m_base, m_inst = parse_qualified_provider(mapped)
            if m_base:
                return make_qualified_key(m_base, m_inst or token)

    base, instance = parse_qualified_provider(token)
    if not base:
        return ""
    return make_qualified_key(base, instance)


def display_provider_name(provider: str) -> str:
    """Human-readable display name: 'opencode:oc1' -> 'OpenCode:oc1'."""
    base, instance = parse_qualified_provider(provider)
    base_display = PROVIDER_DISPLAY_NAMES.get(base, base.capitalize() if base else "")
    if instance:
        return f"{base_display}:{instance}"
    return base_display


def session_filename_for_instance(base_filename: str, instance: str | None) -> str:
    """Derive instance-specific session filename.

    '.codex-session' + 'auth' -> '.codex-auth-session'
    '.codex-session' + None  -> '.codex-session'
    """
    if not instance:
        return base_filename
    instance = instance.strip()
    if not instance:
        return base_filename
    # Insert instance before '-session' suffix
    if base_filename.endswith("-session"):
        prefix = base_filename[:-len("-session")]
        return f"{prefix}-{instance}-session"
    # Fallback: append instance before extension
    return f"{base_filename}-{instance}"


def session_filename_for_provider(provider: str) -> str:
    """Return session filename for a plain or qualified provider key."""
    base, instance = parse_qualified_provider(provider)
    base_file = SESSION_BASE_FILENAMES.get(base, f".{base}-session")
    return session_filename_for_instance(base_file, instance)


def resolve_provider_key(
    raw: str,
    work_dir: object = None,
    allowed_bases: set[str] | tuple[str, ...] | None = None,
) -> tuple[str, str | None, str]:
    """Resolve a provider or bare alias name into (base_provider, instance, qualified_key).

    Allows addressing an instance either as 'opencode:oc1' or directly as 'oc1'
    when 'oc1' is configured in ccb.config or has an active '.<base>-oc1-session' file.
    """
    import json
    import os
    from pathlib import Path

    allowed = set(allowed_bases) if allowed_bases is not None else set(KNOWN_PROVIDERS)
    norm = normalize_provider_token(raw)
    base, instance = parse_qualified_provider(norm)
    if base in allowed:
        qualified = make_qualified_key(base, instance)
        return base, instance, qualified

    # Bare alias lookup (e.g. 'oc1' -> 'opencode:oc1')
    alias = (raw or "").strip().lower()
    if not alias or ":" in alias or "=" in alias:
        return base, instance, make_qualified_key(base, instance)

    roots: list[Path] = []
    if work_dir:
        try:
            roots.append(Path(str(work_dir)).expanduser())
        except Exception:
            pass
    env_wd = (os.environ.get("CCB_WORK_DIR") or "").strip()
    if env_wd:
        try:
            p = Path(env_wd).expanduser()
            if p not in roots:
                roots.append(p)
        except Exception:
            pass
    try:
        cwd = Path.cwd()
        if cwd not in roots:
            roots.append(cwd)
    except Exception:
        pass

    # 1. Check active session files in work_dir (.ccb/.<prov>-<alias>-session)
    for root in roots:
        for cfg_dir in (root / ".ccb", root / ".ccb_config", root):
            if not cfg_dir.is_dir():
                continue
            for cand_base in KNOWN_PROVIDERS:
                if cand_base not in allowed:
                    continue
                base_fn = SESSION_BASE_FILENAMES.get(cand_base, f".{cand_base}-session")
                cand_fn = session_filename_for_instance(base_fn, alias)
                if (cfg_dir / cand_fn).is_file():
                    return cand_base, alias, make_qualified_key(cand_base, alias)

    # 2. Check ccb.config (aliases map or providers list with '<base>:<alias>')
    cfg_paths: list[Path] = []
    for root in roots:
        cfg_paths.append(root / ".ccb" / "ccb.config")
        cfg_paths.append(root / ".ccb_config" / "ccb.config")
    try:
        cfg_paths.append(Path.home() / ".ccb" / "ccb.config")
    except Exception:
        pass

    for cfg_path in cfg_paths:
        if not cfg_path.is_file():
            continue
        try:
            raw_text = cfg_path.read_text(encoding="utf-8-sig").strip()
        except Exception:
            continue
        if not raw_text:
            continue
        try:
            obj = json.loads(raw_text)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            aliases_map = obj.get("aliases")
            if isinstance(aliases_map, dict):
                mapped = aliases_map.get(alias)
                if isinstance(mapped, str) and mapped.strip():
                    m_base, m_inst = parse_qualified_provider(mapped)
                    if m_base in allowed:
                        inst = m_inst or alias
                        return m_base, inst, make_qualified_key(m_base, inst)
            raw_provs = obj.get("providers")
            tokens = raw_provs if isinstance(raw_provs, list) else ([raw_provs] if isinstance(raw_provs, str) else [])
        elif isinstance(obj, list):
            tokens = obj
        else:
            tokens = [raw_text]

        for item in tokens:
            for part in str(item or "").replace(",", " ").split():
                p_base, p_inst = parse_qualified_provider(part)
                if p_base in allowed and p_inst == alias:
                    return p_base, p_inst, make_qualified_key(p_base, p_inst)

    return base, instance, make_qualified_key(base, instance)
