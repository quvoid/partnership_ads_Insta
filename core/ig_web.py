# -*- coding: utf-8 -*-
"""
Instagram web-client data routes (verified 2026-09-28).

Instagram now refuses the old REST endpoints to web sessions - `/api/v1/usertags/{pk}/feed/`,
`/api/v1/users/{pk}/info/` and `web_profile_info` answer `429 "Page Not Found"` or
`400 feedback_required` for every account, network and client tried, including a real
logged-in Chrome. The website itself no longer calls them. These are the routes it does use:

  Tagged tab     GraphQL PolarisProfileTaggedTabContentQuery (+ _connection for pages 2+)
  Post detail    the post page HTML embeds xdt_api__v1__media__shortcode__web_info:
                 taken_at, is_paid_partnership, sponsor_tags, usertags, counts
  Followers      the rendered profile header: span[title] holds the exact count
                 ("181,646" while the text reads "181k") - rung 2 of the AGENTS.md ladder

Tagged-tab nodes carry no taken_at; the post id encodes it (see ts_from_pk), accurate to a
few minutes, which is enough to decide the date window before paying for a detail fetch.
"""
import json, re, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests as cffi

from core.profile_auditor import COOKIES
from core.creator_deep_scan import WEB_UA, _graphql

TAGGED_DOC_FIRST = "28390247837269928"   # PolarisProfileTaggedTabContentQuery
TAGGED_DOC_CONN = "28412176455057653"    # PolarisProfileTaggedTabContentQuery_connection
_TAGGED_RELAY = {"__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False}
IG_EPOCH_MS = 1314220021721              # Instagram's snowflake epoch

_S = None


def _session():
    global _S
    if _S is None:
        _S = cffi.Session(impersonate="chrome120")
    return _S


def ts_from_pk(pk) -> int:
    """Unix seconds encoded in a media pk. Matched real taken_at within 4 minutes on
    every post checked; use it for window decisions, not as the published date."""
    return int(((int(pk) >> 23) + IG_EPOCH_MS) / 1000)


# ---- tagged tab --------------------------------------------------------------------
def tagged_page(handle: str, pk: str, tokens: Dict[str, Any],
                after: Optional[str] = None) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], str]:
    """One page of the Tagged tab. Returns (nodes, next_cursor, error). nodes is None
    on failure, so a refusal can never be mistaken for an empty page."""
    if after is None:
        friendly, doc = "PolarisProfileTaggedTabContentQuery", TAGGED_DOC_FIRST
        v = {"count": 12, "user_id": str(pk), **_TAGGED_RELAY}
    else:
        friendly, doc = "PolarisProfileTaggedTabContentQuery_connection", TAGGED_DOC_CONN
        v = {"after": after, "before": None, "count": 12, "first": 12, "last": None,
             "user_id": str(pk), **_TAGGED_RELAY}
    try:
        r = _graphql(handle, friendly, doc, v, tokens, "tagged/")
    except Exception as e:
        return None, None, type(e).__name__
    if r.status_code != 200 or not r.text.lstrip().startswith("{"):
        return None, None, f"HTTP {r.status_code}"
    body = r.json()
    if body.get("errors") and not body.get("data"):
        return None, None, "graphql error: " + str(body["errors"])[:80]
    conn = (body.get("data") or {}).get("xdt_api__v1__usertags__user_id__feed_connection")
    if conn is None:
        return None, None, "unexpected response shape"
    nodes = [e.get("node") or {} for e in conn.get("edges") or []]
    pi = conn.get("page_info") or {}
    return nodes, (pi.get("end_cursor") if pi.get("has_next_page") else None), ""


# ---- post detail -------------------------------------------------------------------
def post_web_info(code: str) -> Optional[Dict[str, Any]]:
    """The media item embedded in /p/{code}/, matched on its own code - the page also
    embeds other posts, so a bare regex over the HTML can read the wrong one."""
    try:
        r = _session().get(f"https://www.instagram.com/p/{code}/",
                           headers={"User-Agent": WEB_UA, "accept-language": "en-US,en;q=0.9"},
                           cookies=COOKIES, timeout=25)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    t = r.text
    i = t.find('"xdt_api__v1__media__shortcode__web_info"')
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(t[t.find("{", i + 40):])
    except Exception:
        return None
    for it in obj.get("items") or []:
        if it.get("code") == code:
            return it
    return None


# ---- exact followers from the rendered header --------------------------------------
_HEADER_JS = r"""() => {
  const out = {followers: null, text: null, lines: []};
  const header = document.querySelector('header');
  if (!header) return out;
  for (const el of header.querySelectorAll('*')) {
    const t = (el.innerText || '').trim().toLowerCase();
    if (/^[\d.,km]+\s+followers?$/.test(t)) {
      const s = el.querySelector('span[title]');
      out.text = t; out.followers = s ? s.getAttribute('title') : null;
      break;
    }
  }
  out.lines = (header.innerText || '').split('\n').map(x => x.trim()).filter(Boolean).slice(0, 14);
  return out;
}"""


def dom_profiles(handles: List[str], categories: Optional[set] = None, pause_ms: int = 2500,
                 log=print) -> Dict[str, Dict[str, Any]]:
    """Exact follower count (+ category, when the header line matches a known Instagram
    category) for each handle, from one headless browser session."""
    import asyncio
    from playwright.async_api import async_playwright
    cats = {c.lower(): c for c in (categories or set())}
    out: Dict[str, Dict[str, Any]] = {}

    async def launch(p):
        b = await p.chromium.launch(headless=True)
        c = await b.new_context(user_agent=WEB_UA)
        await c.add_cookies([{"name": k, "value": v, "domain": ".instagram.com", "path": "/"}
                             for k, v in COOKIES.items()])
        return b, await c.new_page()

    async def read_one(pg, h):
        rec = {"followers": None, "followers_precision": "unresolved (page did not render)",
               "ig_category": "", "name": ""}
        resp = await pg.goto(f"https://www.instagram.com/{h}/", wait_until="domcontentloaded", timeout=30000)
        if resp and resp.status == 404:
            rec["followers_precision"] = "unresolved (profile not found)"
            return rec
        try:
            await pg.wait_for_selector("header span[title]", timeout=9000)
        except Exception:
            pass
        d = await pg.evaluate(_HEADER_JS)
        n = re.sub(r"[^\d]", "", d.get("followers") or "")
        if n:
            rec["followers"] = int(n)
            rec["followers_precision"] = "exact"
            rec["followers_source"] = "dom_span_title"
            rec["resolved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = d.get("lines") or []
        for ln in lines:
            if ln.lower() in cats:
                rec["ig_category"] = cats[ln.lower()]
                break
        if lines and lines[0].lower() == h.lower() and len(lines) > 1:
            rec["name"] = lines[1] if lines[1].lower() not in cats else ""
        return rec

    async def run():
        # A crashed page or browser must not throw away the handles already read:
        # results accumulate in `out`, and a dead browser is relaunched, one retry per handle.
        async with async_playwright() as p:
            b, pg = await launch(p)
            for i, h in enumerate(handles, 1):
                for attempt in range(2):
                    try:
                        out[h] = await read_one(pg, h)
                        break
                    except Exception as e:
                        out[h] = {"followers": None, "ig_category": "", "name": "",
                                  "followers_precision": f"unresolved ({type(e).__name__})"}
                        try:
                            await b.close()
                        except Exception:
                            pass
                        b, pg = await launch(p)
                if i % 10 == 0:
                    log(f"    followers (browser): {i}/{len(handles)}")
                try:
                    await pg.wait_for_timeout(pause_ms)
                except Exception:
                    b, pg = await launch(p)
            try:
                await b.close()
            except Exception:
                pass

    asyncio.run(run())
    return out


# ---- reels tab ---------------------------------------------------------------------
# Creator-owned collab reels (the creator posts, the brand accepts the co-author invite)
# appear on the brand's Reels tab but NOT on its main Posts grid - verified 2026-10-07 on
# @sencogoldanddiamonds, whose Posts-grid query returned none of its creator collabs while
# the Reels tab's first page held three. The Posts grid alone undercounts collab reels.
REELS_DOC_FIRST = "28217628591240469"    # PolarisProfileReelsTabContentQuery
REELS_DOC_CONN = "28989741540630190"     # PolarisProfileReelsTabContentQuery_connection


def reels_tab_page(handle: str, pk: str, tokens: Dict[str, Any],
                   after: Optional[str] = None) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], str]:
    """One page of the brand's Reels tab -> (media list, next cursor, error); None on failure."""
    data = {"include_feed_video": True, "page_size": 12, "target_user_id": str(pk)}
    if after is None:
        friendly, doc = "PolarisProfileReelsTabContentQuery", REELS_DOC_FIRST
        v = {"data": data, "user_id": str(pk), **_TAGGED_RELAY}
    else:
        friendly, doc = "PolarisProfileReelsTabContentQuery_connection", REELS_DOC_CONN
        v = {"after": after, "before": None, "data": data, "first": 12, "last": None,
             "id": str(pk), **_TAGGED_RELAY}
    try:
        r = _graphql(handle, friendly, doc, v, tokens, "reels/")
    except Exception as e:
        return None, None, type(e).__name__
    if r.status_code != 200 or not r.text.lstrip().startswith("{"):
        return None, None, f"HTTP {r.status_code}"
    body = r.json()
    conn = (((body.get("data") or {}).get("fetch__XDTUserDict") or {}).get("clips_connection")
            or ((body.get("data") or {}).get("xdt_api__v1__clips__user__connection_v2")))
    if conn is None:
        return None, None, "unexpected response shape: " + ",".join((body.get("data") or {}).keys())
    media = [((e.get("node") or {}).get("media") or e.get("node") or {}) for e in conn.get("edges") or []]
    pi = conn.get("page_info") or {}
    return media, (pi.get("end_cursor") if pi.get("has_next_page") else None), ""
