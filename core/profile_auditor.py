"""
================================================================================
CORE MODULE: LIVE PROFILE AUDITOR & EXACT METRICS RESOLVER  (v2.0)
================================================================================
Resolves Instagram profiles to EXACT, provenance-stamped metrics.

Resolution ladder (first source that yields an exact integer wins):
  1. JSON  : /api/v1/users/{pk}/info/  -> user.follower_count           [EXACT]
  2. DOM   : rendered logged-in profile header span[title]              [EXACT]
  3. META  : og:description "165K Followers"                            [ROUNDED]

Every result carries:
  followers            - integer
  followers_precision  - "exact" | "rounded" | "unresolved"
  followers_source     - which rung of the ladder produced it
  resolved_at          - UTC ISO timestamp (live counts drift minute to minute)

Rule: a rounded number is NEVER reported as exact. If nothing resolves, the
count is 0 with precision "unresolved" - it is never guessed or interpolated.

Also resolved when available: verified badge, private flag, media/following
counts, Instagram's own category, structured business email / phone, city,
and the branded-content (paid partnership) eligibility flags.

Tier strings are plain text - no emojis anywhere.
================================================================================
"""

import sys, os, re, json, time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from curl_cffi import requests as cffi_requests

# ==============================================================================
# SESSION - loaded from the git-ignored .env, never hardcoded here
# ==============================================================================
from core.session import load_cookies, playwright_cookies

COOKIES = load_cookies()
PLAYWRIGHT_COOKIES = playwright_cookies(COOKIES)

IG_APP_ID = "936619743392459"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
ANDROID_UA = ("Instagram 269.0.0.18.75 Android (26/8.0.0; 480dpi; 1080x1920; "
              "OnePlus; ONEPLUS A3003; OnePlus3; qcom; en_US; 314665256)")

# ==============================================================================
# GATES & TAXONOMY
# ==============================================================================
MIN_FOLLOWERS = 10_000

# A rounded count this close to the gate cannot be trusted to sit on the right
# side of it, so the audit says so instead of pretending.
BORDERLINE_BAND = 0.15

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')
PHONE_REGEX = re.compile(r'(?:\+91[\-\s]?)?[6789]\d{9}')

AUDIT_CACHE_FILE = os.path.join(BASE_DIR, "profile_audit_cache.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_tier(followers: int) -> str:
    """Canonical taxonomy from AGENTS.md. Plain text, no emojis."""
    if followers >= 1_000_000:
        return "Mega (1M+)"
    if followers >= 500_000:
        return "Macro (500K-1M)"
    if followers >= 100_000:
        return "Mid-Tier (100K-500K)"
    if followers >= MIN_FOLLOWERS:
        return "Micro (10K-100K)"
    return "Nano (<10K)"


def format_followers(num: int) -> str:
    if num >= 1_000_000:
        return f"{num/1_000_000:.1f}M".replace('.0M', 'M')
    elif num >= 1_000:
        return f"{num/1_000:.1f}K".replace('.0K', 'K')
    return str(int(num))


def parse_count(c_str) -> int:
    """Parses '1,229' / '165K' / '1.2M' to an int. Returns 0 on failure."""
    if c_str is None:
        return 0
    s = str(c_str).strip().upper().replace(',', '')
    try:
        if s.endswith('M'):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith('K'):
            return int(float(s[:-1]) * 1_000)
        if s.endswith('B'):
            return int(float(s[:-1]) * 1_000_000_000)
        return int(float(s))
    except Exception:
        return 0


def is_rounded_token(tok: str) -> bool:
    """'165K' is rounded. '1,229' is exact."""
    return bool(re.search(r'[KMB]', str(tok or ""), re.I))


# ==============================================================================
# HTTP PLUMBING
# ==============================================================================
def _headers(ua: str = WEB_UA) -> Dict[str, str]:
    return {
        "User-Agent": ua,
        "x-ig-app-id": IG_APP_ID,
        "x-csrftoken": COOKIES["csrftoken"],
        "x-requested-with": "XMLHttpRequest",
        "referer": "https://www.instagram.com/",
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
    }


def _get(url: str, ua: str = WEB_UA, timeout: int = 20):
    """Every Instagram request goes out with the active session cookies."""
    session = cffi_requests.Session(impersonate="chrome120")
    return session.get(url, headers=_headers(ua), cookies=COOKIES, timeout=timeout)


def is_throttled(response) -> bool:
    """
    Instagram soft-blocks a session per endpoint family. Measured shapes:
      - HTTP 429 with a 'Page Not Found' HTML body
      - HTTP 400 with {"message": "feedback_required", "is_spam": true}
    Neither is a 'profile does not exist' signal - do not treat it as one.
    """
    if response is None:
        return False
    if response.status_code == 429:
        return True
    if response.status_code == 400 and "feedback_required" in (response.text or ""):
        return True
    return False


# ==============================================================================
# RUNG 0: profile HTML -> numeric pk + rounded fallback
# ==============================================================================
def fetch_profile_html(handle: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """Returns (html, meta) where meta carries the pk and the og: fallback."""
    meta: Dict[str, Any] = {
        "pk": None, "og_followers_token": None, "og_display_name": None,
        "exists": None, "http_status": None,
    }
    try:
        r = _get(f"https://www.instagram.com/{handle}/")
        meta["http_status"] = r.status_code
        if is_throttled(r):
            meta["exists"] = None          # unknown, not absent
            return None, meta
        if r.status_code != 200:
            meta["exists"] = False
            return None, meta

        html = r.text
        m = (re.search(r'"profilePage_(\d+)"', html)
             or re.search(r'"props":\{"id":"(\d+)"', html)
             or re.search(r'"user_id":"(\d+)"', html))
        meta["pk"] = m.group(1) if m else None

        # Scope the follower regex to the og:description tag. An unscoped search
        # over the whole document can latch onto an unrelated "N Followers".
        og = re.search(r'og:description"\s+content="([^"]{0,240})', html)
        if og:
            f = re.search(r'([\d,\.]+[KMB]?)\s+Followers', og.group(1), re.I)
            if f:
                meta["og_followers_token"] = f.group(1)
            nm = re.search(r'from\s+(.+?)\s*\(&#064;', og.group(1))
            if nm:
                meta["og_display_name"] = nm.group(1).strip()

        meta["exists"] = meta["pk"] is not None or meta["og_followers_token"] is not None
        return html, meta
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        return None, meta


# ==============================================================================
# RUNG 1: /api/v1/users/{pk}/info/  -> EXACT follower_count + rich fields
# ==============================================================================
def fetch_user_info(pk: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Returns (user_object, status) where status is 'ok' | 'throttled' | 'failed'."""
    if not pk:
        return None, "failed"
    try:
        r = _get(f"https://www.instagram.com/api/v1/users/{pk}/info/", ANDROID_UA)
        if is_throttled(r):
            return None, "throttled"
        if r.status_code != 200:
            return None, "failed"
        return (r.json() or {}).get("user", {}), "ok"
    except Exception:
        return None, "failed"


# ==============================================================================
# RUNG 2: rendered logged-in DOM -> EXACT count from span[title]
# ==============================================================================
# The followers figure renders abbreviated ("125K") but carries the exact
# comma-separated integer in the span's title attribute ("125,749"). Verified
# against the JSON follower_count for the same account at the same moment.
#
# Anchored on the "<n> followers" label rather than on a link href: Instagram
# no longer wraps the count in an <a href=".../followers/">.
_DOM_EXTRACT_JS = """
() => {
    const out = {title: null, label: null, header: null};
    const nodes = [...document.querySelectorAll('header span, header li, header div')];
    for (const n of nodes) {
        const t = (n.innerText || '').trim().toLowerCase();
        if (/^[\\d.,km]+\\s+followers$/.test(t)) {
            const sp = n.querySelector('span[title]');
            if (sp) {
                out.title = sp.getAttribute('title');
                out.label = n.innerText.trim();
                break;
            }
        }
    }
    const h = document.querySelector('header');
    if (h) out.header = h.innerText.slice(0, 600);
    return out;
}
"""


def fetch_dom_followers(page, handle: str) -> Dict[str, Any]:
    """Reads the exact count out of a live Playwright page. Needs a logged-in context."""
    out = {"followers": 0, "precision": "unresolved", "header_text": "", "title_attr": None}
    try:
        page.goto(f"https://www.instagram.com/{handle}/",
                  wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(3200)
        info = page.evaluate(_DOM_EXTRACT_JS)
        out["header_text"] = info.get("header") or ""
        title = info.get("title")
        out["title_attr"] = title
        if title:
            if is_rounded_token(title):
                out["followers"] = parse_count(title)
                out["precision"] = "rounded"
            else:
                out["followers"] = parse_count(title)
                out["precision"] = "exact" if out["followers"] > 0 else "unresolved"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


# ==============================================================================
# THE LADDER
# ==============================================================================
def resolve_profile(handle: str, page=None, want_rich: bool = True) -> Dict[str, Any]:
    """
    Resolves one handle to provenance-stamped metrics.

    page : an optional logged-in Playwright page. Supply it and the DOM rung
           becomes available, which keeps counts exact even while the JSON
           endpoint is throttled.
    """
    clean_h = str(handle).replace("@", "").strip().lower()
    res: Dict[str, Any] = {
        "handle": f"@{clean_h}",
        "raw_handle": clean_h,
        "profile_url": f"https://www.instagram.com/{clean_h}/",
        "name": "",
        "followers": 0,
        "followers_display": "0",
        "followers_precision": "unresolved",
        "followers_source": "none",
        "resolved_at": now_iso(),
        "tier": "Nano (<10K)",
        "pk": None,
        "is_verified": None,
        "is_private": None,
        "media_count": None,
        "following_count": None,
        "ig_category": "",
        "bio": "",
        "email": "N/A",
        "phone": "N/A",
        "city_name": "",
        "external_url": "",
        "branded_content_ready": None,
        "status": "UNRESOLVED",
        "sources_tried": [],
    }

    # --- rung 0 -------------------------------------------------------------
    html, meta = fetch_profile_html(clean_h)
    res["sources_tried"].append(f"html:{meta.get('http_status')}")
    res["pk"] = meta.get("pk")
    if meta.get("og_display_name"):
        res["name"] = meta["og_display_name"]

    if meta.get("exists") is False:
        res["status"] = "404_NOT_FOUND"
        return res

    # --- rung 1: JSON, exact ------------------------------------------------
    user_obj = None
    if res["pk"]:
        user_obj, st = fetch_user_info(res["pk"])
        res["sources_tried"].append(f"users_info:{st}")
        if st == "ok" and user_obj and user_obj.get("follower_count") is not None:
            res["followers"] = int(user_obj["follower_count"])
            res["followers_precision"] = "exact"
            res["followers_source"] = "api/v1/users/{pk}/info"
            res["resolved_at"] = now_iso()

    if user_obj and want_rich:
        res["name"] = user_obj.get("full_name") or res["name"] or clean_h
        res["is_verified"] = user_obj.get("is_verified")
        res["is_private"] = user_obj.get("is_private")
        res["media_count"] = user_obj.get("media_count")
        res["following_count"] = user_obj.get("following_count")
        res["ig_category"] = user_obj.get("category") or ""
        res["city_name"] = user_obj.get("city_name") or ""
        res["external_url"] = user_obj.get("external_url") or ""
        res["bio"] = (user_obj.get("biography") or "").replace("\n", " | ").strip()
        # Structured contact fields beat scraping the bio blob.
        if user_obj.get("public_email"):
            res["email"] = user_obj["public_email"].strip().lower()
        for pk_field in ("public_phone_number", "contact_phone_number"):
            if user_obj.get(pk_field):
                res["phone"] = str(user_obj[pk_field]).strip()
                break
        res["branded_content_ready"] = bool(
            user_obj.get("can_use_branded_content_discovery_as_creator")
            or user_obj.get("can_use_paid_partnership_messaging_as_creator")
        )

    # --- rung 2: rendered DOM, exact ---------------------------------------
    if res["followers_precision"] != "exact" and page is not None:
        dom = fetch_dom_followers(page, clean_h)
        res["sources_tried"].append(f"dom:{dom['precision']}")
        if dom["precision"] in ("exact", "rounded") and dom["followers"] > 0:
            res["followers"] = dom["followers"]
            res["followers_precision"] = dom["precision"]
            res["followers_source"] = "rendered DOM span[title]"
            res["resolved_at"] = now_iso()
        if dom.get("header_text"):
            if not res["bio"]:
                res["bio"] = dom["header_text"][:350].replace("\n", " | ").strip()
            if res["email"] == "N/A":
                found = EMAIL_REGEX.findall(dom["header_text"])
                if found:
                    res["email"] = found[0].lower().rstrip('.')
            if res["phone"] == "N/A":
                found = PHONE_REGEX.findall(dom["header_text"])
                if found:
                    res["phone"] = found[0]

    # --- rung 3: og:description, ROUNDED -----------------------------------
    if res["followers_precision"] == "unresolved" and meta.get("og_followers_token"):
        tok = meta["og_followers_token"]
        res["followers"] = parse_count(tok)
        res["followers_precision"] = "rounded" if is_rounded_token(tok) else "exact"
        res["followers_source"] = "og:description"
        res["sources_tried"].append(f"og:{tok}")

    # --- finalise -----------------------------------------------------------
    if res["followers_precision"] == "unresolved":
        res["status"] = "UNRESOLVED"
        res["followers_display"] = "Unresolved"
        return res

    res["followers_display"] = format_followers(res["followers"])
    res["tier"] = classify_tier(res["followers"])
    if not res["name"]:
        res["name"] = clean_h

    if res["followers"] < MIN_FOLLOWERS:
        res["status"] = f"BELOW_GATE ({res['followers']:,})"
    else:
        res["status"] = "VALID"

    # A rounded count sitting next to the gate is not a decision we can make.
    if res["followers_precision"] != "exact":
        lo = MIN_FOLLOWERS * (1 - BORDERLINE_BAND)
        hi = MIN_FOLLOWERS * (1 + BORDERLINE_BAND)
        if lo <= res["followers"] <= hi:
            res["status"] = "BORDERLINE_ROUNDED_NEEDS_EXACT"

    return res


def passes_gate(res: Dict[str, Any]) -> bool:
    """True only for an exactly-resolved account at or above the 10K gate."""
    return (res.get("status") == "VALID"
            and res.get("followers", 0) >= MIN_FOLLOWERS
            and res.get("followers_precision") == "exact")


# ==============================================================================
# ENGAGEMENT (free, from the creator's own feed - honest about throttling)
# ==============================================================================
def resolve_engagement(pk: str, followers: int, sample: int = 12) -> Dict[str, Any]:
    """
    Averages likes/comments/plays over the most recent posts to derive an
    engagement rate. Returns available=False with a reason rather than a
    guessed rate - the feed endpoint is the first one Instagram throttles.
    """
    out = {
        "available": False, "reason": "", "posts_sampled": 0,
        "avg_likes": None, "avg_comments": None, "engagement_rate_pct": None,
        "reels_sampled": 0, "avg_plays": None, "view_to_follower_multiplier": None,
        "paid_partnership_posts": None, "coauthored_posts": None,
        "latest_post_at": None, "measured_at": now_iso(),
    }
    if not pk or not followers:
        out["reason"] = "missing pk or follower base"
        return out
    try:
        r = _get(f"https://i.instagram.com/api/v1/feed/user/{pk}/?count={sample}",
                 ANDROID_UA, timeout=25)
        if is_throttled(r):
            out["reason"] = "session throttled on the feed endpoint (feedback_required / 429)"
            return out
        if r.status_code != 200:
            out["reason"] = f"feed HTTP {r.status_code}"
            return out
        items = (r.json() or {}).get("items", []) or []
        if not items:
            out["reason"] = "feed returned no items"
            return out

        rows = []
        for it in items:
            m = it.get("media", it)
            rows.append({
                "likes": m.get("like_count") or 0,
                "comments": m.get("comment_count") or 0,
                "plays": m.get("play_count") or m.get("ig_play_count") or 0,
                "taken_at": m.get("taken_at"),
                "paid": bool(m.get("is_paid_partnership")),
                "coauthors": [c.get("username") for c in (m.get("coauthor_producers") or [])],
            })

        n = len(rows)
        avg_l = sum(x["likes"] for x in rows) / n
        avg_c = sum(x["comments"] for x in rows) / n
        reels = [x for x in rows if x["plays"]]

        out.update({
            "available": True,
            "posts_sampled": n,
            "avg_likes": round(avg_l),
            "avg_comments": round(avg_c),
            "engagement_rate_pct": round((avg_l + avg_c) / followers * 100, 2),
            "reels_sampled": len(reels),
            "paid_partnership_posts": sum(1 for x in rows if x["paid"]),
            "coauthored_posts": sum(1 for x in rows if x["coauthors"]),
            "latest_post_at": max((x["taken_at"] for x in rows if x["taken_at"]), default=None),
        })
        if reels:
            avg_p = sum(x["plays"] for x in reels) / len(reels)
            out["avg_plays"] = round(avg_p)
            out["view_to_follower_multiplier"] = round(avg_p / followers, 2)
    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


# ==============================================================================
# BACKWARD-COMPATIBLE ENTRYPOINT (run.py "audit profile @handle")
# ==============================================================================
def audit_profile(handle: str, page=None, with_engagement: bool = False) -> Dict[str, Any]:
    """Resolves a handle. Keeps the keys older callers expect (full_name, tier)."""
    res = resolve_profile(handle, page=page)
    res["full_name"] = res.get("name", "")
    if with_engagement and res.get("pk") and res.get("followers"):
        res["engagement"] = resolve_engagement(res["pk"], res["followers"])
    return res


def save_audit_cache(records, path: str = AUDIT_CACHE_FILE) -> None:
    """Incremental progress save - call after every audited handle."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_audit_cache(path: str = AUDIT_CACHE_FILE):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    target = sys.argv[1] if len(sys.argv) > 1 else "@whatkolkataeats"
    want_eng = "--engagement" in sys.argv

    result = audit_profile(target, with_engagement=want_eng)
    print("=" * 68)
    print(f"PROFILE AUDIT: {result['handle']}")
    print("=" * 68)
    print(f"Name:          {result.get('name', '')}")
    print(f"Followers:     {result.get('followers', 0):,} "
          f"({result.get('followers_display')})")
    print(f"Precision:     {result.get('followers_precision').upper()}  "
          f"via {result.get('followers_source')}")
    print(f"Resolved at:   {result.get('resolved_at')}")
    print(f"Tier:          {result.get('tier', '')}")
    print(f"IG Category:   {result.get('ig_category') or 'N/A'}")
    print(f"Verified:      {result.get('is_verified')}")
    print(f"Email:         {result.get('email')}")
    print(f"Phone:         {result.get('phone')}")
    print(f"Status:        {result.get('status')}")
    print(f"Sources tried: {', '.join(result.get('sources_tried', []))}")
    if want_eng:
        e = result.get("engagement", {})
        if e.get("available"):
            print(f"Engagement:    {e['engagement_rate_pct']}% over {e['posts_sampled']} posts "
                  f"(avg {e['avg_likes']:,} likes / {e['avg_comments']:,} comments)")
            if e.get("avg_plays"):
                print(f"Reels:         avg {e['avg_plays']:,} plays "
                      f"= {e['view_to_follower_multiplier']}x followers")
        else:
            print(f"Engagement:    NOT AVAILABLE - {e.get('reason')}")
    print("=" * 68)
