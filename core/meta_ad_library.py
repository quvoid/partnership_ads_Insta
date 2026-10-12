"""
Meta Ad Library v2 — full-node capture for Indian ads.

Two ways to get ads out of the public Ad Library without an API token:

  1. Per-page enumeration  (``enumerate_page_ads``)
     Loads ``?view_all_page_id=<page_id>`` and scrolls. Every scroll is a
     GraphQL page of ~30 nodes, so a brand with 400 active ads costs ~15
     scrolls, not 400 page loads. This is how we break the old 120-ad cap
     and pick up brands the keyword search never surfaced.

  2. Per-ad lookup  (``fetch_ad_nodes``)
     Loads ``?id=<ad_archive_id>``. Used only for ids that the page
     enumeration did not return (ads that went inactive, or whose page we
     could not resolve).

Both paths parse the SAME node shape. Meta serves it either server-side
rendered inside ``<script type="application/json">`` or as a follow-up
``/api/graphql/`` response — it varies per load, so we scan both.

Every node is cached to ``meta_ads_nodes_cache.json`` keyed by
ad_archive_id so reruns are cheap and resumable.

Field inventory (what Meta exposes for IN ads, verified 2026-09-17):
  top level : is_active, start_date, end_date (epoch), publisher_platform[],
              page_id, page_name, collation_count, collation_id,
              contains_digital_created_media (AI flag), gated_type,
              contains_sensitive_content, total_active_time (usually null)
  snapshot  : body.text, title, caption (display domain), cta_text, cta_type,
              link_url, link_description, display_format,
              page_like_count, page_categories[], page_profile_uri,
              branded_content{page_name,page_id,page_profile_uri}  <- brand
                 when the ad runs from a CREATOR page (paid partnership),
              images[{original_image_url}], videos[{video_hd_url,
              video_sd_url, video_preview_image_url}],
              cards[{title, body, link_url, cta_text, image/video urls}]
  NOT exposed for India: spend, impressions, reach, demographics
  (those only appear for political ads and EU/DSA ads).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import quote

from playwright.async_api import async_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE_CACHE = os.path.join(ROOT, "meta_ads_nodes_cache.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

BASE = "https://www.facebook.com/ads/library/"


def ad_url(ad_id: str) -> str:
    return (f"{BASE}?active_status=all&ad_type=all&country=IN&id={ad_id}"
            "&is_targeted_country=false&media_type=all")


def page_url(page_id: str, active_only: bool = True) -> str:
    status = "active" if active_only else "all"
    return (f"{BASE}?active_status={status}&ad_type=all&country=IN"
            f"&view_all_page_id={page_id}&media_type=all&search_type=page")


def keyword_url(q: str) -> str:
    return (f"{BASE}?active_status=active&ad_type=all&country=IN&q={quote(q)}"
            "&search_type=keyword_unordered&media_type=all")


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

def load_cache() -> dict:
    if os.path.exists(NODE_CACHE):
        with open(NODE_CACHE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict) -> None:
    tmp = NODE_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, NODE_CACHE)


# --------------------------------------------------------------------------
# node extraction
# --------------------------------------------------------------------------

def _walk(obj, out: dict, counts: list):
    if isinstance(obj, dict):
        if "ad_archive_id" in obj and isinstance(obj.get("snapshot"), dict):
            out[str(obj["ad_archive_id"])] = obj
        if "search_results_connection" in obj:
            c = obj["search_results_connection"].get("count")
            if isinstance(c, int):
                counts.append(c)
        for v in obj.values():
            _walk(v, out, counts)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out, counts)


def scan_text(text: str, out: dict, counts: list) -> None:
    """Pull ad nodes out of either a GraphQL body or an SSR HTML document."""
    if "ad_archive_id" not in text:
        return
    if text.lstrip().startswith(("{", "for (;;);")):
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("for (;;);"):
                line = line[9:]
            if line.startswith("{"):
                try:
                    _walk(json.loads(line), out, counts)
                except json.JSONDecodeError:
                    pass
        return
    for m in re.finditer(r'<script type="application/json"[^>]*>(.*?)</script>', text, re.S):
        try:
            _walk(json.loads(m.group(1)), out, counts)
        except json.JSONDecodeError:
            pass


class _Collector:
    """Attach to a page; accumulates every ad node seen on that page."""

    def __init__(self):
        self.nodes: dict = {}
        self.counts: list = []

    def attach(self, page):
        async def on_resp(resp):
            try:
                ct = resp.headers.get("content-type", "")
                if "graphql" in resp.url or "text/html" in ct:
                    scan_text(await resp.text(), self.nodes, self.counts)
            except Exception:
                pass
        page.on("response", lambda r: asyncio.ensure_future(on_resp(r)))


async def _new_browser(pw, headless=True):
    browser = await pw.chromium.launch(headless=headless)
    ctx = await browser.new_context(user_agent=UA, locale="en-US",
                                    viewport={"width": 1400, "height": 1000},
                                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
    return browser, ctx


# --------------------------------------------------------------------------
# 1. per-page enumeration
# --------------------------------------------------------------------------

async def _enumerate_one(ctx, page_id: str, active_only: bool, max_scrolls: int, log) -> tuple[dict, int | None]:
    page = await ctx.new_page()
    col = _Collector()
    col.attach(page)
    await page.goto(page_url(page_id, active_only), wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(5)
    expected = col.counts[0] if col.counts else None
    stale = 0
    last = -1
    for i in range(max_scrolls):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2.5)
        n = len(col.nodes)
        if expected and n >= expected:
            break
        if n == last:
            stale += 1
            if stale >= 4:
                break
        else:
            stale = 0
        last = n
        if i % 5 == 4:
            log(f"    scroll {i+1}: {n} nodes" + (f" / ~{expected}" if expected else ""))
    await page.close()
    return col.nodes, expected


async def enumerate_page_ads(page_ids: Iterable[str], active_only=True, max_scrolls=80,
                             headless=True, log=print) -> dict:
    """Return {ad_archive_id: node} for every ad on the given pages."""
    all_nodes: dict = {}
    async with async_playwright() as pw:
        browser, ctx = await _new_browser(pw, headless)
        for pid in page_ids:
            log(f"  enumerating page {pid} ...")
            try:
                nodes, expected = await _enumerate_one(ctx, pid, active_only, max_scrolls, log)
                log(f"    -> {len(nodes)} nodes (library says ~{expected})")
                all_nodes.update(nodes)
            except Exception as e:
                log(f"    !! failed: {e}")
        await browser.close()
    return all_nodes


# --------------------------------------------------------------------------
# 2. per-ad lookup
# --------------------------------------------------------------------------

async def fetch_ad_nodes(ad_ids: Iterable[str], concurrency=3, headless=True, log=print) -> dict:
    """Return {ad_archive_id: node}. Ids not found (removed ads) are absent."""
    ad_ids = [str(a) for a in ad_ids]
    found: dict = {}
    sem = asyncio.Semaphore(concurrency)
    async with async_playwright() as pw:
        browser, ctx = await _new_browser(pw, headless)

        async def one(aid):
            async with sem:
                page = await ctx.new_page()
                col = _Collector()
                col.attach(page)
                try:
                    await page.goto(ad_url(aid), wait_until="networkidle", timeout=60000)
                    await asyncio.sleep(1.5)
                    if aid not in col.nodes:
                        # sometimes the graphql lands a beat late
                        await asyncio.sleep(3)
                    found.update(col.nodes)          # an id page returns the page's list too
                    if aid not in col.nodes:
                        body = await page.inner_text("body")
                        # Meta drops non-political ads from the library as soon as they
                        # stop running, so this is the normal fate of an old id.
                        if re.search(r"isn[’']t in the ad library", body, re.I):
                            found[aid] = {"ad_archive_id": aid, "_removed": True}
                except Exception as e:
                    log(f"    !! {aid}: {e}")
                finally:
                    await page.close()

        for i in range(0, len(ad_ids), 25):
            batch = ad_ids[i:i + 25]
            await asyncio.gather(*(one(a) for a in batch))
            log(f"  fetched {min(i+25, len(ad_ids))}/{len(ad_ids)}  (cache now {len(found)})")
        await browser.close()
    return found


# --------------------------------------------------------------------------
# 3. page-id discovery via keyword search
# --------------------------------------------------------------------------

async def find_pages(query: str, headless=True, log=print) -> list[tuple[str, str, int, int]]:
    """Keyword-search the library and return [(page_id, page_name, ads_seen, likes)]
    ordered by how many ads in the result set came from that page."""
    async with async_playwright() as pw:
        browser, ctx = await _new_browser(pw, headless)
        page = await ctx.new_page()
        col = _Collector()
        col.attach(page)
        await page.goto(keyword_url(query), wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
        await browser.close()
    c = Counter()
    meta = {}
    for n in col.nodes.values():
        snap = n.get("snapshot") or {}
        bc = snap.get("branded_content")
        # for partnership ads the *brand* is the branded_content page
        pid = (bc or {}).get("page_id") or n.get("page_id")
        name = (bc or {}).get("page_name") or n.get("page_name")
        c[pid] += 1
        meta[pid] = (name, snap.get("page_like_count") or 0)
    return [(pid, meta[pid][0], cnt, meta[pid][1]) for pid, cnt in c.most_common()]


# --------------------------------------------------------------------------
# 4. normalisation → flat record
# --------------------------------------------------------------------------

_DISPLAY_FORMAT_MAP = {
    "IMAGE": "Image", "VIDEO": "Video", "DCO": "Dynamic Catalog (DCO)",
    "DPA": "Dynamic Product Ad (DPA)", "CAROUSEL": "Carousel",
    "MULTI_IMAGES": "Multi-Image", "MULTI_VIDEOS": "Multi-Video",
    "MULTI_MEDIAS": "Multi-Media", "EVENT": "Event",
}

_OFFER_RE = re.compile(r"(\d{1,2})\s*%\s*(?:off|discount|deduction|flat)|flat\s*(\d{1,2})\s*%", re.I)
_CODE_RE = re.compile(r"\b(?:code|coupon)\s*[:\-]?\s*([A-Z][A-Z0-9]{3,14})\b", re.I)
_CODE_RE2 = re.compile(r"\b(?:use|apply)\s+([A-Z]{3,}[A-Z0-9]{0,10})\b")
_HASHTAG_RE = re.compile(r"#\w+")
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]")

_FESTIVALS = [
    ("Akshaya Tritiya", r"akshaya\s*trit"), ("Diwali / Dhanteras", r"diwali|dhanteras|deepavali"),
    ("Navratri / Dussehra", r"navratri|dussehra|durga"), ("Onam", r"\bonam\b"),
    ("Pongal", r"pongal"), ("Raksha Bandhan", r"rakhi|raksha"), ("Valentine", r"valentine"),
    ("Wedding", r"wedding|bridal|bride|muhurat|shaadi"), ("Mother's Day", r"mother'?s day"),
    ("Friendship Day", r"friendship"), ("Karwa Chauth", r"karwa|karva"), ("Christmas / NY", r"christmas|new year"),
    ("Independence Day", r"independence|15th aug|freedom"), ("Ganesh Chaturthi", r"ganesh|ganpati"),
    ("Aadi", r"\baadi\b"), ("Teej", r"\bteej\b"),
]
_THEMES = [
    ("Gold Exchange", r"exchange|purana sona|old gold"), ("Savings Scheme", r"scheme|savings plan|advance|instalment|installment|gold tree|golden harvest"),
    ("Making Charges Offer", r"making charge|wastage|\bva\b|value addition"), ("Discount / Sale", r"\d+\s*%|sale|offer|discount"),
    ("Celebrity", r"dhoni|alia|kajol|nayanthara|vijay|rashmika|samantha|trisha|sobhita|deepika|kareena|anushka"),
    ("Gold Rate / Purity", r"hallmark|huid|bis|purity|22k|24k|18k|gold rate"), ("Silver", r"silver"),
    ("Diamond", r"diamond|solitaire"), ("Lightweight / Daily Wear", r"lightweight|daily wear|everyday|office wear|minimal"),
    ("Anti-tarnish / Demi-fine", r"anti[- ]tarnish|gold vermeil|demi[- ]fine|waterproof"), ("Store Launch", r"new store|now open|grand opening|launch"),
    ("Gold Loan / Cash", r"gold loan|urgent cash|disburse"), ("Lab Grown", r"lab[- ]grown"), ("Men's", r"\bmen\b|\bmen'?s\b|him"),
    ("Kids", r"\bkids?\b|children|baby"),
]

_LANG_HINTS = [
    ("Tamil", r"[஀-௿]"), ("Malayalam", r"[ഀ-ൿ]"), ("Telugu", r"[ఀ-౿]"),
    ("Kannada", r"[ಀ-೿]"), ("Bengali", r"[ঀ-৿]"), ("Hindi (Devanagari)", r"[ऀ-ॿ]"),
    ("Gujarati", r"[઀-૿]"), ("Punjabi", r"[਀-੿]"), ("Odia", r"[଀-୿]"),
]
_HINGLISH = re.compile(r"\b(karein|karo|apna|purana|sona|bharosa|hai|aur|par|ke liye|kijiye|ab|sirf|paise)\b", re.I)


def _epoch_to_date(v):
    if not v:
        return ""
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _landing_type(url: str) -> str:
    u = (url or "").lower()
    if not u:
        return "None"
    if "fb.me" in u or "facebook.com" in u or "fb.com" in u:
        return "Facebook / Messenger"
    if "instagram.com" in u:
        return "Instagram"
    if "whatsapp" in u or "wa.me" in u:
        return "WhatsApp"
    if "play.google" in u or "apps.apple" in u:
        return "App Install"
    if "google.com/maps" in u or "maps.app" in u or "/stores" in u or "stores." in u or "store-locator" in u:
        return "Store Locator"
    if "youtube" in u or "youtu.be" in u:
        return "YouTube"
    if any(k in u for k in ("scheme", "savings", "instagold", "goldtree", "digigold")):
        return "Savings Scheme / Digital Gold"
    if any(k in u for k in ("/product", ".html", "/p/", "/products/")) and "collection" not in u:
        return "Product Page (PDP)"
    if any(k in u for k in ("/collection", "/category", "/jewellery/", "/all-jewellery", "/c/", "/shop")):
        return "Category / Collection Page"
    path = re.sub(r"https?://[^/]+", "", u).split("?")[0].strip("/")
    return "Homepage" if not path else "Landing Page"


def _utm(url: str) -> dict:
    out = {"utm_source": "", "utm_medium": "", "utm_campaign": ""}
    if not url or "utm_" not in url:
        return out
    for k in out:
        m = re.search(rf"{k}=([^&#]+)", url)
        if m:
            out[k] = m.group(1)[:80]
    return out


def _language(text: str) -> str:
    for name, pat in _LANG_HINTS:
        if re.search(pat, text):
            return name
    if len(_HINGLISH.findall(text)) >= 2:
        return "Hinglish"
    return "English"


def _match_all(text: str, table) -> str:
    hits = [name for name, pat in table if re.search(pat, text, re.I)]
    return ", ".join(hits)


def normalize(node: dict, brand: str = "", category: str = "", today: datetime | None = None) -> dict:
    """Flatten a raw node into the record the workbook uses."""
    today = today or datetime.now(timezone.utc)
    snap = node.get("snapshot") or {}
    bc = snap.get("branded_content") or {}
    body = (snap.get("body") or {}).get("text") or ""
    cards = snap.get("cards") or []
    # DCO/carousel ads keep the real copy inside cards
    card_titles = [c.get("title") for c in cards if c.get("title") and "{{" not in c.get("title")]
    card_bodies = [c.get("body") for c in cards if c.get("body") and "{{" not in c.get("body")]
    title = snap.get("title") or ""
    if "{{" in title and card_titles:
        title = card_titles[0]
    if ("{{" in body or not body) and card_bodies:
        body = card_bodies[0]
    link_url = snap.get("link_url") or (cards[0].get("link_url") if cards else "") or ""
    videos = [v for v in (snap.get("videos") or []) if v.get("video_sd_url") or v.get("video_hd_url")]
    card_videos = [c for c in cards if c.get("video_sd_url") or c.get("video_hd_url")]
    images = snap.get("images") or []
    card_images = [c for c in cards if c.get("original_image_url")]
    video = (videos or card_videos or [{}])[0]
    image = (images or card_images or [{}])[0]

    start = _epoch_to_date(node.get("start_date"))
    end = _epoch_to_date(node.get("end_date"))
    is_active = node.get("is_active")
    days_active = ""
    if start:
        s = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        e = today if (is_active or not end) else datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_active = max((e - s).days, 0)

    is_partnership = bool(bc)
    creator_name = snap.get("page_name") if is_partnership else ""
    creator_uri = snap.get("page_profile_uri") if is_partnership else ""
    creator_likes = snap.get("page_like_count") if is_partnership else ""
    brand_page_name = bc.get("page_name") if is_partnership else snap.get("page_name")
    brand_page_id = bc.get("page_id") if is_partnership else node.get("page_id")

    fmt = snap.get("display_format") or ""
    fmt_label = _DISPLAY_FORMAT_MAP.get(fmt, fmt.title() if fmt else "")
    if is_partnership:
        fmt_label = f"Creator Partnership ({fmt_label})"

    full_text = " ".join([title, body, snap.get("link_description") or ""] + card_titles + card_bodies)
    offer = _OFFER_RE.search(full_text)
    offer_pct = next((g for g in offer.groups() if g), "") if offer else ""
    code = _CODE_RE.search(full_text) or _CODE_RE2.search(full_text)
    promo = code.group(1).upper() if code else ""
    hook = re.split(r"[\n.!?]", body.strip(), 1)[0].strip()[:120] if body else ""
    utm = _utm(link_url)
    plats = node.get("publisher_platform") or []

    return {
        "category": category, "brand": brand,
        "ad_archive_id": str(node.get("ad_archive_id")),
        "ad_library_url": f"{BASE}?id={node.get('ad_archive_id')}",
        "status": "Active" if is_active else ("Inactive" if is_active is False else ""),
        "started_running": start, "ended_running": "" if is_active else end,
        "days_active": days_active,
        "display_format": fmt_label, "display_format_raw": fmt,
        "is_creator_partnership": "Yes" if is_partnership else "No",
        "creator_page_name": creator_name or "", "creator_page_url": creator_uri or "",
        "creator_page_likes": creator_likes if creator_likes != "" else "",
        "brand_page_name": brand_page_name or "", "brand_page_id": str(brand_page_id or ""),
        "brand_page_likes": (bc and "") or (snap.get("page_like_count") or ""),
        "page_categories": ", ".join(snap.get("page_categories") or []),
        "title": title, "body": body,
        "link_description": snap.get("link_description") or "",
        "hook_line": hook,
        "collation_count": node.get("collation_count") or 1,
        "publisher_platforms": ", ".join(p.title() for p in plats),
        "platform_count": len(plats),
        "cta_text": (snap.get("cta_text") or (cards[0].get("cta_text") if cards else "") or "").strip().title(),
        "cta_type": snap.get("cta_type") or "",
        "link_url": link_url, "display_domain": snap.get("caption") or "",
        "landing_page_type": _landing_type(link_url),
        **utm,
        "offer_pct": offer_pct, "promo_code": promo,
        "festival_hook": _match_all(full_text, _FESTIVALS),
        "themes": _match_all(full_text, _THEMES),
        "language": _language(full_text),
        "hashtags": " ".join(dict.fromkeys(_HASHTAG_RE.findall(full_text)))[:200],
        "body_word_count": len(body.split()), "emoji_count": len(_EMOJI_RE.findall(body)),
        "card_count": len(cards),
        "card_titles": " | ".join(dict.fromkeys(card_titles))[:500],
        "has_video": "Yes" if video else "No",
        "video_url": video.get("video_sd_url") or video.get("video_hd_url") or "",
        "video_hd_url": video.get("video_hd_url") or "",
        "video_thumbnail_url": video.get("video_preview_image_url") or "",
        "image_url": image.get("original_image_url") or "",
        "media_count": len(videos) + len(images) + len(cards),
        "ai_generated_media": "Yes" if node.get("contains_digital_created_media") else "No",
        "sensitive_content": "Yes" if node.get("contains_sensitive_content") else "No",
        "gated_type": node.get("gated_type") or "",
        "fetched_at": today.strftime("%Y-%m-%d"),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("find", help="find page ids for a brand keyword")
    p1.add_argument("query")
    p2 = sub.add_parser("page", help="enumerate all active ads on page ids (comma separated)")
    p2.add_argument("page_ids")
    p2.add_argument("--all", action="store_true", help="include inactive ads")
    p3 = sub.add_parser("ids", help="fetch specific ad ids (comma separated)")
    p3.add_argument("ad_ids")
    a = ap.parse_args()

    if a.cmd == "find":
        for pid, name, cnt, likes in asyncio.run(find_pages(a.query)):
            print(f"{pid:>20}  {name:<40} ads_in_results={cnt:<4} likes={likes}")
    else:
        cache = load_cache()
        if a.cmd == "page":
            nodes = asyncio.run(enumerate_page_ads(a.page_ids.split(","), active_only=not a.all))
        else:
            nodes = asyncio.run(fetch_ad_nodes(a.ad_ids.split(",")))
        cache.update(nodes)
        save_cache(cache)
        print(f"{len(nodes)} nodes captured, cache = {len(cache)}")
