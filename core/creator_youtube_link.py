# -*- coding: utf-8 -*-
"""
================================================================================
CREATOR YOUTUBE LINK - find each creator's YouTube channel, with proof
================================================================================
For every creator in the store, resolves a YouTube channel name + link using
public YouTube pages only (no Instagram session needed, no paid API).

Evidence ladder, strongest first. The rung that matched is written to the row
as `match_method` so a reader can see how much to trust it:

  bio_link         the creator's Instagram bio link IS a YouTube URL
  bio_aggregator   the bio link is a Linktree / bio.link / beacons / linkin.bio
                   page and that page contains a YouTube link
  yt_backlink      we searched YouTube for the creator's name / handle, opened
                   each candidate channel's About page, and the channel links
                   back to this exact Instagram handle
  possible         NOT confirmed, kept in a separate column: either a channel
                   that links to this Instagram but carries an unrelated name
                   (what a fan page looks like), or a channel with the same
                   name and no backlink.

Results land in `cross_platform` (platform='youtube') and are exported by
`creator_deep_scan.export` as YouTube Channel / URL / Subscribers / Match Method.

  python core/creator_youtube_link.py one @handle
  python core/creator_youtube_link.py run --region kolkata [--limit N] [--force]
================================================================================
"""
import sys, os, re, json, time, html as _html, unicodedata
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import quote, unquote

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from curl_cffi import requests as cffi
from core import creator_db as cdb
from core.discovery_sources import clean_handle

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PAUSE = 1.0
AGGREGATORS = ("linktr.ee", "bio.link", "beacons.ai", "linkin.bio", "lnk.bio",
               "withkoji.com", "solo.to", "tap.bio", "allmylinks.com", "hoo.be")
YT_HANDLE_RE = re.compile(r'youtube\.com/(@[A-Za-z0-9_.\-]{3,60})', re.I)
YT_CHANNEL_RE = re.compile(r'youtube\.com/(channel/UC[A-Za-z0-9_\-]{22}|c/[A-Za-z0-9_.\-]+|user/[A-Za-z0-9_.\-]+)', re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(url: str, timeout: int = 25):
    s = cffi.Session(impersonate="chrome120")
    return s.get(url, headers={"User-Agent": UA, "accept-language": "en-US,en;q=0.9",
                               # forces the English, cookie-free rendering
                               "cookie": "CONSENT=YES+; SOCS=CAI"}, timeout=timeout, allow_redirects=True)


def _count(tok: str) -> Optional[int]:
    tok = tok.replace(",", "").strip().upper()
    m = re.match(r'^([\d.]+)\s*([KMB]?)$', tok)
    if not m:
        return None
    n = float(m.group(1))
    return int(round(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)]))


# ---- YouTube channel page --------------------------------------------------------
def resolve_channel(ref: str) -> Dict[str, Any]:
    """
    ref: '@handle' or 'channel/UC...' or 'c/name' or 'user/name'.
    Returns title, canonical @handle, url, subscribers, and every Instagram
    handle the About page links to.
    """
    out = {"status": "failed", "title": "", "yt_handle": "", "url": "", "subscribers": None,
           "subscribers_precision": "rounded", "instagram_links": []}
    ref = ref.strip("/")
    for path in (f"{ref}/about", ref):
        try:
            r = _get(f"https://www.youtube.com/{path}")
        except Exception as e:
            out["status"] = type(e).__name__
            continue
        if r.status_code != 200:
            out["status"] = f"http_{r.status_code}"
            continue
        html = r.text
        t = re.search(r'<title>([^<]{0,120})', html)
        if t:
            out["title"] = unicodedata.normalize("NFKC", _html.unescape(t.group(1))).replace(" - YouTube", "").strip()
        m = re.search(r'"canonicalBaseUrl":"/(@[A-Za-z0-9_.\-]+)"', html) or \
            re.search(r'<link rel="canonical" href="https://www\.youtube\.com/(@[A-Za-z0-9_.\-]+)"', html)
        if m:
            out["yt_handle"] = m.group(1)
            out["url"] = f"https://www.youtube.com/{m.group(1)}"
        else:
            out["url"] = f"https://www.youtube.com/{ref}"
        subs = re.search(r'"subscriberCountText":\{"simpleText":"([^"]+?) subscribers"', html) or \
               re.search(r'([\d.,]+[KMB]?)\s+subscribers', html)
        if subs:
            tok = subs.group(1)
            out["subscribers"] = _count(tok)
            out["subscribers_precision"] = "rounded" if re.search(r'[KMB]', tok.upper()) else "exact"
        igs = set(re.findall(r'instagram\.com%2F([A-Za-z0-9_.]{3,30})', html))
        igs |= set(re.findall(r'instagram\.com/([A-Za-z0-9_.]{3,30})', html))
        out["instagram_links"] = sorted({x for x in (clean_handle(unquote(i)) for i in igs) if x})
        out["status"] = "ok"
        if out["yt_handle"]:
            break
    return out


def youtube_search(query: str) -> List[Dict[str, str]]:
    """Channel results (title + @handle) for one search."""
    try:
        r = _get(f"https://www.youtube.com/results?search_query={quote(query)}&sp=EgIQAg%3D%3D")  # channels only
    except Exception:
        return []
    if r.status_code != 200:
        return []
    out, seen = [], set()
    # channelRenderer blocks carry both the title and the canonical handle
    for blk in re.finditer(r'"channelRenderer":\{(.{0,4000}?)"canonicalBaseUrl":"/(@[A-Za-z0-9_.\-]+)"', r.text):
        h = blk.group(2)
        if h in seen:
            continue
        seen.add(h)
        t = re.search(r'"title":\{"simpleText":"([^"]+)"', blk.group(1))
        out.append({"yt_handle": h, "title": (_html.unescape(t.group(1)) if t else "")})
    if not out:
        for h in dict.fromkeys(re.findall(r'"canonicalBaseUrl":"/(@[A-Za-z0-9_.\-]+)"', r.text)):
            out.append({"yt_handle": h, "title": ""})
    return out[:8]


def _yt_refs_in(text: str) -> List[str]:
    refs = [m.group(1) for m in YT_HANDLE_RE.finditer(text)]
    refs += [m.group(1) for m in YT_CHANNEL_RE.finditer(text)]
    return list(dict.fromkeys(refs))


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (s or "").lower())


# ---- the ladder ------------------------------------------------------------------
def find_youtube(handle: str, name: str, external_url: str, bio: str = "") -> Dict[str, Any]:
    handle = clean_handle(handle) or handle
    res = {"handle": handle, "status": "none", "match_method": "", "yt_handle": "", "title": "",
           "url": "", "subscribers": None, "subscribers_precision": "", "possible_yt": "",
           "possible_title": "", "checked_at": now_iso()}
    ext = (external_url or "").strip()

    # Rung 1: bio link is YouTube
    refs = _yt_refs_in(ext) if "youtu" in ext.lower() else []
    if not refs and "youtu.be" in ext.lower():
        refs = []  # a single video link; the channel is not stated - fall through
    if refs:
        ch = resolve_channel(refs[0])
        if ch["status"] == "ok":
            return _accept(res, ch, "bio_link")

    # Rung 2: bio link is an aggregator page
    if any(a in ext.lower() for a in AGGREGATORS):
        try:
            r = _get(ext)
            if r.status_code == 200:
                refs = _yt_refs_in(r.text)
                if refs:
                    ch = resolve_channel(refs[0])
                    if ch["status"] == "ok":
                        return _accept(res, ch, "bio_aggregator")
        except Exception:
            pass
        time.sleep(PAUSE)

    # Rung 2b: a YouTube link written in the bio text itself
    refs = _yt_refs_in(bio or "")
    if refs:
        ch = resolve_channel(refs[0])
        if ch["status"] == "ok":
            return _accept(res, ch, "bio_link")

    # Rung 3: reverse search, accept only on a backlink to this Instagram handle
    queries = []
    if name and len(name.strip()) >= 3:
        queries.append(name.strip())
    queries.append(handle)
    cands: List[Dict[str, str]] = []
    seen = set()
    for q in queries:
        for c in youtube_search(q):
            if c["yt_handle"] not in seen:
                seen.add(c["yt_handle"]); cands.append(c)
        time.sleep(PAUSE)
    # cheapest-first: a handle that equals the IG handle is the likeliest
    cands.sort(key=lambda c: 0 if _norm(c["yt_handle"]) == _norm(handle) else 1)
    name_match = None
    backlink_only = None
    nn, nh = _norm(name), _norm(handle)
    for c in cands[:6]:
        ch = resolve_channel(c["yt_handle"])
        time.sleep(PAUSE)
        if ch["status"] != "ok":
            continue
        nt, nyh = _norm(ch["title"]), _norm(ch["yt_handle"].lstrip("@"))
        identity = bool(nn and len(nn) >= 4 and (nn in nt or nt in nn)) or                    bool(nh and (nh in nyh or nyh in nh) and len(nh) >= 5)
        backlinked = handle.lower() in [i.lower() for i in ch["instagram_links"]]
        if backlinked and identity:
            return _accept(res, ch, "yt_backlink")
        # a backlink from a channel with an unrelated name is what a fan page looks like
        if backlinked and backlink_only is None:
            backlink_only = ch
        if name_match is None and nn and nt == nn:
            name_match = ch
    poss = backlink_only or name_match
    if poss:
        res["status"] = "possible"
        res["possible_yt"] = poss["url"]
        res["possible_title"] = poss["title"] + (" [links to this IG, name differs]" if poss is backlink_only else " [same name, no backlink]")
    return res


def _accept(res, ch, method) -> Dict[str, Any]:
    res.update({"status": "found", "match_method": method, "yt_handle": ch["yt_handle"],
                "title": ch["title"], "url": ch["url"], "subscribers": ch["subscribers"],
                "subscribers_precision": ch["subscribers_precision"] if ch["subscribers"] is not None else ""})
    return res


# ---- store -----------------------------------------------------------------------
def _ensure_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cross_platform)")}
    for col, typ in (("match_method", "TEXT"), ("possible_url", "TEXT"), ("possible_title", "TEXT"),
                     ("status", "TEXT"), ("checked_at", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE cross_platform ADD COLUMN {col} {typ}")
    conn.commit()


def save(conn, r: Dict[str, Any]):
    _ensure_columns(conn)
    conn.execute("DELETE FROM cross_platform WHERE handle=? AND platform='youtube'", (r["handle"],))
    conn.execute(
        "INSERT INTO cross_platform (handle, platform, profile_url, display_name, reach, reach_precision, "
        "resolved_at, match_method, possible_url, possible_title, status, checked_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (r["handle"], "youtube", r["url"] or "", r["title"] or "", r["subscribers"],
         r["subscribers_precision"] or "", r["checked_at"], r["match_method"], r["possible_yt"],
         r["possible_title"], r["status"], r["checked_at"]))
    conn.commit()


def run(region: Optional[str], limit: int = 0, force: bool = False):
    conn = cdb.connect()
    _ensure_columns(conn)
    params: Dict[str, Any] = {}
    where = ["c.followers_precision='exact'", "c.followers >= 10000",
             "(c.review_flag IS NULL OR c.review_flag='')",
             "EXISTS (SELECT 1 FROM creator_deep_scan d WHERE d.handle=c.handle)"]
    if region:
        params["region"] = region
        where.append("(EXISTS (SELECT 1 FROM observations o WHERE o.handle=c.handle AND o.region=:region)"
                     " OR EXISTS (SELECT 1 FROM geo_evidence g WHERE g.handle=c.handle AND g.region=:region))")
    if not force:
        where.append("NOT EXISTS (SELECT 1 FROM cross_platform x WHERE x.handle=c.handle AND x.platform='youtube')")
    rows = conn.execute(f"SELECT c.handle, c.name, c.external_url, c.bio FROM creators c "
                        f"WHERE {' AND '.join(where)} ORDER BY c.followers DESC"
                        + (f" LIMIT {int(limit)}" if limit else ""), params).fetchall()
    print(f"[youtube] {len(rows)} creators to resolve")
    tally = {"found": 0, "possible": 0, "none": 0}
    for i, r in enumerate(rows, 1):
        r = dict(r)
        try:
            res = find_youtube(r["handle"], r["name"] or "", r["external_url"] or "", r.get("bio") or "")
        except Exception as e:
            res = {"handle": r["handle"], "status": "error", "match_method": "", "yt_handle": "", "title": "",
                   "url": "", "subscribers": None, "subscribers_precision": "", "possible_yt": "",
                   "possible_title": "", "checked_at": now_iso()}
            print(f"  [{i}/{len(rows)}] @{r['handle']} ERROR {type(e).__name__}")
        save(conn, res)
        tally[res["status"]] = tally.get(res["status"], 0) + 1
        tag = f"{res['match_method']} -> {res['url']}" if res["status"] == "found" else \
              (f"possible only: {res['possible_yt']}" if res["status"] == "possible" else "no channel found")
        print(f"  [{i}/{len(rows)}] @{r['handle']}: {tag}", flush=True)
    print(f"[youtube] done {tally}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__); sys.exit(0)
    if a[0] == "one":
        h = clean_handle(a[1]) or a[1].lstrip("@")
        conn = cdb.connect()
        row = conn.execute("SELECT name, external_url, bio FROM creators WHERE handle=?", (h,)).fetchone()
        name, ext, bio = (row["name"], row["external_url"], row["bio"]) if row else ("", "", "")
        res = find_youtube(h, name or "", ext or "", bio or "")
        print(json.dumps(res, indent=2, ensure_ascii=False))
        save(conn, res)
    elif a[0] == "run":
        reg = a[a.index("--region") + 1] if "--region" in a else None
        lim = int(a[a.index("--limit") + 1]) if "--limit" in a else 0
        run(reg, lim, force="--force" in a)
