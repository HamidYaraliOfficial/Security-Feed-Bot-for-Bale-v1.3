# -*- coding: utf-8 -*-
"""Config file handling (data/config.json)."""
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

from .sources import CATEGORIES, CATEGORY_EMOJI, SOURCES, SOURCES_BY_ID

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BALEBOT_DATA") or (BASE_DIR / "data"))
CONFIG_PATH = DATA_DIR / "config.json"
_lock = threading.Lock()

DEFAULTS = {
    "bale_token": "",
    "channel_id": "",
    "bale_api_base": "https://tapi.bale.ai",
    "interval_minutes": 15,
    "delay_between_posts_sec": 4,
    "max_age_hours": 24,
    "summary_chars": 350,
    "send_images": True,
    "full_text": True,
    "max_images": 0,
    "skip_backlog_first_run": False,
    "selected_sources": [s["id"] for s in SOURCES if s["default"]],
    "custom_feeds": [],
    "custom_categories": [],
    "translate_enabled": True,
    "translate_provider": "edge",
    "translate_show_original": True,
    "translate_body": True,
    "translate_original_body": True,
    "translate_chunk_chars": 2500,
    "translate_delay_sec": 1.2,
    "translate_give_up_min": 30,
    "lingva_url": "https://lingva.ml, https://lingva.garudalinux.org, https://translate.plausibility.cloud",
    "google_api_key": "",
    "libretranslate_url": "",
    "libretranslate_key": "",
    "mymemory_email": "",
    "include_keywords": [],
    "exclude_keywords": [],
    "fetch_proxy": "",
    "bale_proxy": "",
    "seen_retention_days": 30,
    "auto_start": True,
    "panel_port": 8765,
}


def _clamp(v, lo, hi, default):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _as_list(v):
    if isinstance(v, str):
        v = re.split(r"[\n,،;]+", v)
    out = []
    for x in v or []:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def normalize_channel(v):
    """Accepts @id, id, numeric id, or a ble.ir link and returns what the API wants."""
    v = (v or "").strip()
    if not v:
        return ""
    if "/join/" in v:            # invite link of a private channel: not usable as chat_id
        return v
    m = re.search(r"(?:ble\.ir|bale\.ai)/@?([A-Za-z0-9_]{3,})", v)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"-?\d+", v):
        return v
    return v if v.startswith("@") else "@" + v


def normalize_proxy(v):
    v = (v or "").strip()
    if not v:
        return ""
    if not re.match(r"^(https?|socks4a?|socks5h?)://", v, re.I):
        v = "http://" + v
    return v


_CAT_ID_RE = re.compile(r"^c-[a-z0-9]{4,12}$")


def parse_custom_categories(v):
    """List of {id, name, emoji}. Ids look like 'c-ab12cd'; they are generated when missing."""
    out, names = [], set()
    for row in v or []:
        if not isinstance(row, dict):
            continue
        name = re.sub(r"\s+", " ", str(row.get("name", ""))).strip()[:40]
        if not name or name.lower() in names:
            continue
        names.add(name.lower())
        emoji = str(row.get("emoji", "") or "").strip()[:8] or "📌"
        cid = str(row.get("id", "") or "")
        if not _CAT_ID_RE.match(cid):
            cid = "c-" + hashlib.sha1(name.lower().encode()).hexdigest()[:6]
        if any(c["id"] == cid for c in out):
            continue
        out.append({"id": cid, "name": name, "emoji": emoji})
    return out


def parse_custom_feeds(v, valid_categories=None):
    """Accepts a list of dicts {name,url,category} or text lines 'name | url' (or just 'url')."""
    valid = set(valid_categories or CATEGORIES)
    items = []
    if isinstance(v, str):
        v = v.splitlines()
    for row in v or []:
        if isinstance(row, dict):
            name, url = str(row.get("name", "")).strip(), str(row.get("url", "")).strip()
            cat = str(row.get("category", "") or "custom")
        else:
            row = str(row).strip()
            if not row:
                continue
            if "|" in row:
                name, url = [p.strip() for p in row.split("|", 1)]
            else:
                name, url = "", row
            cat = "custom"
        if not re.match(r"^https?://", url, re.I):
            continue
        host = urlparse(url).netloc or url
        items.append({
            "id": "custom-" + hashlib.sha1(url.encode()).hexdigest()[:8],
            "name": (name or host)[:80],
            "url": url,
            "category": cat if cat in valid else "custom",
        })
    seen, out = set(), []
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"])
            out.append(it)
    return out


def category_info(cfg):
    """id -> {name, emoji} for built-in and user-defined categories."""
    info = {k: {"name": CATEGORIES[k], "emoji": CATEGORY_EMOJI[k]} for k in CATEGORIES}
    for c in cfg.get("custom_categories", []):
        info[c["id"]] = {"name": c["name"], "emoji": c["emoji"]}
    return info


def sanitize(cfg):
    d = dict(DEFAULTS)
    d.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
    out = {}
    out["bale_token"] = str(d["bale_token"] or "").strip()
    out["channel_id"] = normalize_channel(d["channel_id"])
    base = str(d["bale_api_base"] or "").strip().rstrip("/")
    out["bale_api_base"] = base if re.match(r"^https?://", base, re.I) else DEFAULTS["bale_api_base"]
    out["interval_minutes"] = _clamp(d["interval_minutes"], 1, 1440, 15)
    out["delay_between_posts_sec"] = _clamp(d["delay_between_posts_sec"], 1, 120, 4)
    out["max_age_hours"] = _clamp(d["max_age_hours"], 1, 720, 24)
    out["summary_chars"] = _clamp(d["summary_chars"], 0, 800, 350)
    out["send_images"] = bool(d["send_images"])
    out["full_text"] = bool(d["full_text"])
    out["max_images"] = _clamp(d["max_images"], 0, 500, 0)
    out["skip_backlog_first_run"] = bool(d["skip_backlog_first_run"])
    out["selected_sources"] = [i for i in _as_list(d["selected_sources"]) if i in SOURCES_BY_ID]
    out["custom_categories"] = parse_custom_categories(d["custom_categories"])
    valid_cats = set(CATEGORIES) | {c["id"] for c in out["custom_categories"]}
    out["custom_feeds"] = parse_custom_feeds(d["custom_feeds"], valid_cats)
    out["include_keywords"] = [k.lower() for k in _as_list(d["include_keywords"])]
    out["exclude_keywords"] = [k.lower() for k in _as_list(d["exclude_keywords"])]
    out["fetch_proxy"] = normalize_proxy(d["fetch_proxy"])
    out["bale_proxy"] = normalize_proxy(d["bale_proxy"])
    out["seen_retention_days"] = _clamp(d["seen_retention_days"], 3, 365, 30)
    out["translate_enabled"] = bool(d["translate_enabled"])
    out["translate_provider"] = d["translate_provider"] if d["translate_provider"] in (
        "edge", "google", "lingva", "mymemory", "libretranslate", "google_cloud") else "edge"
    out["translate_chunk_chars"] = _clamp(d["translate_chunk_chars"], 300, 3000, 2500)
    out["translate_give_up_min"] = _clamp(d["translate_give_up_min"], 0, 1440, 30)
    try:
        out["translate_delay_sec"] = max(0.0, min(30.0, float(d["translate_delay_sec"])))
    except (TypeError, ValueError):
        out["translate_delay_sec"] = 1.2
    urls = [u.rstrip("/") for u in re.split(r"[,\s]+", str(d["lingva_url"] or "")) if re.match(r"^https?://", u, re.I)]
    out["lingva_url"] = ", ".join(urls) if urls else DEFAULTS["lingva_url"]
    out["google_api_key"] = str(d["google_api_key"] or "").strip()
    out["translate_show_original"] = bool(d["translate_show_original"])
    out["translate_body"] = bool(d["translate_body"])
    out["translate_original_body"] = bool(d["translate_original_body"])
    lt = str(d["libretranslate_url"] or "").strip().rstrip("/")
    out["libretranslate_url"] = lt if re.match(r"^https?://", lt, re.I) else ""
    out["libretranslate_key"] = str(d["libretranslate_key"] or "").strip()
    out["mymemory_email"] = str(d["mymemory_email"] or "").strip()
    out["auto_start"] = bool(d["auto_start"])
    out["panel_port"] = _clamp(d["panel_port"], 1024, 65535, 8765)
    return out


def load():
    with _lock:
        raw = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
        return sanitize(raw)


def save(cfg):
    cfg = sanitize(cfg)
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, CONFIG_PATH)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return cfg


def mask_token(token):
    if not token:
        return ""
    return token[:4] + "…" + token[-4:] if len(token) > 12 else "••••••"


def active_sources(cfg):
    chosen = [SOURCES_BY_ID[i] for i in cfg["selected_sources"] if i in SOURCES_BY_ID]
    return chosen + list(cfg["custom_feeds"])
