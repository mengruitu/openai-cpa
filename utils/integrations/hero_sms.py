import os
import time
import random
import threading
from typing import Any, Dict, List, Optional
from curl_cffi import requests
from utils import db_manager
from utils import config as cfg
from utils.auth_core import generate_payload

class UserStoppedError(Exception): pass
def _ssl_verify() -> bool: return True

def _info(msg):
    print(f"[{cfg.ts()}] [INFO] {msg}")

def _warn(msg):
    print(f"[{cfg.ts()}] [INFO] {msg}")

def _raise_if_stopped() -> None:
    if getattr(cfg, 'GLOBAL_STOP', False):
        raise UserStoppedError("stopped")

def _sleep_interruptible(sec: float) -> bool:
    for _ in range(int(sec * 10)):
        if getattr(cfg, 'GLOBAL_STOP', False):
            return True
        time.sleep(0.1)
    return False


def _build_sentinel_for_session(session, flow: str, proxies: Any) -> str:
    """Legacy compat helper for tests and fallback path."""
    try:
        from utils.sentinel import get_token
        return get_token(session, flow, proxies) or ""
    except Exception:
        return ""


def _post_with_retry(session, url: str, headers: dict = None, json_body: dict = None, proxies: Any = None,
                     timeout: int = 30, retries: int = 1):
    for attempt in range(retries + 1):
        try:
            return session.post(url, headers=headers, json=json_body, proxies=proxies, timeout=timeout)
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(1.5)

def _extract_next_url(vj: dict) -> str:
    if not isinstance(vj, dict): return ""
    page = vj.get("page")
    if isinstance(page, dict) and page.get("url"): return str(page["url"])
    return str(vj.get("continue_url") or "")


def _hero_sms_extract_code_payload(data: Any) -> str:
    if isinstance(data, dict):
        direct_candidates = [
            data.get("code"),
            data.get("sms_code"),
            data.get("otp"),
            data.get("message"),
        ]
        for value in direct_candidates:
            text = str(value or "").strip()
            if text and any(ch.isdigit() for ch in text):
                return text

        nested_dict_keys = ["sms", "data", "result", "payload"]
        for key in nested_dict_keys:
            nested = data.get(key)
            code = _hero_sms_extract_code_payload(nested)
            if code:
                return code

        nested_list_keys = ["smsList", "messages", "list", "items"]
        for key in nested_list_keys:
            nested = data.get(key)
            code = _hero_sms_extract_code_payload(nested)
            if code:
                return code

    if isinstance(data, list):
        for item in data:
            code = _hero_sms_extract_code_payload(item)
            if code:
                return code
    return ""


def _iter_countries_payload(data: Any):
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(data, dict):
        # wrapped format: {"status":"1","data":[...]}
        wrapped = data.get("data")
        if isinstance(wrapped, list):
            for item in wrapped:
                if isinstance(item, dict):
                    yield item
            return
        # fallback: dict keyed by country id
        for item in data.values():
            if isinstance(item, dict):
                yield item


def _follow_redirect_chain(session, url: str, proxies: Any):
    return "", url

def _hero_sms_enabled() -> bool:
    return bool(cfg.HERO_SMS_ENABLED) and bool(_hero_sms_api_key())

def _hero_sms_api_key() -> str:
    return str(cfg.HERO_SMS_API_KEY).strip()

def _hero_sms_base_url() -> str:
    url = str(cfg.HERO_SMS_BASE_URL).strip()
    return url or "https://hero-sms.com/stubs/handler_api.php"

def _hero_sms_min_balance_limit() -> float:
    return float(cfg.HERO_SMS_MIN_BALANCE)

def _hero_sms_order_max_price() -> float:
    return float(cfg.HERO_SMS_MAX_PRICE)


def _hero_sms_order_min_price() -> float:
    try:
        conf = _hero_sms_config_dict()
        return float(conf.get("min_price") or 0.0)
    except Exception:
        return 0.0

def _hero_sms_reuse_enabled() -> bool:
    return bool(cfg.HERO_SMS_REUSE_PHONE)

def _hero_sms_auto_pick_country() -> bool:
    return bool(cfg.HERO_SMS_AUTO_PICK_COUNTRY)

def _hero_sms_poll_timeout_sec() -> int:
    return int(cfg.HERO_SMS_POLL_TIMEOUT_SEC)

def _hero_sms_max_tries() -> int:
    return int(cfg.HERO_SMS_MAX_TRIES)

def _hero_sms_country_timeout_limit() -> int: return 2

def _hero_sms_country_cooldown_sec() -> int: return 900

def _hero_sms_price_cache_ttl_sec() -> int: return 90

def _hero_sms_reuse_ttl_sec() -> int: return 1200

def _hero_sms_reuse_max_uses() -> int:
    return int(getattr(cfg, 'HERO_SMS_REUSE_MAX', 2))

def _hero_sms_mark_ready_enabled() -> bool: return True

_HERO_SMS_SERVICE_CACHE: str = ""
_HERO_SMS_COUNTRY_CACHE: dict[str, int] = {}
_HERO_SMS_VERIFY_LOCK = threading.Lock()
_HERO_SMS_STATS_LOCK = threading.Lock()
_HERO_SMS_RUNTIME: dict[str, float] = {
    "spent_total_usd": 0.0,
    "balance_last_usd": -1.0,
    "balance_start_usd": -1.0,
    "updated_at": 0.0,
}
_HERO_SMS_REUSE_LOCK = threading.Lock()
_HERO_SMS_REUSE_STATE: dict[str, Any] = {
    "items": [],
}
_HERO_SMS_COUNTRY_LOCK = threading.Lock()
_HERO_SMS_COUNTRY_TIMEOUTS: dict[int, int] = {}
_HERO_SMS_COUNTRY_COOLDOWN_UNTIL: dict[int, float] = {}
_HERO_SMS_COUNTRY_METRICS: dict[int, dict[str, float]] = {}
_HERO_SMS_PRICE_CACHE_LOCK = threading.Lock()
_HERO_SMS_PRICE_CACHE: dict[str, Any] = {
    "service": "",
    "updated_at": 0.0,
    "items": [],
}
_HERO_SMS_PRICE_V3_CACHE_LOCK = threading.Lock()
_HERO_SMS_PRICE_V3_CACHE: dict[str, Any] = {
    "service": "",
    "updated_at": 0.0,
    "items": [],
}
_HERO_SMS_COUNTRY_ENG_NAME_CACHE: Dict[int, str] = {}

_OPENAI_SMS_BLOCKED_COUNTRY_IDS = {
    0,  # Russia
    3,  # China
    14,  # Hong Kong
    20,  # Macao
    51,  # Belarus
    57,  # Iran
    110,  # Syria
    113,  # Cuba
    191,  # North Korea
}

def _load_reuse_state_from_db():
    global _HERO_SMS_REUSE_STATE
    saved = db_manager.get_sys_kv("sms_reuse_data")
    if saved and isinstance(saved, dict):
        with _HERO_SMS_REUSE_LOCK:
            if isinstance(saved.get("items"), list):
                _HERO_SMS_REUSE_STATE["items"] = [dict(item) for item in saved.get("items") if isinstance(item, dict)]
            elif saved.get("activation_id") and saved.get("phone"):
                # migrate old single-item schema
                _HERO_SMS_REUSE_STATE["items"] = [{
                    "activation_id": str(saved.get("activation_id") or "").strip(),
                    "phone": str(saved.get("phone") or "").strip(),
                    "service": str(saved.get("service") or "").strip(),
                    "country": int(saved.get("country") or -1),
                    "provider_ids": [],
                    "uses": int(saved.get("uses") or 0),
                    "created_at": float(saved.get("updated_at") or time.time()),
                    "updated_at": float(saved.get("updated_at") or time.time()),
                    "in_use": False,
                    "claimed_by": "",
                    "last_result": "migrated",
                }]

_load_reuse_state_from_db()

def _sync_reuse_to_db():
    db_manager.set_sys_kv("sms_reuse_data", _HERO_SMS_REUSE_STATE)

def _hero_sms_reuse_prune_locked(now: Optional[float] = None) -> None:
    now_ts = float(now or time.time())
    ttl = _hero_sms_reuse_ttl_sec()
    max_uses = _hero_sms_reuse_max_uses()
    items = _HERO_SMS_REUSE_STATE.get("items") or []
    kept: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        created_at = float(raw.get("created_at") or raw.get("updated_at") or now_ts)
        uses = int(raw.get("uses") or 0)
        phone = str(raw.get("phone") or "").strip()
        aid = str(raw.get("activation_id") or "").strip()
        if not aid or not phone:
            continue
        if (now_ts - created_at) > ttl:
            continue
        if uses >= max_uses:
            continue
        kept.append(raw)
    _HERO_SMS_REUSE_STATE["items"] = kept


def _hero_sms_reuse_claim(service: str, country: int, provider_ids: Optional[list[int]] = None) -> tuple[str, str, int]:
    now = time.time()
    svc = str(service or "").strip()
    requested_provider_ids = set(_parse_int_list(provider_ids or []))
    with _HERO_SMS_REUSE_LOCK:
        _hero_sms_reuse_prune_locked(now)
        items = _HERO_SMS_REUSE_STATE.get("items") or []
        matched_index = -1
        same_country_match = -1
        cross_country_match = -1
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if str(item.get("service") or "").strip() != svc:
                continue
            if bool(item.get("in_use")):
                continue
            item_provider_ids = set(_parse_int_list(item.get("provider_ids") or []))
            if requested_provider_ids and item_provider_ids and requested_provider_ids.isdisjoint(item_provider_ids):
                continue
            item_country = int(item.get("country") or -1)
            if item_country == int(country):
                same_country_match = idx
                break
            if cross_country_match < 0:
                cross_country_match = idx
        if same_country_match >= 0:
            matched_index = same_country_match
        elif cross_country_match >= 0:
            matched_index = cross_country_match
        if matched_index < 0:
            return "", "", 0
        item = items[matched_index]
        item["in_use"] = True
        item["claimed_by"] = str(threading.get_ident())
        item["updated_at"] = now
        return str(item.get("activation_id") or "").strip(), str(item.get("phone") or "").strip(), int(item.get("uses") or 0)


def _hero_sms_reuse_add(activation_id: str, phone: str, service: str, country: int, provider_ids: Optional[list[int]] = None, result: str = "") -> None:
    aid = str(activation_id or "").strip()
    ph = str(phone or "").strip()
    if not aid or not ph:
        return
    with _HERO_SMS_REUSE_LOCK:
        now = time.time()
        _hero_sms_reuse_prune_locked(now)
        items = _HERO_SMS_REUSE_STATE.setdefault("items", [])
        normalized_provider_ids = _parse_int_list(provider_ids or [])
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("activation_id") or "").strip() != aid:
                continue
            item.update({
                "phone": ph,
                "service": str(service or "").strip(),
                "country": int(country),
                "provider_ids": normalized_provider_ids,
                "updated_at": now,
                "in_use": False,
                "claimed_by": "",
                "last_result": result or str(item.get("last_result") or ""),
            })
            _sync_reuse_to_db()
            return
        items.append({
            "activation_id": aid,
            "phone": ph,
            "service": str(service or "").strip(),
            "country": int(country),
            "provider_ids": normalized_provider_ids,
            "uses": 0,
            "created_at": now,
            "updated_at": now,
            "in_use": False,
            "claimed_by": "",
            "last_result": result,
        })
    _sync_reuse_to_db()


def _hero_sms_reuse_release(activation_id: str, *, increase: bool = False, result: str = "", keep: bool = True) -> None:
    aid = str(activation_id or "").strip()
    if not aid:
        return
    with _HERO_SMS_REUSE_LOCK:
        now = time.time()
        items = _HERO_SMS_REUSE_STATE.get("items") or []
        for idx, item in enumerate(list(items)):
            if not isinstance(item, dict):
                continue
            if str(item.get("activation_id") or "").strip() != aid:
                continue
            if keep:
                if increase:
                    item["uses"] = int(item.get("uses") or 0) + 1
                item["updated_at"] = now
                item["in_use"] = False
                item["claimed_by"] = ""
                if result:
                    item["last_result"] = result
            else:
                items.pop(idx)
            break
        _hero_sms_reuse_prune_locked(now)
    _sync_reuse_to_db()


def _hero_sms_reuse_clear(activation_id: str = "") -> None:
    aid = str(activation_id or "").strip()
    with _HERO_SMS_REUSE_LOCK:
        if not aid:
            _HERO_SMS_REUSE_STATE["items"] = []
        else:
            items = _HERO_SMS_REUSE_STATE.get("items") or []
            _HERO_SMS_REUSE_STATE["items"] = [
                item for item in items
                if isinstance(item, dict) and str(item.get("activation_id") or "").strip() != aid
            ]
    _sync_reuse_to_db()


def _hero_sms_reuse_get(service: str, country: int) -> tuple[str, str, int]:
    return _hero_sms_reuse_claim(service, country, None)


def _hero_sms_reuse_set(activation_id: str, phone: str, service: str, country: int) -> None:
    _hero_sms_reuse_add(activation_id, phone, service, country, provider_ids=None, result="set")


def _hero_sms_reuse_touch(increase: bool = False) -> None:
    # legacy compatibility path: update currently in-use item first, else newest item
    with _HERO_SMS_REUSE_LOCK:
        items = _HERO_SMS_REUSE_STATE.get("items") or []
        target = None
        for item in items:
            if isinstance(item, dict) and item.get("in_use"):
                target = item
                break
        if target is None and items:
            target = items[-1]
        if isinstance(target, dict):
            if increase:
                target["uses"] = int(target.get("uses") or 0) + 1
            target["updated_at"] = time.time()
            target["in_use"] = False
            target["claimed_by"] = ""
    _sync_reuse_to_db()

def _hero_sms_country_is_on_cooldown(country_id: int) -> bool:
    cid = int(country_id)
    now = time.time()
    with _HERO_SMS_COUNTRY_LOCK:
        until = float(_HERO_SMS_COUNTRY_COOLDOWN_UNTIL.get(cid) or 0.0)
        if until <= 0:
            return False
        if until <= now:
            _HERO_SMS_COUNTRY_COOLDOWN_UNTIL.pop(cid, None)
            _HERO_SMS_COUNTRY_TIMEOUTS.pop(cid, None)
            return False
        return True

def _hero_sms_country_mark_success(country_id: int) -> None:
    cid = int(country_id)
    with _HERO_SMS_COUNTRY_LOCK:
        _HERO_SMS_COUNTRY_TIMEOUTS.pop(cid, None)

def _hero_sms_country_mark_timeout(country_id: int) -> bool:
    cid = int(country_id)
    limit = _hero_sms_country_timeout_limit()
    cooldown_sec = _hero_sms_country_cooldown_sec()
    now = time.time()
    with _HERO_SMS_COUNTRY_LOCK:
        current = int(_HERO_SMS_COUNTRY_TIMEOUTS.get(cid) or 0) + 1
        _HERO_SMS_COUNTRY_TIMEOUTS[cid] = current
        if current < limit:
            return False
        _HERO_SMS_COUNTRY_TIMEOUTS[cid] = 0
        _HERO_SMS_COUNTRY_COOLDOWN_UNTIL[cid] = now + float(cooldown_sec)
        return True

def _hero_sms_country_record_result(country_id: int, success: bool, reason: str = "") -> None:
    cid = int(country_id)
    now = time.time()
    low = str(reason or "").strip().lower()
    with _HERO_SMS_COUNTRY_LOCK:
        row = _HERO_SMS_COUNTRY_METRICS.get(cid)
        if not isinstance(row, dict):
            row = {
                "attempts": 0.0,
                "success": 0.0,
                "timeout": 0.0,
                "send_fail": 0.0,
                "verify_fail": 0.0,
                "other_fail": 0.0,
                "last_used_at": 0.0,
                "last_success_at": 0.0,
            }
            _HERO_SMS_COUNTRY_METRICS[cid] = row

        row["attempts"] = float(row.get("attempts") or 0.0) + 1.0
        row["last_used_at"] = now

        if success:
            row["success"] = float(row.get("success") or 0.0) + 1.0
            row["last_success_at"] = now
            return

        if "接码超时" in low or "status_wait_code" in low or "timeout" in low:
            row["timeout"] = float(row.get("timeout") or 0.0) + 1.0
        elif "发送手机验证码失败" in low:
            row["send_fail"] = float(row.get("send_fail") or 0.0) + 1.0
        elif "手机验证码校验失败" in low:
            row["verify_fail"] = float(row.get("verify_fail") or 0.0) + 1.0
        else:
            row["other_fail"] = float(row.get("other_fail") or 0.0) + 1.0

def _hero_sms_country_score(
        country_id: int,
        *,
        cost: float,
        count: int,
        preferred_country: int,
) -> float:
    cid = int(country_id)
    preferred = int(preferred_country)
    if cid in _OPENAI_SMS_BLOCKED_COUNTRY_IDS:
        return -1e9
    if count <= 0:
        return -1e9
    if _hero_sms_country_is_on_cooldown(cid):
        return -1e9

    now = time.time()
    with _HERO_SMS_COUNTRY_LOCK:
        stats = dict(_HERO_SMS_COUNTRY_METRICS.get(cid) or {})
        timeout_streak = int(_HERO_SMS_COUNTRY_TIMEOUTS.get(cid) or 0)

    attempts = max(0.0, float(stats.get("attempts") or 0.0))
    success_num = max(0.0, float(stats.get("success") or 0.0))
    timeout_num = max(0.0, float(stats.get("timeout") or 0.0))
    send_fail_num = max(0.0, float(stats.get("send_fail") or 0.0))
    verify_fail_num = max(0.0, float(stats.get("verify_fail") or 0.0))
    other_fail_num = max(0.0, float(stats.get("other_fail") or 0.0))
    last_success_at = float(stats.get("last_success_at") or 0.0)

    if attempts <= 0:
        success_rate = 0.55
        timeout_rate = 0.0
        send_fail_rate = 0.0
        verify_fail_rate = 0.0
        other_fail_rate = 0.0
        explore_bonus = 9.0
    else:
        success_rate = success_num / attempts
        timeout_rate = timeout_num / attempts
        send_fail_rate = send_fail_num / attempts
        verify_fail_rate = verify_fail_num / attempts
        other_fail_rate = other_fail_num / attempts
        explore_bonus = max(0.0, 6.0 - min(6.0, attempts))

    score = 0.0
    score += success_rate * 80.0
    score -= timeout_rate * 70.0
    score -= send_fail_rate * 45.0
    score -= verify_fail_rate * 30.0
    score -= other_fail_rate * 20.0
    score -= float(timeout_streak) * 8.0
    score += explore_bonus

    if cost >= 0:
        score -= min(5.0, float(cost)) * 10.0
    score += min(20000, max(0, int(count))) / 2000.0

    if cid == preferred:
        score += 3.0

    if last_success_at > 0:
        age = max(0.0, now - last_success_at)
        if age < 900:
            score += 4.0
        elif age < 3600:
            score += 2.0

    return float(score)

_HERO_SMS_COUNTRY_NAME_CACHE: Dict[int, str] = {}

def _get_hero_country_names(proxies: Any) -> Dict[int, str]:
    global _HERO_SMS_COUNTRY_NAME_CACHE
    if _HERO_SMS_COUNTRY_NAME_CACHE:
        return _HERO_SMS_COUNTRY_NAME_CACHE

    ok, _, data = _hero_sms_request("getCountries", proxies=proxies, timeout=20)
    if ok:
        for item in _iter_countries_payload(data):
            try:
                cid = int(item.get("id"))
                name = item.get("chn") or item.get("eng") or f"未知({cid})"
                _HERO_SMS_COUNTRY_NAME_CACHE[cid] = name
            except:
                continue
    return _HERO_SMS_COUNTRY_NAME_CACHE


def _get_hero_country_eng_names(proxies: Any) -> Dict[int, str]:
    global _HERO_SMS_COUNTRY_ENG_NAME_CACHE
    if _HERO_SMS_COUNTRY_ENG_NAME_CACHE:
        return _HERO_SMS_COUNTRY_ENG_NAME_CACHE

    ok, _, data = _hero_sms_request("getCountries", proxies=proxies, timeout=20)
    if ok:
        for item in _iter_countries_payload(data):
            try:
                cid = int(item.get("id"))
            except Exception:
                continue
            name = item.get("eng") or item.get("chn") or f"Country {cid}"
            _HERO_SMS_COUNTRY_ENG_NAME_CACHE[cid] = name
    return _HERO_SMS_COUNTRY_ENG_NAME_CACHE


def _hero_sms_country_label(country_id: Any, proxies: Any = None) -> str:
    try:
        cid = int(country_id)
    except Exception:
        return str(country_id)
    name_map = _get_country_names_map(proxies)
    return f"{name_map.get(cid, f'国家{cid}')}({cid})"


_HERO_SMS_COUNTRY_NAMES_MAP: dict[int, str] = {}


def _get_country_names_map(proxies: Any) -> dict[int, str]:
    global _HERO_SMS_COUNTRY_NAMES_MAP
    if _HERO_SMS_COUNTRY_NAMES_MAP:
        return _HERO_SMS_COUNTRY_NAMES_MAP

    _info("正在同步 HeroSMS 全球国家名称对照表...")
    ok, _, data = _hero_sms_request("getCountries", proxies=proxies, timeout=20)

    mapping = {}
    if ok:
        for item in _iter_countries_payload(data):
            try:
                cid = int(item.get("id"))
                name = item.get("chn") or item.get("eng") or f"国家{cid}"
                mapping[cid] = name
            except:
                continue

    if mapping:
        _HERO_SMS_COUNTRY_NAMES_MAP = mapping
    return _HERO_SMS_COUNTRY_NAMES_MAP


def _hero_sms_service_lookup_code(service_code: str) -> str:
    svc = str(service_code or "").strip()
    return "dr" if svc.lower() == "openai" else svc


def _hero_sms_config_dict() -> dict[str, Any]:
    root = getattr(cfg, "_c", {})
    if isinstance(root, dict):
        section = root.get("hero_sms")
        if isinstance(section, dict):
            return section
    return {}


def _parse_int_list(raw: Any) -> list[int]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, tuple):
        items = list(raw)
    elif isinstance(raw, str):
        items = [x.strip() for x in raw.split(",")]
    else:
        items = []

    values: list[int] = []
    seen: set[int] = set()
    for item in items:
        try:
            num = int(str(item).strip())
        except Exception:
            continue
        if num in seen:
            continue
        seen.add(num)
        values.append(num)
    return values


def _parse_country_provider_map(raw: Any) -> dict[int, list[int]]:
    result: dict[int, list[int]] = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        try:
            cid = int(str(key).strip())
        except Exception:
            continue
        result[cid] = _parse_int_list(value)
    return result


def _hero_sms_selected_country_ids(preferred_country: int) -> list[int]:
    conf = _hero_sms_config_dict()
    selected = _parse_int_list(conf.get("selected_countries"))
    priority = _parse_int_list(conf.get("country_priority"))
    if not selected:
        if priority:
            selected = list(priority)
        else:
            selected = [int(preferred_country)]
    if not priority:
        return list(selected)

    selected_set = set(selected)
    ordered = [cid for cid in priority if cid in selected_set]
    for cid in selected:
        if cid not in ordered:
            ordered.append(cid)
    return ordered


def _hero_sms_multi_selector_enabled() -> bool:
    conf = _hero_sms_config_dict()
    selected = _parse_int_list(conf.get("selected_countries"))
    provider_map = _parse_country_provider_map(conf.get("country_provider_map"))
    provider_priority_map = _parse_country_provider_map(conf.get("provider_priority_map"))
    return len(selected) > 1 or bool(provider_map) or bool(provider_priority_map)


def _hero_sms_provider_ids_for_country(country_id: int) -> list[int]:
    conf = _hero_sms_config_dict()
    selected_map = _parse_country_provider_map(conf.get("country_provider_map"))
    priority_map = _parse_country_provider_map(conf.get("provider_priority_map"))
    selected = list(selected_map.get(int(country_id), []))
    priority = list(priority_map.get(int(country_id), []))
    if not selected:
        return priority
    if not priority:
        return selected

    selected_set = set(selected)
    ordered = [pid for pid in priority if pid in selected_set]
    for pid in selected:
        if pid not in ordered:
            ordered.append(pid)
    return ordered


def _hero_sms_provider_sort_key(item: dict[str, Any]) -> tuple[float, int, int]:
    try:
        price = float(item.get("price") or 999999.0)
    except Exception:
        price = 999999.0
    try:
        count = int(item.get("count") or 0)
    except Exception:
        count = 0
    try:
        provider_id = int(item.get("provider_id") or 0)
    except Exception:
        provider_id = 0
    return price, -count, provider_id


def _hero_sms_prices_v3_by_service(
        service_code: str,
        proxies: Any,
        *,
        force_refresh: bool = False,
) -> list[dict[str, Any]]:
    svc = str(service_code or "").strip()
    search_svc = _hero_sms_service_lookup_code(svc)
    _info(f"正在拉取 [{svc}] (API代号: {search_svc}) 的 V3 全球库存...")
    if not search_svc:
        return []

    ttl = _hero_sms_price_cache_ttl_sec()
    now = time.time()
    with _HERO_SMS_PRICE_V3_CACHE_LOCK:
        cache_svc = str(_HERO_SMS_PRICE_V3_CACHE.get("service") or "")
        cache_at = float(_HERO_SMS_PRICE_V3_CACHE.get("updated_at") or 0.0)
        cache_items = list(_HERO_SMS_PRICE_V3_CACHE.get("items") or [])
        if (not force_refresh) and cache_svc == svc and cache_items and (now - cache_at) <= float(ttl):
            return [dict(x) for x in cache_items if isinstance(x, dict)]

    name_map = _get_country_names_map(proxies)
    eng_name_map = _get_hero_country_eng_names(proxies)
    ok, text, data = _hero_sms_request(
        "getPricesV3",
        proxies=proxies,
        params={"service": search_svc},
        timeout=25,
    )
    if not ok:
        _warn(f"HeroSMS V3 请求失败，错误信息: {text}")
        return []
    if not isinstance(data, dict):
        _warn(f"HeroSMS V3 响应格式异常: {str(text or '')[:100]}")
        return []

    rows: list[dict[str, Any]] = []
    for country_key, country_entry in data.items():
        if not str(country_key).isdigit():
            continue
        cid = int(country_key)
        if cid in _OPENAI_SMS_BLOCKED_COUNTRY_IDS:
            continue
        if not isinstance(country_entry, dict):
            continue
        service_entry = country_entry.get(search_svc)
        if not isinstance(service_entry, dict):
            continue

        providers: list[dict[str, Any]] = []
        total_count = 0
        for provider_key, provider_entry in service_entry.items():
            if not isinstance(provider_entry, dict):
                continue
            try:
                provider_id = int(provider_entry.get("provider_id") or provider_key)
                count = int(provider_entry.get("count") or 0)
                price = float(provider_entry.get("price") or -1.0)
            except Exception:
                continue
            if count <= 0 or price < 0:
                continue
            total_count += count
            providers.append({
                "provider_id": provider_id,
                "count": count,
                "price": price,
            })

        if not providers:
            continue
        providers.sort(key=_hero_sms_provider_sort_key)
        rows.append({
            "country": cid,
            "name": name_map.get(cid, f"国家{cid}"),
            "eng_name": eng_name_map.get(cid, f"Country {cid}"),
            "service": search_svc,
            "min_cost": float(providers[0]["price"]),
            "total_count": int(total_count),
            "providers": providers,
        })

    if rows:
        with _HERO_SMS_PRICE_V3_CACHE_LOCK:
            _HERO_SMS_PRICE_V3_CACHE["service"] = svc
            _HERO_SMS_PRICE_V3_CACHE["updated_at"] = now
            _HERO_SMS_PRICE_V3_CACHE["items"] = [
                {
                    "country": int(item["country"]),
                    "name": str(item["name"]),
                    "eng_name": str(item.get("eng_name") or ""),
                    "service": str(item["service"]),
                    "min_cost": float(item["min_cost"]),
                    "total_count": int(item["total_count"]),
                    "providers": [dict(x) for x in item.get("providers") or [] if isinstance(x, dict)],
                }
                for item in rows
            ]
    return rows


def _hero_sms_build_number_candidates(
        proxies: Any,
        *,
        service_code: str,
        preferred_country: int,
) -> list[dict[str, Any]]:
    ordered_countries = _hero_sms_selected_country_ids(preferred_country)
    if not ordered_countries:
        return []

    rows = _hero_sms_prices_v3_by_service(service_code, proxies)
    available_countries = {int(x.get("country")) for x in rows if isinstance(x, dict)}
    candidates: list[dict[str, Any]] = []
    for cid in ordered_countries:
        country_id = int(cid)
        if country_id in _OPENAI_SMS_BLOCKED_COUNTRY_IDS:
            continue
        if country_id not in available_countries:
            continue
        if _hero_sms_country_is_on_cooldown(country_id):
            continue
        provider_ids = _hero_sms_provider_ids_for_country(country_id)
        candidates.append({
            "country": country_id,
            "provider_ids": [int(x) for x in provider_ids] if provider_ids else [],
        })
    return candidates


def _hero_sms_prices_by_service(
        service_code: str,
        proxies: Any,
        *,
        force_refresh: bool = False,
) -> list[dict[str, Any]]:
    svc = str(service_code or "").strip()
    search_svc = "dr" if svc.lower() == "openai" else svc

    _info(f"正在拉取 [{svc}] (API代号: {search_svc}) 的全球实时库存...")
    if not search_svc:
        return []

    ttl = _hero_sms_price_cache_ttl_sec()
    now = time.time()
    with _HERO_SMS_PRICE_CACHE_LOCK:
        cache_svc = str(_HERO_SMS_PRICE_CACHE.get("service") or "")
        cache_at = float(_HERO_SMS_PRICE_CACHE.get("updated_at") or 0.0)
        cache_items = list(_HERO_SMS_PRICE_CACHE.get("items") or [])
        if (not force_refresh) and cache_svc == svc and cache_items and (now - cache_at) <= float(ttl):
            return [dict(x) for x in cache_items if isinstance(x, dict)]

    name_map = _get_country_names_map(proxies)

    ok, text, data = _hero_sms_request(
        "getPrices",
        proxies=proxies,
        params={"service": search_svc},
        timeout=25,
    )

    if not ok:
        _warn(f"HeroSMS 请求失败，错误信息: {text}")
        return []

    if isinstance(data, dict) and "error" in data:
        _warn(f"❌ HeroSMS API 报错: {data.get('error')}")
        return []

    rows: list[dict[str, Any]] = []
    all_found_codes = set()

    items_to_parse = []
    if isinstance(data, dict):
        items_to_parse = list(data.items())
    elif isinstance(data, list):
        items_to_parse = [(str(item.get("country") or i), item) for i, item in enumerate(data)]
    else:
        _warn(f"⚠️ 响应格式异常: {text[:100]}")
        return []

    for country_key, entry in items_to_parse:
        if not str(country_key).isdigit(): continue
        cid = int(country_key)

        if cid in _OPENAI_SMS_BLOCKED_COUNTRY_IDS:
            continue
        if not isinstance(entry, dict): continue

        for k in entry.keys():
            if isinstance(entry[k], dict) and "cost" in entry[k]:
                all_found_codes.add(k)

        target = entry.get(search_svc) if search_svc in entry else entry
        if isinstance(target, dict) and "cost" in target:
            try:
                count = int(target.get("count") or 0)
                cost = float(target.get("cost") or -1.0)
                if count > 0:
                    rows.append({
                        "country": cid,
                        "name": name_map.get(cid, f"国家{cid}"),
                        "cost": cost,
                        "count": count
                    })
            except:
                continue

    if rows:
        _info(f"✅ 成功! 获取到 {len(rows)} 个国家的 [{svc}] 库存数据。")
        rows.sort(key=lambda x: (x.get("cost", 999), -x.get("count", 0)))
        with _HERO_SMS_PRICE_CACHE_LOCK:
            _HERO_SMS_PRICE_CACHE["service"] = svc
            _HERO_SMS_PRICE_CACHE["updated_at"] = now
            _HERO_SMS_PRICE_CACHE["items"] = [dict(x) for x in rows]
    else:
        _warn(f"⚠️ 在 [{search_svc}] 代号下未匹配到库存。")
        if all_found_codes:
            _warn(f"💡 探测到 API 实际返回的代号有: {', '.join(list(all_found_codes)[:10])}...")
            _warn("建议：在前端配置中尝试切换项目代号重试。")
        else:
            _warn("💡 API 返回的数据中不包含任何价格信息，请确认 API Key 是否正确。")

    return rows


def _hero_sms_pick_country_id(
        proxies: Any,
        *,
        service_code: str,
        preferred_country: int,
        exclude_country_ids: Optional[set[int]] = None,
        force_refresh: bool = False,
) -> int:
    preferred = int(preferred_country)
    excluded = {int(x) for x in (exclude_country_ids or set())}
    if not _hero_sms_auto_pick_country():
        if preferred in _OPENAI_SMS_BLOCKED_COUNTRY_IDS:
            _warn(f"HeroSMS 已关闭自动选国：首选国家 ID {preferred} 在黑名单内，仍将尝试使用该 ID")
            return preferred
        if _hero_sms_country_is_on_cooldown(preferred):
            _warn(f"HeroSMS 已关闭自动选国：首选国家冷却中，仍使用 {preferred}")
        return preferred

    rows = _hero_sms_prices_by_service(service_code, proxies, force_refresh=force_refresh)
    if not rows:
        if preferred not in _OPENAI_SMS_BLOCKED_COUNTRY_IDS and not _hero_sms_country_is_on_cooldown(preferred):
            return preferred
        return preferred

    scored: list[tuple[float, int, float, int]] = []
    for row in rows:
        cid = int(row.get("country") or -1)
        if cid < 0:
            continue
        if cid in excluded:
            continue
        try:
            cost = float(row.get("cost") or -1.0)
        except Exception:
            cost = -1.0
        try:
            count = int(row.get("count") or 0)
        except Exception:
            count = 0
        score = _hero_sms_country_score(
            cid,
            cost=cost,
            count=count,
            preferred_country=preferred,
        )
        if score <= -1e8:
            continue
        scored.append((score, cid, cost, count))

    if not scored:
        if preferred not in _OPENAI_SMS_BLOCKED_COUNTRY_IDS and not _hero_sms_country_is_on_cooldown(preferred):
            return preferred
        return preferred

    scored.sort(key=lambda x: (-float(x[0]), float(x[2]) if float(x[2]) >= 0 else 999999.0, -int(x[3]), int(x[1])))
    top_score, top_country, top_cost, top_count = scored[0]

    if top_country != preferred:
        _info(
            "HeroSMS 国家评分选优: "
            f"{preferred} -> {top_country} (score={top_score:.2f}, cost={top_cost:.3f}, stock={top_count})"
        )
    return int(top_country)

def _hero_sms_update_runtime(
        *,
        spent_delta: float = 0.0,
        balance: Optional[float] = None,
        init_start: bool = False,
) -> None:
    delta = max(0.0, float(spent_delta or 0.0))
    bal = None
    if balance is not None:
        try:
            bal = float(balance)
        except Exception:
            bal = None

    with _HERO_SMS_STATS_LOCK:
        if delta > 0:
            _HERO_SMS_RUNTIME["spent_total_usd"] = round(
                max(0.0, float(_HERO_SMS_RUNTIME.get("spent_total_usd") or 0.0)) + delta,
                4,
            )
        if bal is not None and bal >= 0:
            _HERO_SMS_RUNTIME["balance_last_usd"] = round(bal, 4)
            current_start = float(_HERO_SMS_RUNTIME.get("balance_start_usd") or -1.0)
            if init_start and current_start < 0:
                _HERO_SMS_RUNTIME["balance_start_usd"] = round(bal, 4)
        _HERO_SMS_RUNTIME["updated_at"] = time.time()


def reset_hero_sms_runtime_stats() -> None:
    with _HERO_SMS_STATS_LOCK:
        _HERO_SMS_RUNTIME["spent_total_usd"] = 0.0
        _HERO_SMS_RUNTIME["balance_last_usd"] = -1.0
        _HERO_SMS_RUNTIME["balance_start_usd"] = -1.0
        _HERO_SMS_RUNTIME["updated_at"] = time.time()
    _hero_sms_reuse_clear()
    with _HERO_SMS_COUNTRY_LOCK:
        _HERO_SMS_COUNTRY_TIMEOUTS.clear()
        _HERO_SMS_COUNTRY_COOLDOWN_UNTIL.clear()
    with _HERO_SMS_PRICE_CACHE_LOCK:
        _HERO_SMS_PRICE_CACHE["service"] = ""
        _HERO_SMS_PRICE_CACHE["updated_at"] = 0.0
        _HERO_SMS_PRICE_CACHE["items"] = []
    with _HERO_SMS_PRICE_V3_CACHE_LOCK:
        _HERO_SMS_PRICE_V3_CACHE["service"] = ""
        _HERO_SMS_PRICE_V3_CACHE["updated_at"] = 0.0
        _HERO_SMS_PRICE_V3_CACHE["items"] = []
    _HERO_SMS_COUNTRY_ENG_NAME_CACHE.clear()

def get_hero_sms_runtime_stats() -> dict[str, float]:
    with _HERO_SMS_STATS_LOCK:
        return {
            "spent_total_usd": round(
                max(0.0, float(_HERO_SMS_RUNTIME.get("spent_total_usd") or 0.0)),
                4,
            ),
            "balance_last_usd": round(
                float(_HERO_SMS_RUNTIME.get("balance_last_usd") or -1.0),
                4,
            ),
            "balance_start_usd": round(
                float(_HERO_SMS_RUNTIME.get("balance_start_usd") or -1.0),
                4,
            ),
            "updated_at": float(_HERO_SMS_RUNTIME.get("updated_at") or 0.0),
        }

def _hero_sms_request(
        action: str,
        *,
        proxies: Any,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 25,
) -> tuple[bool, str, Any]:
    key = _hero_sms_api_key()
    if not key:
        return False, "NO_KEY", None

    query: Dict[str, Any] = {
        "action": str(action or "").strip(),
        "api_key": key,
    }
    if isinstance(params, dict):
        for k, v in params.items():
            if v is None:
                continue
            sv = str(v).strip() if isinstance(v, str) else v
            if sv == "":
                continue
            query[str(k)] = sv

    try:
        resp = requests.get(
            _hero_sms_base_url(),
            params=query,
            proxies=proxies,
            verify=_ssl_verify(),
            timeout=timeout,
            impersonate="chrome131",
        )
    except Exception as e:
        return False, f"REQUEST_ERROR:{e}", None

    code = int(getattr(resp, "status_code", 0) or 0)
    text = str(getattr(resp, "text", "") or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = None
    if not (200 <= code < 300):
        if text:
            return False, text, data
        return False, f"HTTP {code}", data
    return True, text, data


def hero_sms_get_balance(proxies: Any = None) -> tuple[float, str]:
    _info("正在查询 HeroSMS 账户余额...")
    ok, text, data = _hero_sms_request("getBalance", proxies=proxies, timeout=20)
    if not ok:
        _warn(f"查询余额失败: {text}")
        return -1.0, str(text or "getBalance failed")

    line = str(text or "").strip()
    if line.upper().startswith("ACCESS_BALANCE:"):
        raw = line.split(":", 1)[1].strip()
        try:
            value = float(raw)
            _info(f"HeroSMS 余额查询成功: ${value:.2f}")
            _hero_sms_update_runtime(balance=value, init_start=True)
            return value, ""
        except Exception:
            pass

    if isinstance(data, dict):
        status_value = str(data.get("status") or "").strip().lower()
        if status_value in {"0", "false", "fail", "error"}:
            msg = str(data.get("message") or line or "getBalance failed").strip()
            return -1.0, msg
        candidates = [
            data.get("balance"),
            data.get("amount"),
            data.get("data"),
        ]
        for val in candidates:
            try:
                if isinstance(val, dict):
                    num = float(val.get("balance") or val.get("amount") or -1)
                else:
                    num = float(val)
            except Exception:
                continue
            if num >= 0:
                _hero_sms_update_runtime(balance=num, init_start=True)
                return num, ""

    return -1.0, line or "无法解析余额"

def _hero_sms_resolve_service_code(proxies: Any) -> str:
    global _HERO_SMS_SERVICE_CACHE

    raw = str(cfg.HERO_SMS_SERVICE).strip()
    if raw and raw.lower() not in {"auto", "openai", "chatgpt", "gpt", "codex"}:
        return raw
    if _HERO_SMS_SERVICE_CACHE:
        return _HERO_SMS_SERVICE_CACHE

    ok, _, data = _hero_sms_request(
        "getServicesList",
        proxies=proxies,
        params={"lang": "en"},
        timeout=30,
    )
    services: List[Dict[str, Any]] = []
    if ok and isinstance(data, dict):
        if isinstance(data.get("services"), list):
            services = [x for x in data.get("services") if isinstance(x, dict)]
        elif isinstance(data.get("data"), list):
            services = [x for x in data.get("data") if isinstance(x, dict)]

    selected = ""
    for item in services:
        code = str(item.get("code") or item.get("id") or "").strip()
        name = str(item.get("name") or item.get("title") or item.get("eng") or "").strip()
        low = f"{code} {name}".lower()
        if "openai" in low:
            selected = code
            break
    if not selected:
        for item in services:
            code = str(item.get("code") or item.get("id") or "").strip()
            name = str(item.get("name") or item.get("title") or item.get("eng") or "").strip()
            low = f"{code} {name}".lower()
            if any(k in low for k in ("chatgpt", "codex", "gpt")):
                selected = code
                break

    if not selected:
        selected = "dr"

    _HERO_SMS_SERVICE_CACHE = selected
    _info(f"HeroSMS 服务代码: {selected}")
    return selected


def _hero_sms_resolve_country_id(proxies: Any) -> int:
    raw = str(cfg.HERO_SMS_COUNTRY).strip()
    if not raw:
        raw = "US"
    if raw.isdigit():
        return max(0, int(raw))

    key = raw.upper()
    if key in _HERO_SMS_COUNTRY_CACHE:
        return int(_HERO_SMS_COUNTRY_CACHE[key])

    wanted_tokens = {
        key,
        key.replace(" ", ""),
    }
    if key in {"US", "USA", "UNITEDSTATES", "UNITED STATES", "AMERICA"}:
        wanted_tokens.update({"US", "USA", "UNITEDSTATES", "UNITED STATES"})

    ok, _, data = _hero_sms_request("getCountries", proxies=proxies, timeout=30)
    countries: List[Dict[str, Any]] = []
    if ok:
        countries = [x for x in _iter_countries_payload(data) if isinstance(x, dict)]

    matched = -1
    for item in countries:
        cid = item.get("id")
        try:
            cid_i = int(cid)
        except Exception:
            continue
        names = [
            str(item.get("eng") or "").strip().upper(),
            str(item.get("rus") or "").strip().upper(),
            str(item.get("chn") or "").strip().upper(),
            str(item.get("iso") or "").strip().upper(),
            str(item.get("iso2") or "").strip().upper(),
        ]
        compact = {x.replace(" ", "") for x in names if x}
        exact = {x for x in names if x}
        if wanted_tokens & exact or wanted_tokens & compact:
            matched = cid_i
            break

    if matched < 0 and key in {"US", "USA", "UNITEDSTATES", "UNITED STATES", "AMERICA"}:
        matched = 187
    if matched < 0:
        matched = 0

    _HERO_SMS_COUNTRY_CACHE[key] = matched
    _info(f"HeroSMS 国家ID: {matched} ({raw})")
    return matched


def _hero_sms_set_status(activation_id: str, status: int, proxies: Any) -> str:
    if not activation_id:
        return ""
    _, text, _ = _hero_sms_request(
        "setStatus",
        proxies=proxies,
        params={"id": activation_id, "status": int(status)},
        timeout=20,
    )
    return str(text or "")

def _hero_sms_mark_ready(activation_id: str, proxies: Any) -> None:
    if not activation_id or not _hero_sms_mark_ready_enabled():
        return
    resp = _hero_sms_set_status(activation_id, 1, proxies)
    if resp:
        low = str(resp).strip().upper()
        if low.startswith("ACCESS_") or "OK" in low:
            _info(f"HeroSMS 标记就绪")
        else:
            _warn(f"HeroSMS 返回异常（仍将尝试发码）: {resp}")
    else:
        _info("HeroSMS 已调用（无文本响应）")


def _is_hero_sms_balance_issue(reason: str) -> bool:
    low = str(reason or "").strip().lower()
    if not low:
        return False
    return "no_balance" in low or "余额不足" in low


def _is_hero_sms_timeout_issue(reason: str) -> bool:
    low = str(reason or "").strip().lower()
    if not low:
        return False
    return "接码超时" in low or "status_wait_code" in low or "timeout" in low


def _is_hero_sms_country_blocked_issue(reason: str) -> bool:
    low = str(reason or "").strip().lower()
    if not low:
        return False
    return "country_blocked" in low or "国家受限" in low


def _is_hero_sms_no_numbers_issue(reason: str) -> bool:
    low = str(reason or "").strip().lower()
    if not low:
        return False
    return "no_numbers" in low or "no numbers" in low or "no free phones" in low

def _hero_sms_get_number(
        proxies: Any,
        *,
        service_code: str = "",
        country_id: Optional[int] = None,
        provider_id: Optional[int] = None,
        provider_ids: Optional[list[int]] = None,
) -> tuple[str, str, str]:
    svc = str(service_code or "").strip() or _hero_sms_resolve_service_code(proxies)
    ctry = int(country_id) if country_id is not None else _hero_sms_resolve_country_id(proxies)
    if int(ctry) in _OPENAI_SMS_BLOCKED_COUNTRY_IDS:
        return "", "", f"COUNTRY_BLOCKED: 国家ID {ctry} 不支持 OpenAI 注册"
    min_balance = _hero_sms_min_balance_limit()
    _info(f"HeroSMS 取号参数: service={svc}, country={ctry}")

    balance_now, balance_err = hero_sms_get_balance(proxies)
    if balance_now >= 0:
        _info(
            "HeroSMS 当前余额: "
            f"${balance_now:.2f}（低于 ${min_balance:.2f} 不取号）"
        )
        if balance_now < min_balance:
            return "", "", f"NO_BALANCE: 当前余额 ${balance_now:.2f} < 下限 ${min_balance:.2f}"
    elif balance_err:
        _warn(f"HeroSMS 余额查询失败: {balance_err}")

    params: Dict[str, Any] = {
        "service": svc,
        "country": ctry,
    }
    max_px = _hero_sms_order_max_price()
    min_px = _hero_sms_order_min_price()
    provider_ids_clean = _parse_int_list(provider_ids or [])
    if not provider_ids_clean and provider_id is not None:
        try:
            provider_ids_clean = [int(provider_id)]
        except Exception:
            provider_ids_clean = []
    if max_px > 0:
        params["maxPrice"] = max_px
        _info(f"HeroSMS 取号最高价格：{max_px}")
    if min_px > 0:
        params["minPrice"] = min_px
    if provider_ids_clean:
        params["providerIds"] = ",".join(str(x) for x in provider_ids_clean)

    def _parse_number_response(text: str, data: Any) -> tuple[str, str]:
        line = str(text or "").strip()
        if line.upper().startswith("ACCESS_NUMBER:"):
            parts = line.split(":", 2)
            if len(parts) >= 3:
                activation_id = str(parts[1] or "").strip()
                phone_raw = str(parts[2] or "").strip()
                if activation_id and phone_raw:
                    phone = phone_raw if phone_raw.startswith("+") else f"+{phone_raw}"
                    return activation_id, phone
        if isinstance(data, dict):
            activation_id = str(
                data.get("activationId")
                or data.get("activation_id")
                or data.get("id")
                or ""
            ).strip()
            phone_raw = str(
                data.get("phoneNumber")
                or data.get("phone")
                or data.get("number")
                or ""
            ).strip()
            if activation_id and phone_raw:
                phone = phone_raw if phone_raw.startswith("+") else f"+{phone_raw}"
                return activation_id, phone
        return "", ""

    use_v2_first = "smsbower." in str(_hero_sms_base_url()).lower()
    request_order = ["getNumberV2", "getNumber"] if use_v2_first else ["getNumber", "getNumberV2"]
    last_error = "NO_NUMBERS"

    for action in request_order:
        ok, text, data = _hero_sms_request(action, proxies=proxies, params=params, timeout=30)
        if not ok:
            last_error = str(text or f"{action} failed")
            continue

        activation_id, phone = _parse_number_response(text, data)
        if activation_id and phone:
            return activation_id, phone, ""

        line = str(text or "").strip()
        if line:
            last_error = line
        elif isinstance(data, dict):
            last_error = str(data.get("message") or data.get("error") or last_error)

    return "", "", last_error


def _hero_sms_poll_code(activation_id: str, proxies: Any) -> str:
    if not activation_id:
        return ""
    timeout_sec = _hero_sms_poll_timeout_sec()
    interval_sec = 3.0
    progress_sec = 8
    resend_after_sec = 24

    started_at = time.time()
    next_progress_at = float(progress_sec)
    resent_once = False
    last_status = ""

    # 状态汉化映射表
    status_map = {
        "STATUS_WAIT_CODE": "⏳ 等待验证码中...",
        "STATUS_WAIT_RETRY": "🔄 正在尝试重新获取...",
        "STATUS_WAIT_RESEND": "📩 等待短信重发...",
        "STATUS_CANCEL": "❌ 任务已取消",
        "NO_ACTIVATION": "❓ 无效的激活ID",
        "BAD_STATUS": "⚠️ 状态异常",
        "STATUS_OK": "✅ 成功获取验证码",
        "ACCESS_RETRY_GET": "🔁 已请求重发指令"
    }

    _info(f"HeroSMS 开始等码: 激活ID={activation_id}, 预计最长等待 {timeout_sec}s")

    def _try_resend(reason: str) -> None:
        nonlocal resent_once
        if resent_once or resend_after_sec <= 0:
            return
        # 调用 setStatus(3) 触发重发
        resend_resp = _hero_sms_set_status(activation_id, 3, proxies)
        resent_once = True
        msg = status_map.get(str(resend_resp).strip().upper(), resend_resp)
        _info(f"HeroSMS 触发补救机制({reason}): {msg}")

    while time.time() - started_at < timeout_sec:
        _raise_if_stopped()
        ok, text, data = _hero_sms_request(
            "getStatus",
            proxies=proxies,
            params={"id": activation_id},
            timeout=20,
        )
        line = str(text or "").strip()
        upper = line.upper()

        raw_tag = ""
        if upper:
            raw_tag = upper.split(":", 1)[0].strip() if ":" in upper else upper

        if not raw_tag and isinstance(data, dict):
            raw_tag = str(data.get("status") or data.get("title") or "").strip().upper()

        if raw_tag and raw_tag != last_status:
            last_status = raw_tag
            cn_status = status_map.get(raw_tag, raw_tag)
            _info(f"HeroSMS 实时状态: {cn_status}")

        nested_code = _hero_sms_extract_code_payload(data)
        if nested_code:
            _info(f"🎉 成功匹配验证码: {nested_code}")
            return nested_code

        if ok and upper.startswith("STATUS_OK"):
            code = line.split(":", 1)[1].strip() if ":" in line else ""
            if not code and isinstance(data, dict):
                sms_obj = data.get("sms") if isinstance(data.get("sms"), dict) else {}
                code = str(sms_obj.get("code") or data.get("code") or "").strip()
            if code:
                _info(f"🎉 成功匹配验证码: {code}")
                return code

        if raw_tag in {"STATUS_WAIT_RETRY", "STATUS_WAIT_RESEND"}:
            _try_resend("平台请求重试")

        if raw_tag in {"STATUS_CANCEL", "NO_ACTIVATION", "BAD_STATUS"}:
            _warn(f"HeroSMS 等码终止: {status_map.get(raw_tag, raw_tag)}")
            return ""

        elapsed = time.time() - started_at

        if (not resent_once) and resend_after_sec > 0 and elapsed >= float(resend_after_sec):
            _try_resend("超时被动重发")

        if elapsed >= next_progress_at:
            left = max(0, int(timeout_sec - elapsed))
            _info(f"HeroSMS 努力等码中... 已耗时 {int(elapsed)}s，剩余约 {left}s")
            next_progress_at += float(progress_sec)

        if _sleep_interruptible(interval_sec):
            raise UserStoppedError("stopped")

    _warn(f"HeroSMS 等码最终超时，共等待 {timeout_sec}s 未收到短信")
    return ""

def _try_verify_phone_via_hero_sms(
        session: requests.Session,
        *,
        proxies: Any,
        hint_url: str = "",
        device_id: str = "",
        user_agent: str = "",
        run_ctx: dict = None,
        proxy: Optional[str] = None,
) -> tuple[bool, str]:
    if not _hero_sms_enabled():
        return False, "HeroSMS 未配置 API Key 或HeroSMS主开关未开启，如果不想花钱接码请忽略该条提示"

    max_tries = _hero_sms_max_tries()
    last_reason = "HeroSMS 手机验证失败"
    lock_acquired = False

    serial_on = True
    wait_sec = 180
    verify_balance_start = -1.0

    if serial_on:
        _info("等待 HeroSMS 手机验证锁...")
        started = time.time()
        while True:
            _raise_if_stopped()
            if _HERO_SMS_VERIFY_LOCK.acquire(timeout=0.5):
                lock_acquired = True
                break
            if time.time() - started >= wait_sec:
                return False, "HeroSMS 手机验证排队超时"

    def _verify_once(
            activation_id: str,
            phone_number: str,
            *,
            source: str,
            close_on_success: bool,
            cancel_on_fail: bool,
    ) -> tuple[bool, str, str]:
        finished = False
        fail_reason = ""
        try:
            send_headers: Dict[str, str] = {
                "referer": "https://auth.openai.com/add-phone",
                "accept": "application/json",
                "content-type": "application/json",
            }

            send_sentinel = generate_payload(
                did=device_id,
                flow="authorize_continue",
                proxy=proxy,
                user_agent=user_agent,
                impersonate="chrome110",
                ctx=run_ctx
            )

            if send_sentinel:
                send_headers["openai-sentinel-token"] = send_sentinel

            _hero_sms_mark_ready(activation_id, proxies)

            send_resp = _post_with_retry(
                session,
                "https://auth.openai.com/api/accounts/add-phone/send",
                headers=send_headers,
                json_body={"phone_number": phone_number},
                proxies=proxies,
                timeout=30,
                retries=1,
            )
            if send_resp.status_code == 200:
                _info(f"{source} 发送成功")
                try:
                    sj = send_resp.json()
                except Exception:
                    sj = None
                if isinstance(sj, dict):
                    if sj.get("success") is False:
                        _warn(
                            f"{source} 业务失败: "
                            f"{str(sj.get('message') or sj.get('error') or sj)[:280]}"
                        )
                    err_v = sj.get("error")
                    if err_v and sj.get("success") is not False:
                        _warn(f"{source} add-phone/send 返回含 error 字段: {str(err_v)[:240]}")
            if send_resp.status_code != 200:
                fail_reason = f"发送手机验证码失败: HTTP {send_resp.status_code}"
                _warn(f"{source} {fail_reason} | {str(send_resp.text or '')[:240]}")
                return False, "", fail_reason

            sms_code = _hero_sms_poll_code(activation_id, proxies)
            if not sms_code:
                fail_reason = "接码超时，未收到手机验证码"
                _warn(f"{source} {fail_reason}")
                return False, "", fail_reason
            _info(f"{source} HeroSMS 收到手机验证码: {sms_code}")

            verify_headers: Dict[str, str] = {
                "referer": "https://auth.openai.com/phone-verification",
                "accept": "application/json",
                "content-type": "application/json",
            }
            verify_sentinel = generate_payload(
                did=device_id,
                flow="authorize_continue",
                proxy=proxy,
                user_agent=user_agent,
                impersonate="chrome110",
                ctx=run_ctx
            )

            if verify_sentinel:
                verify_headers["openai-sentinel-token"] = verify_sentinel

            verify_resp = _post_with_retry(
                session,
                "https://auth.openai.com/api/accounts/phone-otp/validate",
                headers=verify_headers,
                json_body={"code": sms_code},
                proxies=proxies,
                timeout=30,
                retries=1,
            )
            _info(f"{source} phone-otp/validate HTTP {verify_resp.status_code}")
            if verify_resp.status_code != 200:
                fail_reason = f"手机验证码校验失败: HTTP {verify_resp.status_code}"
                _warn(f"{source} {fail_reason} | {str(verify_resp.text or '')[:240]}")
                return False, "", fail_reason

            if close_on_success:
                _hero_sms_set_status(activation_id, 6, proxies)
            else:
                keep_resp = _hero_sms_set_status(activation_id, 3, proxies)
                if keep_resp:
                    _info(f"{source} 复用保持激活: {keep_resp}")
            finished = True

            try:
                vj = verify_resp.json() or {}
            except Exception:
                vj = {}
            next_url = _extract_next_url(vj).strip() or str(vj.get("continue_url") or "").strip()
            if next_url and not next_url.startswith("http"):
                next_url = (
                    f"https://auth.openai.com{next_url}"
                    if next_url.startswith("/")
                    else next_url
                )
            if next_url:
                try:
                    _, follow_url = _follow_redirect_chain(session, next_url, proxies)
                    if follow_url:
                        next_url = follow_url
                except UserStoppedError:
                    raise
                except Exception:
                    pass
            if not next_url:
                next_url = str(hint_url or "").strip()
            return True, next_url, ""
        except UserStoppedError:
            raise
        except Exception as e:
            fail_reason = f"手机验证异常: {e}"
            _warn(f"{source} {fail_reason}")
            return False, "", fail_reason
        finally:
            if (not finished) and cancel_on_fail:
                _hero_sms_set_status(activation_id, 8, proxies)

    try:
        verify_balance_start, _ = hero_sms_get_balance(proxies)
        if verify_balance_start >= 0:
            _hero_sms_update_runtime(balance=verify_balance_start, init_start=True)

        service_code = _hero_sms_resolve_service_code(proxies)
        preferred_country_id = _hero_sms_resolve_country_id(proxies)
        _info(
            "HeroSMS 国家策略: "
            f"超时阈值：{_hero_sms_country_timeout_limit()}次, "
            f"冷却：{_hero_sms_country_cooldown_sec()}s"
        )
        country_id = _hero_sms_pick_country_id(
            proxies,
            service_code=service_code,
            preferred_country=preferred_country_id,
        )
        excluded_country_ids: set[int] = set()
        if country_id != preferred_country_id:
            _warn(f"HeroSMS 国家自动切换: {preferred_country_id} -> {country_id}")

        reuse_on = _hero_sms_reuse_enabled()
        multi_selector_on = _hero_sms_multi_selector_enabled()
        multi_candidates: list[dict[str, Any]] = []
        if multi_selector_on:
            multi_candidates = _hero_sms_build_number_candidates(
                proxies,
                service_code=service_code,
                preferred_country=country_id,
            )
            if not multi_candidates:
                _warn("HeroSMS 多国家/供应商策略未命中有效候选，将回退默认国家逻辑")

        candidate_pool = list(multi_candidates) if multi_candidates else [{"country": country_id, "provider_ids": []}]

        for candidate in candidate_pool:
            _raise_if_stopped()
            current_country_id = int(candidate.get("country") or country_id)
            current_provider_ids = _parse_int_list(candidate.get("provider_ids") or [])

            if current_provider_ids:
                _info(
                    "HeroSMS 候选国家: "
                    f"{_hero_sms_country_label(current_country_id, proxies)}, "
                    f"供应商优先={current_provider_ids}"
                )
            else:
                _info(f"HeroSMS 候选国家: {_hero_sms_country_label(current_country_id, proxies)}")

            for attempt in range(1, max_tries + 1):
                _raise_if_stopped()
                get_number_kwargs: Dict[str, Any] = {
                    "service_code": service_code,
                    "country_id": current_country_id,
                }
                if current_provider_ids:
                    get_number_kwargs["provider_ids"] = current_provider_ids

                reused_this_candidate = False
                if reuse_on:
                    reuse_id, reuse_phone, reuse_used = _hero_sms_reuse_claim(
                        service_code,
                        current_country_id,
                        current_provider_ids,
                    )
                    if reuse_id and reuse_phone:
                        reused_this_candidate = True
                        _info(
                            "HeroSMS 复用命中: "
                            f"国家={_hero_sms_country_label(current_country_id, proxies)}, "
                            f"号码={reuse_phone}, uses={reuse_used}"
                        )
                        ok_reuse, next_reuse, reason_reuse = _verify_once(
                            reuse_id,
                            reuse_phone,
                            source="复用号码",
                            close_on_success=False,
                            cancel_on_fail=False,
                        )
                        if ok_reuse:
                            _hero_sms_country_mark_success(current_country_id)
                            _hero_sms_country_record_result(current_country_id, True, "reuse_success")
                            _hero_sms_reuse_release(reuse_id, increase=True, result="success", keep=True)
                            return True, next_reuse

                        last_reason = reason_reuse or "复用手机号失败"
                        _hero_sms_country_record_result(current_country_id, False, last_reason)
                        if _is_hero_sms_timeout_issue(last_reason):
                            _hero_sms_set_status(reuse_id, 8, proxies)
                            _hero_sms_reuse_release(reuse_id, result="timeout", keep=False)
                        else:
                            _hero_sms_set_status(reuse_id, 8, proxies)
                            _hero_sms_reuse_release(reuse_id, result="failed", keep=False)
                        _warn(f"复用号码失败，回退新购: {last_reason}")

                activation_id, phone_number, get_err = _hero_sms_get_number(
                    proxies,
                    **get_number_kwargs,
                )
                if not activation_id or not phone_number:
                    last_reason = f"取号失败: {get_err or 'NO_NUMBERS'}"
                    _warn(
                        f"HeroSMS 第 {attempt}/{max_tries} 次取号失败"
                        f"{'，在复用池失败后继续新购' if reused_this_candidate else ''}: {get_err or 'NO_NUMBERS'}"
                    )
                    if _is_hero_sms_country_blocked_issue(get_err):
                        break
                    if (
                            attempt < max_tries
                            and _hero_sms_auto_pick_country()
                            and _is_hero_sms_no_numbers_issue(get_err)
                    ):
                        excluded_country_ids.add(int(current_country_id))
                        next_country = _hero_sms_pick_country_id(
                            proxies,
                            service_code=service_code,
                            preferred_country=preferred_country_id,
                            exclude_country_ids=excluded_country_ids,
                            force_refresh=True,
                        )
                        if next_country != current_country_id:
                            _warn(f"当前国家无号，自动重选国家: {current_country_id} -> {next_country}")
                            current_country_id = next_country
                            if _sleep_interruptible(0.3):
                                raise UserStoppedError("stopped")
                            continue
                    if _sleep_interruptible(1.2):
                        raise UserStoppedError("stopped")
                    continue

                _info(f"HeroSMS 取号成功: 第 {attempt}/{max_tries} 次, 号码：{phone_number}")
                ok_new, next_new, reason_new = _verify_once(
                    activation_id,
                    phone_number,
                    source=f"新购号码#{attempt}",
                    close_on_success=(not reuse_on),
                    cancel_on_fail=(not reuse_on),
                )
                if ok_new:
                    _hero_sms_country_mark_success(current_country_id)
                    _hero_sms_country_record_result(current_country_id, True, "new_success")
                    if reuse_on:
                        _hero_sms_reuse_add(
                            activation_id,
                            phone_number,
                            service_code,
                            current_country_id,
                            current_provider_ids,
                            result="new_success",
                        )
                        _hero_sms_reuse_release(activation_id, increase=True, result="success", keep=True)
                    return True, next_new

                last_reason = reason_new or "手机验证失败"
                _hero_sms_country_record_result(current_country_id, False, last_reason)
                if _is_hero_sms_timeout_issue(last_reason):
                    switched = _hero_sms_country_mark_timeout(current_country_id)
                    _hero_sms_set_status(activation_id, 8, proxies)
                    if switched:
                        _warn(f"国家 {current_country_id} 连续超时已进入冷却，将在后续尝试其他候选")
                    if _sleep_interruptible(0.8):
                        raise UserStoppedError("stopped")
                    continue
                if reuse_on:
                    _hero_sms_set_status(activation_id, 8, proxies)
                    _hero_sms_reuse_release(activation_id, result="failed", keep=False)

        return False, last_reason
    finally:
        try:
            verify_balance_end, _ = hero_sms_get_balance(proxies)
            if verify_balance_end >= 0:
                spent_delta = 0.0
                if verify_balance_start >= 0:
                    spent_delta = max(0.0, verify_balance_start - verify_balance_end)
                _hero_sms_update_runtime(
                    spent_delta=spent_delta,
                    balance=verify_balance_end,
                    init_start=True,
                )
        except Exception:
            pass
        if lock_acquired:
            try:
                _HERO_SMS_VERIFY_LOCK.release()
            except Exception:
                pass
