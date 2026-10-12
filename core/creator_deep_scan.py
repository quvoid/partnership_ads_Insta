"""
================================================================================
CREATOR DEEP SCAN - 90 days of a creator's own posts, read properly
================================================================================
For a verified creator, page back through their posts (default 90 days) and
answer the questions a brand actually asks before a deal:

  WHO HAVE THEY WORKED WITH?   every post carrying a partnership signal, and
                               the brand behind it. Five signals, strongest
                               first: Meta's paid-partnership toggle, sponsor
                               tags, co-authored posts, #ad-style hashtags, and
                               business accounts tagged in the post.
  WHAT ARE THEY PULLING?       likes and comments across the whole window
                               (medians, so one viral post cannot carry it),
                               exact reel views on the recent reels, posting
                               cadence, days since last post.
  WHAT DO THEY MAKE?           content category scored over every caption in
                               the window, plus Instagram's own category.
  HOW DO WE REACH THEM?        business email from the profile, else any email
                               in the bio, else the external link.

Tagged accounts are resolved once through users/{pk}/info and cached in the
database (business flag + Instagram category), so a brand tagged by twenty
creators costs one request, not twenty. A tagged account is treated as a
brand when Instagram marks it business, or its category is commercial, or it
sits in the caption next to a partnership hashtag. A friend tagged in a cafe
reel is not a partnership and is not reported as one.

Pagination uses the web app's own connection query, which keeps working
while the REST feed is throttled. Pinned posts are excluded from the window
so an old trophy cannot end the walk early.

Everything lands in creator_intelligence.db:
  brand_collabs        one row per (creator, brand, post)
  creator_deep_scan    one row per creator: the rolled-up metrics and category
  tagged_accounts      the resolved-account cache

  python core/creator_deep_scan.py run --region kolkata --limit 200 --days 90
  python core/creator_deep_scan.py one @handle
  python core/creator_deep_scan.py export --region kolkata --xlsx Kolkata_Deep.xlsx
================================================================================
"""

import sys, os, re, json, time, statistics
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from curl_cffi import requests as cffi

from core.profile_auditor import (
    COOKIES, WEB_UA, ANDROID_UA, IG_APP_ID, is_throttled, now_iso,
    resolve_profile, MIN_FOLLOWERS,
)
from core.discovery_sources import CATEGORIES, clean_handle
from core import creator_db as cdb

POSTS_DOC_ID = "38154989454116081"    # PolarisProfilePostsQuery (first page)
CONN_DOC_ID = "39535953862670189"     # PolarisProfilePostsTabContentQuery_connection (next pages)
REELS_DOC_ID = "37945290971781723"    # PolarisProfileReelsTabContentQuery (exact plays)

PARTNER_HASHTAG_RE = re.compile(
    r'#(ad|ads|sponsored|paidpartnership|paid_partnership|collab|collaboration|partnership|'
    r'gifted|brandpartner|sponsoredpost|promotion|promo)\b', re.I)

# Instagram categories that mark a tagged account as a commercial entity.
BRAND_CATEGORY_RE = re.compile(
    r'brand|product|service|retail|company|shop|store|restaurant|cafe|bar|hotel|resort|'
    r'salon|clinic|hospital|jewel|fashion|clothing|apparel|beauty|cosmetic|food|beverage|'
    r'grocery|bakery|e-?commerce|business|agency|media|publisher|app|software|finance|'
    r'bank|insurance|education|school|college|university|real estate|automotive|travel|'
    r'airline|electronics|furniture|health|pharmacy|entertainment|event|organization|'
    r'nonprofit|government|local business', re.I)

EMAIL_RE = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')

DEEP_SCHEMA = """
CREATE TABLE IF NOT EXISTS tagged_accounts (
    username     TEXT PRIMARY KEY,
    pk           TEXT,
    full_name    TEXT,
    is_business  INTEGER,
    is_verified  INTEGER,
    category     TEXT,
    followers    INTEGER,
    is_brand     INTEGER,
    resolved_at  TEXT
);
CREATE TABLE IF NOT EXISTS creator_deep_scan (
    handle                 TEXT PRIMARY KEY,
    window_days            INTEGER,
    scanned_at             TEXT,
    posts_in_window        INTEGER,
    pages_read             INTEGER,
    window_reached         INTEGER,
    partnership_posts      INTEGER,
    paid_toggle_posts      INTEGER,
    distinct_brands        INTEGER,
    brands                 TEXT,
    median_likes           INTEGER,
    median_comments        INTEGER,
    avg_likes              INTEGER,
    avg_comments           INTEGER,
    likes_hidden           INTEGER,
    median_reel_views      INTEGER,
    avg_reel_views         INTEGER,
    reels_measured         INTEGER,
    engagement_per_view_pct REAL,
    engagement_vs_followers_pct REAL,
    posts_per_week         REAL,
    days_since_last_post   REAL,
    content_category       TEXT,
    category_breakdown     TEXT,
    email                  TEXT,
    email_source           TEXT,
    external_url           TEXT,
    status                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_deep_cat ON creator_deep_scan(content_category);
"""


def _conn():
    conn = cdb.connect()
    conn.executescript(DEEP_SCHEMA)
    return conn


# ==============================================================================
# HTTP
# ==============================================================================
def _session():
    return cffi.Session(impersonate="chrome120")


def fetch_tokens(handle: str) -> Dict[str, Any]:
    out = {"pk": None, "fb_dtsg": None, "lsd": None, "throttled": False, "exists": None}
    try:
        r = _session().get(f"https://www.instagram.com/{handle}/",
                           headers={"User-Agent": WEB_UA, "accept-language": "en-US,en;q=0.9"},
                           cookies=COOKIES, timeout=25)
        if is_throttled(r):
            out["throttled"] = True
            return out
        if r.status_code != 200:
            out["exists"] = False
            return out
        html = r.text
        m = re.search(r'"profilePage_(\d+)"', html) or re.search(r'"user_id":"(\d+)"', html)
        out["pk"] = m.group(1) if m else None
        d = re.search(r'"dtsg":\{"token":"([^"]+)"', html) or re.search(r'"fb_dtsg":"([^"]+)"', html)
        out["fb_dtsg"] = d.group(1) if d else None
        l = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', html) or re.search(r'"lsd":"([^"]+)"', html)
        out["lsd"] = l.group(1) if l else None
        out["exists"] = out["pk"] is not None
    except Exception:
        pass
    return out


def _graphql(handle: str, friendly: str, doc_id: str, variables: Dict[str, Any],
             tokens: Dict[str, Any], referer_suffix: str = ""):
    body = {"av": COOKIES["ds_user_id"], "__d": "www", "__user": "0", "__a": "1",
            "__comet_req": "7", "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": friendly, "server_timestamps": "true",
            "doc_id": doc_id, "variables": json.dumps(variables, separators=(",", ":"))}
    if tokens.get("fb_dtsg"):
        body["fb_dtsg"] = tokens["fb_dtsg"]
    if tokens.get("lsd"):
        body["lsd"] = tokens["lsd"]
    headers = {"User-Agent": WEB_UA, "content-type": "application/x-www-form-urlencoded",
               "x-csrftoken": COOKIES["csrftoken"], "x-ig-app-id": IG_APP_ID,
               "x-fb-friendly-name": friendly, "x-asbd-id": "359341",
               "origin": "https://www.instagram.com",
               "referer": f"https://www.instagram.com/{handle}/{referer_suffix}",
               "accept": "*/*", "accept-language": "en-US,en;q=0.9"}
    if tokens.get("lsd"):
        headers["x-fb-lsd"] = tokens["lsd"]
    return _session().post("https://www.instagram.com/graphql/query", headers=headers,
                           cookies=COOKIES, data=body, timeout=30)


_RELAY = {
    "__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider": True,
    "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
    "__relay_internal__pv__PolarisReelsRecoDebugOverlayEnabledrelayprovider": False,
}
_DATA = {"count": 12, "include_reel_media_seen_timestamp": True,
         "include_relationship_info": True, "latest_besties_reel_media": True,
         "latest_reel_media": True}


def _parse_conn(r) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    conn = ((r.json().get("data") or {})
            .get("xdt_api__v1__feed__user_timeline_graphql_connection") or {})
    return [e.get("node", {}) for e in (conn.get("edges") or [])], conn.get("page_info") or {}


def walk_posts(handle: str, tokens: Dict[str, Any], days: int = 90,
               max_pages: int = 25, pause: float = 1.5) -> Dict[str, Any]:
    """Pages back until the oldest non-pinned post is older than the window."""
    out = {"status": "failed", "posts": [], "pages": 0, "window_reached": False}
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    try:
        r = _graphql(handle, "PolarisProfilePostsQuery", POSTS_DOC_ID,
                     {"data": _DATA, "username": handle, **_RELAY}, tokens)
        if is_throttled(r):
            out["status"] = "throttled"
            return out
        if r.status_code != 200:
            out["status"] = f"http_{r.status_code}"
            return out
        nodes, pi = _parse_conn(r)
        oldest = None
        while True:
            out["pages"] += 1
            for n in nodes:
                ts = n.get("taken_at")
                pinned = bool(n.get("timeline_pinned_user_ids"))
                out["posts"].append(_post_record(n, pinned))
                if ts and not pinned:
                    oldest = ts if oldest is None else min(oldest, ts)
            if oldest is not None and oldest < cutoff:
                out["window_reached"] = True
                break
            after = pi.get("end_cursor")
            if not pi.get("has_next_page") or not after or out["pages"] >= max_pages:
                break
            time.sleep(pause)
            r = _graphql(handle, "PolarisProfilePostsTabContentQuery_connection", CONN_DOC_ID,
                         {"after": after, "before": None, "data": _DATA, "first": 12,
                          "include_multi_captions": True, "last": None,
                          "username": handle, **_RELAY}, tokens)
            if is_throttled(r):
                out["status"] = "throttled_midway"
                return out
            if r.status_code != 200:
                break
            nodes, pi = _parse_conn(r)
        out["status"] = "ok"
    except Exception as e:
        out["status"] = f"{type(e).__name__}"
    return out


def _post_record(n: Dict[str, Any], pinned: bool) -> Dict[str, Any]:
    caption = ((n.get("caption") or {}).get("text") or "")
    tagged = []
    for u in ((n.get("usertags") or {}).get("in") or []):
        un = clean_handle((u.get("user") or {}).get("username"))
        if un:
            tagged.append(un)
    coauthors = [clean_handle(c.get("username")) for c in (n.get("coauthor_producers") or [])]
    sponsors = [clean_handle((s.get("sponsor") or {}).get("username"))
                for s in (n.get("sponsor_tags") or [])]
    return {
        "code": n.get("code"), "taken_at": n.get("taken_at"), "media_type": n.get("media_type"),
        "pinned": pinned, "like_count": n.get("like_count"),
        "comment_count": n.get("comment_count"),
        "counts_hidden": bool(n.get("like_and_view_counts_disabled")),
        "is_paid_partnership": bool(n.get("is_paid_partnership")),
        "coauthors": [c for c in coauthors if c],
        "sponsors": [s for s in sponsors if s],
        "tagged": tagged,
        "partner_hashtags": sorted(set(m.group(1).lower() for m in PARTNER_HASHTAG_RE.finditer(caption))),
        "mentions": sorted(set(clean_handle(m) for m in re.findall(r'@([A-Za-z0-9_.]{3,30})', caption)
                               if clean_handle(m))),
        "location": ((n.get("location") or {}).get("name") or ""),
        "reposts": n.get("media_repost_count"),
        "product_type": n.get("product_type") or "",
        "caption": caption[:600],
    }


def fetch_reel_views(handle: str, pk: str, tokens: Dict[str, Any]) -> Dict[str, int]:
    """code -> exact play_count for the recent reels."""
    out: Dict[str, int] = {}
    try:
        r = _graphql(handle, "PolarisProfileReelsTabContentQuery", REELS_DOC_ID,
                     {"data": {"include_feed_video": True, "page_size": 12,
                               "target_user_id": str(pk)}, "user_id": str(pk),
                      "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False},
                     tokens, referer_suffix="reels/")
        if r.status_code != 200 or is_throttled(r):
            return out

        def walk(o):
            if isinstance(o, dict):
                if o.get("code") and o.get("play_count") is not None:
                    out[o["code"]] = o["play_count"]
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(r.json())
    except Exception:
        pass
    return out


# ==============================================================================
# TAGGED-ACCOUNT RESOLUTION (cached in the database)
# ==============================================================================
def resolve_tagged(conn, username: str, pause: float = 1.2) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM tagged_accounts WHERE username=?", (username,)).fetchone()
    if row:
        return dict(row)
    prof = fetch_tokens(username)
    if prof.get("throttled"):
        return None
    rec = {"username": username, "pk": prof.get("pk"), "full_name": "", "is_business": None,
           "is_verified": None, "category": "", "followers": None, "is_brand": 0,
           "resolved_at": now_iso()}
    if prof.get("pk"):
        time.sleep(pause)
        try:
            r = _session().get(f"https://www.instagram.com/api/v1/users/{prof['pk']}/info/",
                               headers={"User-Agent": ANDROID_UA, "x-ig-app-id": IG_APP_ID,
                                        "x-csrftoken": COOKIES["csrftoken"],
                                        "referer": "https://www.instagram.com/"},
                               cookies=COOKIES, timeout=25)
            if r.status_code == 200 and not is_throttled(r):
                u = (r.json() or {}).get("user", {})
                rec.update({
                    "full_name": u.get("full_name") or "",
                    "is_business": 1 if u.get("is_business") else 0,
                    "is_verified": 1 if u.get("is_verified") else 0,
                    "category": u.get("category") or "",
                    "followers": u.get("follower_count"),
                })
                rec["is_brand"] = 1 if (rec["is_business"] or
                                        BRAND_CATEGORY_RE.search(rec["category"] or "")) else 0
        except Exception:
            pass
    conn.execute("INSERT OR REPLACE INTO tagged_accounts VALUES "
                 "(:username,:pk,:full_name,:is_business,:is_verified,:category,:followers,"
                 ":is_brand,:resolved_at)", rec)
    conn.commit()
    return rec


# ==============================================================================
# CONTENT CATEGORY
# ==============================================================================
def classify_content(captions: List[str], ig_category: str = "") -> Tuple[str, Dict[str, float]]:
    """Scores every category's keyword list over all captions; returns the winner and shares."""
    scores: Dict[str, int] = {}
    blob = " ".join(captions).lower()
    for key, cfg in CATEGORIES.items():
        n = 0
        for w in cfg["bio_keywords"]:
            n += len(re.findall(r'\b' + re.escape(w) + r'\w*', blob))
        if n:
            scores[cfg["label"]] = n
    total = sum(scores.values()) or 1
    breakdown = {k: round(v / total, 2) for k, v in sorted(scores.items(), key=lambda x: -x[1])}
    if not scores:
        return (ig_category or "Uncategorised"), {}
    top = next(iter(breakdown))
    return top, breakdown


# ==============================================================================
# ONE CREATOR
# ==============================================================================
def deep_scan(handle: str, days: int = 90, conn=None, pause: float = 1.5,
              resolve_tags: bool = True) -> Dict[str, Any]:
    own = conn is None
    conn = conn or _conn()
    handle = clean_handle(handle) or handle
    res: Dict[str, Any] = {"handle": handle, "window_days": days, "scanned_at": now_iso(),
                           "status": "failed"}

    prof = resolve_profile(handle, want_rich=True)
    time.sleep(pause)
    if prof["status"] == "404_NOT_FOUND":
        res["status"] = "NOT FOUND"
        return _finish(conn, res, own)
    followers = prof.get("followers") or 0

    # ---- email --------------------------------------------------------------
    email, src = "", ""
    if prof.get("email") and prof["email"] != "N/A":
        email, src = prof["email"], "business email on profile" if prof.get("ig_category") else "bio"
    if not email:
        m = EMAIL_RE.search(prof.get("bio") or "")
        if m:
            email, src = m.group(0).lower(), "bio"
    res.update({"email": email, "email_source": src or "none found",
                "external_url": prof.get("external_url") or ""})

    tokens = fetch_tokens(handle)
    if tokens.get("throttled"):
        res["status"] = "THROTTLED"
        return _finish(conn, res, own)
    if not tokens.get("pk"):
        res["status"] = "NO PK"
        return _finish(conn, res, own)
    time.sleep(pause)

    walk = walk_posts(handle, tokens, days=days, pause=pause, max_pages=max(25, int(days / 30 * 4)))
    if walk["status"].startswith("throttled"):
        res["status"] = "THROTTLED"
        return _finish(conn, res, own)
    posts = [p for p in walk["posts"] if not p["pinned"]]
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    in_window = [p for p in posts if p["taken_at"] and p["taken_at"] >= cutoff]
    res.update({"posts_in_window": len(in_window), "pages_read": walk["pages"],
                "window_reached": 1 if walk["window_reached"] else 0})
    time.sleep(pause)

    # ---- views on the recent reels -------------------------------------------
    views = fetch_reel_views(handle, tokens["pk"], tokens)
    res["_views_by_code"] = views
    matched = [views[p["code"]] for p in in_window if p["code"] in views and views[p["code"]]]
    if matched:
        res["median_reel_views"] = round(statistics.median(matched))
        res["avg_reel_views"] = round(statistics.mean(matched))
        res["reels_measured"] = len(matched)

    # ---- likes / comments over the window --------------------------------------
    hidden = [p for p in in_window if p["counts_hidden"]]
    res["likes_hidden"] = 1 if in_window and len(hidden) >= max(1, len(in_window) // 2) else 0
    vis_likes = [p["like_count"] for p in in_window
                 if not p["counts_hidden"] and p.get("like_count") is not None]
    comments = [p["comment_count"] for p in in_window if p.get("comment_count") is not None]
    if vis_likes and not res["likes_hidden"]:
        res["median_likes"] = round(statistics.median(vis_likes))
        res["avg_likes"] = round(statistics.mean(vis_likes))
    if comments:
        res["median_comments"] = round(statistics.median(comments))
        res["avg_comments"] = round(statistics.mean(comments))
    eng = (res.get("median_likes") or 0) + (res.get("median_comments") or 0)
    if eng and followers:
        res["engagement_vs_followers_pct"] = round(eng / followers * 100, 3)
    if eng and res.get("median_reel_views"):
        res["engagement_per_view_pct"] = round(eng / res["median_reel_views"] * 100, 3)
    elif res["likes_hidden"] and res.get("median_comments") and res.get("median_reel_views"):
        res["engagement_per_view_pct"] = round(res["median_comments"] / res["median_reel_views"] * 100, 3)

    stamps = [p["taken_at"] for p in in_window if p["taken_at"]]
    if stamps:
        now = datetime.now(timezone.utc).timestamp()
        res["days_since_last_post"] = round((now - max(stamps)) / 86400, 1)
        span = max((max(stamps) - min(stamps)) / 86400, 1.0)
        res["posts_per_week"] = round((len(stamps) - 1) / span * 7, 1) if len(stamps) > 1 else None

    # ---- partnerships -------------------------------------------------------------
    partner_posts, brands, paid_toggle = [], {}, 0
    for p in in_window:
        signals, candidates = [], set()
        if p["is_paid_partnership"]:
            signals.append("paid_partnership_toggle"); paid_toggle += 1
        if p["sponsors"]:
            signals.append("sponsor_tag"); candidates.update(p["sponsors"])
        if p["coauthors"]:
            signals.append("coauthor"); candidates.update(p["coauthors"])
        if p["partner_hashtags"]:
            signals.append("hashtag:" + ",".join(p["partner_hashtags"]))
            candidates.update(p["tagged"]); candidates.update(p["mentions"])
        # tagged business accounts count even without a hashtag
        for t in p["tagged"]:
            candidates.add(t)
        candidates.discard(handle)
        if not signals and not candidates:
            continue

        confirmed = []
        for c in sorted(candidates):
            acc = resolve_tagged(conn, c, pause) if resolve_tags else None
            if acc is None and resolve_tags:
                continue                       # throttled on resolution; skip silently
            is_brand = bool(acc and acc.get("is_brand"))
            strong = bool(p["is_paid_partnership"] or c in p["sponsors"] or c in p["coauthors"]
                          or p["partner_hashtags"])
            if is_brand or strong:
                confirmed.append({"brand": c, "is_business": bool(acc and acc.get("is_business")),
                                  "category": (acc or {}).get("category") or "",
                                  "reason": "business account" if is_brand else "partnership signal"})
        if not confirmed and not p["is_paid_partnership"]:
            continue
        partner_posts.append({"code": p["code"], "taken_at": p["taken_at"], "signals": signals,
                              "brands": [b["brand"] for b in confirmed]})
        _save_post_metrics(conn, handle, p, res.get("_views_by_code", {}).get(p["code"]),
                           [b["brand"] for b in confirmed], signals, res, followers)
        for b in confirmed:
            brands.setdefault(b["brand"], {"posts": 0, "paid_toggle": 0, "category": b["category"],
                                           "first": p["taken_at"], "last": p["taken_at"]})
            brands[b["brand"]]["posts"] += 1
            brands[b["brand"]]["paid_toggle"] += 1 if p["is_paid_partnership"] else 0
            brands[b["brand"]]["first"] = min(brands[b["brand"]]["first"], p["taken_at"])
            brands[b["brand"]]["last"] = max(brands[b["brand"]]["last"], p["taken_at"])
            cdb.add_brand_collab(conn, handle, b["brand"],
                                 f"https://www.instagram.com/p/{p['code']}/",
                                 p["is_paid_partnership"],
                                 "Toggle ON" if p["is_paid_partnership"] else "Toggle OFF",
                                 p["taken_at"])
    conn.commit()
    res.update({"partnership_posts": len(partner_posts), "paid_toggle_posts": paid_toggle,
                "distinct_brands": len(brands),
                "brands": "; ".join(f"{b} x{v['posts']}" + (" (paid toggle)" if v["paid_toggle"] else "")
                                    for b, v in sorted(brands.items(), key=lambda x: -x[1]["posts"]))})

    # ---- content category ------------------------------------------------------
    cat, breakdown = classify_content([p["caption"] for p in in_window], prof.get("ig_category") or "")
    res["content_category"] = cat
    res["category_breakdown"] = "; ".join(f"{k} {int(v*100)}%" for k, v in list(breakdown.items())[:4])

    res["status"] = "OK"
    res.pop("_views_by_code", None)
    # refresh the creators row too (exact count, email, category)
    prof["category"] = cat
    cdb.upsert_creator(conn, prof)
    return _finish(conn, res, own)


def _save_post_metrics(conn, handle, p, views, brands, signals, res, followers):
    """One row per partnership post with its own numbers, so a collab can be valued."""
    conn.execute("""CREATE TABLE IF NOT EXISTS collab_post_metrics (
        handle TEXT, code TEXT, post_url TEXT, taken_at INTEGER, media_type INTEGER, product_type TEXT,
        likes INTEGER, likes_hidden INTEGER, comments INTEGER, views INTEGER, reposts INTEGER,
        is_paid_partnership INTEGER, brands TEXT, signals TEXT, sponsors TEXT, coauthors TEXT,
        tagged TEXT, location TEXT, caption TEXT, followers_at_scan INTEGER, scanned_at TEXT,
        PRIMARY KEY (handle, code))""")
    kind = "reel" if (p.get("product_type") == "clips" or p.get("media_type") == 2) else ("carousel" if p.get("media_type") == 8 else "photo")
    conn.execute("INSERT OR REPLACE INTO collab_post_metrics VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (handle, p["code"], f"https://www.instagram.com/{'reel' if kind == 'reel' else 'p'}/{p['code']}/",
                  p["taken_at"], p.get("media_type"), kind,
                  None if p["counts_hidden"] else p.get("like_count"), 1 if p["counts_hidden"] else 0,
                  p.get("comment_count"), views, p.get("reposts"), 1 if p["is_paid_partnership"] else 0,
                  ";".join(brands), ";".join(signals), ";".join(p.get("sponsors") or []),
                  ";".join(p.get("coauthors") or []), ";".join(p.get("tagged") or []), p.get("location") or "",
                  (p.get("caption") or "")[:300], followers, res.get("scanned_at")))


def _finish(conn, res: Dict[str, Any], own: bool) -> Dict[str, Any]:
    cols = ["handle", "window_days", "scanned_at", "posts_in_window", "pages_read",
            "window_reached", "partnership_posts", "paid_toggle_posts", "distinct_brands",
            "brands", "median_likes", "median_comments", "avg_likes", "avg_comments",
            "likes_hidden", "median_reel_views", "avg_reel_views", "reels_measured",
            "engagement_per_view_pct", "engagement_vs_followers_pct", "posts_per_week",
            "days_since_last_post", "content_category", "category_breakdown", "email",
            "email_source", "external_url", "status"]
    row = {c: res.get(c) for c in cols}
    conn.execute(f"INSERT OR REPLACE INTO creator_deep_scan ({','.join(cols)}) "
                 f"VALUES ({','.join(':' + c for c in cols)})", row)
    conn.commit()
    if own:
        conn.close()
    return res


# ==============================================================================
# BATCH
# ==============================================================================
def run(region: Optional[str], limit: int = 200, days: int = 90, min_followers: int = MIN_FOLLOWERS,
        pause: float = 1.5, rescan_days: int = 14) -> Dict[str, int]:
    conn = _conn()
    params: Dict[str, Any] = {"minf": min_followers, "lim": limit,
                              "stale": (datetime.now(timezone.utc) - timedelta(days=rescan_days)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    where = ["c.followers_precision='exact'", "c.followers >= :minf",
             "(c.review_flag IS NULL OR c.review_flag='')",
             "NOT EXISTS (SELECT 1 FROM creator_deep_scan d WHERE d.handle=c.handle "
             "            AND d.status='OK' AND d.scanned_at > :stale)"]
    if region:
        params["region"] = region
        where.append("(EXISTS (SELECT 1 FROM observations o WHERE o.handle=c.handle AND o.region=:region)"
                     " OR EXISTS (SELECT 1 FROM geo_evidence g WHERE g.handle=c.handle AND g.region=:region))")
    # Residence evidence first: a creator who posts from several places in the
    # region is worth scanning before a bigger account with no tie to it. Sorting
    # by followers alone once spent a day on Bollywood celebrities that had been
    # backfilled from unrelated client workbooks.
    region_places = ("(SELECT COUNT(DISTINCT g.place_pk) FROM geo_evidence g "
                     "WHERE g.handle=c.handle" + (" AND g.region=:region" if region else "") + ")")
    live_sources = ("(SELECT COUNT(DISTINCT o.source) FROM observations o "
                    "WHERE o.handle=c.handle AND o.source!='manual')")
    rows = conn.execute(f"SELECT c.handle, c.followers, {region_places} AS places, "
                        f"{live_sources} AS live FROM creators c WHERE {' AND '.join(where)} "
                        f"ORDER BY places DESC, live DESC, c.followers DESC LIMIT :lim",
                        params).fetchall()
    print("=" * 76)
    print(f"DEEP SCAN  region={region or 'all'}  queue={len(rows)}  window={days}d")
    print("=" * 76)
    tally = {"ok": 0, "throttled": 0, "failed": 0}
    streak = 0
    for i, r in enumerate(rows, 1):
        h = r["handle"]
        print(f"[{i}/{len(rows)}] @{h:28s} f={r['followers']:>9,} places={r['places']} ...",
              end=" ", flush=True)
        res = deep_scan(h, days=days, conn=conn, pause=pause)
        if res["status"] == "OK":
            tally["ok"] += 1; streak = 0
            print(f"{res.get('posts_in_window',0):3d} posts | {res.get('partnership_posts',0):2d} partner posts "
                  f"| {res.get('distinct_brands',0):2d} brands | {res.get('content_category','')[:22]:22s} "
                  f"| email={'yes' if res.get('email') else 'no'}")
        elif res["status"] == "THROTTLED":
            tally["throttled"] += 1; streak += 1
            print("THROTTLED")
            if streak >= 3:
                print("\n[STOP] three consecutive throttles - Instagram is rate limiting. "
                      "Progress saved; re-run later to continue.")
                break
            time.sleep(20)
        else:
            tally["failed"] += 1; streak = 0
            print(res["status"])
        time.sleep(pause)
    conn.close()
    print(f"\nDEEP SCAN COMPLETE {tally}")
    return tally


# ==============================================================================
# EXPORT
# ==============================================================================
def _real_phone(v) -> str:
    """Only a number with at least 8 digits; legacy rosters stored 'N/A' placeholders."""
    v = (v or "").strip()
    return v if sum(ch.isdigit() for ch in v) >= 8 else ""


def export(region: Optional[str], xlsx: str, min_followers: int = MIN_FOLLOWERS) -> str:
    import openpyxl
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter
    from core.kolkata_engine import clean_cell

    conn = _conn()
    params: Dict[str, Any] = {"minf": min_followers}
    where = ["c.followers_precision='exact'", "c.followers >= :minf",
             "(c.review_flag IS NULL OR c.review_flag='')"]
    if region:
        params["region"] = region
        where.append("(EXISTS (SELECT 1 FROM observations o WHERE o.handle=c.handle AND o.region=:region)"
                     " OR EXISTS (SELECT 1 FROM geo_evidence g WHERE g.handle=c.handle AND g.region=:region))")
    rows = conn.execute(f"""
        SELECT c.handle, c.name, c.followers, c.tier, c.ig_category, c.is_verified,
               c.resolved_at, c.city_name, c.phone,
               (SELECT COUNT(DISTINCT g.place_pk) FROM geo_evidence g
                  WHERE g.handle=c.handle {'AND g.region=:region' if region else ''}) AS places,
               (SELECT GROUP_CONCAT(DISTINCT g.place_name) FROM geo_evidence g
                  WHERE g.handle=c.handle {'AND g.region=:region' if region else ''}) AS place_names,
               x.display_name AS yt_title, x.profile_url AS yt_url, x.reach AS yt_subs,
               x.reach_precision AS yt_subs_precision, x.match_method AS yt_method,
               x.possible_url AS yt_possible_url, x.possible_title AS yt_possible_title,
               d.*
        FROM creators c LEFT JOIN creator_deep_scan d ON d.handle=c.handle
             LEFT JOIN cross_platform x ON x.handle=c.handle AND x.platform='youtube'
        WHERE {' AND '.join(where)}
        ORDER BY c.followers DESC
    """, params).fetchall()

    headers = ["S.No", "Handle", "Name", "Profile URL", "Followers (Exact)", "Tier",
               "Verified", "Email", "Email Source", "Phone", "External Link",
               "YouTube Channel", "YouTube URL", "YouTube Subscribers", "YouTube Match Method",
               "Possible YouTube (Unverified)",
               "Content Category", "Category Breakdown", "IG Category",
               "Locations Posted From (Region)", "Places",
               "Posts Last 90 Days", "Partnership Posts (90d)", "Paid Toggle Posts (90d)",
               "Distinct Brands (90d)", "Brands Worked With (90d)",
               "Median Reel Views", "Average Reel Views", "Reels Measured",
               "Median Likes", "Median Comments", "Engagement Per View %",
               "Engagement vs Followers %", "Posts Per Week", "Days Since Last Post",
               "Deep Scan Status", "Follower Count Read At (UTC)", "Deep Scanned At (UTC)"]
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Creators"
    bold = Font(bold=True)
    for c, h in enumerate(headers, 1):
        cell = ws.cell(1, c, h); cell.font = bold
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for i, r in enumerate(rows, 2):
        r = dict(r)
        vals = [i - 1, r["handle"], r["name"], f"https://www.instagram.com/{r['handle']}/",
                r["followers"], r["tier"],
                "Yes" if r["is_verified"] == 1 else ("No" if r["is_verified"] == 0 else "Unknown"),
                r.get("email") or "", r.get("email_source") or "", _real_phone(r.get("phone")), r.get("external_url") or "",
                r.get("yt_title") or "", r.get("yt_url") or "",
                (f"{r['yt_subs']:,} ({r['yt_subs_precision']})" if r.get("yt_subs") is not None else ""),
                r.get("yt_method") or "", 
                (f"{r['yt_possible_title']} - {r['yt_possible_url']}" if r.get("yt_possible_url") else ""),
                r.get("content_category") or "", r.get("category_breakdown") or "",
                r.get("ig_category") or "", r.get("place_names") or "", r.get("places") or 0,
                r.get("posts_in_window"), r.get("partnership_posts"), r.get("paid_toggle_posts"),
                r.get("distinct_brands"), r.get("brands") or "",
                r.get("median_reel_views"), r.get("avg_reel_views"), r.get("reels_measured"),
                "Hidden by page" if r.get("likes_hidden") else r.get("median_likes"),
                r.get("median_comments"), r.get("engagement_per_view_pct"),
                "Hidden by page" if r.get("likes_hidden") else r.get("engagement_vs_followers_pct"),
                r.get("posts_per_week"), r.get("days_since_last_post"),
                r.get("status") or "not scanned", r.get("resolved_at"), r.get("scanned_at")]
        for c, v in enumerate(vals, 1):
            ws.cell(i, c, clean_cell(v)).alignment = Alignment(vertical="center")
    widths = [6, 26, 26, 40, 14, 20, 9, 30, 20, 18, 30, 26, 40, 18, 18, 44, 22, 40, 18, 50, 7, 12, 14, 14, 12, 60,
              14, 14, 10, 12, 14, 14, 16, 12, 14, 16, 20, 20]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(len(rows) + 1, 2)}"

    ws2 = wb.create_sheet("Method")
    notes = [
        ("Followers", "Exact, read live from Instagram at the time shown. Never estimated."),
        ("Email", "Business email from the profile where the account exposes one, else an email "
                  "found in the bio. 'none found' means neither exists publicly."),
        ("Phone", "Public contact number the account exposes on its profile. Blank if none."),
        ("YouTube Match Method", "How the channel was tied to this creator. bio_link = the Instagram bio "
                                "link is the channel; bio_aggregator = found on their Linktree-style page; "
                                "yt_backlink = the YouTube channel's About page links back to this exact "
                                "Instagram handle. All three are confirmed. Possible YouTube (Unverified) "
                                "is a channel with the same name and no backlink - check before using."),
        ("YouTube Subscribers", "As YouTube shows it. (exact) or (rounded) is stated next to the number."),
        ("Collab Posts sheet", "One row per creator x brand x post inside the window, with the post link, "
                              "date, and whether Meta's paid-partnership toggle was on. Same evidence "
                              "the Brands Worked With column is summarised from."),
        ("Locations Posted From", "Real Instagram locations in the region the creator has posted at. "
                                  "Two or more distinct places is treated as residence evidence."),
        ("Partnership Posts (90d)", "Posts in the last 90 days carrying a partnership signal: Meta's "
                                    "paid-partnership toggle, a sponsor tag, a co-authored post, an "
                                    "#ad-style hashtag, or a tagged business account. A tagged friend "
                                    "is not counted."),
        ("Brands Worked With", "Each brand with how many posts, and '(paid toggle)' where Meta's "
                               "official disclosure was on. Tagged accounts are resolved through "
                               "Instagram to confirm they are businesses."),
        ("Content Category", "Scored over every caption in the window against category keyword "
                             "lists; the breakdown shows the share of each. IG Category is Instagram's own label."),
        ("Reel Views", "Exact play counts on the creator's recent reels."),
        ("Median Likes / Comments", "Middle value across all posts in the window, so one viral post "
                                    "cannot carry the figure. 'Hidden by page' means the account hides "
                                    "like counts; the placeholder Instagram returns is never printed."),
        ("Engagement Per View %", "(median likes + median comments) / median reel views. The honest "
                                  "measure for reels-driven pages."),
        ("Deep Scan Status", "OK = full 90-day walk completed. THROTTLED = Instagram rate-limited "
                             "this creator; re-run to fill in. Blank = not yet scanned."),
    ]
    ws2.cell(1, 1, "Field").font = bold; ws2.cell(1, 2, "Definition").font = bold
    for i, (k, v) in enumerate(notes, 2):
        ws2.cell(i, 1, k); ws2.cell(i, 2, v).alignment = Alignment(wrap_text=True, vertical="top")
    ws2.column_dimensions["A"].width = 26; ws2.column_dimensions["B"].width = 110

    # ---- one row per creator x brand x post link, inside the scan window ------
    ws3 = wb.create_sheet("Collab Posts (90d)")
    h3 = ["Creator Handle", "Creator Followers (Exact)", "Brand", "Brand Profile URL",
          "Post URL", "Post Date", "Paid Partnership Toggle", "Brand IG Category"]
    for c, h in enumerate(h3, 1):
        ws3.cell(1, c, h).font = bold
    handles = [r["handle"] for r in rows]
    fol = {r["handle"]: r["followers"] for r in rows}
    cutoff = int(time.time()) - 90 * 86400   # same window as the scan
    n = 2
    for chunk in range(0, len(handles), 400):
        hs = handles[chunk:chunk + 400]
        q = f"""SELECT b.handle, b.brand, b.post_url, b.is_paid_partnership, b.post_taken_at,
                       (SELECT t.category FROM tagged_accounts t WHERE t.username=b.brand) AS bcat
                FROM brand_collabs b
                WHERE b.handle IN ({','.join('?' * len(hs))}) AND b.post_taken_at >= ?
                ORDER BY b.handle, b.post_taken_at DESC, b.brand"""
        for b in conn.execute(q, hs + [cutoff]):
            b = dict(b)
            vals = [b["handle"], fol.get(b["handle"]), b["brand"],
                    f"https://www.instagram.com/{b['brand']}/", b["post_url"],
                    datetime.fromtimestamp(b["post_taken_at"], tz=timezone.utc).strftime("%Y-%m-%d"),
                    "ON" if b["is_paid_partnership"] else "OFF", b.get("bcat") or ""]
            for c, v in enumerate(vals, 1):
                ws3.cell(n, c, clean_cell(v))
            n += 1
    for i, w in enumerate([26, 14, 30, 44, 46, 12, 12, 24], 1):
        ws3.column_dimensions[get_column_letter(i)].width = w
    ws3.freeze_panes = "A2"
    ws3.auto_filter.ref = f"A1:H{max(n - 1, 2)}"

    path = xlsx if (os.sep in xlsx or "/" in xlsx) else os.path.join(BASE_DIR, "deliverables", xlsx)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        wb.save(path)
    except PermissionError:
        path = path.replace(".xlsx", f"_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        wb.save(path)
    conn.close()
    print(f"[EXCEL] {path}  ({len(rows)} creators)")
    return path


# ==============================================================================
# CLI
# ==============================================================================
if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__); sys.exit(0)

    def opt(flag, default=None, cast=str):
        return cast(a[a.index(flag) + 1]) if flag in a else default

    if a[0] == "one":
        h = a[1] if len(a) > 1 else ""
        res = deep_scan(h, days=opt("--days", 90, int))
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif a[0] == "run":
        run(opt("--region"), limit=opt("--limit", 200, int), days=opt("--days", 90, int),
            pause=opt("--pause", 1.5, float))
    elif a[0] == "export":
        export(opt("--region"), opt("--xlsx", "Creator_Deep_Scan.xlsx"))
    else:
        print("commands: one @handle | run --region R --limit N --days 90 | export --region R --xlsx out.xlsx")
