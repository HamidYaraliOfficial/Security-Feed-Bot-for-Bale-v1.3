# -*- coding: utf-8 -*-
"""Extract the full article (text blocks + images, in reading order) from feed HTML or from the article page."""
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "button", "svg", "iframe",
             "select", "template", "canvas", "audio", "video", "menu", "dialog", "head", "label", "input"}
JUNK_RE = re.compile(
    r"(comment|related|sharing|share-|-share|social|newsletter|subscribe|sidebar|advert|promo|banner|cookie|"
    r"breadcrumb|author-?bio|post-?tags|tag-?list|toolbar|popup|modal|widget|recommend|outbrain|taboola|paywall|"
    r"signup|sign-up|more-stories|trending|pagination|skip-link|print-only|sr-only|screen-reader)", re.I)
BOILER_RE = re.compile(r"^(share( this)?|tweet|advertisement|sponsored( content)?|related( articles?| posts?)?|"
                       r"subscribe.*|read more|continue reading|follow us.*|click here.*)\W*$", re.I)
BAD_IMG_RE = re.compile(r"(pixel|tracker|feedburner\.com/~|doubleclick|gravatar|emoji|/ads?/|\.svg(\?|$)|spacer|1x1|"
                        r"logo|avatar|icon|sprite|badge|/button|blank\.|placeholder|loading\.)", re.I)
PREFIX = {"h1": "▪ ", "h2": "▪ ", "h3": "▪ ", "h4": "▪ ", "h5": "▪ ", "h6": "▪ ", "li": "• ", "blockquote": "❝ "}
TEXT_BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "figcaption", "dd", "dt"}
MAX_TOTAL_CHARS = 250_000


def _ws(s):
    return re.sub(r"[ \t\r\f\v\u00a0\u200b]+", " ", re.sub(r"\n\s*\n+", "\n", s or "")).strip()


def _junk(tag):
    if tag.name in SKIP_TAGS:
        return True
    attrs = getattr(tag, "attrs", None) or {}
    if attrs.get("hidden") is not None or str(attrs.get("aria-hidden", "")).lower() == "true":
        return True
    if "display:none" in str(attrs.get("style", "")).replace(" ", "").lower():
        return True
    ident = " ".join(attrs.get("class", []) if isinstance(attrs.get("class"), list) else [str(attrs.get("class", ""))])
    ident += " " + str(attrs.get("id", ""))
    return bool(JUNK_RE.search(ident))


def _img_url(tag, base):
    cands = []
    for key in ("data-src", "data-lazy-src", "data-original", "src"):
        v = tag.get(key)
        if v and not v.startswith("data:"):
            cands.append(v)
    for key in ("data-srcset", "srcset"):
        ss = tag.get(key)
        if ss:
            best, best_w = "", -1
            for part in ss.split(","):
                bits = part.strip().split()
                if not bits or bits[0].startswith("data:"):
                    continue
                w = int(bits[1][:-1]) if len(bits) > 1 and bits[1][:-1].isdigit() else 0
                if w > best_w:
                    best, best_w = bits[0], w
            if best:
                cands.insert(0, best)
    for c in cands:
        url = urljoin(base, c.strip())
        if url.startswith("http") and not BAD_IMG_RE.search(url):
            try:
                if int(tag.get("width", 100)) < 80 or int(tag.get("height", 100)) < 80:
                    return ""
            except (ValueError, TypeError):
                pass
            return url
    return ""


def _emit_images(el, base, out):
    for img in ([el] if el.name == "img" else el.find_all("img")):
        u = _img_url(img, base)
        if u:
            out.append({"k": "img", "url": u})


def _walk(el, base, out):
    for child in el.children:
        if isinstance(child, NavigableString):
            if not isinstance(child, Comment):
                t = _ws(str(child))
                if len(t) > 60:
                    out.append({"k": "text", "text": t})
            continue
        if not isinstance(child, Tag) or _junk(child):
            continue
        name = child.name
        if name == "img":
            _emit_images(child, base, out)
        elif name == "pre":
            t = child.get_text().strip("\n")
            if t.strip():
                out.append({"k": "text", "text": t[:3000], "pre": True})
        elif name == "tr":
            cells = [_ws(c.get_text(" ")) for c in child.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if cells:
                out.append({"k": "text", "text": " | ".join(cells)})
        elif name in TEXT_BLOCKS:
            if name in ("li", "blockquote") and child.find(["p", "li", "ul", "ol"]):
                _walk(child, base, out)                      # nested structure: go deeper
                continue
            t = _ws(child.get_text(" "))
            if len(t) >= 2 and not BOILER_RE.match(t):
                blk = {"k": "text", "text": t}
                if name in PREFIX:
                    blk["p"] = PREFIX[name]
                out.append(blk)
            _emit_images(child, base, out)
        else:
            _walk(child, base, out)


def _dedupe(blocks):
    out, seen_img, last = [], set(), None
    total = 0
    for b in blocks:
        if b["k"] == "img":
            key = b["url"].split("?")[0]
            if key in seen_img:
                continue
            seen_img.add(key)
        else:
            if b["text"] == last:
                continue
            last = b["text"]
            total += len(b["text"])
            if total > MAX_TOTAL_CHARS:
                break
        out.append(b)
    return out


def text_len(blocks):
    return sum(len(b.get("p", "")) + len(b["text"]) for b in blocks if b["k"] == "text")


def html_to_blocks(html, base_url=""):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    _walk(soup, base_url, out)
    return _dedupe(out)


def _para_len(el):
    return sum(len(p.get_text()) for p in el.find_all("p"))


def _pick_container(soup):
    for sel in ('[itemprop="articleBody"]', "article", ".entry-content", ".post-content", ".article-content",
                ".article-body", ".post-body", ".story-body", ".td-post-content", ".c-article-body", "main"):
        els = soup.select(sel)
        if els:
            best = max(els, key=_para_len)
            if _para_len(best) >= 400:
                return best
    scores, refs = {}, {}
    for p in soup.find_all("p"):
        n = len(p.get_text())
        if n < 40:
            continue
        for par in (p.parent, p.parent.parent if p.parent else None):
            if par is not None and par.name not in ("body", "html", "[document]"):
                scores[id(par)] = scores.get(id(par), 0) + n
                refs[id(par)] = par
    if not scores:
        return soup.body or soup
    return refs[max(scores, key=scores.get)]


def fetch_article(session, url, max_bytes=4_000_000):
    """Returns (blocks, og_image_url). Raises requests exceptions / ValueError on failure."""
    r = session.get(url, timeout=(8, 25), stream=True)
    try:
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype:
            raise ValueError("not html")
        buf = bytearray()
        for part in r.iter_content(65536):
            buf.extend(part)
            if len(buf) > max_bytes:
                break
        enc = r.encoding or "utf-8"
        final_url = r.url
    finally:
        r.close()
    soup = BeautifulSoup(bytes(buf).decode(enc, "ignore"), "html.parser")
    og = ""
    for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content"):
            og = urljoin(final_url, m["content"].strip())
            break
    container = _pick_container(soup)
    out = []
    _walk(container, final_url, out)
    return _dedupe(out), og


def discover_feed(session, url):
    """Given a site or feed URL, find a working RSS/Atom feed. Returns {feed_url, title, count}."""
    import feedparser
    from urllib.parse import urlparse
    r = session.get(url, timeout=(8, 20))
    r.raise_for_status()
    d = feedparser.parse(r.content[:3_000_000])
    if d.entries:
        return {"feed_url": url, "title": _ws(d.feed.get("title", "")), "count": len(d.entries)}
    soup = BeautifulSoup(r.content[:1_500_000].decode(r.encoding or "utf-8", "ignore"), "html.parser")
    cands = []
    for l in soup.find_all("link"):
        rel = [x.lower() for x in (l.get("rel") or [])]
        t = (l.get("type") or "").lower()
        if "alternate" in rel and ("rss" in t or "atom" in t) and l.get("href"):
            cands.append(urljoin(r.url, l["href"]))
    p = urlparse(r.url)
    base = f"{p.scheme}://{p.netloc}"
    cands += [base + x for x in ("/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/index.xml", "/feed.xml")]
    tried = set()
    for u in cands:
        if u in tried or len(tried) >= 12:
            continue
        tried.add(u)
        try:
            r2 = session.get(u, timeout=(6, 15))
            if r2.status_code != 200:
                continue
            d2 = feedparser.parse(r2.content[:3_000_000])
            if d2.entries:
                return {"feed_url": u, "title": _ws(d2.feed.get("title", "")), "count": len(d2.entries)}
        except requests.RequestException:
            continue
    raise ValueError("در این آدرس فید RSS/Atom پیدا نشد. آدرس خودِ فید را وارد کنید.")
