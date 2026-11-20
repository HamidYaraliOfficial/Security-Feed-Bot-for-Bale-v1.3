# -*- coding: utf-8 -*-
"""Translate text to Persian (fa), piece by piece, patiently, with several providers.

Providers (free first):
  edge            Microsoft Edge translator (free, token fetched automatically, unofficial)
  google          unofficial Google endpoint, no key (often rate limited per IP)
  lingva          open-source Google front-end; several public instances are rotated
  mymemory        free, small daily quota
  libretranslate  your own server (or an API key)
  google_cloud    official Google Cloud Translation API (API key)

Limits / errors:
  * requests are spaced out (`translate_delay_sec`); after an HTTP 429 the spacing of that provider is increased
    automatically (and slowly relaxed again) and `Retry-After` is honoured
  * a failing provider is paused with exponential back-off (30s, 60s, 120s ... max 10 min); the others are tried
  * `blocked_until()` tells the caller when at least one provider is available again, so the caller can do other
    work meanwhile instead of waiting
"""
import html
import re
import time
from urllib.parse import quote

import requests

GOOGLE_URLS = ["https://translate.googleapis.com/translate_a/single",
               "https://translate.google.com/translate_a/single"]
EDGE_AUTH = "https://edge.microsoft.com/translate/auth"
EDGE_URL = "https://api-edge.cognitive.microsofttranslator.com/translate"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
GCLOUD_URL = "https://translation.googleapis.com/language/translate/v2"
MAX_REQUEST_CHARS = 4000
PROVIDERS = ("edge", "google", "lingva", "mymemory", "libretranslate", "google_cloud")
PERSIAN_ONLY = "\u067e\u0686\u0698\u06af\u06a9\u06cc"      # پ چ ژ گ ک ی
ARABIC_ONLY = "\u064a\u0643\u0629\u0649"                    # ي ك ة ى
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36 Edg/124.0")


class TranslateError(Exception):
    def __init__(self, msg, hard=False, retry_after=None):
        super().__init__(msg)
        self.hard = hard                  # rate-limit / quota / blocked: pause this provider for longer
        self.retry_after = retry_after


# ------------------------------------------------------------------ language helpers
def _script_counts(text):
    arabic = foreign = fa = ar = 0
    for ch in text:
        if not ch.isalpha():
            continue
        o = ord(ch)
        if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F or 0xFB50 <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF:
            arabic += 1
            fa += ch in PERSIAN_ONLY
            ar += ch in ARABIC_ONLY
        else:
            foreign += 1
    return arabic, foreign, fa, ar


def needs_translation(text):
    """False for text that is already Persian (or has too few letters to bother)."""
    arabic, foreign, fa, ar = _script_counts(text or "")
    total = arabic + foreign
    if total < 3:
        return False
    if arabic / total >= 0.5:
        return ar > fa          # Arabic-language text still gets translated; Persian does not
    return True


def guess_source_lang(text):
    letters = [c for c in text if c.isalpha()] or ["a"]
    n = len(letters)
    if sum(0x0600 <= ord(c) <= 0x06FF for c in letters) / n > 0.5:
        return "ar"
    if sum(0x0400 <= ord(c) <= 0x04FF for c in letters) / n > 0.5:
        return "ru"
    if sum(ord(c) < 0x250 for c in letters) / n > 0.5:
        return "en"
    return "auto"


def split_sentences(text, size):
    """Cut text into pieces of at most `size` chars, preferring sentence / line boundaries."""
    pieces, cur = [], ""
    for s in re.split(r"(?<=[.!?؟\n])\s+", text):
        while len(s) > size:
            cut = s.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(s[:cut].strip())
            s = s[cut:].strip()
        if cur and len(cur) + len(s) + 1 > size:
            pieces.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        pieces.append(cur)
    return pieces


def parse_urls(value):
    return [u.rstrip("/") for u in re.split(r"[,\s]+", value or "") if re.match(r"^https?://", u, re.I)]


# ------------------------------------------------------------------ translator
class Translator:
    SOFT_BASE, HARD_BASE, MIN_SLEEP = 8, 30, 1.0         # back-off seconds (tests lower them)
    MIN_GAP = {"mymemory": 0.6, "libretranslate": 0.2, "google_cloud": 0.05}   # others use translate_delay_sec

    def __init__(self, cfg):
        self.primary = cfg["translate_provider"]
        self.delay = float(cfg["translate_delay_sec"])
        self.libre_url, self.libre_key = cfg["libretranslate_url"], cfg["libretranslate_key"]
        self.lingva_urls = parse_urls(cfg["lingva_url"])
        self.gkey = cfg["google_api_key"]
        self.email = cfg["mymemory_email"]
        self.session = requests.Session()
        self.session.headers["User-Agent"] = BROWSER_UA
        if cfg["fetch_proxy"]:
            self.session.proxies = {"http": cfg["fetch_proxy"], "https": cfg["fetch_proxy"]}
        self.next_ok, self.fails, self.errors, self.last_req, self.extra_gap = {}, {}, {}, {}, {}
        self.last_provider = ""
        self.cache = {}
        self.host_i = self.lingva_i = 0
        self._edge_tok, self._edge_exp = "", 0.0
        self.sleep = time.sleep                    # the worker replaces these two so Stop interrupts waiting
        self.stopped = lambda: False

    # ------------------------------------------------------------ providers
    @staticmethod
    def _check(r, name):
        if r.status_code == 429:
            ra = None
            try:
                ra = min(float(r.headers.get("Retry-After", "")), 900)
            except ValueError:
                pass
            raise TranslateError(f"{name}: محدودیت نرخ (HTTP 429)", hard=True, retry_after=ra)
        if r.status_code != 200:
            raise TranslateError(f"{name}: HTTP {r.status_code}", hard=r.status_code in (403, 451, 401))

    def _edge_token(self):
        if self._edge_tok and time.time() < self._edge_exp:
            return self._edge_tok
        try:
            r = self.session.get(EDGE_AUTH, timeout=(6, 15))
        except requests.RequestException as e:
            raise TranslateError(f"edge: اتصال برقرار نشد ({type(e).__name__})") from None
        self._check(r, "edge")
        tok = r.text.strip()
        if len(tok) < 40:
            raise TranslateError("edge: توکن نامعتبر")
        self._edge_tok, self._edge_exp = tok, time.time() + 420
        return tok

    def _edge(self, text):
        out, lang = [], ""
        for piece in ([text] if len(text) <= 3000 else split_sentences(text, 3000)):
            r = None
            for attempt in (1, 2):
                tok = self._edge_token()
                try:
                    r = self.session.post(EDGE_URL, params={"to": "fa", "api-version": "3.0", "includeSentenceLength": "true"},
                                          headers={"Authorization": "Bearer " + tok}, json=[{"Text": piece}], timeout=(6, 30))
                except requests.RequestException as e:
                    raise TranslateError(f"edge: اتصال برقرار نشد ({type(e).__name__})") from None
                if r.status_code == 401 and attempt == 1:        # token expired: fetch a new one
                    self._edge_exp = 0
                    continue
                break
            self._check(r, "edge")
            try:
                j = r.json()
                out.append(j[0]["translations"][0]["text"])
                lang = (j[0].get("detectedLanguage") or {}).get("language", lang)
            except (ValueError, IndexError, KeyError, TypeError):
                raise TranslateError("edge: پاسخ نامعتبر") from None
        return "\n".join(out), lang

    def _google(self, text):
        params = {"client": "gtx", "sl": "auto", "tl": "fa", "dt": "t"}
        last = None
        for k in range(len(GOOGLE_URLS)):                         # second Google host as an immediate alternative
            url = GOOGLE_URLS[(self.host_i + k) % len(GOOGLE_URLS)]
            try:
                r = self.session.post(url, params=params, data={"q": text}, timeout=(6, 25))
                if r.status_code not in (200, 429) and len(text) <= 1800:     # never double the load when rate limited
                    r = self.session.get(url, params={**params, "q": text}, timeout=(6, 25))
                self._check(r, "google")
                data = r.json()
                out = "".join(seg[0] for seg in data[0] if seg and seg[0])
                lang = data[2] if len(data) > 2 and isinstance(data[2], str) else ""
                self.host_i = (self.host_i + k) % len(GOOGLE_URLS)
                return out, lang
            except TranslateError as e:
                last = e
            except requests.RequestException as e:
                last = TranslateError(f"google: اتصال برقرار نشد ({type(e).__name__})")
            except (ValueError, IndexError, TypeError, KeyError):
                last = TranslateError("google: ساختار پاسخ ناشناخته")
        raise last

    def _lingva(self, text):
        out, last = [], None
        for piece in split_sentences(text, 800):
            done = False
            for k in range(len(self.lingva_urls)):                # try the instances in turn
                base = self.lingva_urls[(self.lingva_i + k) % len(self.lingva_urls)]
                try:
                    r = self.session.get(f"{base}/api/v1/auto/fa/{quote(piece, safe='')}", timeout=(6, 25))
                    self._check(r, "lingva")
                    out.append(r.json()["translation"])
                    self.lingva_i = (self.lingva_i + k) % len(self.lingva_urls)
                    done = True
                    break
                except TranslateError as e:
                    last = e
                except requests.RequestException as e:
                    last = TranslateError(f"lingva: اتصال برقرار نشد ({type(e).__name__})")
                except (ValueError, KeyError, TypeError):
                    last = TranslateError("lingva: پاسخ نامعتبر")
            if not done:
                raise last or TranslateError("lingva: سروری تنظیم نشده")
        return " ".join(out), ""

    def _mymemory(self, text):
        src = guess_source_lang(text)
        src = "Autodetect" if src == "auto" else src
        out = []
        for chunk in split_sentences(text, 450):          # MyMemory accepts at most ~500 chars per request
            params = {"q": chunk, "langpair": f"{src}|fa"}
            if self.email:
                params["de"] = self.email
            try:
                r = self.session.get(MYMEMORY_URL, params=params, timeout=(6, 20))
            except requests.RequestException as e:
                raise TranslateError(f"mymemory: اتصال برقرار نشد ({type(e).__name__})") from None
            self._check(r, "mymemory")
            try:
                j = r.json()
            except ValueError:
                raise TranslateError("mymemory: پاسخ نامعتبر") from None
            t = str((j.get("responseData") or {}).get("translatedText") or "")
            if "MYMEMORY WARNING" in t.upper() or str(j.get("responseStatus")) != "200":
                raise TranslateError("mymemory: سهمیهٔ رایگان امروز تمام شده", hard=True, retry_after=3600)
            out.append(html.unescape(t))
        return " ".join(out), ""

    def _libretranslate(self, text):
        payload = {"q": text, "source": "auto", "target": "fa", "format": "text"}
        if self.libre_key:
            payload["api_key"] = self.libre_key
        try:
            r = self.session.post(self.libre_url + "/translate", json=payload, timeout=(6, 40))
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            raise TranslateError(f"libretranslate: خطا ({type(e).__name__})") from None
        if r.status_code != 200 or "translatedText" not in j:
            raise TranslateError("libretranslate: " + str(j.get("error") or f"HTTP {r.status_code}")[:80],
                                 hard=r.status_code in (401, 403, 429))
        return j["translatedText"], (j.get("detectedLanguage") or {}).get("language", "")

    def _google_cloud(self, text):
        try:
            r = self.session.post(GCLOUD_URL, params={"key": self.gkey},
                                  json={"q": text, "target": "fa", "format": "text"}, timeout=(6, 30))
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            raise TranslateError(f"google_cloud: خطا ({type(e).__name__})") from None
        if r.status_code != 200:
            msg = str((j.get("error") or {}).get("message", f"HTTP {r.status_code}"))[:90]
            raise TranslateError("google_cloud: " + msg, hard=True)
        try:
            t = j["data"]["translations"][0]
            return t["translatedText"], t.get("detectedSourceLanguage", "")
        except (KeyError, IndexError, TypeError):
            raise TranslateError("google_cloud: پاسخ نامعتبر") from None

    # ------------------------------------------------------------ orchestration
    def _order(self):
        usable = [p for p in PROVIDERS
                  if (p != "libretranslate" or self.libre_url) and (p != "google_cloud" or self.gkey)
                  and (p != "lingva" or self.lingva_urls)]
        first = [self.primary] if self.primary in usable else []
        return first + [p for p in usable if p not in first]

    def blocked_until(self):
        """0 if some provider can be tried now, else the time (epoch) when the first one becomes available again."""
        now, order = time.time(), self._order()
        if not order:
            return 0
        soonest = min(self.next_ok.get(p, 0) for p in order)
        return soonest if soonest > now else 0

    def _throttle(self, p):
        base = self.MIN_GAP.get(p, self.delay)
        wait = self.last_req.get(p, 0) + base + self.extra_gap.get(p, 0) - time.time()
        if wait > 0:
            self.sleep(wait)
        self.last_req[p] = time.time()

    def _penalize(self, p, err):
        self.fails[p] = self.fails.get(p, 0) + 1
        base = self.HARD_BASE if err.hard else self.SOFT_BASE
        delay = min(base * 2 ** (self.fails[p] - 1), 600)
        if err.retry_after:
            delay = max(delay, err.retry_after)
        self.next_ok[p] = time.time() + delay
        if err.hard:                                       # be gentler with this provider from now on
            self.extra_gap[p] = min(max(1.5, self.extra_gap.get(p, 0) * 2), 10)

    def _one(self, text, patient, max_wait, ignore_cooldown):
        deadline = time.time() + (max_wait if patient else 0)
        while True:
            now = time.time()
            order = self._order()
            for p in order:
                if not ignore_cooldown and self.next_ok.get(p, 0) > now:
                    continue
                self._throttle(p)
                try:
                    out, lang = getattr(self, "_" + p)(text)
                except TranslateError as e:
                    self.errors[p] = str(e)
                    self._penalize(p, e)
                    continue
                out = html.unescape(out).strip()
                if lang == "fa":
                    return text, "same"
                if not out:
                    self._penalize(p, TranslateError("empty"))
                    continue
                self.fails[p], self.next_ok[p] = 0, 0
                if self.extra_gap.get(p):
                    self.extra_gap[p] = self.extra_gap[p] * 0.7 if self.extra_gap[p] > 0.4 else 0
                self.errors.pop(p, None)
                self.last_provider = p
                return out, "ok"
            if not patient or time.time() >= deadline or self.stopped():
                return text, "fail"
            soonest = min((self.next_ok.get(p, now) for p in order), default=now + 5)
            self.sleep(max(self.MIN_SLEEP, min(soonest - time.time(), deadline - time.time(), 20)))

    def translate(self, text, patient=False, max_wait=20, ignore_cooldown=False):
        """-> (text, status). status: 'ok' translated | 'same' already Persian / nothing to do | 'fail' (original returned).
        Long input is cut into pieces so the output is always complete."""
        text = (text or "").strip()
        if not text or not needs_translation(text):
            return text, "same"
        if text in self.cache:
            return self.cache[text], "ok"
        pieces = [text] if len(text) <= MAX_REQUEST_CHARS else split_sentences(text, 3500)
        out, any_ok = [], False
        for piece in pieces:
            t, st = self._one(piece, patient, max_wait, ignore_cooldown)
            if st == "fail":
                return text, "fail"                    # never return a half-translated text
            any_ok = any_ok or st == "ok"
            out.append(t)
        if not any_ok:
            return text, "same"
        result = "\n".join(out) if len(out) > 1 else out[0]
        if len(self.cache) > 800:
            self.cache.clear()
        self.cache[text] = result
        return result, "ok"
