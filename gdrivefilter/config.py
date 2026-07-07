"""Configuration loading + credential resolution.

Reuses Google OAuth credentials from sibling projects when this project's
.env does not define them, exactly as requested (no new GCP project needed).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .logging_conf import get_logger

log = get_logger("config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

def _borrow_env_candidates() -> list[Path]:
    """Optional .env files to borrow GOOGLE_CLIENT_ID/SECRET from when this
    project's .env doesn't define them. Set GDRIVE_BORROW_ENV to a
    colon-separated list of .env paths to enable (empty by default)."""
    raw = os.environ.get("GDRIVE_BORROW_ENV", "").strip()
    if not raw:
        return []
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]

SCOPE_MAP = {
    "readonly": ["https://www.googleapis.com/auth/drive.readonly"],
    "full": ["https://www.googleapis.com/auth/drive"],
}


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE, optional quotes, # comments)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                out[key] = val
    except OSError:
        pass
    return out


def _resolve_google_creds(explicit_id: str, explicit_secret: str) -> tuple[str, str, str]:
    """Return (client_id, client_secret, source). Falls back to sibling projects."""
    if explicit_id and explicit_secret:
        return explicit_id, explicit_secret, "local .env"
    for cand in _borrow_env_candidates():
        env = _parse_env_file(cand)
        cid = env.get("GOOGLE_CLIENT_ID", "").strip()
        csec = env.get("GOOGLE_CLIENT_SECRET", "").strip()
        if cid and csec:
            return cid, csec, str(cand)
    return "", "", "none"


@dataclass
class Config:
    client_id: str
    client_secret: str
    creds_source: str
    scope: list[str]
    scope_name: str
    oauth_port: int
    backup_root: Path
    backup_mirror_ext: Path | None
    disk_margin_gb: float
    google_export_format: str
    ollama_host: str
    ollama_embed_model: str
    ollama_llm_model: str
    download_workers: int = 8
    token_dir: Path = field(default=PROJECT_ROOT / ".tokens")

    @property
    def destinations(self) -> list[Path]:
        dests = [self.backup_root]
        if self.backup_mirror_ext:
            dests.append(self.backup_mirror_ext)
        return dests


def load_config(env: dict | None = None) -> Config:
    """Load config from OS env + this project's .env (OS env wins)."""
    file_env = _parse_env_file(PROJECT_ROOT / ".env")
    src = env if env is not None else {**file_env, **os.environ}

    def g(key: str, default: str = "") -> str:
        return str(src.get(key, default) or default)

    cid, csec, source = _resolve_google_creds(g("GOOGLE_CLIENT_ID"), g("GOOGLE_CLIENT_SECRET"))
    scope_name = g("DRIVE_SCOPE", "readonly").lower()
    scope = SCOPE_MAP.get(scope_name, SCOPE_MAP["readonly"])

    ext = g("BACKUP_MIRROR_EXT").strip()
    cfg = Config(
        client_id=cid,
        client_secret=csec,
        creds_source=source,
        scope=scope,
        scope_name=scope_name if scope_name in SCOPE_MAP else "readonly",
        oauth_port=int(g("OAUTH_LOOPBACK_PORT", "8765")),
        backup_root=Path(g("BACKUP_ROOT", "./backups")).expanduser(),
        backup_mirror_ext=Path(ext).expanduser() if ext else None,
        disk_margin_gb=float(g("DISK_SAFETY_MARGIN_GB", "5")),
        google_export_format=g("GOOGLE_EXPORT_FORMAT", "office").lower(),
        ollama_host=g("OLLAMA_HOST", "http://localhost:11434").rstrip("/"),
        ollama_embed_model=g("OLLAMA_EMBED_MODEL", "bge-m3"),
        ollama_llm_model=g("OLLAMA_LLM_MODEL", "qwen2.5:7b"),
        download_workers=max(1, int(g("DOWNLOAD_WORKERS", "8"))),
    )
    return cfg
