# -*- coding: utf-8 -*-
"""Background engine.

  * every `interval_minutes` the feeds are checked and new items go into a queue (feeds that keep failing are
    retried with a growing pause, so a few dead sites do not slow everything down)
  * the queue is processed CONTINUOUSLY (no per-cycle limit): build the post -> translate it piece by piece ->
    send header, full original text (+photos) and full Persian text -> delete it from the queue
  * a post whose translation is temporarily blocked is put aside for a while and the next posts continue;
    the worker comes back to it automatically (and never idles for the whole interval while the queue is not empty)
"""
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import composer, config, fetcher
from .bale import BaleClient, BaleError, explain, is_fatal
from .db import Store
from .translate import Translator

MAX_QUEUE = 200          # safety cap (posts are kept with full text until sent); oldest overflow is dropped
MAX_TRIES = 3            # after 3 failed attempts a post is dropped (so the DB never clogs)
PART_DELAY = 1.2         # seconds between the messages of one post
TR_MAX_WAIT = 12         # seconds one translation call may wait for a provider before the post is put aside
FEED_WORKERS = 4         # feeds downloaded in parallel


class TranslationDeferred(Exception):
    """Translation is temporarily impossible; the post resumes from the same piece a bit later."""
    def __init__(self, msg, retry_in=60):
        super().__init__(msg)
        self.retry_in = retry_in


class Engine:
    def __init__(self):
        self.store = Store(config.DATA_DIR / "bot.db")
        self.logs = deque(maxlen=400)
        self.stop_evt = threading.Event()
        self.wake_evt = threading.Event()
        self.thread = None
        self.cache = {}                 # ETag / Last-Modified of feeds
        self.feed_state = {}            # url -> {fails, next, err, name, id}
        self.skip_until = {}            # queue row id -> time before which it is not retried
        self.tr_blocked_until = 0.0     # translation providers all paused until this time
        self.force_ingest = False
        self._tr = None
        self._tr_key = None
        self._token = ""
        self._secret2 = ""
        self.status = {"running": False, "state": "متوقف", "last_cycle": None, "next_cycle": None,
                       "last_summary": "", "last_error": "", "feeds": {"total": 0, "failed": []}}

    # ------------------------------------------------------------ utilities
    def log(self, msg, level="info"):
        for secret in (self._token, self._secret2):
            if secret:
                msg = msg.replace(secret, "***")
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append({"t": stamp, "level": level, "msg": msg})
        try:
            print(f"[{stamp}] {level.upper():5} {msg}", flush=True)
        except Exception:
            pass

    def snapshot(self):
        st = dict(self.status)
        st.update(self.store.stats())
        st["sent_total"] = int(self.store.get_meta("sent_total", 0) or 0)
        tr = self._tr
        st["tr"] = {"enabled": tr is not None,
                    "blocked_until": self.tr_blocked_until if self.tr_blocked_until > time.time() else None,
                    "provider": tr.last_provider if tr else "",
                    "errors": list(tr.errors.values())[:4] if tr else []}
        return st

    def translator(self, cfg):
        if not cfg["translate_enabled"]:
            return None
        key = (cfg["translate_provider"], cfg["libretranslate_url"], cfg["libretranslate_key"], cfg["mymemory_email"],
               cfg["fetch_proxy"], cfg["lingva_url"], cfg["google_api_key"], cfg["translate_delay_sec"])
        if key != self._tr_key:
            self._tr, self._tr_key = Translator(cfg), key
            self._tr.sleep = self.stop_evt.wait          # pressing Stop interrupts any waiting
            self._tr.stopped = self.stop_evt.is_set
            self.tr_blocked_until = 0.0
        return self._tr

    def _remember_secrets(self, cfg):
        self._token = cfg["bale_token"]
        self._secret2 = cfg["google_api_key"] or cfg["libretranslate_key"]

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self.thread and self.thread.is_alive():
            return False
        self.stop_evt.clear()
        self.wake_evt.clear()
        self.thread = threading.Thread(target=self._loop, name="engine", daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stop_evt.set()
        self.wake_evt.set()
        if self.thread:
            self.thread.join(timeout=10)
        self.status.update(running=False, state="متوقف", next_cycle=None)
        self.log("ربات متوقف شد.")

    def run_now(self):
        """Check all feeds right now (ignoring the pause of failing feeds) and keep draining the queue."""
        self.force_ingest = True
        self.skip_until.clear()
        self.tr_blocked_until = 0.0
        self.wake_evt.set()

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def _loop(self):
        self.status.update(running=True, state="در حال اجرا")
        self.log("ربات شروع به کار کرد.")
        next_ingest, warned = 0.0, False
        while not self.stop_evt.is_set():
            cfg = config.load()
            self._remember_secrets(cfg)
            if not cfg["bale_token"] or not cfg["channel_id"]:
                self.status.update(state="تنظیمات ناقص (توکن یا کانال)", last_error="توکن یا آیدی کانال وارد نشده است.")
                if not warned:
                    self.log("توکن یا آیدی کانال وارد نشده؛ منتظر تنظیمات می‌مانم.", "warn")
                    warned = True
                self.wake_evt.clear()
                self.wake_evt.wait(15)
                continue
            warned = False
            if self.force_ingest or time.time() >= next_ingest:
                forced = self.force_ingest or next_ingest == 0          # 'run now' / first run ignore the failing-feed pause
                self.force_ingest = False
                try:
                    self.ingest_phase(cfg, force=forced)
                except Exception as e:
                    self.log(f"خطا در دریافت منابع: {type(e).__name__}: {e}", "error")
                next_ingest = time.time() + cfg["interval_minutes"] * 60
                self.status.update(last_cycle=time.time(), next_cycle=next_ingest)
            try:
                retry_in = self.drain(cfg)
            except Exception as e:                       # never let the loop die
                self.log(f"خطای غیرمنتظره: {type(e).__name__}: {e}", "error")
                retry_in = 30
            if self.stop_evt.is_set():
                break
            now = time.time()
            pending = self.store.stats()["queue"]
            if pending == 0:
                wait = next_ingest - now
                self.status["state"] = "در حال اجرا (صف خالی؛ منتظر بررسی بعدی منابع)"
            else:
                wait = min(max(retry_in, 5), max(1.0, next_ingest - now))
                self.status["state"] = f"در حال اجرا — {pending} خبر در صف؛ تلاش بعدی تا {int(wait)} ثانیه دیگر"
            self.wake_evt.clear()
            if self.force_ingest:                        # 'run now' arrived while we were busy: do not sleep
                continue
            self.wake_evt.wait(max(1.0, wait))
        self.status.update(running=False, state="متوقف", next_cycle=None)

    def run_cycle(self):
        """One full pass: check every feed, then drain the queue (used by tests and manual runs)."""
        cfg = config.load()
        self._remember_secrets(cfg)
        self.ingest_phase(cfg, force=True)
        return self.drain(cfg)

    # ------------------------------------------------------------ feeds -> queue
    def ingest_phase(self, cfg, force=False):
        self.status["state"] = "در حال دریافت منابع…"
        added, due, failed = self.ingest(cfg, force)
        pruned = self.store.prune(cfg["seen_retention_days"], MAX_QUEUE)
        if pruned:
            self.log(f"{pruned} مورد قدیمی از صف حذف شد (سقف صف {MAX_QUEUE}).", "warn")
        self.status["last_summary"] = f"{added} مطلب جدید؛ {due - failed} منبع سالم، {failed} ناموفق"
        self.log(f"بررسی منابع: {added} مطلب جدید در صف | {due - failed} منبع سالم، {failed} ناموفق (از {due} منبع بررسی‌شده).")

    def ingest(self, cfg, force=False):
        sources = config.active_sources(cfg)
        if not sources:
            self.log("هیچ منبعی انتخاب نشده است.", "warn")
            self.status["feeds"] = {"total": 0, "failed": []}
            return 0, 0, 0
        now = time.time()
        interval = cfg["interval_minutes"] * 60
        first_run = self.store.get_meta("initialized") != "1"
        skip = first_run and cfg["skip_backlog_first_run"]
        cutoff = now - cfg["max_age_hours"] * 3600
        due = [s for s in sources if force or self.feed_state.get(s["url"], {}).get("next", 0) <= now]

        def work(src):
            try:
                content = fetcher.fetch_feed(fetcher.new_session(cfg["fetch_proxy"]), src["url"], self.cache)
                return src, ([] if content is None else fetcher.parse_feed(content, src)), None
            except Exception as e:
                return src, [], fetcher.describe_error(e)

        with ThreadPoolExecutor(max_workers=FEED_WORKERS) as pool:
            results = list(pool.map(work, due))

        added, failures = 0, []
        for src, items, err in results:
            st = self.feed_state.setdefault(src["url"], {"fails": 0})
            st.update(name=src["name"], id=src["id"])
            if err:
                st["fails"] += 1
                st["err"] = err
                st["next"] = now + min(interval * 2 ** (st["fails"] - 1), 6 * 3600)     # retry later and later
                failures.append(f"{src['name']} ({err})")
                continue
            st.update(fails=0, err="", next=0)
            for it in items:
                if it["published_ts"] < cutoff or self.store.is_known(it["uid"]):
                    continue
                if not fetcher.passes_filters(it, cfg["include_keywords"], cfg["exclude_keywords"]):
                    continue
                if skip:
                    self.store.mark_seen(it["uid"])
                elif self.store.add_pending(it):
                    added += 1
        if first_run:
            self.store.set_meta("initialized", "1")
            if skip:
                self.log("اولین اجرا: مطالب موجود فعلی نادیده گرفته شد؛ فقط مطالب جدید ارسال می‌شود.")
        if failures:
            shown = "، ".join(failures[:6]) + (f" و {len(failures) - 6} مورد دیگر" if len(failures) > 6 else "")
            self.log(f"منابع ناموفق: {shown}. این منابع با فاصلهٔ فزاینده دوباره امتحان می‌شوند.", "warn")
            if len(failures) * 2 > len(due) >= 4:
                self.log("بیشتر منابع از شبکهٔ شما باز نمی‌شوند؛ احتمالاً به پروکسی/VPN نیاز دارید "
                         "(تب «پیشرفته» ← پروکسی دریافت منابع) یا منابع ناموفق را غیرفعال کنید.", "warn")
        active_urls = {s["url"] for s in sources}
        self.feed_state = {u: v for u, v in self.feed_state.items() if u in active_urls}
        self.status["feeds"] = {"total": len(sources), "failed": [
            {"id": v.get("id"), "name": v.get("name"), "error": v.get("err"), "fails": v["fails"]}
            for v in self.feed_state.values() if v["fails"] > 0]}
        return added, len(due), len(failures)

    # ------------------------------------------------------------ queue -> channel
    def drain(self, cfg):
        """Send everything that can be sent, continuously, until the queue is empty or only blocked posts remain.
        Returns the number of seconds after which it makes sense to try again (0 = queue empty)."""
        if not self.store.stats()["queue"]:
            return 0
        client = BaleClient(cfg["bale_token"], cfg["bale_api_base"], cfg["bale_proxy"])
        session = fetcher.new_session(cfg["fetch_proxy"])
        sent, deferred, reason = 0, 0, ""
        while not self.stop_evt.is_set():
            now = time.time()
            rows = [r for r in self.store.pending(MAX_QUEUE + 50) if self.skip_until.get(r["id"], 0) <= now]
            if not rows:
                break
            progressed = False
            for row in rows:
                if self.stop_evt.is_set():
                    break
                left = self.store.stats()["queue"]
                self.status["state"] = f"در حال ارسال: {row['title'][:40]} — {left} خبر در صف"
                try:
                    done = self._process_row(client, session, cfg, row)
                except TranslationDeferred as e:
                    if self.stop_evt.is_set():
                        return 0
                    self.skip_until[row["id"]] = time.time() + e.retry_in
                    deferred, reason = deferred + 1, str(e)
                    continue
                except BaleError as e:
                    if e.network or e.code == 429:
                        wait = 60 if not e.retry_after else min(int(e.retry_after), 300)
                        self.status["last_error"] = explain(e)
                        self.log(f"ارسال موقتاً متوقف شد؛ {wait} ثانیه دیگر ادامه می‌دهم: {explain(e)}", "error")
                        return wait
                    if is_fatal(e):
                        self.status["last_error"] = explain(e)
                        self.log("خطای تنظیمات: " + explain(e), "error")
                        return 120
                    tries = self.store.bump(row["id"])
                    self.log(f"ارسال «{row['title'][:50]}» ناموفق ({e.description}) تلاش {tries}/{MAX_TRIES}", "warn")
                    if tries >= MAX_TRIES:
                        self.store.finish(row["id"], row["uid"])
                        self.log("این مطلب پس از چند تلاش حذف شد.", "warn")
                    else:
                        self.skip_until[row["id"]] = time.time() + 30
                    continue
                if not done:                  # Stop pressed in the middle of a post; it resumes later
                    return 0
                self.store.finish(row["id"], row["uid"])
                self.store.set_meta("sent_total", int(self.store.get_meta("sent_total", 0) or 0) + 1)
                self.skip_until.pop(row["id"], None)
                self.status["last_error"] = ""
                sent += 1
                progressed = True
                self.stop_evt.wait(cfg["delay_between_posts_sec"])
            if not progressed:
                break
        # what is left, and when to look again
        rest = self.store.pending(MAX_QUEUE + 50)
        if not rest:
            if sent:
                self.log(f"صف خالی شد؛ {sent} خبر ارسال شد.")
            return 0
        now = time.time()
        waits = [self.skip_until[r["id"]] - now for r in rest if self.skip_until.get(r["id"], 0) > now]
        retry_in = max(5, min(waits)) if waits else 30
        if deferred:
            self.status["last_error"] = f"ترجمه موقتاً محدود است ({reason})"
            self.log(f"{deferred} خبر در انتظار ترجمه کنار گذاشته شد (علت: {reason}). {sent} خبر ارسال شد؛ "
                     f"تلاش بعدی حدود {int(retry_in)} ثانیه دیگر.", "warn")
        elif sent:
            self.log(f"{sent} خبر ارسال شد؛ {len(rest)} خبر هنوز در صف است.")
        return retry_in

    def _load_plan(self, row):
        plan = json.loads(row["plan"])
        if isinstance(plan, list):                       # plans written by older versions
            plan = {"v": 2, "stage": "send", "parts": plan}
        return plan

    def _process_row(self, client, session, cfg, row):
        """1) build the post once (stored as 'plan')  2) if needed translate it piece by piece (every result is saved)
        3) send: header -> FULL original text -> FULL Persian translation. Resumes after any interruption."""
        if row.get("plan"):
            plan = self._load_plan(row)
        else:
            try:
                plan, info = composer.build_plan(row, cfg, session)
            except Exception as e:
                self.log(f"ساخت متن کامل ناموفق ({type(e).__name__}); فقط خلاصه ارسال می‌شود.", "warn")
                plan, info = composer.fallback_plan(row, cfg), {}
            self.store.save_plan(row["id"], json.dumps(plan, ensure_ascii=False))
            row["progress"] = 0
            note = " | ترجمهٔ تکه‌به‌تکه" if info.get("translate") else ""
            self.log(f"آماده‌سازی: «{row['title'][:50]}» → {len(plan['parts'])} بخش، {info.get('images', 0)} عکس{note}")
        if plan["stage"] == "translate":
            plan = self._translate_plan(cfg, row, plan)          # may raise TranslationDeferred
        parts = plan["parts"]
        for i in range(int(row.get("progress") or 0), len(parts)):
            if self.stop_evt.is_set():
                return False
            self._send_part_retry(client, session, cfg, parts[i])
            self.store.set_progress(row["id"], i + 1)
            if i < len(parts) - 1:
                self.stop_evt.wait(PART_DELAY)
        self.log(f"ارسال شد: {row['title'][:70]} ({len(parts)} پیام)")
        return True

    # ------------------------------------------------------------ translation
    def _translate(self, cfg, plan, text, gave_up):
        """Translate one piece. -> ('ok'|'same'|'failed', text). Raises TranslationDeferred when services are limited."""
        tr = self.translator(cfg)
        if tr is None:
            return "same", text
        out, st = tr.translate(text, patient=not gave_up, max_wait=TR_MAX_WAIT)
        if st == "ok":
            plan.pop("blocked_since", None)
            self.tr_blocked_until = 0.0
            return "ok", out
        if st == "same":
            return "same", text
        if self.stop_evt.is_set():
            raise TranslationDeferred("توقف درخواست شد")
        why = "؛ ".join(tr.errors.values()) or "نامشخص"
        if gave_up:
            if not plan.get("gave_up_logged"):
                plan["gave_up_logged"] = True
                self.log(f"ترجمه تا {cfg['translate_give_up_min']} دقیقه ممکن نشد؛ متن اصلی با نشانه ارسال می‌شود ({why})", "warn")
            return "failed", text
        plan.setdefault("blocked_since", time.time())
        wait = min(max(20.0, (tr.blocked_until() or time.time() + 30) - time.time()), 600.0)
        self.tr_blocked_until = time.time() + wait
        raise TranslationDeferred(why, retry_in=wait)

    def _translate_plan(self, cfg, row, plan):
        """Translate title + every piece (one request each, saved immediately so nothing is redone after a pause).
        When everything is done the plan becomes the final list of messages."""
        save = lambda: self.store.update_plan(row["id"], json.dumps(plan, ensure_ascii=False), progress=0)
        give_up_sec = cfg["translate_give_up_min"] * 60
        since = plan.get("blocked_since")
        gave_up = bool(give_up_sec and since and time.time() - since >= give_up_sec)
        if not gave_up and time.time() < self.tr_blocked_until:      # all providers are paused: do not even try
            raise TranslationDeferred("همهٔ سرویس‌های ترجمه موقتاً محدودند", retry_in=self.tr_blocked_until - time.time())
        try:
            if plan.get("tr_title") and "title_done" not in plan:
                st, out = self._translate(cfg, plan, plan["hd"]["title"], gave_up)
                plan["title_fa"] = out if st == "ok" else ""
                plan["title_failed"] = st == "failed"
                plan["title_done"] = True
                save()
            todo = [p for p in plan["parts"] if p["k"] == "text" and p.get("tr") and "fa" not in p and not p.get("failed")]
            for n, part in enumerate(todo, 1):
                if self.stop_evt.is_set():
                    raise TranslationDeferred("توقف درخواست شد")
                st, out = self._translate(cfg, plan, part["text"], gave_up)
                if st == "ok":
                    part["fa"] = out
                elif st == "same":
                    part["fa"], part["same"] = out, True
                else:
                    part["failed"] = True
                save()
                self.status["state"] = f"ترجمه: {n}/{len(todo)} تکه — {row['title'][:30]}"
        except TranslationDeferred:
            save()                                   # keep what is translated + the 'blocked_since' time
            raise
        final = {"v": 2, "stage": "send", "parts": composer.finalize(plan)}
        self.store.update_plan(row["id"], json.dumps(final, ensure_ascii=False), progress=0)
        row["progress"] = 0
        self.log(f"ترجمه کامل شد؛ ابتدا متن اصلی و سپس ترجمهٔ فارسی ارسال می‌شود ({len(final['parts'])} پیام)")
        return final

    # ------------------------------------------------------------ sending
    def _send_part_retry(self, client, session, cfg, part):
        try:
            self._send_part(client, session, cfg, part)
        except BaleError as e:
            if e.code == 429:
                wait = max(1, min(int(e.retry_after or 10), 120))
                self.log(f"محدودیت نرخ بله؛ {wait} ثانیه صبر می‌کنم.", "warn")
                self.stop_evt.wait(wait)
                self._send_part(client, session, cfg, part)
            else:
                raise

    def _send_part(self, client, session, cfg, part):
        chat = cfg["channel_id"]
        if part["k"] == "text":
            return self._send_text_safe(client, chat, part["text"])
        return self._send_photo_or_text(client, session, chat, part["url"], part.get("caption", ""))

    def _send_photo_or_text(self, client, session, chat, url, caption):
        img = fetcher.download_image(session, url) if url else None
        if img:
            tried_jpeg = False
            while True:
                try:
                    client.send_photo(chat, img[0], img[1], caption)
                    return
                except BaleError as e:
                    if e.network or e.code in (401, 403, 429) or is_fatal(e):
                        raise
                    conv = None if tried_jpeg else fetcher.to_jpeg(img[0])
                    tried_jpeg = True
                    if conv is None:
                        self.log(f"ارسال تصویر نشد ({e.description})", "warn")
                        break
                    img = conv                                    # retry once as JPEG
        if caption:                      # image unavailable: the header/text must still reach the channel
            self._send_text_safe(client, chat, caption)

    def _send_text_safe(self, client, chat, text, depth=0):
        try:
            client.send_message(chat, text)
        except BaleError as e:
            if e.network or e.code in (401, 403, 429) or is_fatal(e):
                raise
            if e.code == 400 and len(text) >= 400 and depth < 3:      # maybe too long for Bale: send in two halves
                mid = len(text) // 2
                cut = max(text.rfind("\n\n", 0, mid + 200), text.rfind(" ", 0, mid + 200))
                cut = cut if cut > 100 else mid
                self._send_text_safe(client, chat, text[:cut].strip(), depth + 1)
                self._send_text_safe(client, chat, text[cut:].strip(), depth + 1)
            else:
                self.log(f"یک پیام رد شد و نادیده گرفته شد: {e.description}", "warn")
