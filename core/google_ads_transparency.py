"""
Google Ads Transparency Center — headless, browser-free collector.

The Transparency Center UI is an SPA over JSON RPCs under
``https://adstransparency.google.com/anji/_/rpc/``. They accept plain
HTTP POSTs as long as the TLS/HTTP fingerprint looks like Chrome, so we
use ``curl_cffi`` with Chrome impersonation and never open a browser.
(Headless Chromium's fingerprint gets a reCAPTCHA wall; a headed real
Chrome passes — that path is kept only as ``--headed`` fallback.)

Endpoints (2356 = India region code; all POST, body ``f.req=<json>``):

  SearchService/SearchSuggestions   {"1": q, "2": 10, "3": 10, "4": [2356], "5": {"1": 1}}
      verified advertisers matching q: name, AR-id, country, AD-COUNT RANGE;
      plus matching domains.
  SearchService/SearchCreatives     {"2": 40, "3": {"8": [2356], "12": {"1": domain, "2": true}}, "7": {...}}
                                    {"2": 40, "3": {"8": [2356], "12": {"1": "", "2": true}, "13": {"1": [AR-id]}}, "7": {...}}
      newest-first creatives: advertiser id/name, CR-id, format code
      (1 text, 2 image, 3 video), first/last shown, preview. "2" in the
      response is a pagination token -> send back as "4".
  LookupService/GetCreativeById     {"1": AR-id, "2": CR-id, "5": {"1": 1, "2": 0, "3": 2356}}
      "22" = Ad funded by (payer), "8" = format code, "5" = variations
      (each with its own preview), "17" = per-region last-shown, "4" = last shown.
  LookupService/GetAdvertiserById   {"1": AR-id, "3": {"1": 1}}
  preview content.js (displayads-formats.googleusercontent.com, plain GET)
      the rendered ad. Text ads embed headline / display URL / description;
      display + video ads embed the final URL (with UTM campaign names) and
      the YouTube video id. This is the only place the ad COPY lives.

Not exposed anywhere (do not fabricate): spend, impressions, clicks,
keywords, targeting, sitelinks. Google's BigQuery public dataset
``bigquery-public-data.google_ads_transparency_center`` is the official
bulk route for the same creative metadata if this ever gets blocked.

Politeness: 1.5-3 s between RPCs, 0.5-1 s between preview GETs, exponential
back-off on 429 / CAPTCHA, everything cached in
``google_ads_transparency_cache.json`` so runs resume.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote

from curl_cffi import requests as creq

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "google_ads_transparency_cache.json")
BASE = "https://adstransparency.google.com"
REGION_IN = 2356
FORMAT_CODE = {1: "Text (Search)", 2: "Image (Display)", 3: "Video (YouTube)"}


class CaptchaError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

def load_cache() -> dict:
    c = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    for k in ("suggestions", "creatives", "advertisers", "enriched"):
        c.setdefault(k, {})
    return c


def save_cache(c: dict) -> None:
    tmp = CACHE + ".tmp"
    json.dump(c, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE)


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

class TransparencyClient:
    """Chrome-impersonating HTTP session against the Transparency Center RPCs."""

    def __init__(self, impersonate="chrome124", log=print):
        self.s = creq.Session(impersonate=impersonate)
        self.log = log
        self.calls = 0
        self.s.get(f"{BASE}/?region=IN", timeout=30)          # cookies
        self.h = {"content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                  "x-framework-xsrf-token": "", "x-same-domain": "1",
                  "origin": BASE, "referer": f"{BASE}/?region=IN"}

    def rpc(self, svc: str, body: dict, retries=4) -> dict:
        data = "f.req=" + quote(json.dumps(body, separators=(",", ":")))
        for attempt in range(retries):
            r = self.s.post(f"{BASE}/anji/_/rpc/{svc}?authuser=", headers=self.h, data=data, timeout=30)
            self.calls += 1
            blocked = r.status_code == 429 or "unusual traffic" in r.text[:3000].lower()
            if not blocked and r.status_code == 200:
                time.sleep(random.uniform(1.5, 3.0))
                return json.loads(r.text) if r.text.strip() else {}
            wait = 30 * (2 ** attempt)
            self.log(f"   {svc}: {'rate-limited' if blocked else 'HTTP '+str(r.status_code)} - backing off {wait}s")
            time.sleep(wait)
        raise CaptchaError(f"{svc} still blocked after {retries} retries")

    def get(self, url: str) -> str:
        r = self.s.get(url, timeout=30)
        time.sleep(random.uniform(0.5, 1.0))
        return r.text if r.status_code == 200 else ""


# --------------------------------------------------------------------------
# RPC parsing
# --------------------------------------------------------------------------

def _ts(v) -> str:
    try:
        return datetime.fromtimestamp(int(v["1"]), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


def parse_suggestions(raw: dict) -> dict:
    advertisers, domains = [], []
    for item in raw.get("1") or []:
        if "1" in item:
            a = item["1"]
            rng = (a.get("4") or {}).get("2") or {}
            advertisers.append({"name": a.get("1"), "advertiser_id": a.get("2"), "country": a.get("3"),
                                "ads_min": int(rng.get("1", 0) or 0), "ads_max": int(rng.get("2", 0) or 0)})
        elif "2" in item:
            domains.append(item["2"].get("1"))
    return {"advertisers": advertisers, "domains": domains}


def _preview_of(prev: dict) -> str:
    if "1" in prev:
        return (prev["1"] or {}).get("4", "")
    if "3" in prev:
        m = re.search(r'src="([^"]+)"', (prev["3"] or {}).get("2", ""))
        return m.group(1) if m else ""
    return ""


def parse_creatives(raw: dict) -> tuple[list, str | None]:
    out = []
    for c in raw.get("1") or []:
        out.append({
            "advertiser_id": c.get("1"), "creative_id": c.get("2"),
            "advertiser_name": c.get("12", ""), "domain": c.get("14", ""),
            "format_code": c.get("4"), "format": FORMAT_CODE.get(c.get("4"), f"Unknown({c.get('4')})"),
            "first_shown": _ts(c.get("6") or {}), "last_shown": _ts(c.get("7") or {}),
            "regions_shown": c.get("13", ""),
            "preview_url": _preview_of(c.get("3") or {}),
            "creative_url": f"{BASE}/advertiser/{c.get('1')}/creative/{c.get('2')}?region=IN",
        })
    return out, raw.get("2")


def parse_creative_detail(raw: dict) -> dict:
    d = raw.get("1") or {}
    regions = d.get("17") or []
    return {"funded_by": (d.get("22") or {}).get("1", ""),
            "format_code_detail": d.get("8"),
            "variations": len(d.get("5") or []),
            "variation_previews": [_preview_of(v) for v in (d.get("5") or [])],
            "last_shown_detail": _ts(d.get("4") or {}),
            "regions": [r.get("1") for r in regions]}


# --------------------------------------------------------------------------
# preview payload parsing (the ad copy lives here)
# --------------------------------------------------------------------------

_STR_RE = re.compile(r'\\x22((?:[^\\]|\\.)*?)\\x22')
_TEXT_BLOCK_RE = re.compile(r'361903925\\x22:\[(.{0,1500}?)\]', re.S)
_ADURL_RE = re.compile(r'adurl(?:\\x3d|=)(https?(?:%3A|:)[^\\"&\s]+)')
_YT_RE = re.compile(r'(?:youtube\.com/embed/|ytimg\.com/vi/|youtube\.com/watch\?v=|videoId\\x22:\\x22)([A-Za-z0-9_-]{11})')
_IMG_RE = re.compile(r'https://tpc\.googlesyndication\.com/(?:simgad|archive/simgad|daca_images)/[^\\"\s\']+')


def _unescape(t: str) -> str:
    for a, b in (("\\x22", '"'), ("\\x27", "'"), ("\\x3d", "="), ("\\x26", "&"), ("\\x3c", "<"),
                 ("\\x3e", ">"), ("\\/", "/"), ("\\n", " "), ("\\u0026", "&")):
        t = t.replace(a, b)
    return t


def parse_preview(text: str) -> dict:
    out = {"headlines": [], "descriptions": [], "display_url": "", "final_url": "", "youtube_id": "",
           "image_url": "", "utm_source": "", "utm_medium": "", "utm_campaign": "", "content_format": ""}
    for m in _TEXT_BLOCK_RE.finditer(text):
        strs = [_unescape(x) for x in _STR_RE.findall(m.group(1))]
        strs = [x for x in strs if x and not x.startswith("http")]
        if len(strs) >= 2:
            out["headlines"].append(strs[0])
            if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", strs[1]):
                out["display_url"] = strs[1]
                if len(strs) > 2:
                    out["descriptions"].append(strs[2])
            else:
                out["descriptions"].append(strs[1])
    m = _ADURL_RE.search(text)
    if m:
        out["final_url"] = unquote(unquote(m.group(1)))
        for k in ("utm_source", "utm_medium", "utm_campaign"):
            mm = re.search(rf"{k}=([^&#\s]+)", out["final_url"])
            if mm:
                out[k] = mm.group(1)[:80]
    m = _YT_RE.search(text)
    if m:
        out["youtube_id"] = m.group(1)
    m = _IMG_RE.search(text)
    if m:
        out["image_url"] = _unescape(m.group(0))
    if out["headlines"]:
        out["content_format"] = "Text (Search)"
    elif out["youtube_id"]:
        out["content_format"] = "Video (YouTube)"
    elif "html5" in text.lower() or "\\x3cvideo" in text:
        out["content_format"] = "Display (HTML5 banner)"
    elif out["image_url"] or "simgad" in text:
        out["content_format"] = "Image (Display)"
    return out


# --------------------------------------------------------------------------
# extra content for non-text creatives
# --------------------------------------------------------------------------

_ocr = None


def youtube_meta(cl: "TransparencyClient", video_id: str) -> dict:
    """Title + channel of the YouTube video behind a video ad (oEmbed, no auth)."""
    try:
        r = cl.s.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json", timeout=20)
        if r.status_code == 200:
            j = r.json()
            return {"youtube_title": j.get("title", ""), "youtube_channel": j.get("author_name", "")}
    except Exception:
        pass
    return {"youtube_title": "", "youtube_channel": ""}


def image_ocr(cl: "TransparencyClient", url: str) -> str:
    """On-image text of a display banner via RapidOCR (local, ONNX)."""
    global _ocr
    try:
        if _ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr = RapidOCR()
        r = cl.s.get(url, timeout=30)
        if r.status_code != 200 or len(r.content) < 500:
            return ""
        import numpy as np
        import cv2
        img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return ""
        res, _ = _ocr(img)
        return " | ".join(t[1].strip() for t in (res or []) if t[1].strip())[:500]
    except Exception:
        return ""


# --------------------------------------------------------------------------
# stage 1: advertisers + creative lists
# --------------------------------------------------------------------------

def _brand_tokens(brand: str) -> list[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z]{4,}", brand)]


_CORP_RE = re.compile(r"jewel|gold|gems|diamond|trading|limited|ltd|pvt|private|company|fashion|lifestyle", re.I)


def _entities_for(brand: dict, cache: dict) -> list[dict]:
    """Verified Google advertisers for a brand. Uses the explicit
    ``google_entity_ids`` allowlist when the config has one (even if empty,
    which means 'agency-only brand'); otherwise a strict name match that
    needs a brand token AND a corporate/jewellery word, so persons named
    Tanishq and namesakes like KALYANI JEWELLERS are excluded."""
    queries = list(dict.fromkeys(list(brand.get("google_advertiser_queries") or []) + [brand["brand"]]))
    pool = {}
    for q in queries:
        for a in cache["suggestions"].get(q, {}).get("advertisers", []):
            pool[a["advertiser_id"]] = a
    if "google_entity_ids" in brand:
        return [pool[i] if i in pool else cache["advertisers"].get(i, {"advertiser_id": i, "name": i, "country": "IN", "ads_min": 0, "ads_max": 0})
                for i in brand["google_entity_ids"]]
    tokens = _brand_tokens(brand["brand"])
    return [a for a in pool.values() if a["country"] == "IN" and any(t in (a["name"] or "").lower() for t in tokens)
            and _CORP_RE.search(a["name"] or "")]


def collect(brands: list[dict], creative_pages=2, client: TransparencyClient | None = None, log=print) -> dict:
    cache = load_cache()
    cl = client or TransparencyClient(log=log)
    try:
        for b in brands:
            log(f"== {b['brand']}")
            queries = list(dict.fromkeys(list(b.get("google_advertiser_queries") or []) + [b["brand"]]))
            for q in queries:
                if q in cache["suggestions"]:
                    continue
                raw = cl.rpc("SearchService/SearchSuggestions", {"1": q, "2": 10, "3": 10, "4": [REGION_IN], "5": {"1": 1}})
                cache["suggestions"][q] = parse_suggestions(raw)
                save_cache(cache)
                log(f"   suggest '{q}': {len(cache['suggestions'][q]['advertisers'])} advertisers")

            targets = [("domain", d) for d in b.get("google_domains") or []]
            for a in _entities_for(b, cache):
                targets.append(("advertiser", a["advertiser_id"]))
                cache["advertisers"][a["advertiser_id"]] = a
            for kind, key in dict.fromkeys(targets):
                ck = f"{kind}:{key}"
                if ck in cache["creatives"] and len(cache["creatives"][ck]) >= 40 * creative_pages:
                    continue
                if ck in cache["creatives"] and len(cache["creatives"][ck]) % 40:
                    continue                                    # fewer than a page = exhausted
                rows, token = [], None
                for _ in range(creative_pages):
                    filt = {"8": [REGION_IN], "12": {"1": key if kind == "domain" else "", "2": True}}
                    if kind == "advertiser":
                        filt["13"] = {"1": [key]}
                    req = {"2": 40, "3": filt, "7": {"1": 1, "2": 0, "3": REGION_IN}}
                    if token:
                        req["4"] = token
                    got, token = parse_creatives(cl.rpc("SearchService/SearchCreatives", req))
                    rows.extend(got)
                    if not token or not got:
                        break
                cache["creatives"][ck] = rows
                save_cache(cache)
                log(f"   creatives {ck}: {len(rows)}")
    except CaptchaError as e:
        log(f"!! {e} - progress cached, rerun later (or --headed)")
    finally:
        save_cache(cache)
    return cache


# --------------------------------------------------------------------------
# stage 2: per-creative enrichment (copy, final URL, YouTube id, payer)
# --------------------------------------------------------------------------

def enrich(cache: dict, limit_per_target=40, details=True, client: TransparencyClient | None = None, log=print,
           brands: list[dict] | None = None) -> dict:
    cl = client or TransparencyClient(log=log)
    keys = None
    if brands:
        keys = set()
        for b in brands:
            keys |= {f"domain:{d}" for d in b.get("google_domains") or []}
            keys |= {f"advertiser:{a['advertiser_id']}" for a in _entities_for(b, cache)}
    todo = []
    for ck, rows in cache["creatives"].items():
        if keys is not None and ck not in keys:
            continue
        for c in rows[:limit_per_target]:
            done = cache["enriched"].get(c["creative_id"]) or {}
            if ("headlines" not in done or (details and "funded_by" not in done)
                    or (done.get("youtube_id") and "youtube_title" not in done)
                    or (done.get("image_url") and not done.get("headlines") and "image_text" not in done)):
                todo.append(c)
    log(f"enrich: {len(todo)} creatives to fetch")
    try:
        for n, c in enumerate(todo, 1):
            rec = cache["enriched"].get(c["creative_id"]) or {"content_format": c["format"]}
            if "headlines" not in rec:
                try:
                    if not c["preview_url"]:
                        rec.update(parse_preview(""))
                    elif "googlesyndication.com" in c["preview_url"]:
                        rec.update(parse_preview(""))
                        rec["image_url"], rec["content_format"] = c["preview_url"], "Image (Display)"
                    else:
                        rec.update(parse_preview(cl.get(c["preview_url"])))
                except Exception as e:
                    rec["error"] = str(e)[:150]
            if rec.get("youtube_id") and "youtube_title" not in rec:
                rec.update(youtube_meta(cl, rec["youtube_id"]))
            if rec.get("image_url") and not rec.get("headlines") and "image_text" not in rec:
                rec["image_text"] = image_ocr(cl, rec["image_url"])
            if details and "funded_by" not in rec:
                d = parse_creative_detail(cl.rpc("LookupService/GetCreativeById",
                                                 {"1": c["advertiser_id"], "2": c["creative_id"], "5": {"1": 1, "2": 0, "3": REGION_IN}}))
                rec.update(d)
                fmt = FORMAT_CODE.get(d.get("format_code_detail"))
                if fmt and not rec.get("headlines") and not rec.get("youtube_id"):
                    rec["content_format"] = fmt
            cache["enriched"][c["creative_id"]] = rec
            if n % 20 == 0 or n == len(todo):
                save_cache(cache)
                log(f"   {n}/{len(todo)}  ({cl.calls} rpc calls)")
    except CaptchaError as e:
        log(f"!! {e}")
    finally:
        save_cache(cache)
    return cache


# --------------------------------------------------------------------------
# stage 3: rows for the workbook
# --------------------------------------------------------------------------

def _ad_content(e: dict) -> str:
    """One always-populated description of what the creative says/shows."""
    if e.get("headlines"):
        return " | ".join(dict.fromkeys(e["headlines"])) + (" -- " + " | ".join(dict.fromkeys(e.get("descriptions", []))) if e.get("descriptions") else "")
    if e.get("youtube_title"):
        return f"[YouTube] {e['youtube_title']}"
    if e.get("image_text"):
        return f"[On-image text] {e['image_text']}"
    if e.get("youtube_id"):
        return "[YouTube video - title unavailable]"
    if e.get("image_url"):
        return "[Image ad - no readable text]"
    if e.get("final_url"):
        return f"[Display banner -> {e['final_url'][:120]}]"
    return "[not enriched yet]" if "headlines" not in e else "[No extractable content]"


def summarize(brands: list[dict], cache: dict) -> tuple[list[dict], list[dict]]:
    adv_rows, cre_rows = [], []
    enriched = cache.get("enriched") or {}
    for b in brands:
        queries = list(dict.fromkeys(list(b.get("google_advertiser_queries") or []) + [b["brand"]]))
        tokens = _brand_tokens(b["brand"])
        entities = _entities_for(b, cache)
        note = b.get("google_entity_note", "")
        domain_creatives = [c for d in b.get("google_domains") or [] for c in cache["creatives"].get(f"domain:{d}", [])]
        entity_creatives = [c for a in entities for c in cache["creatives"].get(f"advertiser:{a['advertiser_id']}", [])]
        allc = []
        for c in {c["creative_id"]: c for c in domain_creatives + entity_creatives}.values():
            e = enriched.get(c["creative_id"], {})
            allc.append({**c,
                         "format": e.get("content_format") or c["format"],
                         "headline": " | ".join(dict.fromkeys(e.get("headlines", [])))[:300],
                         "description": " | ".join(dict.fromkeys(e.get("descriptions", [])))[:400],
                         "display_url": e.get("display_url", ""), "final_url": e.get("final_url", ""),
                         "utm_source": e.get("utm_source", ""), "utm_medium": e.get("utm_medium", ""),
                         "utm_campaign": e.get("utm_campaign", ""),
                         "youtube_url": f"https://www.youtube.com/watch?v={e['youtube_id']}" if e.get("youtube_id") else "",
                         "image_url": e.get("image_url", ""), "funded_by": e.get("funded_by", ""),
                         "variations": e.get("variations", ""),
                         "youtube_title": e.get("youtube_title", ""), "youtube_channel": e.get("youtube_channel", ""),
                         "image_text": e.get("image_text", ""),
                         "ad_content": _ad_content(e)})
        agencies = sorted({c["advertiser_name"] for c in domain_creatives if c["advertiser_name"]
                           and not any(t in c["advertiser_name"].lower() for t in tokens)})
        fmts = {}
        for c in allc:
            fmts[c["format"]] = fmts.get(c["format"], 0) + 1
        lasts = [c["last_shown"] for c in allc if c["last_shown"]]
        firsts = [c["first_shown"] for c in allc if c["first_shown"]]
        adv_rows.append({
            "category": b["category"], "brand": b["brand"],
            "verified_entities": "; ".join(f"{a['name']} ({a['advertiser_id']})" for a in entities)
                                 or "None found (not a verified Google advertiser in IN under this name)",
            "verified_entity_count": len(entities),
            "entity_note": note,
            "ads_range_total": (f"{sum(a['ads_min'] for a in entities):,} - {sum(a['ads_max'] for a in entities):,}" if entities else ""),
            "domains_searched": ", ".join(b.get("google_domains") or []),
            "domain_ads_sampled": len(domain_creatives),
            "agencies_running_domain_ads": "; ".join(agencies),
            "funded_by": "; ".join(sorted({c["funded_by"] for c in allc if c["funded_by"]})),
            "creatives_sampled": len(allc),
            "format_mix": ", ".join(f"{k}: {v}" for k, v in sorted(fmts.items(), key=lambda x: -x[1])),
            "campaign_names": ", ".join(sorted({c["utm_campaign"] for c in allc if c["utm_campaign"]}))[:500],
            "sample_headlines": " | ".join(list(dict.fromkeys(h for c in allc for h in c["headline"].split(" | ") if h))[:8]),
            "youtube_videos": sum(1 for c in allc if c["youtube_url"]),
            "earliest_first_shown": min(firsts) if firsts else "",
            "latest_last_shown": max(lasts) if lasts else "",
            "transparency_url": (f"{BASE}/advertiser/{entities[0]['advertiser_id']}?region=IN" if entities
                                 else (f"{BASE}/?region=IN&domain={b['google_domains'][0]}" if b.get("google_domains") else "")),
        })
        for c in allc:
            cre_rows.append({"category": b["category"], "brand": b["brand"], **c})
    return adv_rows, cre_rows


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=2, help="creative pages (x40) per target")
    ap.add_argument("--only")
    ap.add_argument("--enrich", type=int, default=40, help="creatives per target to fetch ad copy for (0 = skip)")
    ap.add_argument("--no-details", action="store_true", help="skip GetCreativeById (payer / variations)")
    ap.add_argument("--enrich-only", action="store_true", help="skip the list stage; just fill copy / titles / OCR / payer")
    ap.add_argument("--config", default=os.path.join(ROOT, "catalog", "jewellery_ads_brand_config.json"))
    ap.add_argument("--cache", default=None, help="cache path (default google_ads_transparency_cache.json)")
    a = ap.parse_args()
    if a.cache:
        CACHE = a.cache
    cfg = json.load(open(a.config, encoding="utf-8"))["brands"]
    if a.only:
        cfg = [b for b in cfg if b["brand"] in a.only.split(",")]
    cl = TransparencyClient()
    cache = load_cache() if a.enrich_only else collect(cfg, creative_pages=a.pages, client=cl)
    if a.enrich:
        cache = enrich(cache, limit_per_target=a.enrich, details=not a.no_details, client=cl, brands=cfg)
    adv, cre = summarize(cfg, cache)
    print(f"\n{cl.calls} RPC calls total")
    for r in adv:
        print(f"{r['brand']:<28} entities={r['verified_entity_count']} ads={r['ads_range_total']:<16} sampled={r['creatives_sampled']:<4} "
              f"agencies={r['agencies_running_domain_ads'][:45]} | funded={r['funded_by'][:40]}")
