from pathlib import Path

from gdrivefilter import config as cfgmod


def test_env_parser(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text('A=1\nB="two"\n# comment\nC=\n', encoding="utf-8")
    parsed = cfgmod._parse_env_file(p)
    assert parsed == {"A": "1", "B": "two", "C": ""}


def test_explicit_creds_win():
    cid, csec, src = cfgmod._resolve_google_creds("myid", "mysecret")
    assert (cid, csec, src) == ("myid", "mysecret", "local .env")


def test_scope_defaults_to_readonly():
    cfg = cfgmod.load_config(env={"BACKUP_ROOT": "/tmp/x"})
    assert cfg.scope_name == "readonly"
    assert "drive.readonly" in cfg.scope[0]
