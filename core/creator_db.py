"""
================================================================================
CREATOR INTELLIGENCE DATABASE  (creator_intelligence.db)
================================================================================
The permanent store. Every creator ever discovered, for every client and every
region, with the evidence behind each claim - so a later question is answered by
a query instead of another scrape.

One SQLite file, no server. Any IDE, agent, or script can open it and run SQL.
Schema and worked queries are documented in docs/CREATOR_DB.md.

TABLES
  creators           one row per Instagram handle - the exact follower count and
                     where it came from, tier, IG category, contacts
  observations       append-only: which source proposed this handle, when, for
                     which region. Corroboration is COUNT(DISTINCT source)
  geo_evidence       creator posted at a real place in a region. Several distinct
                     places in one region is residence evidence, not a bio guess
  campaign_evidence  creator posted under a campaign hashtag or at a campaign
                     place inside a date window (Durga Puja 2025, and so on)
  cross_platform     the same person on YouTube / Facebook, with reach
  brand_collabs      creator x brand partnership records, fed by the existing
                     competitor-collaborator scan
  regions            region definitions actually used, for reproducibility

WHY IT EXISTS
  Discovery was rebuilding a throwaway list per client - 170 creators for
  Kolkata, ~90 per state for Enamor - because nothing accumulated. Every run
  now adds to one pool, and each client brief becomes a filter over it:
  Britannia is region=kolkata + residence proof + campaign=durga_puja_2025;
  Enamor is the same query with four different regions.
================================================================================
"""

import sys, os, re, json, sqlite3
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Iterable, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

DB_PATH = os.path.join(BASE_DIR, "creator_intelligence.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS creators (
    handle              TEXT PRIMARY KEY,
    pk                  TEXT,
    name                TEXT,
    followers           INTEGER,
    followers_precision TEXT,        -- exact | rounded | unresolved
    followers_source    TEXT,
    resolved_at         TEXT,
    tier                TEXT,
    ig_category         TEXT,
    category            TEXT,        -- our own plain-text category label
    bio                 TEXT,
    email               TEXT,
    phone               TEXT,
    external_url        TEXT,
    city_name           TEXT,
    is_verified         INTEGER,
    is_private          INTEGER,
    media_count         INTEGER,
    following_count     INTEGER,
    branded_content_ready INTEGER,
    engagement_rate_pct REAL,
    avg_plays           INTEGER,
    audit_status        TEXT,
    review_flag         TEXT,
    first_seen          TEXT,
    updated_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_creators_followers ON creators(followers);
CREATE INDEX IF NOT EXISTS idx_creators_category  ON creators(category);
CREATE INDEX IF NOT EXISTS idx_creators_precision ON creators(followers_precision);

CREATE TABLE IF NOT EXISTS observations (
    handle       TEXT NOT NULL,
    source       TEXT NOT NULL,      -- chaining | llm | topsearch | geo | hashtag | hub | directory | youtube | manual
    detail       TEXT,               -- seed handle, query, url, tag ...
    region       TEXT,
    observed_at  TEXT,
    UNIQUE(handle, source, detail, region)
);
CREATE INDEX IF NOT EXISTS idx_obs_handle ON observations(handle);
CREATE INDEX IF NOT EXISTS idx_obs_region ON observations(region);

CREATE TABLE IF NOT EXISTS geo_evidence (
    handle         TEXT NOT NULL,
    region         TEXT NOT NULL,
    place_pk       TEXT NOT NULL,
    place_name     TEXT,
    post_taken_at  INTEGER,
    like_count     INTEGER,
    observed_at    TEXT,
    UNIQUE(handle, place_pk, post_taken_at)
);
CREATE INDEX IF NOT EXISTS idx_geo_handle ON geo_evidence(handle);
CREATE INDEX IF NOT EXISTS idx_geo_region ON geo_evidence(region);

CREATE TABLE IF NOT EXISTS campaign_evidence (
    handle         TEXT NOT NULL,
    campaign       TEXT NOT NULL,    -- durga_puja_2025, diwali_2025 ...
    evidence_type  TEXT,             -- hashtag | location
    evidence_ref   TEXT,             -- the tag or place name
    post_taken_at  INTEGER,
    post_code      TEXT,
    like_count     INTEGER,
    observed_at    TEXT,
    UNIQUE(handle, campaign, evidence_type, evidence_ref, post_taken_at)
);
CREATE INDEX IF NOT EXISTS idx_camp_handle   ON campaign_evidence(handle);
CREATE INDEX IF NOT EXISTS idx_camp_campaign ON campaign_evidence(campaign);

CREATE TABLE IF NOT EXISTS cross_platform (
    handle        TEXT NOT NULL,
    platform      TEXT NOT NULL,     -- youtube | facebook
    profile_url   TEXT,
    display_name  TEXT,
    reach         INTEGER,           -- subscribers / page followers
    reach_precision TEXT,
    resolved_at   TEXT,
    UNIQUE(handle, platform, profile_url)
);
CREATE INDEX IF NOT EXISTS idx_xplat_handle ON cross_platform(handle);

CREATE TABLE IF NOT EXISTS brand_collabs (
    handle              TEXT NOT NULL,
    brand               TEXT NOT NULL,
    post_url            TEXT,
    is_paid_partnership INTEGER,
    tier                TEXT,        -- the 4-tier taxonomy
    post_taken_at       INTEGER,
    observed_at         TEXT,
    UNIQUE(handle, brand, post_url)
);
CREATE INDEX IF NOT EXISTS idx_collab_handle ON brand_collabs(handle);
CREATE INDEX IF NOT EXISTS idx_collab_brand  ON brand_collabs(brand);

CREATE TABLE IF NOT EXISTS regions (
    region       TEXT PRIMARY KEY,
    label        TEXT,
    config_json  TEXT,
    updated_at   TEXT
);

-- Region confidence, derived rather than asserted: distinct places posted at,
-- distinct discovery sources, and whether the bio/name says so.
CREATE VIEW IF NOT EXISTS creator_region_evidence AS
SELECT
    c.handle,
    r.region,
    (SELECT COUNT(DISTINCT g.place_pk) FROM geo_evidence g
       WHERE g.handle = c.handle AND g.region = r.region)          AS distinct_places,
    (SELECT COUNT(DISTINCT o.source) FROM observations o
       WHERE o.handle = c.handle AND o.region = r.region)          AS distinct_sources,
    (SELECT COUNT(*) FROM observations o
       WHERE o.handle = c.handle AND o.region = r.region)          AS observation_count
FROM creators c
CROSS JOIN regions r;
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ==============================================================================
# WRITES
# ==============================================================================
CREATOR_FIELDS = [
    "pk", "name", "followers", "followers_precision", "followers_source",
    "resolved_at", "tier", "ig_category", "category", "bio", "email", "phone",
    "external_url", "city_name", "is_verified", "is_private", "media_count",
    "following_count", "branded_content_ready", "engagement_rate_pct",
    "avg_plays", "audit_status", "review_flag",
]


def upsert_creator(conn: sqlite3.Connection, audit: Dict[str, Any]) -> str:
    """
    Writes one audited creator. Existing rows are only overwritten by a count of
    equal or better precision, so a rounded re-read can never clobber an exact one.
    """
    handle = str(audit.get("raw_handle") or audit.get("handle", "")).replace("@", "").lower()
    if not handle:
        raise ValueError("audit has no handle")

    eng = audit.get("engagement") or {}
    row = {
        "pk": audit.get("pk"),
        "name": audit.get("name") or audit.get("full_name"),
        "followers": audit.get("followers"),
        "followers_precision": audit.get("followers_precision"),
        "followers_source": audit.get("followers_source"),
        "resolved_at": audit.get("resolved_at"),
        "tier": audit.get("tier"),
        "ig_category": audit.get("ig_category"),
        "category": audit.get("category"),
        "bio": audit.get("bio"),
        "email": audit.get("email"),
        "phone": audit.get("phone"),
        "external_url": audit.get("external_url"),
        "city_name": audit.get("city_name"),
        "is_verified": _as_int(audit.get("is_verified")),
        "is_private": _as_int(audit.get("is_private")),
        "media_count": audit.get("media_count"),
        "following_count": audit.get("following_count"),
        "branded_content_ready": _as_int(audit.get("branded_content_ready")),
        "engagement_rate_pct": eng.get("engagement_rate_pct"),
        "avg_plays": eng.get("avg_plays"),
        "audit_status": audit.get("status"),
        "review_flag": audit.get("review_flag") or "",
    }

    existing = conn.execute("SELECT followers_precision FROM creators WHERE handle=?",
                           (handle,)).fetchone()
    rank = {"exact": 3, "rounded": 2, "unresolved": 1, None: 0, "": 0}
    if existing:
        if rank.get(row["followers_precision"], 0) < rank.get(existing["followers_precision"], 0):
            # Keep the better count, but let the non-count fields refresh.
            for k in ("followers", "followers_precision", "followers_source", "resolved_at", "tier"):
                row.pop(k)
        sets = ", ".join(f"{k}=:{k}" for k in row)
        conn.execute(f"UPDATE creators SET {sets}, updated_at=:updated_at WHERE handle=:handle",
                     {**row, "handle": handle, "updated_at": now_iso()})
    else:
        cols = ", ".join(["handle"] + list(row) + ["first_seen", "updated_at"])
        ph = ", ".join([":handle"] + [f":{k}" for k in row] + [":first_seen", ":updated_at"])
        conn.execute(f"INSERT INTO creators ({cols}) VALUES ({ph})",
                     {**row, "handle": handle, "first_seen": now_iso(), "updated_at": now_iso()})
    conn.commit()
    return handle


def _as_int(v):
    if v is None:
        return None
    return 1 if v else 0


def add_observation(conn, handle: str, source: str, detail: str = "",
                    region: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO observations (handle, source, detail, region, observed_at) "
        "VALUES (?,?,?,?,?)",
        (handle.replace("@", "").lower(), source, (detail or "")[:200], region, now_iso()))


def add_geo_evidence(conn, handle: str, region: str, place_pk: str, place_name: str,
                     post_taken_at: Optional[int] = None,
                     like_count: Optional[int] = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO geo_evidence "
        "(handle, region, place_pk, place_name, post_taken_at, like_count, observed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (handle.replace("@", "").lower(), region, str(place_pk), (place_name or "")[:120],
         post_taken_at, like_count, now_iso()))


def add_campaign_evidence(conn, handle: str, campaign: str, evidence_type: str,
                          evidence_ref: str, post_taken_at: Optional[int] = None,
                          post_code: str = "", like_count: Optional[int] = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO campaign_evidence "
        "(handle, campaign, evidence_type, evidence_ref, post_taken_at, post_code, "
        " like_count, observed_at) VALUES (?,?,?,?,?,?,?,?)",
        (handle.replace("@", "").lower(), campaign, evidence_type, (evidence_ref or "")[:120],
         post_taken_at, post_code, like_count, now_iso()))


def add_cross_platform(conn, handle: str, platform: str, profile_url: str,
                       display_name: str = "", reach: Optional[int] = None,
                       reach_precision: str = "rounded") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO cross_platform "
        "(handle, platform, profile_url, display_name, reach, reach_precision, resolved_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (handle.replace("@", "").lower(), platform, profile_url, display_name,
         reach, reach_precision, now_iso()))


def add_brand_collab(conn, handle: str, brand: str, post_url: str = "",
                     is_paid_partnership: Optional[bool] = None, tier: str = "",
                     post_taken_at: Optional[int] = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO brand_collabs "
        "(handle, brand, post_url, is_paid_partnership, tier, post_taken_at, observed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (handle.replace("@", "").lower(), brand.lower(), post_url,
         _as_int(is_paid_partnership), tier, post_taken_at, now_iso()))


def register_region(conn, region: str, label: str, config: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO regions (region, label, config_json, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(region) DO UPDATE SET label=excluded.label, "
        "config_json=excluded.config_json, updated_at=excluded.updated_at",
        (region, label, json.dumps(config, ensure_ascii=False), now_iso()))
    conn.commit()


def add_candidate_only(conn, handle: str, source: str, detail: str = "",
                       region: str = "") -> None:
    """
    Records a lead before it has been audited. The creators row is created with
    precision 'unresolved' so it is never mistaken for a verified metric.
    """
    h = handle.replace("@", "").lower()
    conn.execute(
        "INSERT OR IGNORE INTO creators (handle, followers, followers_precision, "
        "audit_status, first_seen, updated_at) VALUES (?,?,?,?,?,?)",
        (h, 0, "unresolved", "PENDING_AUDIT", now_iso(), now_iso()))
    add_observation(conn, h, source, detail, region)


# ==============================================================================
# QUERY
# ==============================================================================
def query_creators(conn: sqlite3.Connection,
                   region: Optional[str] = None,
                   category: Optional[str] = None,
                   min_followers: Optional[int] = None,
                   max_followers: Optional[int] = None,
                   tier: Optional[str] = None,
                   campaign: Optional[str] = None,
                   brand: Optional[str] = None,
                   exclude_brand: Optional[str] = None,
                   min_distinct_places: int = 0,
                   min_sources: int = 0,
                   require_exact: bool = True,
                   has_email: bool = False,
                   verified_only: bool = False,
                   platform: Optional[str] = None,
                   min_engagement: Optional[float] = None,
                   exclude_flagged: bool = True,
                   order_by: str = "followers DESC",
                   limit: int = 500) -> List[sqlite3.Row]:
    """
    One filter surface over the whole pool. A client brief maps onto it directly:

      Britannia  region='kolkata', min_distinct_places=2,
                 campaign='durga_puja_2025', min_followers=10000
      Enamor     the same call once per region
      Visa       brand='<competitor>' to pull the competitor's collaborators
    """
    where, params = ["1=1"], {}

    if require_exact:
        where.append("c.followers_precision = 'exact'")
    if min_followers is not None:
        where.append("c.followers >= :minf")
        params["minf"] = min_followers
    if max_followers is not None:
        where.append("c.followers <= :maxf")
        params["maxf"] = max_followers
    if tier:
        where.append("c.tier = :tier")
        params["tier"] = tier
    if category:
        where.append("(c.category LIKE :cat OR c.ig_category LIKE :cat)")
        params["cat"] = f"%{category}%"
    if has_email:
        where.append("c.email IS NOT NULL AND c.email != '' AND c.email != 'N/A'")
    if verified_only:
        where.append("c.is_verified = 1")
    if min_engagement is not None:
        where.append("c.engagement_rate_pct >= :mineng")
        params["mineng"] = min_engagement
    if exclude_flagged:
        where.append("(c.review_flag IS NULL OR c.review_flag = '')")

    if region:
        params["region"] = region
        # Present in the region by any route: a discovery observation tagged to it,
        # geo evidence in it, or the account's own city field.
        where.append("""(
            EXISTS (SELECT 1 FROM observations o
                      WHERE o.handle = c.handle AND o.region = :region)
         OR EXISTS (SELECT 1 FROM geo_evidence g
                      WHERE g.handle = c.handle AND g.region = :region)
         OR LOWER(COALESCE(c.city_name,'')) LIKE '%' || :region || '%'
        )""")
    if min_distinct_places > 0:
        params["mdp"] = min_distinct_places
        if region:
            where.append("(SELECT COUNT(DISTINCT g.place_pk) FROM geo_evidence g "
                         "WHERE g.handle = c.handle AND g.region = :region) >= :mdp")
        else:
            where.append("(SELECT COUNT(DISTINCT g.place_pk) FROM geo_evidence g "
                         "WHERE g.handle = c.handle) >= :mdp")
    if min_sources > 0:
        params["msrc"] = min_sources
        where.append("(SELECT COUNT(DISTINCT o.source) FROM observations o "
                     "WHERE o.handle = c.handle) >= :msrc")
    if campaign:
        params["camp"] = campaign
        where.append("EXISTS (SELECT 1 FROM campaign_evidence e "
                     "WHERE e.handle = c.handle AND e.campaign = :camp)")
    if brand:
        params["brand"] = brand.lower()
        where.append("EXISTS (SELECT 1 FROM brand_collabs b "
                     "WHERE b.handle = c.handle AND b.brand = :brand)")
    if exclude_brand:
        params["xbrand"] = exclude_brand.lower()
        where.append("NOT EXISTS (SELECT 1 FROM brand_collabs b "
                     "WHERE b.handle = c.handle AND b.brand = :xbrand)")
    if platform:
        params["plat"] = platform
        where.append("EXISTS (SELECT 1 FROM cross_platform x "
                     "WHERE x.handle = c.handle AND x.platform = :plat)")

    allowed_order = {
        "followers DESC", "followers ASC", "engagement_rate_pct DESC",
        "updated_at DESC", "handle ASC", "distinct_places DESC", "source_count DESC",
    }
    if order_by not in allowed_order:
        order_by = "followers DESC"

    sql = f"""
        SELECT c.*,
               (SELECT COUNT(DISTINCT o.source) FROM observations o
                  WHERE o.handle = c.handle) AS source_count,
               (SELECT GROUP_CONCAT(DISTINCT o.source) FROM observations o
                  WHERE o.handle = c.handle) AS sources,
               (SELECT COUNT(DISTINCT g.place_pk) FROM geo_evidence g
                  WHERE g.handle = c.handle {'AND g.region = :region' if region else ''})
                  AS distinct_places,
               (SELECT GROUP_CONCAT(DISTINCT e.campaign) FROM campaign_evidence e
                  WHERE e.handle = c.handle) AS campaigns,
               (SELECT GROUP_CONCAT(DISTINCT b.brand) FROM brand_collabs b
                  WHERE b.handle = c.handle) AS brands,
               (SELECT GROUP_CONCAT(x.platform || ':' || COALESCE(x.reach,0))
                  FROM cross_platform x WHERE x.handle = c.handle) AS other_platforms
        FROM creators c
        WHERE {' AND '.join(where)}
        ORDER BY {order_by}
        LIMIT :lim
    """
    params["lim"] = limit
    return conn.execute(sql, params).fetchall()


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    def one(sql, *a):
        r = conn.execute(sql, a).fetchone()
        return r[0] if r else 0
    out = {
        "creators_total": one("SELECT COUNT(*) FROM creators"),
        "creators_exact": one("SELECT COUNT(*) FROM creators WHERE followers_precision='exact'"),
        "creators_pending_audit": one("SELECT COUNT(*) FROM creators WHERE audit_status='PENDING_AUDIT'"),
        "creators_above_10k": one("SELECT COUNT(*) FROM creators WHERE followers>=10000 "
                                  "AND followers_precision='exact'"),
        "observations": one("SELECT COUNT(*) FROM observations"),
        "geo_evidence": one("SELECT COUNT(*) FROM geo_evidence"),
        "campaign_evidence": one("SELECT COUNT(*) FROM campaign_evidence"),
        "cross_platform": one("SELECT COUNT(*) FROM cross_platform"),
        "brand_collabs": one("SELECT COUNT(*) FROM brand_collabs"),
    }
    out["by_region"] = {r["region"]: r["n"] for r in conn.execute(
        "SELECT region, COUNT(DISTINCT handle) n FROM observations "
        "WHERE region != '' GROUP BY region ORDER BY n DESC")}
    out["by_source"] = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(DISTINCT handle) n FROM observations GROUP BY source ORDER BY n DESC")}
    out["by_campaign"] = {r["campaign"]: r["n"] for r in conn.execute(
        "SELECT campaign, COUNT(DISTINCT handle) n FROM campaign_evidence GROUP BY campaign")}
    out["by_tier"] = {r["tier"]: r["n"] for r in conn.execute(
        "SELECT tier, COUNT(*) n FROM creators WHERE followers_precision='exact' "
        "GROUP BY tier ORDER BY n DESC")}
    return out


# ==============================================================================
# NATURAL LANGUAGE -> FILTERS
# ==============================================================================
# Deliberately small and predictable. The real interface for an agent is the
# documented schema plus --sql; this just covers the phrasings that come up daily.
_TIER_WORDS = {
    "mega": "Mega (1M+)", "macro": "Macro (500K-1M)",
    "mid": "Mid-Tier (100K-500K)", "mid-tier": "Mid-Tier (100K-500K)",
    "micro": "Micro (10K-100K)", "nano": "Nano (<10K)",
}


def parse_nl_query(text: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Turns 'kolkata food creators 50k-200k who did durga pujo with email' into filters."""
    t = " " + text.lower().strip() + " "
    f: Dict[str, Any] = {}

    known_regions = []
    if conn is not None:
        known_regions = [r["region"] for r in conn.execute("SELECT region FROM regions")]
    if not known_regions:
        known_regions = ["kolkata", "punjab", "hyderabad", "chennai", "mumbai",
                         "delhi", "bangalore", "pune", "ahmedabad", "jaipur", "lucknow"]
    for r in known_regions:
        if re.search(rf'\b{re.escape(r)}\b', t):
            f["region"] = r
            break

    for word, label in _TIER_WORDS.items():
        if re.search(rf'\b{word}\b', t):
            f["tier"] = label
            break

    def numtok(s: str) -> int:
        s = s.replace(",", "").strip()
        if s.endswith("k"):
            return int(float(s[:-1]) * 1_000)
        if s.endswith("m"):
            return int(float(s[:-1]) * 1_000_000)
        return int(float(s))

    rng = re.search(r'([\d.,]+\s*[km]?)\s*(?:-|to|–)\s*([\d.,]+\s*[km]?)', t)
    if rng:
        f["min_followers"], f["max_followers"] = numtok(rng.group(1)), numtok(rng.group(2))
    else:
        above = re.search(r'(?:above|over|more than|min(?:imum)?|\+)\s*([\d.,]+\s*[km]?)', t)
        if above:
            f["min_followers"] = numtok(above.group(1))
        below = re.search(r'(?:below|under|less than|max(?:imum)?)\s*([\d.,]+\s*[km]?)', t)
        if below:
            f["max_followers"] = numtok(below.group(1))
        bare = re.search(r'\b(\d+\s*[km])\+', t)
        if bare and "min_followers" not in f:
            f["min_followers"] = numtok(bare.group(1))

    CAT_WORDS = {
        "food": "Food", "culinary": "Food", "chef": "Food", "fashion": "Fashion",
        "beauty": "Beauty", "makeup": "Beauty", "skincare": "Beauty",
        "comedy": "Comedy", "travel": "Travel", "fitness": "Fitness",
        "dance": "Dance", "cinema": "Tollywood", "actor": "Tollywood",
        "lifestyle": "Lifestyle",
    }
    for w, cat in CAT_WORDS.items():
        if re.search(rf'\b{w}', t):
            f["category"] = cat
            break

    if conn is not None:
        for r in conn.execute("SELECT DISTINCT campaign FROM campaign_evidence"):
            camp = r["campaign"]
            loose = camp.replace("_", " ")
            if camp in t or loose in t:
                f["campaign"] = camp
                break
    if "campaign" not in f:
        m = re.search(r'(durga\s*pu[jz]?[oa]|pujo|diwali|holi|navratri|onam|pongal)', t)
        if m:
            yr = re.search(r'\b(20\d\d)\b', t)
            base = re.sub(r'\s+', '_', m.group(1).strip())
            base = "durga_puja" if "durga" in base or "pujo" in base else base
            f["campaign"] = f"{base}_{yr.group(1)}" if yr else base

    if re.search(r'\b(email|contact|reachable|contactable)\b', t):
        f["has_email"] = True
    if re.search(r'\bverified\b', t):
        f["verified_only"] = True
    if re.search(r'\b(lives?|living|based|resident|residing|actually in|local)\b', t):
        f["min_distinct_places"] = 2
    if re.search(r'\byoutube\b', t):
        f["platform"] = "youtube"

    m = re.search(r'\b(?:top|first|give me|show me)\s+(\d{1,4})\b', t)
    if m:
        f["limit"] = int(m.group(1))

    m = re.search(r'collaborat\w*\s+(?:of|with)\s+@?([a-z0-9_.]{3,30})', t)
    if m:
        f["brand"] = m.group(1)

    return f


# ==============================================================================
# EXPORT
# ==============================================================================
def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


def export_rows_xlsx(rows: Iterable[sqlite3.Row], filename: str, sheet_title: str = "Creators",
                     brief: str = "") -> str:
    """Writes a client-ready workbook. Emoji-free, exact counts labelled as such."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from core.kolkata_engine import clean_cell

    data = rows_to_dicts(rows)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title[:31]

    cols = [
        ("S.No", None), ("Handle", "handle"), ("Creator Name", "name"),
        ("Followers (Exact)", "followers"), ("Count Precision", "followers_precision"),
        ("Resolved At (UTC)", "resolved_at"), ("Tier", "tier"), ("Category", "category"),
        ("IG Category", "ig_category"), ("Verified", "is_verified"),
        ("Engagement Rate %", "engagement_rate_pct"),
        ("Distinct Places In Region", "distinct_places"),
        ("Campaign Evidence", "campaigns"), ("Brand Collabs", "brands"),
        ("Other Platforms", "other_platforms"),
        ("Email", "email"), ("Phone", "phone"), ("City", "city_name"),
        ("Bio Summary", "bio"), ("Discovery Sources", "sources"),
        ("Source Count", "source_count"), ("Profile URL", None),
    ]

    hf = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    hfill = PatternFill(start_color="1A365D", end_color="1A365D", fill_type="solid")
    bd = Border(*[Side(style='thin', color='D5D8DC')] * 4)

    for i, (title, _) in enumerate(cols, 1):
        c = ws.cell(1, i, title)
        c.font, c.fill = hf, hfill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = bd

    for r_i, row in enumerate(data, 1):
        for c_i, (title, key) in enumerate(cols, 1):
            if title == "S.No":
                val = r_i
            elif title == "Profile URL":
                val = f"https://www.instagram.com/{row.get('handle','')}/"
            elif key == "is_verified":
                v = row.get(key)
                val = "Yes" if v == 1 else ("No" if v == 0 else "Unknown")
            else:
                val = row.get(key)
                if key == "bio" and isinstance(val, str):
                    val = val[:220]
            cell = ws.cell(r_i + 1, c_i, clean_cell(val))
            cell.border = bd
            cell.alignment = Alignment(vertical="center")

    widths = [6, 24, 26, 15, 14, 20, 22, 20, 18, 9, 15, 16, 26, 26, 22, 28, 15, 16, 45, 30, 12, 38]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C2"

    ws2 = wb.create_sheet("Brief And Method")
    ws2.cell(1, 1, "Field").font = hf
    ws2.cell(1, 1).fill = hfill
    ws2.cell(1, 2, "Value").font = hf
    ws2.cell(1, 2).fill = hfill
    notes = [
        ("Brief", brief or "not recorded"),
        ("Rows", len(data)),
        ("Generated (UTC)", now_iso()),
        ("Follower counts", "Exact, read live from Instagram. Precision and read time are "
                            "per row. Nothing is estimated or interpolated."),
        ("Residence evidence", "Distinct Places In Region counts separate real Instagram "
                               "locations in the region the creator has posted at. Two or "
                               "more is treated as residence evidence rather than a bio guess."),
        ("Campaign evidence", "Posted under a campaign hashtag or at a campaign location "
                              "inside the campaign date window."),
        ("Source of record", "creator_intelligence.db - see docs/CREATOR_DB.md"),
    ]
    for i, (k, v) in enumerate(notes, 2):
        ws2.cell(i, 1, k).border = bd
        ws2.cell(i, 2, clean_cell(v)).border = bd
    ws2.column_dimensions['A'].width = 24
    ws2.column_dimensions['B'].width = 95

    try:
        wb.save(filename)
    except PermissionError:
        filename = filename.replace(".xlsx", f"_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        wb.save(filename)
    return filename


# ==============================================================================
# CLI
# ==============================================================================
def _print_rows(rows, limit=40):
    print(f"{'handle':26s} {'followers':>10s} {'prec':6s} {'tier':22s} "
          f"{'plc':>3s} {'src':>3s} {'email':28s} campaigns")
    print("-" * 130)
    for r in list(rows)[:limit]:
        print(f"{r['handle'][:26]:26s} {r['followers'] or 0:>10,} "
              f"{(r['followers_precision'] or '')[:6]:6s} {(r['tier'] or '')[:22]:22s} "
              f"{r['distinct_places'] or 0:>3d} {r['source_count'] or 0:>3d} "
              f"{(r['email'] or '')[:28]:28s} {r['campaigns'] or ''}")


def _cli():
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    conn = connect()

    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        print("Commands:")
        print('  stats                              counts by region, source, tier, campaign')
        print('  ask "<natural language>"           NL query -> filters -> rows')
        print('  query --region kolkata --category food --min 10000 [--max N]')
        print('        [--campaign durga_puja_2025] [--brand X] [--residence 2]')
        print('        [--has-email] [--verified] [--sources 2] [--limit N]')
        print('        [--xlsx out.xlsx] [--json out.json] [--allow-rounded]')
        print('  sql "<SELECT ...>"                 raw read-only SQL')
        print('  schema                             print the schema')
        return

    cmd = args[0]

    if cmd == "stats":
        s = stats(conn)
        for k, v in s.items():
            if isinstance(v, dict):
                print(f"{k}:")
                for kk, vv in v.items():
                    print(f"    {kk:28s} {vv}")
            else:
                print(f"{k:26s} {v}")
        return

    if cmd == "schema":
        for r in conn.execute("SELECT type, name, sql FROM sqlite_master "
                              "WHERE sql IS NOT NULL ORDER BY type, name"):
            print(f"\n-- {r['type']} {r['name']}\n{r['sql']};")
        return

    if cmd == "sql":
        if len(args) < 2:
            print("usage: sql \"SELECT ...\"")
            return
        q = args[1].strip()
        if not q.lower().lstrip("(").startswith(("select", "with", "pragma", "explain")):
            print("Read-only: only SELECT / WITH / PRAGMA / EXPLAIN are accepted here.")
            return
        rows = conn.execute(q).fetchall()
        if rows:
            print(" | ".join(rows[0].keys()))
            for r in rows[:200]:
                print(" | ".join(str(x) for x in tuple(r)))
        print(f"\n{len(rows)} row(s)")
        return

    if cmd == "ask":
        if len(args) < 2:
            print('usage: ask "kolkata food creators above 50k who did durga pujo 2025 with email"')
            return
        text = args[1]
        f = parse_nl_query(text, conn)
        print(f"brief   : {text}")
        print(f"filters : {json.dumps(f, ensure_ascii=False)}")
        limit = f.pop("limit", 100)
        rows = query_creators(conn, limit=limit, **f)
        print(f"matches : {len(rows)}\n")
        _print_rows(rows)
        return

    if cmd == "query":
        f: Dict[str, Any] = {}
        out_xlsx = out_json = None
        i = 1
        while i < len(args):
            a = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else None
            if a == "--region":
                f["region"] = nxt; i += 2
            elif a == "--category":
                f["category"] = nxt; i += 2
            elif a == "--min":
                f["min_followers"] = int(nxt); i += 2
            elif a == "--max":
                f["max_followers"] = int(nxt); i += 2
            elif a == "--tier":
                f["tier"] = nxt; i += 2
            elif a == "--campaign":
                f["campaign"] = nxt; i += 2
            elif a == "--brand":
                f["brand"] = nxt; i += 2
            elif a == "--exclude-brand":
                f["exclude_brand"] = nxt; i += 2
            elif a == "--residence":
                f["min_distinct_places"] = int(nxt); i += 2
            elif a == "--sources":
                f["min_sources"] = int(nxt); i += 2
            elif a == "--platform":
                f["platform"] = nxt; i += 2
            elif a == "--min-engagement":
                f["min_engagement"] = float(nxt); i += 2
            elif a == "--limit":
                f["limit"] = int(nxt); i += 2
            elif a == "--has-email":
                f["has_email"] = True; i += 1
            elif a == "--verified":
                f["verified_only"] = True; i += 1
            elif a == "--allow-rounded":
                f["require_exact"] = False; i += 1
            elif a == "--include-flagged":
                f["exclude_flagged"] = False; i += 1
            elif a == "--xlsx":
                out_xlsx = nxt; i += 2
            elif a == "--json":
                out_json = nxt; i += 2
            else:
                print(f"unknown option: {a}")
                return
        rows = query_creators(conn, **f)
        print(f"filters: {json.dumps(f, ensure_ascii=False)}")
        print(f"matches: {len(rows)}\n")
        _print_rows(rows)
        if out_json:
            json.dump(rows_to_dicts(rows), open(out_json, "w", encoding="utf-8"),
                      indent=2, ensure_ascii=False)
            print(f"\n[JSON] {out_json}")
        if out_xlsx:
            p = export_rows_xlsx(rows, out_xlsx,
                                 brief=json.dumps(f, ensure_ascii=False))
            print(f"[EXCEL] {p}")
        return

    print(f"unknown command: {cmd}")


if __name__ == "__main__":
    _cli()
