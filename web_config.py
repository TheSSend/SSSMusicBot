import json
import logging
import time
from typing import Any

from json_store import JsonStore
from runtime_paths import data_path


logger = logging.getLogger(__name__)

_store = JsonStore(data_path("web_config.json"))
_cache_value: dict[str, Any] | None = None
_cache_ts: float = 0.0
_CACHE_TTL_SECONDS = 2.0


def get_web_config() -> dict[str, Any]:
    global _cache_value, _cache_ts
    now = time.time()
    if _cache_value is not None and (now - _cache_ts) < _CACHE_TTL_SECONDS:
        return _cache_value
    try:
        value = _store.load()
        if not isinstance(value, dict):
            value = {}
    except Exception:
        logger.exception("Failed to load web_config.json")
        value = {}
    _cache_value = value
    _cache_ts = now
    return value


def get_int(cfg: dict[str, Any], path: list[str], default: int | None = None) -> int | None:
    cur: Any = cfg
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    if cur is None:
        return default
    if isinstance(cur, bool):
        return default
    if isinstance(cur, int):
        return cur
    try:
        return int(str(cur).strip())
    except Exception:
        return default


def get_int_list(cfg: dict[str, Any], path: list[str], default: list[int] | None = None) -> list[int] | None:
    cur: Any = cfg
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    if cur is None:
        return default
    if isinstance(cur, list):
        out: list[int] = []
        for item in cur:
            try:
                out.append(int(str(item).strip()))
            except Exception:
                continue
        return out
    if isinstance(cur, str):
        parts = [p.strip() for p in cur.split(",")]
        out = [int(p) for p in parts if p.isdigit()]
        return out
    return default


def dumps_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)

