"""Kod dlya parolya vstrechi: pwd khranitsya kak pwd_ref, v logi ne pishetsya (6.2/HT-3).

v1 — prostoy reference-store v BDD-zavisimom fayle (ne git). Pri podklyuchenii
vault prosto menyayetsya etot modul'.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path


def _store_path() -> Path:
    base = os.environ.get("DATA_DIR", "/data")
    p = Path(base) / "secrets"
    p.mkdir(parents=True, exist_ok=True)
    return p / "meeting_pwd.json"


def store_pwd(job_key: str, pwd: str) -> str:
    ref = f"pwdref:{secrets.token_hex(8)}"
    path = _store_path()
    data = json.loads(path.read_text()) if path.exists() else {}
    data[ref] = pwd
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    return ref


def get_pwd(pwd_ref: str | None) -> str | None:
    if not pwd_ref:
        return None
    path = _store_path()
    if not path.exists():
        return None
    return json.loads(path.read_text()).get(pwd_ref)


def delete_pwd(pwd_ref: str | None) -> None:
    if not pwd_ref:
        return
    path = _store_path()
    if not path.exists():
        return
    data = json.loads(path.read_text())
    if pwd_ref in data:
        del data[pwd_ref]
        path.write_text(json.dumps(data))
