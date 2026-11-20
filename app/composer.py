# -*- coding: utf-8 -*-
"""Build the plan (list of messages) for one news item: header, whole article text, every image.

Translation is *lazy*: when a post needs it, the text is cut into small pieces (default 1500 chars) that carry
a "tr" flag; the sender translates each piece right before sending it, so the channel receives the article
piece by piece and a slow / rate-limited translation service never blocks the whole article.
"""
import re

from . import article, config, fetcher
from .translate import needs_translation, split_sentences

TEXT_LIMIT = 3500        # per Bale text message when no translation is involved (API max is 4096)
CAPTION_LIMIT = 1000     # per photo caption
FULL_ENOUGH = 1500       # if the feed already carries this much text we do not download the page


def hashtag(name):
    tag = re.sub(r"[^\w]+", "_", name, flags=re.U).strip("_")
    return "#" + tag if tag else ""


def _para(b):
    return (b.get("p", "") + b["text"]).strip()


# ---------------------------------------------------------------- content
def get_blocks(row, cfg, session):
    """Returns (blocks, og_image). blocks = [{'k':'text','text':..,'p':prefix} | {'k':'img','url':..}]."""
    if not cfg["full_text"]:
        s = fetcher._cut(row.get("summary", ""), cfg["summary_chars"]) if cfg["summary_chars"] else ""
        return ([{"k": "text", "text": s}] if s else []), ""
    link = row.get("link") or ""
    feed_blocks = article.html_to_blocks(row["content_html"], link) if row.get("content_html") else []
    if article.text_len(feed_blocks) >= FULL_ENOUGH:
        return feed_blocks, ""
    page_blocks, og = [], ""
    if link:
        try:
            page_blocks, og = article.fetch_article(session, link)
        except Exception:
            pass
    best = max([feed_blocks, page_blocks], key=article.text_len)
    if article.text_len(best) < 200 and row.get("summary"):       # extraction failed: use the feed summary
        best = [{"k": "text", "text": row["summary"]}] + [b for b in best if b["k"] == "img"]
    return best, og


def split_lead(blocks, feed_image, og):
    lead = feed_image or ""
    if lead:
        key = lead.split("?")[0]
        for i, b in enumerate(blocks):
            if b["k"] == "img" and b["url"].split("?")[0] == key:
                blocks = blocks[:i] + blocks[i + 1:]
                break
    else:
        for i, b in enumerate(blocks):
            if b["k"] == "img":
                lead = b["url"]
                blocks = blocks[:i] + blocks[i + 1:]
                break
    return lead or og, blocks


# ---------------------------------------------------------------- chunking
def _split_lines(text, limit):
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out


def chunk_blocks(blocks, limit, tr=False):
    """Pack text blocks into messages of at most `limit` chars; images become their own 'photo' parts.
    Code blocks are never translated. `tr` marks text parts that must be translated right before sending."""
    parts, buf = [], ""

    def flush():
        nonlocal buf
        if buf.strip():
            parts.append({"k": "text", "text": buf.strip(), "tr": tr})
        buf = ""

    for b in blocks:
        if b["k"] == "img":
            flush()
            parts.append({"k": "photo", "url": b["url"], "caption": ""})
            continue
        if b.get("pre"):
            flush()
            for piece in _split_lines(b["text"], limit):
                parts.append({"k": "text", "text": piece, "tr": False})
            continue
        t = _para(b)
        if not t:
            continue
        for piece in ([t] if len(t) <= limit else split_sentences(t, limit)):
            if buf and len(buf) + 2 + len(piece) > limit:
                flush()
            buf = (buf + "\n\n" + piece) if buf else piece
    flush()
    return parts


# ---------------------------------------------------------------- header
def header_data(row, cfg):
    info = config.category_info(cfg).get(row["category"]) or {"name": "منبع سفارشی", "emoji": "📌"}
    return {"emoji": info["emoji"], "title": row["title"], "source": row["source"], "cat": info["name"],
            "tag": hashtag(info["name"]), "time": fetcher.fmt_time(row["published_ts"]),
            "link": row.get("link", ""), "show_orig": bool(cfg["translate_show_original"])}


def render_header(hd, title_fa="", translated=False, orig_first=False, failed=False):
    lines = [f"{hd['emoji']} {fetcher._cut(title_fa or hd['title'], 300)}"]
    if title_fa and hd["show_orig"]:
        lines.append(f"🔤 {fetcher._cut(hd['title'], 250)}")
    lines += ["", f"📰 منبع: {hd['source']}", f"🏷 دسته: {hd['cat']} {hd['tag']}".rstrip(), f"🕒 {hd['time']}"]
    if translated or title_fa:
        lines.append("🌐 ترجمهٔ خودکار به فارسی" + (" — ابتدا متن اصلی، سپس ترجمهٔ کامل" if orig_first else ""))
    elif failed:
        lines.append("⚠️ ترجمه ممکن نشد؛ فقط متن اصلی ارسال شد")
    if hd.get("link"):
        lines.append(f"🔗 {hd['link']}")
    h = "\n".join(lines)
    return h if len(h) <= CAPTION_LIMIT else h[: CAPTION_LIMIT - 1] + "…"


def _assemble_static(header, lead, body, link):
    """No translation involved: merge the first text chunk into the header message when it fits."""
    parts = list(body)
    if lead:
        first = {"k": "photo", "url": lead, "caption": header}
        if parts and parts[0]["k"] == "text" and len(header) + 2 + len(parts[0]["text"]) <= CAPTION_LIMIT:
            first["caption"] = header + "\n\n" + parts.pop(0)["text"]
        parts.insert(0, first)
    else:
        if parts and parts[0]["k"] == "text" and len(header) + 2 + len(parts[0]["text"]) <= TEXT_LIMIT:
            parts[0] = {"k": "text", "text": header + "\n\n" + parts[0]["text"], "tr": False}
        else:
            parts.insert(0, {"k": "text", "text": header, "tr": False})
    return parts


def _footer(parts, link):
    if len(parts) > 1 and link:
        footer = "🔗 " + link
        if parts[-1]["k"] == "text" and not parts[-1].get("tr") and len(parts[-1]["text"]) + 2 + len(footer) <= TEXT_LIMIT:
            parts[-1]["text"] += "\n\n" + footer
        else:
            parts.append({"k": "text", "text": footer, "tr": False})
    return parts


# ---------------------------------------------------------------- plan
def merge_text(seq, prefix="", limit=TEXT_LIMIT):
    """Join consecutive text parts into messages of up to `limit` chars (photos stay where they are).
    `prefix` (a divider line) is placed in front of the first message."""
    out, buf = [], prefix

    def flush():
        nonlocal buf
        if buf.strip():
            out.append({"k": "text", "text": buf.strip(), "tr": False})
        buf = ""

    has_text = any(p["k"] == "text" for p in seq)
    if not has_text:
        return [p for p in seq]
    for p in seq:
        if p["k"] != "text":
            flush()
            out.append(p)
            continue
        t = p["text"]
        if buf and len(buf) + 2 + len(t) > limit:
            flush()
        buf = (buf + "\n\n" + t) if buf else t
    flush()
    return out


def build_plan(row, cfg, session):
    """-> (plan, info).  plan = {'v':2, 'stage': 'send'|'translate', ...}
       stage 'send'     : plan['parts'] is the final list of messages
       stage 'translate': text pieces carry tr=True; the worker translates them one by one (saving each result),
                          then `finalize` turns the plan into the final list:
                          header -> full original text (+photos) -> full Persian translation."""
    blocks, og = get_blocks(row, cfg, session)
    feed_image = row.get("image_url", "")
    if not cfg["send_images"]:
        blocks, og, feed_image = [b for b in blocks if b["k"] != "img"], "", ""
    lead, blocks = split_lead(blocks, feed_image, og) if cfg["send_images"] else ("", blocks)
    if cfg["max_images"]:
        keep, kept = [], 1 if lead else 0
        for b in blocks:
            if b["k"] == "img":
                if kept >= cfg["max_images"]:
                    continue
                kept += 1
            keep.append(b)
        blocks = keep

    sample = " ".join(b["text"] for b in blocks if b["k"] == "text")[:600]
    tr_on = bool(cfg["translate_enabled"])
    title_tr = tr_on and needs_translation(row["title"])
    body_tr = tr_on and (cfg["translate_body"] or not cfg["full_text"]) and needs_translation(sample)
    hd = header_data(row, cfg)
    link = row.get("link", "")

    if not (title_tr or body_tr):
        parts = _assemble_static(render_header(hd), lead, chunk_blocks(blocks, TEXT_LIMIT), link)
        parts = _footer(parts, link)
        info = {"translate": False, "images": sum(1 for p in parts if p["k"] == "photo")}
        return {"v": 2, "stage": "send", "parts": parts}, info

    parts = chunk_blocks(blocks, min(cfg["translate_chunk_chars"], TEXT_LIMIT), tr=body_tr)
    plan = {"v": 2, "stage": "translate", "hd": hd, "lead": lead, "tr_title": title_tr, "link": link,
            "orig_first": bool(cfg["translate_original_body"]), "parts": parts}
    return plan, {"translate": True, "images": (1 if lead else 0) + sum(1 for p in parts if p["k"] == "photo")}


def finalize(plan):
    """Translation finished (or gave up): build the final messages.
       header -> FULL original text (with all photos) -> FULL Persian translation (-> link)."""
    hd, parts = plan["hd"], plan["parts"]
    title_fa = plan.get("title_fa") or ""
    tr_parts = [p for p in parts if p["k"] == "text" and p.get("tr")]
    body_ok = any(p.get("fa") and not p.get("same") for p in tr_parts)
    failed = bool(plan.get("title_failed")) or any(p.get("failed") for p in tr_parts)
    orig_first = bool(plan.get("orig_first")) and body_ok

    original = [{"k": "text", "text": p["text"]} if p["k"] == "text" else p for p in parts]
    final = []
    if body_ok:
        persian = []
        for p in parts:
            if p["k"] == "photo":
                if not orig_first:
                    persian.append(p)
            elif p.get("tr"):
                txt = p["fa"] if p.get("fa") else "⚠️ (ترجمه ممکن نشد؛ متن اصلی)\n" + p["text"]
                persian.append({"k": "text", "text": txt})
            else:
                persian.append({"k": "text", "text": p["text"]})
        if orig_first:
            final += merge_text(original, "━━━━ 📄 متن اصلی ━━━━")
        final += merge_text(persian, "━━━━ 🌐 ترجمهٔ فارسی ━━━━")
    else:
        final += merge_text(original)
    header = render_header(hd, title_fa, translated=body_ok or bool(title_fa), orig_first=orig_first,
                           failed=failed and not (body_ok or title_fa))
    lead = plan.get("lead", "")
    final.insert(0, {"k": "photo", "url": lead, "caption": header} if lead else {"k": "text", "text": header, "tr": False})
    return _footer(final, plan.get("link", ""))


def fallback_plan(row, cfg):
    """Used when building the full post failed unexpectedly: header + feed summary only."""
    header = render_header(header_data(row, cfg))
    body = chunk_blocks([{"k": "text", "text": row["summary"]}], TEXT_LIMIT) if row.get("summary") else []
    parts = _footer(_assemble_static(header, "", body, row.get("link", "")), row.get("link", ""))
    return {"v": 2, "stage": "send", "parts": parts}
