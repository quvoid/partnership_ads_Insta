# -*- coding: utf-8 -*-
"""
================================================================================
BRAND COLLAB SCAN (HTTP) - every creator post for a brand in a date window, 4-tiered
================================================================================
Pure HTTP through the user's session (no browser). For each brand:

  1. Tagged-in feed   api/v1/usertags/{pk}/feed/   posts by OTHER accounts that
                      tag the brand - the bulk of creator collabs. Paginated
                      with max_id until the window start is passed.
  2. Brand's own feed PolarisProfilePostsQuery (GraphQL) - the brand's posts
                      that credit a creator (co-author or tagged account).
  3. Exact followers  api/v1/users/{pk}/info/ for each creator (cached in the
                      creator store), so the size band is never rounded.
  4. Views            play_count on reels where Instagram exposes it. Photos and
                      carousels have no public view count - blank, never guessed.

Tiers (same rule as the legacy 4-tier scanner, no emojis):
  Toggle  = Meta's "Paid partnership" label on the post
  Boosted = engagement shape looks like ad distribution (see boost_reason)
  Tier 1  Toggle ON  + Boosted      Tier 2  Toggle ON  + Organic
  Tier 3  Toggle OFF + Boosted      Tier 4  Toggle OFF + Organic (noise: fans, UGC)

  python core/brand_collab_scan.py --brands a,b,c --from 2026-04-01 --to 2026-09-19 --xlsx OUT.xlsx
  python core/brand_collab_scan.py --file brands.txt --from 2026-04-01 --xlsx OUT.xlsx
  --own-only   skip the tagged-in feed; only the brand's own posts that credit a creator (fast)
Resumable: results are cached per brand in brand_scan_cache.json; rerun to continue.
================================================================================
"""
import sys, os, re, json, time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from curl_cffi import requests as cffi
from core.profile_auditor import COOKIES, ANDROID_UA, IG_APP_ID, is_throttled, fetch_user_info, classify_tier
from core.creator_deep_scan import fetch_tokens, _graphql, POSTS_DOC_ID, _RELAY, _parse_conn, fetch_reel_views
from core.discovery_sources import clean_handle
from core import creator_db as cdb
from core import ig_web

CACHE = os.path.join(BASE_DIR, "brand_scan_cache.json")
PAUSE = 1.5
MAX_TAG_PAGES = 500          # 12 posts per page -> 6,000 tagged posts per brand
MAX_OWN_PAGES = 150
MAX_FOLLOWER_LOOKUPS = 1500   # per brand, largest posts first; the rest stay "not resolved"
AD_TAGS = re.compile(r'#(ad|ads|advert|advertisement|sponsored|paidpartnership|paidpromotion|collab|collaboration|gifted|partner|brandpartner)\b', re.I)

_S = None
def _session():
    global _S
    if _S is None:
        _S = cffi.Session(impersonate="chrome120")
    return _S

def _hdr():
    return {"User-Agent": ANDROID_UA, "x-ig-app-id": IG_APP_ID, "x-csrftoken": COOKIES["csrftoken"]}


# ---- tiering ---------------------------------------------------------------------
def evaluate(is_paid_toggle: bool, views: Optional[int], likes: Optional[int], comments: int,
             followers: Optional[int], caption: str, likes_hidden: bool) -> Dict[str, Any]:
    likes_n = likes if (likes is not None and not likes_hidden) else None
    like_rate = (likes_n / views * 100) if (views and likes_n is not None) else None
    view_mult = (views / followers) if (views and followers) else None
    er = ((likes_n + comments) / followers * 100) if (followers and likes_n is not None) else None
    has_ad = bool(AD_TAGS.search(caption or ""))

    boosted, reason = False, "Organic engagement pattern"
    if views and like_rate is not None and views >= 1_000_000 and like_rate < 0.35:
        boosted, reason = True, f"1M+ views ({views:,}) with like rate {like_rate:.2f}% - ThruPlay ad signature"
    elif view_mult is not None and er is not None and view_mult >= 5.0 and er < 1.0:
        boosted, reason = True, f"Views {view_mult:.1f}x followers with ER {er:.2f}% - paid distribution"
    elif views and like_rate is not None and views >= 500_000 and like_rate < 0.50:
        boosted, reason = True, f"{views:,} views vs {likes_n:,} likes - disproportionate reach"
    elif likes_n is not None and likes_n >= 50_000:
        boosted, reason = True, f"{likes_n:,} likes - scale consistent with paid support"
    elif has_ad and ((views or 0) >= 100_000 or (likes_n or 0) >= 5_000):
        boosted, reason = True, "Ad-tagged caption with paid-scale reach"
    elif views is None and likes_n is None:
        reason = "No public views or likes - cannot assess boost"

    if is_paid_toggle:
        tier, name = (1, "Tier 1: Toggle ON + Boosted") if boosted else (2, "Tier 2: Toggle ON + Organic")
    else:
        tier, name = (3, "Tier 3: Toggle OFF + Boosted") if boosted else (4, "Tier 4: Toggle OFF + Organic")
    return {"tier": tier, "tier_name": name, "is_boosted": boosted, "boost_reason": reason,
            "like_rate_pct": round(like_rate, 2) if like_rate is not None else None,
            "view_multiplier": round(view_mult, 2) if view_mult is not None else None,
            "er_pct": round(er, 2) if er is not None else None, "has_ad_tag": has_ad}


# ---- record shaping --------------------------------------------------------------
def _views_of(n: Dict[str, Any]) -> Optional[int]:
    for k in ("play_count", "ig_play_count", "view_count"):
        v = n.get(k)
        if isinstance(v, int) and v > 0:
            return v
    return None

def _record(n: Dict[str, Any], brand: str, source: str, creator: Dict[str, Any]) -> Dict[str, Any]:
    cap = ((n.get("caption") or {}).get("text") or "") if isinstance(n.get("caption"), dict) else (n.get("caption") or "")
    mt = n.get("media_type"); pt = n.get("product_type") or ""
    kind = "Reel" if (pt == "clips" or mt == 2) else ("Carousel" if mt == 8 else "Photo")
    sponsors = [s.get("username") or (s.get("sponsor") or {}).get("username") for s in (n.get("sponsor_tags") or [])]
    coauthors = [c.get("username") for c in (n.get("coauthor_producers") or []) if c.get("username")]
    tagged = [u["user"]["username"] for u in ((n.get("usertags") or {}).get("in") or []) if u.get("user", {}).get("username")]
    return {
        "brand": brand, "source": source, "code": n.get("code"),
        "post_url": f"https://www.instagram.com/{'reel' if kind == 'Reel' else 'p'}/{n.get('code')}/",
        "taken_at": n.get("taken_at"), "kind": kind,
        "creator": creator.get("username"), "creator_pk": str(creator.get("pk") or creator.get("id") or ""),
        "creator_name": creator.get("full_name") or "", "creator_verified": bool(creator.get("is_verified")),
        "likes": n.get("like_count"), "comments": n.get("comment_count") or 0, "views": _views_of(n),
        "likes_hidden": bool(n.get("like_and_view_counts_disabled")),
        "paid_flag": bool(n.get("is_paid_partnership")),
        "is_paid_partnership": bool(n.get("is_paid_partnership")) and bool([s for s in sponsors if s]),
        "sponsors": [s for s in sponsors if s], "coauthors": coauthors, "tagged": tagged,
        "reposts": n.get("media_repost_count"), "caption": cap[:300].replace("\n", " "),
    }


# ---- feeds -----------------------------------------------------------------------
INCLUDE_ALL = False   # --include-all: keep sub-brand / company accounts in the brand tabs, date up front

TAGGED_INCOMPLETE: Dict[str, str] = {}   # brand -> why the tagged feed stopped early


def walk_tagged(brand: str, pk: str, start_ts: int, log, tokens: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Posts by other accounts that tag the brand - the website's Tagged tab.

    Uses the GraphQL query the site itself runs; the REST usertags endpoint it replaced
    now refuses every web session. Tab nodes have no taken_at, so the window is decided
    from the post id first and only in-window posts pay for a detail fetch, which supplies
    the exact timestamp and the paid-partnership fields.
    """
    TAGGED_INCOMPLETE.pop(brand, None)
    tokens = tokens or fetch_tokens(brand)
    out, cursor, pages, candidates = [], None, 0, []
    while pages < MAX_TAG_PAGES:
        nodes, nxt, err = ig_web.tagged_page(brand, pk, tokens, cursor)
        pages += 1
        if nodes is None:
            log(f"    tagged feed: {err} on page {pages}; pausing 30s")
            time.sleep(30)
            nodes, nxt, err = ig_web.tagged_page(brand, pk, tokens, cursor)
            if nodes is None:
                # A refusal is not an empty feed: record it so the brand is not marked OK
                # and a rerun resumes, instead of caching "0 posts" as a finding.
                TAGGED_INCOMPLETE[brand] = f"tagged feed refused ({err}) on page {pages}"
                log("    tagged feed: INCOMPLETE - still refused after retry; brand will be marked PARTIAL")
                break
        oldest = None
        for n in nodes:
            ts = ig_web.ts_from_pk(n.get("pk") or 0)
            oldest = ts if oldest is None else min(oldest, ts)
            if ts >= start_ts - 3600:          # an hour of slack for the id-derived time
                candidates.append(n)
        if not nxt or (oldest is not None and oldest < start_ts):
            break
        cursor = nxt
        time.sleep(PAUSE)
    detailed = 0
    for n in candidates:
        it = ig_web.post_web_info(n.get("code") or "")
        if it:
            merged = dict(n); merged.update(it); detailed += 1
        else:
            merged = dict(n); merged["taken_at"] = ig_web.ts_from_pk(n.get("pk") or 0)
            merged["_detail_missing"] = True
        if (merged.get("taken_at") or 0) >= start_ts:
            rec = _record(merged, brand, "tagged_in", merged.get("user") or n.get("user") or {})
            if merged.get("_detail_missing"):
                rec["detail_note"] = "post detail unavailable - paid label and date from tab data only"
            out.append(rec)
        time.sleep(PAUSE)
    log(f"    tagged feed: {pages} pages, {len(out)} posts in window ({detailed}/{len(candidates)} with full detail)")
    return out


def walk_reels_tab(brand: str, tokens: Dict[str, Any], start_ts: int, log) -> List[Dict[str, Any]]:
    """Collab reels owned by someone else that sit on the brand's Reels tab. These never
    reach the Posts grid query, so walk_own() alone misses them (see ig_web.reels_tab_page)."""
    pk = str(tokens["pk"])
    out, cursor, pages, cands = [], None, 0, []
    while pages < MAX_OWN_PAGES:
        media, nxt, err = ig_web.reels_tab_page(brand, pk, tokens, cursor)
        pages += 1
        if media is None:
            log(f"    reels tab: {err} on page {pages}; pausing 30s")
            time.sleep(30)
            media, nxt, err = ig_web.reels_tab_page(brand, pk, tokens, cursor)
            if media is None:
                TAGGED_INCOMPLETE[brand] = f"reels tab refused ({err}) on page {pages}"
                log("    reels tab: INCOMPLETE - still refused after retry; brand will be marked PARTIAL")
                break
        oldest = None
        for m in media:
            ts = ig_web.ts_from_pk(m.get("pk") or 0)
            oldest = ts if oldest is None else min(oldest, ts)
            owner = str((m.get("user") or {}).get("pk") or "")
            if owner and owner != pk and ts >= start_ts - 3600:
                cands.append(m)
        if not nxt or (oldest is not None and oldest < start_ts):
            break
        cursor = nxt
        time.sleep(PAUSE)
    detailed = 0
    for m in cands:
        it = ig_web.post_web_info(m.get("code") or "")
        if not it:
            continue                      # cannot name the creator without the detail page
        merged = dict(m); merged.update(it); detailed += 1
        if not merged.get("play_count") and m.get("play_count"):
            merged["play_count"] = m["play_count"]
        if (merged.get("taken_at") or 0) >= start_ts:
            rec = _record(merged, brand, "reels_tab_collab", merged.get("user") or {})
            rec["coauthors"] = rec.get("coauthors") or [brand]
            out.append(rec)
        time.sleep(PAUSE)
    log(f"    reels tab: {pages} pages, {len(out)} creator-owned collab reels in window ({detailed}/{len(cands)} detailed)")
    return out


def walk_own(brand: str, tokens: Dict[str, Any], start_ts: int, log) -> List[Dict[str, Any]]:
    """Brand's own posts that credit another account (co-author or tag)."""
    out, pages, cursor = [], 0, None
    from core.creator_deep_scan import _DATA, CONN_DOC_ID
    while pages < MAX_OWN_PAGES:
        if cursor is None:
            v = {"data": dict(_DATA), "username": brand, **_RELAY}
            r = _graphql(brand, "PolarisProfilePostsQuery", POSTS_DOC_ID, v, tokens)
        else:
            v = {"after": cursor, "before": None, "data": dict(_DATA), "first": 12, "last": None,
                 "username": brand, **_RELAY}
            r = _graphql(brand, "PolarisProfilePostsTabContentQuery_connection", CONN_DOC_ID, v, tokens)
        pages += 1
        if r.status_code != 200 or is_throttled(r):
            log(f"    own feed: HTTP {r.status_code} on page {pages}; stopping own-feed walk")
            break
        nodes, pi = _parse_conn(r)
        cursor, has_next = pi.get('end_cursor'), bool(pi.get('has_next_page'))
        oldest = None
        for n in nodes:
            ts = n.get("taken_at") or 0
            if n.get("timeline_pinned_user_ids"):
                continue
            oldest = ts if oldest is None else min(oldest, ts)
            if ts < start_ts:
                continue
            author = n.get("user") or {}
            others = [c for c in (n.get("coauthor_producers") or []) if c.get("username") and c["username"].lower() != brand.lower()]
            tagged = [u["user"] for u in ((n.get("usertags") or {}).get("in") or [])
                      if u.get("user", {}).get("username") and u["user"]["username"].lower() != brand.lower()]
            if author.get("username") and author["username"].lower() != brand.lower():
                # the creator published it and invited the brand as co-author: it sits on the
                # brand's grid with user = creator. This is the commonest real collab shape.
                who, src = author, "creator_post_coauthored_with_brand"
            elif others:
                who, src = others[0], "brand_post_coauthor"
            elif tagged:
                who, src = tagged[0], "brand_post_tagged"
            else:
                who, src = None, ""
            if who:
                rec = _record(n, brand, src, who)
                if not rec["coauthors"]:
                    rec["coauthors"] = [c.get("username") for c in (n.get("coauthor_producers") or []) if c.get("username")]
                out.append(rec)
        if not has_next or not cursor or (oldest is not None and oldest < start_ts):
            break
        time.sleep(PAUSE)
    if any(r["kind"] == "Reel" and r["views"] is None for r in out):
        try:
            views = fetch_reel_views(brand, tokens["pk"], tokens)
            for r in out:
                if r["views"] is None and r["code"] in views and views[r["code"]]:
                    r["views"] = views[r["code"]]
        except Exception:
            pass
    log(f"    own feed: {pages} pages, {len(out)} creator-credited posts in window")
    return out



def _media_id(code: str) -> int:
    A = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    m = 0
    for ch in code:
        m = m * 64 + A.index(ch)
    return m


def fill_missing_views(recs: List[Dict[str, Any]], log=print) -> int:
    """Reels published by the creator (co-authored with the brand) are not on the
    brand's reels tab, so their play count is fetched per post instead."""
    todo = [r for r in recs if r.get("kind") == "Reel" and not r.get("views") and r.get("code")]
    filled = 0
    for i, r in enumerate(todo, 1):
        try:
            resp = _session().get(f"https://www.instagram.com/api/v1/media/{_media_id(r['code'])}/info/",
                                  headers=_hdr(), cookies=COOKIES, timeout=25)
            if resp.status_code == 200 and not is_throttled(resp):
                items = resp.json().get("items") or []
                if items:
                    v = _views_of(items[0])
                    if v:
                        r["views"] = v; filled += 1
                    # refresh likes/comments/reposts from the same payload
                    it = items[0]
                    if it.get("like_count") is not None and not it.get("like_and_view_counts_disabled"):
                        r["likes"] = it["like_count"]
                    if it.get("comment_count") is not None:
                        r["comments"] = it["comment_count"]
                    if it.get("media_repost_count") is not None:
                        r["reposts"] = it["media_repost_count"]
            elif is_throttled(resp):
                log("    media info throttled; pausing 30s"); time.sleep(30)
        except Exception:
            pass
        time.sleep(PAUSE)
        if i % 25 == 0:
            log(f"    views filled: {filled}/{i}")
    log(f"    views: filled {filled} of {len(todo)} creator-authored reels")
    return filled


# ---- followers -------------------------------------------------------------------
def resolve_followers(conn, recs: List[Dict[str, Any]], log) -> Dict[str, Dict[str, Any]]:
    """Exact count per creator, largest posts first, cached in the creator store."""
    by = {}
    for r in recs:
        if r["creator"] and r["creator_pk"]:
            by.setdefault(r["creator"], {"pk": r["creator_pk"], "best": 0})
            by[r["creator"]]["best"] = max(by[r["creator"]]["best"], (r["likes"] or 0) + (r["views"] or 0) // 20)
    order = sorted(by, key=lambda h: -by[h]["best"])
    res: Dict[str, Dict[str, Any]] = {}
    fresh_cut = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    looked = 0
    browser_todo: List[str] = []
    rest_refusals = 0
    for h in order:
        row = conn.execute("SELECT followers, followers_precision, resolved_at, email, phone, ig_category "
                           "FROM creators WHERE handle=?", (h,)).fetchone()
        if row and row["followers_precision"] == "exact" and (row["resolved_at"] or "") >= fresh_cut:
            res[h] = dict(row); continue
        if looked >= MAX_FOLLOWER_LOOKUPS:
            res[h] = dict(row) if row else {"followers": None, "followers_precision": "not resolved"}
            continue
        if rest_refusals >= 3:                  # REST is refusing: stop asking, use the browser
            res[h] = dict(row) if row else {"followers": None, "followers_precision": "unresolved"}
            browser_todo.append(h)
            continue
        info, err = fetch_user_info(by[h]["pk"])
        looked += 1
        rest_refusals = 0 if info else rest_refusals + 1
        time.sleep(PAUSE)
        if info:
            prof = {"handle": h, "pk": by[h]["pk"], "name": info.get("full_name") or "",
                    "followers": info.get("follower_count"), "followers_precision": "exact",
                    "followers_source": "users_info", "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "tier": classify_tier(info.get("follower_count") or 0),
                    "ig_category": info.get("category") or "", "bio": info.get("biography") or "",
                    "email": info.get("public_email") or "", "phone": info.get("contact_phone_number") or info.get("public_phone_number") or "",
                    "external_url": info.get("external_url") or "", "is_verified": 1 if info.get("is_verified") else 0,
                    "is_private": 1 if info.get("is_private") else 0, "media_count": info.get("media_count"),
                    "following_count": info.get("following_count"), "audit_status": "VERIFIED"}
            try:
                cdb.upsert_creator(conn, prof)
            except Exception:
                pass
            res[h] = {"followers": prof["followers"], "followers_precision": "exact", "email": prof["email"],
                      "phone": prof["phone"], "ig_category": prof["ig_category"]}
        else:
            res[h] = dict(row) if row else {"followers": None, "followers_precision": f"unresolved ({err})"}
            browser_todo.append(h)
        if looked % 25 == 0:
            log(f"    followers resolved: {looked}")
    # The REST lookup is refused to web sessions; read the exact count off the rendered
    # profile header instead (span[title] - rung 2 of the AGENTS.md ladder).
    if browser_todo:
        log(f"    followers: REST lookup refused for {len(browser_todo)}; reading the profile header")
        cats = {r[0] for r in conn.execute(
            "SELECT DISTINCT ig_category FROM creators WHERE ig_category IS NOT NULL AND ig_category != ''")}
        try:
            got = ig_web.dom_profiles(browser_todo, categories=cats, log=log)
        except Exception as e:
            log(f"    browser follower read failed: {type(e).__name__}: {str(e)[:200]}"); got = {}
        ok = 0
        for h, d in got.items():
            if d.get("followers_precision") != "exact":
                res[h] = {"followers": None, "followers_precision": d.get("followers_precision")}
                continue
            ok += 1
            prof = {"handle": h, "pk": by[h]["pk"], "name": d.get("name") or "",
                    "followers": d["followers"], "followers_precision": "exact",
                    "followers_source": d["followers_source"], "resolved_at": d["resolved_at"],
                    "tier": classify_tier(d["followers"]), "ig_category": d.get("ig_category") or "",
                    "audit_status": "VERIFIED"}
            try:
                cdb.upsert_creator(conn, prof)
            except Exception:
                pass
            prev = res.get(h) or {}
            res[h] = {"followers": d["followers"], "followers_precision": "exact",
                      "email": prev.get("email") or "", "phone": prev.get("phone") or "",
                      "ig_category": d.get("ig_category") or prev.get("ig_category") or ""}
        log(f"    followers: {ok}/{len(browser_todo)} exact from the profile header")
    log(f"    creators: {len(by)}, live lookups: {looked}")
    return res


# ---- per brand -------------------------------------------------------------------
def scan_brand(brand: str, start_ts: int, end_ts: int, conn, log=print, own_only: bool = False) -> Dict[str, Any]:
    brand = clean_handle(brand) or brand
    log(f"[{brand}]")
    tokens = fetch_tokens(brand)
    if not tokens.get("pk"):
        log("    profile not reachable"); return {"brand": brand, "status": "NOT FOUND", "posts": []}
    info, _ = fetch_user_info(tokens["pk"])
    brand_followers = (info or {}).get("follower_count")
    if brand_followers is None:
        try:
            d = ig_web.dom_profiles([brand], log=log).get(brand) or {}
            if d.get("followers_precision") == "exact":
                brand_followers = d["followers"]
        except Exception:
            pass
    brand_name = (info or {}).get("full_name") or brand
    time.sleep(PAUSE)
    recs = [] if own_only else walk_tagged(brand, tokens["pk"], start_ts, log, tokens)
    time.sleep(PAUSE)
    recs += walk_own(brand, tokens, start_ts, log)
    time.sleep(PAUSE)
    recs += walk_reels_tab(brand, tokens, start_ts, log)
    # de-dupe on code (a co-authored post shows in both feeds)
    seen, uniq = set(), []
    for r in sorted(recs, key=lambda x: x["source"] != "tagged_in"):
        if r["code"] in seen or not r["creator"] or r["creator"].lower() == brand.lower():
            continue
        seen.add(r["code"]); uniq.append(r)
    uniq = [r for r in uniq if start_ts <= (r["taken_at"] or 0) <= end_ts]
    fill_missing_views(uniq, log)
    fol = resolve_followers(conn, uniq, log)
    for r in uniq:
        f = fol.get(r["creator"], {})
        r["followers"] = f.get("followers"); r["followers_precision"] = f.get("followers_precision") or "not resolved"
        r["email"] = f.get("email") or ""; r["phone"] = f.get("phone") or ""; r["creator_category"] = f.get("ig_category") or ""
        r["size_band"] = classify_tier(r["followers"]) if r["followers"] else "Not resolved"
        r.update(evaluate(r["is_paid_partnership"], r["views"], r["likes"], r["comments"], r["followers"],
                          r["caption"], r["likes_hidden"]))
        try:
            cdb.add_brand_collab(conn, r["creator"], brand, r["post_url"], r["is_paid_partnership"],
                                 r["tier_name"], r["taken_at"])
        except Exception:
            pass
    conn.commit()
    tiers = {t: sum(1 for r in uniq if r["tier"] == t) for t in (1, 2, 3, 4)}
    log(f"    done: {len(uniq)} posts, {len({r['creator'] for r in uniq})} creators, tiers {tiers}")
    return {"brand": brand, "brand_name": brand_name, "brand_followers": brand_followers,
            "status": ("PARTIAL - " + TAGGED_INCOMPLETE[brand]) if brand in TAGGED_INCOMPLETE else "OK", "posts": uniq, "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}



# ---- who is the credited account? -------------------------------------------------
BRAND_ALIASES = ("godrej", "britannia", "castrol", "parle", "bajaj", "vivo", "visa", "croma",
                 "mccain", "adani", "odisha", "simpolo", "mochi", "boheco", "bridgestone", "nerolac",
                 "spectaquartz", "hajmola", "indriya", "louisphilippe", "aditya", "birla", "interio")
ORG_CATEGORY = re.compile(r"(compan|organi[sz]ation|store|shop|business|^brand$|product|service|retail|restaurant|"
                          r"hotel|dealership|agency|studio|theat|universit|college|website|magazine|sports team|"
                          r"nonprofit|government|^community$|electronics|food & beverage|travel|automotive|"
                          r"design & fashion|manufactur|bank|insurance|real estate|school|clinic|hospital|"
                          r"grocery|cafe|bar$|gym|salon|spa$|market|mall|news|media|broadcast|publisher|label|"
                          r"team$|club$|league|federation|association|institute|foundation|ministry|tourism)", re.I)

def _roots(brand: str):
    b = re.sub(r"[^a-z]", "", brand.lower())
    roots = {b[:6]} if len(b) >= 6 else {b}
    roots |= {a for a in BRAND_ALIASES if a in b}
    return roots

ORG_TEXT = re.compile(r"(official ?(instagram )?(page|account|handle)|pvt\.? ?ltd|private limited|ltd|inc|llp|"
                      r"corp|limited|company|group|dealership|showroom|car care|body shop|repairs?|"
                      r"customer care|toll.?free|head office|manufacturer|distributor|authori[sz]ed dealer|"
                      r"we are an? |our (products|stores|team)|est\.? ?(19|20)\d\d)", re.I)

def account_type(brand: str, handle: str, name: str, category: str, bio: str = "") -> str:
    """Own sub-brand / Company or organisation / Creator or individual."""
    h = re.sub(r"[^a-z]", "", (handle or "").lower()); n = re.sub(r"[^a-z]", "", (name or "").lower())
    if any(r and (r in h or r in n) for r in _roots(brand)):
        return "Own sub-brand"
    if category and ORG_CATEGORY.search(category):
        return "Company / organisation"
    if not category and (ORG_TEXT.search(name or "") or ORG_TEXT.search(bio or "")):
        return "Company / organisation"
    return "Creator / individual"

_BIO_CACHE: Dict[str, str] = {}
def _bio_of(handle: str) -> str:
    if handle not in _BIO_CACHE:
        try:
            row = cdb.connect().execute("SELECT bio FROM creators WHERE handle=?", (handle,)).fetchone()
            _BIO_CACHE[handle] = (row["bio"] if row else "") or ""
        except Exception:
            _BIO_CACHE[handle] = ""
    return _BIO_CACHE[handle]

# ---- workbook --------------------------------------------------------------------
def export(results: Dict[str, Dict[str, Any]], xlsx: str, start: datetime, end: datetime, order: List[str]):
    import openpyxl
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter
    from core.kolkata_engine import clean_cell
    bold = Font(bold=True)
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Summary"
    win = f"{start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}"
    ws.cell(1, 1, f"Brand partnership scan - window {win}").font = bold
    hdr = ["Brand", "Brand Followers (Exact)", "Creator Posts In Window", "Distinct Creators",
           "Tier 1: Toggle ON + Boosted", "Tier 2: Toggle ON + Organic", "Tier 3: Toggle OFF + Boosted",
           "Tier 4: Toggle OFF + Organic", "Paid Label Shown", "Paid Flag Only (no sponsor)", "Collab Posts (2+ profiles)",
           "Excluded: Own Sub-brand Posts", "Excluded: Company / Org Posts", "Top Creators (by followers)", "Scan Status"]
    for c, h in enumerate(hdr, 1):
        x = ws.cell(3, c, h); x.font = bold; x.alignment = Alignment(wrap_text=True, vertical="center")
    row = 4
    for b in order:
        res = results.get(b) or {"brand": b, "status": "not scanned", "posts": []}
        allp = res.get("posts", [])
        for p in allp:
            p["account_type"] = account_type(b, p["creator"], p.get("creator_name", ""), p.get("creator_category", ""), _bio_of(p["creator"]))
        posts = allp if INCLUDE_ALL else [p for p in allp if p["account_type"] == "Creator / individual"]
        n_sub = sum(1 for p in allp if p["account_type"] == "Own sub-brand")
        n_org = sum(1 for p in allp if p["account_type"] == "Company / organisation")
        tiers = {t: sum(1 for p in posts if p.get("tier") == t) for t in (1, 2, 3, 4)}
        creators = {}
        for p in posts:
            creators[p["creator"]] = max(creators.get(p["creator"], 0), p.get("followers") or 0)
        top = ", ".join(f"@{h} ({f:,})" for h, f in sorted(creators.items(), key=lambda x: -x[1])[:5])
        vals = [f"@{b}", res.get("brand_followers"), len(posts), len(creators), tiers[1], tiers[2], tiers[3], tiers[4],
                sum(1 for p in posts if p.get("is_paid_partnership")),
                sum(1 for p in posts if p.get("paid_flag") and not p.get("is_paid_partnership")),
                sum(1 for p in posts if p.get("coauthors")), n_sub, n_org, top, res.get("status")]
        for c, v in enumerate(vals, 1):
            ws.cell(row, c, clean_cell(v))
        row += 1
    for i, w in enumerate([26, 16, 14, 12, 16, 16, 16, 16, 12, 14, 14, 16, 16, 70, 12], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B4"

    cols = ["S.No", "Tier", "Collab Post (2+ profiles)", "Creator Handle", "Creator Profile URL", "Creator Name", "Creator Followers (Exact)",
            "Follower Precision", "Creator Size Band", "Creator IG Category", "Post URL", "Post Date", "Post Type",
            "Likes", "Comments", "Views (Reels Only)", "Reposts", "Paid Label Shown (with sponsor)", "Paid Flag Only (no sponsor named)", "Boosted",
            "Boost Reason", "Like Rate % (likes/views)", "Views x Followers", "ER % (likes+comments / followers)",
            "Ad Hashtag In Caption", "Sponsor Tags", "Co-Authors", "Found Via", "Creator Email", "Creator Phone", "Caption"]
    widths = [6, 28, 12, 26, 40, 24, 14, 12, 22, 20, 44, 12, 9, 10, 10, 14, 9, 14, 14, 9, 48, 12, 12, 14, 10, 20, 20, 18, 28, 16, 60]
    for b in order:
        res = results.get(b)
        if not res:
            continue
        title = re.sub(r'[\[\]\*\?/\\:]', '', b)[:31]
        w = wb.create_sheet(title)
        allp = res.get("posts", [])
        for p in allp:
            p.setdefault("account_type", account_type(b, p["creator"], p.get("creator_name", ""), p.get("creator_category", ""), _bio_of(p["creator"])))
        if INCLUDE_ALL:
            cposts = allp
            n_other = sum(1 for p in allp if p["account_type"] != "Creator / individual")
            w.cell(1, 1, f"@{b} - every account the brand collaborated with on its page {win} - {len(allp)} posts "
                         f"({n_other} of them with brands / companies / own sub-accounts - see Account Type)").font = bold
        else:
            cposts = [p for p in allp if p["account_type"] == "Creator / individual"]
            w.cell(1, 1, f"@{b} - creator posts {win} - {len(cposts)} creator posts ({len(allp) - len(cposts)} sub-brand / company posts moved to the Excluded Accounts sheet)").font = bold
        tcols = (["S.No", "Post Date", "Account Type"] + [c for c in cols if c not in ("S.No", "Post Date")]) if INCLUDE_ALL else cols
        for c, h in enumerate(tcols, 1):
            x = w.cell(3, c, h); x.font = bold; x.alignment = Alignment(wrap_text=True, vertical="center")
        if INCLUDE_ALL:     # newest first, so the date column reads as a timeline
            posts = sorted(cposts, key=lambda p: -(p.get("taken_at") or 0))
        else:
            posts = sorted(cposts, key=lambda p: (p.get("tier", 9), -(p.get("followers") or 0), -(p.get("taken_at") or 0)))
        for i, p in enumerate(posts, 1):
            likes = "Hidden by page" if p.get("likes_hidden") else p.get("likes")
            vals = [i, p.get("tier_name"), "Yes" if p.get("coauthors") else "No", p["creator"], f"https://www.instagram.com/{p['creator']}/", p.get("creator_name"),
                    p.get("followers"), p.get("followers_precision"), p.get("size_band"), p.get("creator_category"),
                    p["post_url"], datetime.fromtimestamp(p["taken_at"], tz=timezone.utc).strftime("%Y-%m-%d"),
                    p.get("kind"), likes, p.get("comments"), p.get("views") if p.get("views") is not None else "",
                    p.get("reposts") if p.get("reposts") is not None else "",
                    "ON" if p.get("is_paid_partnership") else "OFF",
                    ("Yes" if (p.get("paid_flag") and not p.get("is_paid_partnership")) else "No"),
                    "Yes" if p.get("is_boosted") else "No",
                    p.get("boost_reason"), p.get("like_rate_pct"), p.get("view_multiplier"), p.get("er_pct"),
                    "Yes" if p.get("has_ad_tag") else "No", ", ".join(p.get("sponsors") or []), ", ".join(p.get("coauthors") or []),
                    p.get("source"), p.get("email"), p.get("phone"), p.get("caption")]
            if INCLUDE_ALL:
                byc = dict(zip(cols, vals))
                byc["Account Type"] = p["account_type"]
                vals = [byc[c] for c in tcols]
            for c, v in enumerate(vals, 1):
                w.cell(i + 3, c, clean_cell(v)).alignment = Alignment(vertical="center")
        twidths = ([6, 12, 24] + [wd for c, wd in zip(cols, widths) if c not in ("S.No", "Post Date")]) if INCLUDE_ALL else widths
        for i, wd in enumerate(twidths, 1):
            w.column_dimensions[get_column_letter(i)].width = wd
        w.freeze_panes = "E4"
        if posts:
            w.auto_filter.ref = f"A3:{get_column_letter(len(tcols))}{len(posts) + 3}"

    ex = wb.create_sheet("Excluded Accounts")
    ex.cell(1, 1, "Accounts credited by the brand that are not creators: the brand's own sub-accounts, and companies / organisations. Kept here so nothing is lost.").font = bold
    exh = ["Brand", "Account Handle", "Account Name", "Followers (Exact)", "IG Category", "Why Excluded", "Posts In Window", "Latest Post URL"]
    for c, h in enumerate(exh, 1):
        ex.cell(3, c, h).font = bold
    rr = 4
    for b in order:
        res = results.get(b)
        if not res:
            continue
        agg = {}
        for p in res.get("posts", []):
            t = p.get("account_type") or account_type(b, p["creator"], p.get("creator_name", ""), p.get("creator_category", ""), _bio_of(p["creator"]))
            if t == "Creator / individual":
                continue
            a = agg.setdefault(p["creator"], {"name": p.get("creator_name"), "fol": p.get("followers"), "cat": p.get("creator_category"),
                                              "why": t, "n": 0, "url": p["post_url"], "ts": p.get("taken_at") or 0})
            a["n"] += 1
            if (p.get("taken_at") or 0) > a["ts"]:
                a["ts"], a["url"] = p["taken_at"], p["post_url"]
        for h, a in sorted(agg.items(), key=lambda x: (x[1]["why"], -x[1]["n"])):
            for c, v in enumerate([f"@{b}", h, a["name"], a["fol"], a["cat"], a["why"], a["n"], a["url"]], 1):
                ex.cell(rr, c, clean_cell(v))
            rr += 1
    for i, wd in enumerate([24, 28, 28, 14, 24, 24, 10, 44], 1):
        ex.column_dimensions[get_column_letter(i)].width = wd
    ex.freeze_panes = "C4"

    m = wb.create_sheet("Method")
    notes = [
        ("Window", win + ". A post counts if its publish date is inside the window."),
        ("Who counts as a creator", "The brand tabs list only accounts classed Creator / individual. Two kinds are moved to the "
                                    "Excluded Accounts sheet: Own sub-brand (the handle or name contains the brand's name, e.g. "
                                    "castrol.india under castrol.drive) and Company / organisation (Instagram category is a business "
                                    "type such as Product/service, Local business, Government organization, Travel Company). "
                                    "Accounts with no category are individuals unless their name or bio reads like a company "
                                    "(official page, Pvt Ltd, Inc, dealership, car care...)."),
        ("Collab Post (2+ profiles)", "Yes when the post was published jointly (Instagram's co-author feature), so two or more "
                                      "profile names appear in the header and it sits on both grids."),
        ("Where posts come from", "Brand's grid, three shapes. creator_post_coauthored_with_brand: the creator published it and "
                                  "invited the brand as co-author (the two-name header; the post lives on both grids). "
                                  "brand_post_coauthor: the brand published it and added the creator as co-author. "
                                  "brand_post_tagged: the brand's own post that tags the creator. Tagged-in feed (tagged_in) "
                                  "only when the scan is run without --own-only."),
        ("Creator Followers", "Exact, read live from Instagram for each creator. 'not resolved' = lookup cap reached or account unreachable; never estimated."),
        ("Views", "Public play count on reels only. Photos and carousels have no public view count on Instagram - left blank."),
        ("Likes", "'Hidden by page' when the creator turned off like counts; the placeholder Instagram returns is never printed."),
        ("Paid Label Shown (with sponsor)", "ON only when the post carries the paid-partnership flag AND names a sponsor brand, which is "
                                            "when Instagram actually renders 'Paid partnership with ...' on the post. This is the disclosure used for Tiers 1 and 2."),
        ("Paid Flag Only (no sponsor named)", "Instagram's paid-partnership flag is set but no sponsor is attached, and the label is not shown "
                                              "on the post. Ambiguous - kept as its own column, not counted as disclosed."),
        ("Boosted", "Engagement shape consistent with paid distribution. Any one of: 1M+ views with like rate under 0.35%; views over 5x followers "
                    "with ER under 1%; 500K+ views with like rate under 0.5%; 50K+ likes; #ad-style caption with 100K+ views or 5K+ likes. "
                    "Heuristic - a genuinely viral organic reel can trip it."),
        ("Tier 1", "Toggle ON + Boosted: declared paid deal and ad money behind it."),
        ("Tier 2", "Toggle ON + Organic: declared paid deal, ran organically."),
        ("Tier 3", "Toggle OFF + Boosted: undeclared but numbers say paid distribution. Check manually."),
        ("Tier 4", "Toggle OFF + Organic: mentions, UGC, contest entries, fan posts. Included for completeness; mostly noise."),
        ("Creator Size Band", "Nano <10K, Micro 10K-100K, Mid-Tier 100K-500K, Macro 500K-1M, Mega 1M+ on the exact count."),
    ]
    m.cell(1, 1, "Field").font = bold; m.cell(1, 2, "Definition").font = bold
    for i, (k, v) in enumerate(notes, 2):
        m.cell(i, 1, k); m.cell(i, 2, v).alignment = Alignment(wrap_text=True, vertical="top")
    m.column_dimensions["A"].width = 26; m.column_dimensions["B"].width = 120

    path = xlsx if (os.sep in xlsx or "/" in xlsx) else os.path.join(BASE_DIR, "deliverables", xlsx)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if INCLUDE_ALL and "Excluded Accounts" in wb.sheetnames:
            del wb["Excluded Accounts"]   # those accounts are in the brand tabs now
        wb.save(path)
    except PermissionError:
        path = path.replace(".xlsx", f"_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"); wb.save(path)
    print(f"[EXCEL] {path}")
    return path


# ---- cli -------------------------------------------------------------------------
def _load_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE):
        with open(CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_cache(c):
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__); sys.exit(0)
    brands: List[str] = []
    if "--brands" in a:
        brands += [b.strip() for b in a[a.index("--brands") + 1].split(",") if b.strip()]
    if "--file" in a:
        with open(a[a.index("--file") + 1], "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    h = clean_handle(s) or s
                    if h and h not in brands:
                        brands.append(h)
    start = datetime.fromisoformat(a[a.index("--from") + 1]).replace(tzinfo=timezone.utc) if "--from" in a else datetime.now(timezone.utc) - timedelta(days=180)
    end = datetime.fromisoformat(a[a.index("--to") + 1]).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc) if "--to" in a else datetime.now(timezone.utc)
    xlsx = a[a.index("--xlsx") + 1] if "--xlsx" in a else "Brand_Partnership_Tiers.xlsx"
    excel_only = "--excel-only" in a
    own_only = "--own-only" in a
    INCLUDE_ALL = "--include-all" in a
    globals()["INCLUDE_ALL"] = INCLUDE_ALL
    cache_key = f"{start.date()}_{end.date()}" + ("_own" if own_only else "")
    cache = _load_cache(); cache.setdefault(cache_key, {})
    if "--fill-views" in a:
        for b in brands:
            res = cache[cache_key].get(b)
            if not res or res.get("status") != "OK":
                continue
            print(f"[{b}] filling views")
            fill_missing_views(res["posts"])
            for r in res["posts"]:
                if "paid_flag" not in r:
                    r["paid_flag"] = bool(r.get("is_paid_partnership"))
                r["is_paid_partnership"] = bool(r.get("paid_flag")) and bool(r.get("sponsors"))
                r.update(evaluate(r["is_paid_partnership"], r["views"], r["likes"], r["comments"], r.get("followers"),
                                  r["caption"], r["likes_hidden"]))
            _save_cache(cache)
        excel_only = True
    if not excel_only:
        conn = cdb.connect()
        for b in brands:
            if b in cache[cache_key] and cache[cache_key][b].get("status") == "OK" and "--force" not in a:
                print(f"[{b}] cached ({len(cache[cache_key][b]['posts'])} posts)"); continue
            try:
                cache[cache_key][b] = scan_brand(b, int(start.timestamp()), int(end.timestamp()), conn, own_only=own_only)
            except Exception as e:
                print(f"[{b}] ERROR {type(e).__name__}: {str(e)[:120]}")
                cache[cache_key][b] = {"brand": b, "status": f"ERROR {type(e).__name__}", "posts": []}
            _save_cache(cache)
            time.sleep(PAUSE * 2)
    export(cache[cache_key], xlsx, start, end, brands)
