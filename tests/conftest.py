from __future__ import annotations

from pathlib import Path

import pytest

from gdrivefilter.config import Config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        client_id="fake-id",
        client_secret="fake-secret",
        creds_source="test",
        scope=["https://www.googleapis.com/auth/drive.readonly"],
        scope_name="readonly",
        oauth_port=8765,
        backup_root=tmp_path / "primary",
        backup_mirror_ext=tmp_path / "external",
        disk_margin_gb=0.0,
        google_export_format="office",
        ollama_host="http://localhost:11434",
        ollama_embed_model="bge-m3",
        ollama_llm_model="qwen2.5:7b",
        token_dir=tmp_path / ".tokens",
    )
