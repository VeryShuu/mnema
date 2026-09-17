import subprocess
import sys
from pathlib import Path

import pytest

from mnema.core import registry

ROOT = Path(__file__).resolve().parents[1]


def test_registry_names_unique():
    names = [k.name for b in registry.REGISTRY for k in b.keys]
    assert len(names) == len(set(names))


def test_registry_index_consistent():
    assert set(registry.KEY_INDEX) == {k.name for b in registry.REGISTRY for k in b.keys}


def test_required_keys_per_tz():
    required = {k.name for b in registry.REGISTRY for k in b.keys
                if k.klass == registry.REQUIRED}
    assert required == {"ZOOM_PROFILE_DIR", "SMTP_HOST", "NOTIFY_EMAIL_TO", "ARCHIVE_DIR"}


def test_preflight_empty_config_fails_with_key_names():
    errs = registry.preflight(env={})
    assert len(errs) == 4
    for e in errs:
        key = e.split(" ", 2)[1]
        assert key in registry.KEY_INDEX
        assert ".env.example" in e
        # oshibka imenuet KLYUCH, ne znachenie
        assert "changeme" not in e


def test_preflight_full_config_passes():
    env = {k.name: "x" for k in registry.KEY_INDEX.values()}
    assert registry.preflight(env=env) == []


def test_capability_service_starts_without_keys():
    env = {k.name: "x" for k in registry.KEY_INDEX.values()}
    env["LLM_BASE_URL"] = ""
    env["ASR_BASE_URL"] = ""
    assert registry.preflight(env=env) == []


def test_capability_state(llm_env_clean):
    import os
    for var in ("LLM_BASE_URL", "LLM_MODEL", "ASR_BASE_URL"):
        os.environ.pop(var, None)
    assert registry.state_for("llm")["state"] == "not_configured"
    assert registry.state_for("asr")["state"] == "not_configured"
    os.environ["LLM_BASE_URL"] = "http://ai:11434/v1"
    os.environ["LLM_MODEL"] = "m1"
    st = registry.state_for("llm")
    assert st["state"] == "configured" and st["model"] == "m1"


def test_env_example_drift_test():
    """.env.example v rejpositorii = generacia reestra (FT-38)."""
    expected = registry.render_env_example()
    actual = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert actual == expected, ".env.example ustarel - peredelat': python -m mnema.core.registry > .env.example"


def test_env_example_no_battle_placeholders():
    for b in registry.REGISTRY:
        for k in b.keys:
            if k.secret:
                assert not (k.default or ""), f"{k.name} secret ne dolzhen imet bootscrap-znachenie"
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for bad in ("changeme", "CHANGEME", "secret123"):
        assert bad not in text
