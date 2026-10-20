"""Хранение ключей основного и копирующих счетов в одном JSON-файле (data/followers.json).

Формат: {"main": {"api_key", "api_secret", "testnet"}, "followers": [{"name", "api_key", "api_secret", ...}]}.
Секреты наружу (в веб-интерфейс) не отдаются; пустой секрет при сохранении означает «оставить прежний».
"""
from __future__ import annotations

import json
import os
import tempfile

from .followers import parse_specs

FOLLOWER_FIELDS = ("name", "api_key", "api_secret", "testnet", "multiplier", "dry_run", "enabled")


def read_raw(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {"main": {}, "followers": []}
    if isinstance(raw, list):   # старый формат: просто список копий
        raw = {"followers": raw}
    raw.setdefault("main", {})
    raw.setdefault("followers", [])
    return raw


def load_main(path: str) -> dict | None:
    """Ключи основного счёта из файла (если заданы) — они приоритетнее .env."""
    main = read_raw(path).get("main") or {}
    if main.get("api_key") and main.get("api_secret"):
        return {"api_key": str(main["api_key"]).strip(), "api_secret": str(main["api_secret"]).strip(),
                "testnet": bool(main.get("testnet", False))}
    return None


def public_view(raw: dict) -> dict:
    m = raw.get("main") or {}
    return {
        "main": {"api_key": m.get("api_key", ""), "has_secret": bool(m.get("api_secret")),
                 "testnet": bool(m.get("testnet", False))},
        "followers": [{"name": f.get("name", ""), "api_key": f.get("api_key", ""),
                       "has_secret": bool(f.get("api_secret")), "testnet": bool(f.get("testnet", False)),
                       "multiplier": float(f.get("multiplier", 1.0)), "dry_run": bool(f.get("dry_run", False)),
                       "enabled": bool(f.get("enabled", True))} for f in raw.get("followers", [])],
    }


def merge(old: dict, new: dict) -> dict:
    """Новые данные из формы + прежние секреты там, где поле секрета пришло пустым."""
    om = old.get("main") or {}
    nm = new.get("main") or {}
    main = {"api_key": str(nm.get("api_key", "")).strip(),
            "api_secret": str(nm.get("api_secret", "")).strip() or om.get("api_secret", ""),
            "testnet": bool(nm.get("testnet", False))}
    if not main["api_key"]:
        main["api_secret"] = ""
    old_by_name = {f.get("name"): f for f in old.get("followers", [])}
    followers = []
    for f in new.get("followers", []):
        name = str(f.get("name", "")).strip()
        prev = old_by_name.get(name, {})
        followers.append({
            "name": name,
            "api_key": str(f.get("api_key", "")).strip(),
            "api_secret": str(f.get("api_secret", "")).strip() or prev.get("api_secret", ""),
            "testnet": bool(f.get("testnet", False)),
            "multiplier": float(f.get("multiplier") or 1.0),
            "dry_run": bool(f.get("dry_run", False)),
            "enabled": bool(f.get("enabled", True)),
        })
    return {"main": main, "followers": followers}


def save(path: str, new: dict) -> dict:
    """Проверяет и атомарно записывает (права 0600). Бросает ValueError при неверных данных."""
    merged = merge(read_raw(path), new)
    parse_specs(merged["followers"], "followers")
    if merged["main"]["api_key"] and not merged["main"]["api_secret"]:
        raise ValueError("main: укажите API Secret")
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".followers-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return merged
