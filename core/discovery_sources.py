"""
================================================================================
CORE MODULE: FREE MULTI-SOURCE CREATOR CANDIDATE HARVESTER  (v1.0)
================================================================================
Everything here is free. No paid influencer-discovery product is used anywhere.

Candidate sources, ordered by measured precision (see notes on each function):

  S1  CHAINING GRAPH   Instagram's own "similar accounts" recommender.
                       /api/v1/discover/chaining/?target_id={pk}
                       ~77-80 accounts per seed. Highest precision source found:
                       seeded with a Kolkata food blogger it returns Kolkata
                       food bloggers. BFS-expandable - every creator that clears
                       the gate becomes the next hop's seed.

  S2  LLM CITATIONS    What ChatGPT / Perplexity / Gemini answer when asked for
                       the top creators in a region and category, harvested by
                       the LLMxCitations scraper and read back from its
                       output.csv. Strong on established names, blind to
                       emerging ones. Leads only - never a metric.

  S3  TOPSEARCH GRID   /web/search/topsearch/ over a region x category query
                       grid. ~5 users per query, matched on username and
                       display name, so it finds accounts that self-label their
                       region. Also pulls in shops named after the region -
                       flagged, not silently dropped.

  S4  GEO SECTIONS     Posts actually geotagged at real places in the region
                       (fbsearch/places -> locations/{pk}/sections/). Low
                       precision, high recall, and the only source that proves
                       physical presence rather than a bio keyword. Best used
                       to corroborate authenticity and to surface creators no
                       listicle mentions.

  S5  HASHTAG SECTIONS Authors posting under regional hashtags.

  S6  HUB CRAWL        The legacy source: scrape handles out of community hub
                       pages. Kept, but health-checked first - hub accounts rot.

Nothing here reports a follower count. Counts come only from
core.profile_auditor, which resolves them exactly and stamps their provenance.
================================================================================
"""

import sys, os, re, json, time
from datetime import datetime, timezone
from typing import Dict, List, Any, Set, Optional, Iterable, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from curl_cffi import requests as cffi_requests

from core.profile_auditor import (
    COOKIES, IG_APP_ID, WEB_UA, ANDROID_UA,
    _headers, is_throttled, now_iso,
)

CANDIDATE_POOL_FILE = os.path.join(BASE_DIR, "discovery_candidates_pool.json")
LLM_PROMPTS_FILE = os.path.join(BASE_DIR, "llm_discovery_prompts.csv")

IGNORE_HANDLES = {
    'instagram', 'threads', 'explore', 'about', 'developer', 'accounts',
    'directory', 'legal', 'privacy', 'p', 'reel', 'reels', 'stories', 'direct',
    'popular', 'nametag', 'help', 'api', 'terms', 'meta', 'facebook',
    'schwarzdontfollow', 'creators', 'shop', 'igtv',
}

# Accounts whose identity reads like a shop or reseller rather than a creator.
# This is a FLAG, not a filter - some are legitimate partnership targets.
RETAILER_TOKENS = [
    'mart', 'bazar', 'bazaar', 'store', 'stores', 'shop', 'shoppe', 'wholesale',
    'reseller', 'collection', 'collections', 'emporium', 'trader', 'traders',
    'official_store', 'onlineshop', 'buy', 'sale', 'offers',
]


def _get(url: str, ua: str = WEB_UA, timeout: int = 25):
    session = cffi_requests.Session(impersonate="chrome120")
    return session.get(url, headers=_headers(ua), cookies=COOKIES, timeout=timeout)


def _post(url: str, data: Dict[str, str], ua: str = ANDROID_UA, timeout: int = 30):
    session = cffi_requests.Session(impersonate="chrome120")
    return session.post(url, headers=_headers(ua), cookies=COOKIES, data=data, timeout=timeout)


# "@" in scraped prose is as often an email or a website as it is a handle, so
# anything ending in a TLD is rejected: @gmail.com, @terareach.com, @clout.pocketaces.in
DOMAIN_SUFFIX_RE = re.compile(
    r'\.(com|in|co|net|org|io|ai|me|app|shop|store|info|biz|us|uk|xyz|dev|live|tv)$')


def clean_handle(raw: str) -> Optional[str]:
    h = str(raw or "").strip().lower().replace("@", "").rstrip('.').rstrip('/')
    if not h or len(h) < 3 or len(h) > 30:
        return None
    if not re.fullmatch(r'[a-z0-9_.]+', h):
        return None
    if h in IGNORE_HANDLES or h.isdigit():
        return None
    if DOMAIN_SUFFIX_RE.search(h):
        return None
    return h


def looks_like_retailer(username: str, full_name: str = "") -> bool:
    blob = f"{username} {full_name}".lower()
    return any(t in blob for t in RETAILER_TOKENS)


# ==============================================================================
# REGION / CATEGORY CONFIGURATION - the parameterizable part
# ==============================================================================
REGIONS: Dict[str, Dict[str, Any]] = {
    "kolkata": {
        "label": "Kolkata & West Bengal",
        # Every neighbourhood, landmark and district a Kolkata creator might tag.
        # A creator who posts from two or more of these is treated as resident.
        "place_queries": [
            # city-wide
            "kolkata", "calcutta", "kolkata west bengal",
            # south
            "park street kolkata", "gariahat", "ballygunge", "bhowanipore", "alipore",
            "new alipore", "tollygunge", "jadavpur", "garia", "behala", "kalighat", "hazra",
            "chetla", "rashbehari avenue", "southern avenue", "golpark", "kasba", "santoshpur",
            "park circus", "topsia", "tangra", "taratala", "joka", "thakurpukur", "narendrapur",
            "sonarpur", "baruipur",
            # central
            "esplanade kolkata", "dalhousie kolkata", "chowringhee", "college street kolkata",
            "sealdah", "entally", "beleghata", "phoolbagan", "kankurgachi", "ultadanga",
            "maniktala", "bowbazar",
            # north
            "shyambazar", "bagbazar", "kumartuli", "sovabazar", "hatibagan", "dum dum",
            "lake town kolkata", "baguiati", "kestopur", "belgharia", "barrackpore",
            "barasat", "madhyamgram", "dakshineswar", "sinthee",
            # east / new town
            "salt lake city kolkata", "sector 5 salt lake", "new town kolkata", "rajarhat",
            "eco park kolkata", "city centre salt lake", "action area newtown",
            # howrah and hooghly
            "howrah", "shibpur", "howrah bridge", "belur math", "serampore", "chandannagar",
            "uttarpara", "konnagar", "chinsurah",
            # landmarks and venues
            "victoria memorial", "eden gardens", "prinsep ghat", "maidan kolkata",
            "science city kolkata", "nicco park", "south city mall", "quest mall",
            "acropolis mall", "mani square", "forum mall kolkata", "axis mall",
            "netaji subhash chandra bose international airport", "howrah station",
            "sealdah station", "kalighat temple", "birla planetarium", "indian museum",
            "jorasanko thakur bari", "st pauls cathedral kolkata", "millennium park kolkata",
            "nandan kolkata", "rabindra sadan", "salt lake stadium", "princep ghat",
            "new market kolkata", "flurys", "peter cat", "mocambo", "arsalan", "kookie jar",
            # west bengal districts
            "hooghly", "north 24 parganas", "south 24 parganas", "nadia", "krishnanagar",
            "murshidabad", "berhampore", "bardhaman", "durgapur", "asansol", "siliguri",
            "darjeeling", "kalimpong", "jalpaiguri", "cooch behar", "malda", "kharagpur",
            "medinipur", "haldia", "digha", "mandarmani", "bankura", "purulia", "birbhum",
            "bolpur", "santiniketan", "tarapith", "bishnupur", "diamond harbour", "sundarbans",
        ],
        "authenticity_keywords": [
            'kolkata', 'calcutta', 'bengal', 'bengali', 'bong', 'pujo', 'durga puja',
            'rannaghar', 'adda', 'bangla', 'tollygunge', 'howrah', 'salt lake',
            'new town', 'rabindra', 'ghat', 'hooghly', 'tollywood', 'south kolkata',
            'north kolkata', 'park street', 'gariahat', 'new market', 'jadavpur',
            'city of joy', 'west bengal', 'mishti', 'durga', 'behala', 'ballygunge',
            'bhowanipore', 'kalighat', 'shantiniketan', 'digha', 'darjeeling', 'siliguri',
            'durgapur', 'asansol', 'kharagpur', 'barasat', 'barrackpore', 'dum dum',
            'rajarhat', 'garia', 'sonarpur', 'serampore', 'chandannagar', 'malda',
            'murshidabad', 'nadia', 'bankura', 'purulia', 'sundarban',
        ],
        "search_tokens": ["kolkata", "calcutta", "bengali", "bengal", "bong"],
        "language_hints": ["bengali", "bangla"],
    },
    "punjab": {
        "label": "Punjab & Chandigarh",
        "place_queries": ["amritsar", "ludhiana", "jalandhar", "chandigarh", "patiala",
                          "mohali", "bathinda", "golden temple amritsar", "sector 17 chandigarh",
                          "elante mall chandigarh"],
        "authenticity_keywords": [
            'punjab', 'punjabi', 'amritsar', 'ludhiana', 'jalandhar', 'chandigarh',
            'patiala', 'mohali', 'bathinda', 'pathankot', 'golden temple', 'harmandir',
            'pind', 'sardar', 'sardarni', 'jatt', 'chd', 'tricity', 'malwa', 'doaba',
            'majha', 'wagah', 'anandpur', 'kapurthala', 'moga', 'firozpur',
        ],
        "search_tokens": ["punjab", "punjabi", "chandigarh", "amritsar", "ludhiana"],
        "language_hints": ["punjabi", "gurmukhi"],
    },
    "hyderabad": {
        "label": "Hyderabad & Telangana",
        "place_queries": ["hyderabad", "secunderabad", "charminar", "hitech city hyderabad",
                          "gachibowli", "jubilee hills", "banjara hills", "madhapur",
                          "hussain sagar", "inorbit mall hyderabad"],
        "authenticity_keywords": [
            'hyderabad', 'hyderabadi', 'telangana', 'secunderabad', 'charminar',
            'hitech city', 'hitec city', 'gachibowli', 'jubilee hills', 'banjara hills',
            'madhapur', 'kukatpally', 'begumpet', 'nizam', 'deccan', 'hussain sagar',
            'golconda', 'tollywood telugu', 'telugu', 'warangal', 'nizamabad',
        ],
        "search_tokens": ["hyderabad", "hyderabadi", "telangana", "telugu"],
        "language_hints": ["telugu"],
    },
    "chennai": {
        "label": "Chennai & Tamil Nadu",
        "place_queries": ["chennai", "marina beach chennai", "t nagar chennai",
                          "anna nagar chennai", "adyar chennai", "mylapore",
                          "besant nagar", "velachery", "express avenue chennai",
                          "phoenix marketcity chennai"],
        "authenticity_keywords": [
            'chennai', 'madras', 'tamil', 'tamilnadu', 'tamil nadu', 'marina',
            't nagar', 'tnagar', 'anna nagar', 'adyar', 'mylapore', 'besant nagar',
            'velachery', 'kodambakkam', 'nungambakkam', 'kollywood', 'coimbatore',
            'madurai', 'trichy', 'pondicherry', 'ecr', 'omr',
        ],
        "search_tokens": ["chennai", "madras", "tamil", "kollywood"],
        "language_hints": ["tamil"],
    },
    "mumbai": {
        "label": "Mumbai & Maharashtra",
        "place_queries": ["mumbai", "bandra", "andheri", "juhu beach", "colaba",
                          "marine drive mumbai", "powai", "lower parel", "thane",
                          "phoenix marketcity mumbai"],
        "authenticity_keywords": [
            'mumbai', 'bombay', 'maharashtra', 'marathi', 'bandra', 'andheri', 'juhu',
            'colaba', 'marine drive', 'powai', 'lower parel', 'dadar', 'borivali',
            'thane', 'navi mumbai', 'versova', 'khar', 'worli', 'bollywood', 'aamchi',
        ],
        "search_tokens": ["mumbai", "bombay", "marathi", "maharashtra"],
        "language_hints": ["marathi"],
    },
    "delhi": {
        "label": "Delhi NCR",
        "place_queries": ["delhi", "connaught place", "hauz khas", "saket delhi",
                          "chandni chowk", "gurgaon", "noida", "cyber hub gurgaon",
                          "select citywalk saket", "khan market delhi"],
        "authenticity_keywords": [
            'delhi', 'dilli', 'ncr', 'new delhi', 'gurgaon', 'gurugram', 'noida',
            'connaught', 'cp delhi', 'hauz khas', 'saket', 'chandni chowk',
            'south delhi', 'dwarka', 'rohini', 'lajpat', 'khan market', 'faridabad',
            'ghaziabad', 'dilliwala',
        ],
        "search_tokens": ["delhi", "gurgaon", "noida", "dilli"],
        "language_hints": ["hindi"],
    },
    "bangalore": {
        "label": "Bangalore & Karnataka",
        "place_queries": ["bangalore", "indiranagar", "koramangala", "mg road bangalore",
                          "whitefield bangalore", "hsr layout", "jayanagar",
                          "cubbon park", "phoenix mall of asia bangalore", "electronic city"],
        "authenticity_keywords": [
            'bangalore', 'bengaluru', 'karnataka', 'kannada', 'indiranagar',
            'koramangala', 'whitefield', 'hsr', 'jayanagar', 'mg road', 'cubbon',
            'malleshwaram', 'basavanagudi', 'electronic city', 'namma bengaluru',
            'blr', 'mysore', 'mangalore', 'udupi',
        ],
        "search_tokens": ["bangalore", "bengaluru", "kannada", "karnataka"],
        "language_hints": ["kannada"],
    },
}


# ==============================================================================
# CAMPAIGN / EVENT WINDOWS
# ==============================================================================
# A client asking for "creators who did Durga Pujo last year" wants proof, not a
# bio keyword. A campaign is a set of hashtags plus real place names, bounded by
# a date window - a creator qualifies only if a post of theirs actually falls
# inside it. Every hashtag and location media object carries `taken_at`, so the
# window is enforced against Instagram's own timestamp.
CAMPAIGNS: Dict[str, Dict[str, Any]] = {
    "durga_puja_2025": {
        "label": "Durga Puja 2025",
        "region": "kolkata",
        # Mahalaya 2025-09-21 through a week past Vijaya Dashami 2025-10-02.
        "start": "2025-09-18",
        "end": "2025-10-10",
        "hashtags": [
            "durgapuja2025", "durgapujo2025", "pujo2025", "durgapuja", "durgapujo",
            "pujovibes", "kolkatadurgapuja", "pandalhopping", "sarbojanin",
            "pujoparikrama", "pujoshopping", "durgapujakolkata", "pujofashion",
        ],
        "place_queries": [
            "bagbazar sarbojanin", "kumartuli park", "sreebhumi sporting club",
            "ekdalia evergreen", "suruchi sangha", "mohammad ali park",
            "college square kolkata", "santosh mitra square", "deshapriya park",
            "bosepukur sitala mandir", "chetla agrani", "md ali park",
        ],
    },
    "durga_puja_2026": {
        "label": "Durga Puja 2026",
        "region": "kolkata",
        "start": "2026-10-07",
        "end": "2026-10-28",
        "hashtags": ["durgapuja2026", "durgapujo2026", "pujo2026", "durgapuja",
                     "pandalhopping", "sarbojanin"],
        "place_queries": ["bagbazar sarbojanin", "kumartuli park",
                          "sreebhumi sporting club", "ekdalia evergreen"],
    },
    "diwali_2025": {
        "label": "Diwali 2025",
        "region": None,
        "start": "2025-10-12",
        "end": "2025-10-27",
        "hashtags": ["diwali2025", "diwali", "deepavali2025", "diwalilooks",
                     "diwalidecor", "diwaligifting"],
        "place_queries": [],
    },
}


def campaign_window(campaign: str) -> Tuple[Optional[int], Optional[int]]:
    """Campaign start/end as unix timestamps, for filtering `taken_at`."""
    cfg = CAMPAIGNS.get(campaign)
    if not cfg:
        return None, None
    from datetime import datetime as _dt, timezone as _tz

    def ts(d):
        return int(_dt.strptime(d, "%Y-%m-%d").replace(tzinfo=_tz.utc).timestamp())
    return ts(cfg["start"]), ts(cfg["end"])

# Category -> the vocabulary each source is queried with. Add a category here
# and every source (topsearch grid, hashtags, LLM prompts) picks it up.
CATEGORIES: Dict[str, Dict[str, Any]] = {
    "food": {
        "label": "Food & Culinary",
        "search_terms": ["food", "foodie", "food blogger", "street food", "cafe", "restaurant review"],
        "hashtags": ["kolkatafood", "kolkatastreetfood", "kolkatafoodblogger", "bongfoodie"],
        "bio_keywords": ['food', 'chef', 'recipe', 'biryani', 'eat', 'restaurant',
                         'cafe', 'foodie', 'kitchen', 'rannaghar', 'dessert', 'bake'],
    },
    "fashion": {
        "label": "Fashion & Styling",
        "search_terms": ["fashion", "fashion blogger", "style", "saree", "outfit", "stylist"],
        "hashtags": ["kolkatafashion", "kolkatafashionblogger", "bengalisaree"],
        "bio_keywords": ['fashion', 'saree', 'style', 'outfit', 'model', 'stylist',
                         'wardrobe', 'ootd', 'thrift'],
    },
    "beauty": {
        "label": "Beauty & Skincare",
        "search_terms": ["makeup", "makeup artist", "beauty", "skincare", "mua"],
        "hashtags": ["kolkatamakeupartist", "kolkatabeauty"],
        "bio_keywords": ['beauty', 'makeup', 'skincare', 'mua', 'cosmetic', 'salon', 'hair'],
    },
    "comedy": {
        "label": "Comedy & Entertainment",
        "search_terms": ["comedy", "comedian", "meme", "sketch", "roast"],
        "hashtags": ["kolkatacomedy", "banglacomedy", "bengalimemes"],
        "bio_keywords": ['comedy', 'roast', 'humor', 'humour', 'sketch', 'meme',
                         'comedian', 'entertainer'],
    },
    "travel": {
        "label": "Travel & Culture",
        "search_terms": ["travel", "traveller", "vlogger", "explore", "wanderer"],
        "hashtags": ["kolkatadiaries", "kolkataphotography", "kolkatacity"],
        "bio_keywords": ['travel', 'explore', 'wanderlust', 'vlogger', 'journey', 'trip'],
    },
    "lifestyle": {
        "label": "Urban Lifestyle",
        "search_terms": ["blogger", "influencer", "content creator", "lifestyle"],
        "hashtags": ["kolkatablogger", "bengalicreator"],
        "bio_keywords": ['lifestyle', 'blogger', 'creator', 'influencer'],
    },
    "fitness": {
        "label": "Fitness & Wellness",
        "search_terms": ["fitness", "gym", "trainer", "yoga", "wellness"],
        "hashtags": ["kolkatafitness"],
        "bio_keywords": ['fitness', 'gym', 'trainer', 'yoga', 'wellness', 'nutrition'],
    },
    "dance": {
        "label": "Dance & Performing Arts",
        "search_terms": ["dance", "dancer", "choreographer", "classical dance"],
        "hashtags": ["kolkatadance"],
        "bio_keywords": ['dance', 'dancer', 'choreograph', 'kathak', 'odissi'],
    },
    "cinema": {
        "label": "Tollywood & Cinema",
        "search_terms": ["actor", "actress", "tollywood", "bengali actor"],
        "hashtags": ["tollywood", "bengalicinema"],
        "bio_keywords": ['actor', 'actress', 'tollywood', 'cinema', 'film', 'serial'],
    },
    "festive": {
        "label": "Festive & Durga Puja",
        "search_terms": ["durga puja", "pujo", "pandal", "festive"],
        "hashtags": ["durgapuja2026", "pujo", "durgapujakolkata"],
        "bio_keywords": ['pujo', 'durga puja', 'pandal', 'festive'],
    },
}


def category_of_bio(bio: str, ig_category: str = "") -> str:
    """Maps a bio to a plain-text category label. No emojis."""
    blob = f"{bio} {ig_category}".lower()
    for key, cfg in CATEGORIES.items():
        if key == "lifestyle":
            continue                      # lifestyle is the fallback, checked last
        if any(w in blob for w in cfg["bio_keywords"]):
            return cfg["label"]
    return CATEGORIES["lifestyle"]["label"]


def is_region_authentic(text: str, region: str = "kolkata") -> bool:
    keywords = REGIONS.get(region, REGIONS["kolkata"])["authenticity_keywords"]
    blob = str(text or "").lower()
    return any(k in blob for k in keywords)


# ==============================================================================
# S1 - CHAINING GRAPH (highest precision)
# ==============================================================================
def chaining_candidates(pk: str) -> Dict[str, Any]:
    """
    Instagram's own similar-accounts recommender for one account.
    Returns {"status": ..., "users": [{username, full_name, is_verified}, ...]}.
    Seed it with the WRONG pk and it returns unrelated accounts from another
    country, so always resolve the pk from the live profile first.
    """
    out = {"status": "failed", "users": []}
    if not pk:
        out["status"] = "no_pk"
        return out
    try:
        r = _get(f"https://www.instagram.com/api/v1/discover/chaining/?target_id={pk}",
                 ANDROID_UA)
        if is_throttled(r):
            out["status"] = "throttled"
            return out
        if r.status_code != 200:
            out["status"] = f"http_{r.status_code}"
            return out
        for u in (r.json() or {}).get("users", []) or []:
            h = clean_handle(u.get("username"))
            if h:
                out["users"].append({
                    "username": h,
                    "full_name": u.get("full_name") or "",
                    "is_verified": bool(u.get("is_verified")),
                })
        out["status"] = "ok"
    except Exception as e:
        out["status"] = f"{type(e).__name__}"
    return out


# ==============================================================================
# S3 - TOPSEARCH GRID
# ==============================================================================
def build_search_grid(region: str = "kolkata",
                      categories: Optional[Iterable[str]] = None) -> List[str]:
    """Cross-product of the region's search tokens and each category's terms."""
    cfg = REGIONS.get(region, REGIONS["kolkata"])
    cats = list(categories) if categories else list(CATEGORIES.keys())
    queries = []
    for token in cfg["search_tokens"]:
        for cat in cats:
            for term in CATEGORIES[cat]["search_terms"]:
                queries.append(f"{token} {term}")
    # De-dupe while keeping the ordering stable so runs are reproducible.
    seen, out = set(), []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def topsearch_candidates(query: str) -> Dict[str, Any]:
    """Instagram's blended search. Caps at roughly 5 users per query."""
    out = {"status": "failed", "users": []}
    try:
        from urllib.parse import quote
        r = _get("https://www.instagram.com/web/search/topsearch/"
                 f"?context=blended&query={quote(query)}&count=50")
        if is_throttled(r):
            out["status"] = "throttled"
            return out
        if r.status_code != 200:
            out["status"] = f"http_{r.status_code}"
            return out
        for entry in (r.json() or {}).get("users", []) or []:
            u = entry.get("user", {})
            h = clean_handle(u.get("username"))
            if not h:
                continue
            fn = u.get("full_name") or ""
            out["users"].append({
                "username": h,
                "full_name": fn,
                "is_verified": bool(u.get("is_verified")),
                "retailer_signal": looks_like_retailer(h, fn),
            })
        out["status"] = "ok"
    except Exception as e:
        out["status"] = f"{type(e).__name__}"
    return out


# ==============================================================================
# S4 - GEO SECTIONS (the only physical-presence proof)
# ==============================================================================
def region_place_ids(region: str = "kolkata", per_query: int = 8) -> List[Dict[str, Any]]:
    """Resolves the region's place queries to Instagram location pks."""
    cfg = REGIONS.get(region, REGIONS["kolkata"])
    from urllib.parse import quote
    places, seen = [], set()
    for q in cfg["place_queries"]:
        try:
            r = _get(f"https://www.instagram.com/api/v1/fbsearch/places/?query={quote(q)}")
            if is_throttled(r) or r.status_code != 200:
                time.sleep(2)
                continue
            for item in (r.json() or {}).get("items", [])[:per_query]:
                loc = item.get("location", {})
                pk = str(loc.get("pk") or "")
                if pk and pk not in seen:
                    seen.add(pk)
                    places.append({"pk": pk, "name": loc.get("name") or "",
                                   "city": loc.get("city") or "", "query": q})
        except Exception:
            pass
        time.sleep(2)
    return places


def location_authors(location_pk: str, tab: str = "recent",
                     min_likes: int = 30) -> Dict[str, Any]:
    """
    Authors of posts geotagged at one location. Most accounts posting from a
    city location are ordinary users, so min_likes filters out the civilians.
    """
    out = {"status": "failed", "users": [], "more_available": False}
    try:
        r = _post(f"https://www.instagram.com/api/v1/locations/{location_pk}/sections/",
                  {"tab": tab, "page": "0"})
        if is_throttled(r):
            out["status"] = "throttled"
            return out
        if r.status_code != 200:
            out["status"] = f"http_{r.status_code}"
            return out
        j = r.json() or {}
        out["more_available"] = bool(j.get("more_available"))
        seen = set()
        for section in j.get("sections", []) or []:
            for m in (section.get("layout_content", {}) or {}).get("medias", []) or []:
                media = m.get("media", {})
                u = media.get("user", {})
                h = clean_handle(u.get("username"))
                likes = media.get("like_count") or 0
                if not h or h in seen or likes < min_likes:
                    continue
                seen.add(h)
                out["users"].append({
                    "username": h,
                    "full_name": u.get("full_name") or "",
                    "is_verified": bool(u.get("is_verified")),
                    "geo_proof_location_pk": location_pk,
                    "post_like_count": likes,
                })
        out["status"] = "ok"
    except Exception as e:
        out["status"] = f"{type(e).__name__}"
    return out


def _extract_medias(payload: Dict[str, Any]):
    """Yields (username, full_name, is_verified, taken_at, like_count, code) per media."""
    for section in payload.get("sections", []) or []:
        for m in (section.get("layout_content", {}) or {}).get("medias", []) or []:
            media = m.get("media", {})
            u = media.get("user", {}) or {}
            un = clean_handle(u.get("username"))
            if not un:
                continue
            yield (un, u.get("full_name") or "", bool(u.get("is_verified")),
                   media.get("taken_at"), media.get("like_count") or 0,
                   media.get("code") or "")


def location_sweep(location_pk: str, pages: int = 5, tab: str = "recent",
                   min_likes: int = 0, pause: float = 2.5,
                   since_ts: Optional[int] = None, until_ts: Optional[int] = None,
                   on_progress=None) -> Dict[str, Any]:
    """
    Pages through one location instead of taking only the first page.

    This is the volume fix. One Kolkata place yielded 61 authors on page 0 and
    176 distinct authors by page 2, still reporting more_available - so the old
    single-page read was leaving the great majority of a place behind. Sweep
    many places and a creator turning up at SEVERAL distinct places in a region
    becomes residence evidence rather than a bio keyword guess.
    """
    out = {"status": "failed", "observations": [], "pages_read": 0,
           "distinct_authors": 0, "exhausted": False}
    page, max_id, seen = 0, None, set()
    for _ in range(max(1, pages)):
        data = {"tab": tab, "page": str(page)}
        if max_id:
            data["max_id"] = max_id
        try:
            r = _post(f"https://www.instagram.com/api/v1/locations/{location_pk}/sections/", data)
            if is_throttled(r):
                out["status"] = "throttled"
                return out
            if r.status_code != 200:
                out["status"] = f"http_{r.status_code}"
                return out
            j = r.json() or {}
            for un, fn, ver, ts, likes, code in _extract_medias(j):
                if likes < min_likes:
                    continue
                if since_ts and ts and ts < since_ts:
                    continue
                if until_ts and ts and ts > until_ts:
                    continue
                seen.add(un)
                out["observations"].append({
                    "username": un, "full_name": fn, "is_verified": ver,
                    "taken_at": ts, "like_count": likes, "code": code,
                    "place_pk": str(location_pk),
                })
            out["pages_read"] += 1
            if on_progress:
                on_progress(f"        page {page}: {len(seen)} distinct authors so far")
            if not j.get("more_available"):
                out["exhausted"] = True
                break
            page = j.get("next_page", page + 1)
            max_id = j.get("next_max_id")
            if not max_id:
                out["exhausted"] = True
                break
        except Exception as e:
            out["status"] = f"{type(e).__name__}"
            return out
        time.sleep(pause)
    out["distinct_authors"] = len(seen)
    out["status"] = "ok"
    return out


def hashtag_sweep(tag: str, pages: int = 5, tab: str = "recent", min_likes: int = 0,
                  pause: float = 2.5, since_ts: Optional[int] = None,
                  until_ts: Optional[int] = None, on_progress=None) -> Dict[str, Any]:
    """
    Pages through one hashtag, optionally bounded to a date window.

    With a window this is campaign proof: every media carries Instagram's own
    `taken_at`, so "posted under #durgapuja2025 between 18 Sep and 10 Oct 2025"
    is a verifiable fact about the creator rather than an assertion.
    """
    out = {"status": "failed", "observations": [], "pages_read": 0,
           "distinct_authors": 0, "in_window": 0, "out_of_window": 0, "exhausted": False}
    page, max_id, seen = 0, None, set()
    for _ in range(max(1, pages)):
        data = {"tab": tab, "page": str(page)}
        if max_id:
            data["max_id"] = max_id
        try:
            r = _post(f"https://www.instagram.com/api/v1/tags/{tag}/sections/", data)
            if is_throttled(r):
                out["status"] = "throttled"
                return out
            if r.status_code != 200:
                out["status"] = f"http_{r.status_code}"
                return out
            j = r.json() or {}
            for un, fn, ver, ts, likes, code in _extract_medias(j):
                if likes < min_likes:
                    continue
                if (since_ts or until_ts) and ts:
                    if (since_ts and ts < since_ts) or (until_ts and ts > until_ts):
                        out["out_of_window"] += 1
                        continue
                    out["in_window"] += 1
                seen.add(un)
                out["observations"].append({
                    "username": un, "full_name": fn, "is_verified": ver,
                    "taken_at": ts, "like_count": likes, "code": code, "hashtag": tag,
                })
            out["pages_read"] += 1
            if on_progress:
                on_progress(f"        page {page}: {len(seen)} distinct authors "
                            f"(in window {out['in_window']}, outside {out['out_of_window']})")
            if not j.get("more_available"):
                out["exhausted"] = True
                break
            page = j.get("next_page", page + 1)
            max_id = j.get("next_max_id")
            if not max_id:
                out["exhausted"] = True
                break
        except Exception as e:
            out["status"] = f"{type(e).__name__}"
            return out
        time.sleep(pause)
    out["distinct_authors"] = len(seen)
    out["status"] = "ok"
    return out


# ==============================================================================
# CAMPAIGN PARTICIPATION - verified per creator, not swept from a hashtag
# ==============================================================================
# Measured limitation: the hashtag and location `recent` tabs return NEWEST posts
# first. Reaching a window a year in the past would take thousands of pages - a
# 13-hashtag x 3-page sweep for Durga Puja 2025 produced exactly 2 in-window
# posts, because everything recent under #durgapuja is from 2026.
#
# So a past campaign is verified the other way round: take the creator, read
# their own media, and look for a post inside the window carrying a campaign
# hashtag or campaign location. That is per-creator work, so it belongs in the
# audit phase, and it needs the feed endpoint - which is the first thing
# Instagram throttles. It reports unavailability instead of guessing.
#
# For sweeps, use tab="top": the top tab is ranked by engagement rather than
# recency, so a past festival's best posts are still reachable there.
def verify_campaign_for_creator(pk: str, campaign: str, max_posts: int = 60,
                                pause: float = 1.5) -> Dict[str, Any]:
    """
    Reads a creator's own recent media and returns the posts that fall inside
    the campaign window and match its hashtags or venues.
    """
    out = {"status": "failed", "campaign": campaign, "matches": [],
           "posts_scanned": 0, "reason": ""}
    cfg = CAMPAIGNS.get(campaign)
    if not cfg:
        out["reason"] = f"unknown campaign {campaign}"
        return out
    if not pk:
        out["reason"] = "no pk"
        return out

    since_ts, until_ts = campaign_window(campaign)
    tags = {t.lower().lstrip("#") for t in cfg.get("hashtags", [])}
    venues = [v.lower() for v in cfg.get("place_queries", [])]

    max_id, scanned = None, 0
    while scanned < max_posts:
        url = f"https://i.instagram.com/api/v1/feed/user/{pk}/?count=33"
        if max_id:
            url += f"&max_id={max_id}"
        try:
            r = _get(url, ANDROID_UA, timeout=25)
            if is_throttled(r):
                out["status"] = "throttled"
                out["reason"] = ("session throttled on the feed endpoint - campaign "
                                 "participation cannot be verified right now")
                return out
            if r.status_code != 200:
                out["reason"] = f"feed HTTP {r.status_code}"
                return out
            j = r.json() or {}
        except Exception as e:
            out["reason"] = f"{type(e).__name__}: {str(e)[:70]}"
            return out

        items = j.get("items", []) or []
        if not items:
            break
        oldest = None
        for it in items:
            m = it.get("media", it)
            ts = m.get("taken_at")
            scanned += 1
            oldest = ts or oldest
            if not ts or ts < since_ts or ts > until_ts:
                continue
            caption = ((m.get("caption") or {}).get("text") or "").lower()
            loc = ((m.get("location") or {}).get("name") or "").lower()
            hit_tags = sorted(t for t in tags if f"#{t}" in caption)
            hit_venue = next((v for v in venues if v and v in loc), "")
            if hit_tags or hit_venue:
                out["matches"].append({
                    "code": m.get("code"), "taken_at": ts,
                    "like_count": m.get("like_count") or 0,
                    "hashtags": hit_tags,
                    "location": (m.get("location") or {}).get("name") or "",
                    "evidence_type": "location" if hit_venue and not hit_tags else "hashtag",
                    "evidence_ref": hit_venue or (f"#{hit_tags[0]}" if hit_tags else ""),
                })
        out["posts_scanned"] = scanned
        # Once we are past the start of the window there is nothing older to find.
        if oldest and oldest < since_ts:
            break
        if not j.get("more_available"):
            break
        max_id = j.get("next_max_id")
        if not max_id:
            break
        time.sleep(pause)

    out["status"] = "ok"
    return out


# ==============================================================================
# S8 - YOUTUBE (free, no API key) -> regional creators and their Instagram handles
# ==============================================================================
# YouTube's search results page and channel pages are served with the data
# embedded in the HTML, so no API key and no quota. Two things come out of it
# that Instagram cannot give us: creators whose primary platform is YouTube (and
# who therefore never surface in Instagram's own graph), and a second, verifiable
# reach number per creator.
YT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def _plain_get(url: str, timeout: int = 25):
    session = cffi_requests.Session(impersonate="chrome120")
    return session.get(url, headers={"User-Agent": YT_UA,
                                     "accept-language": "en-US,en;q=0.9"}, timeout=timeout)


def youtube_search_channels(query: str) -> Dict[str, Any]:
    """Channel handles from one YouTube search. Verified: 13 channels per query."""
    from urllib.parse import quote
    out = {"status": "failed", "channels": [], "query": query}
    try:
        r = _plain_get(f"https://www.youtube.com/results?search_query={quote(query)}")
        if r.status_code != 200:
            out["status"] = f"http_{r.status_code}"
            return out
        handles = dict.fromkeys(re.findall(r'"canonicalBaseUrl":"/(@[A-Za-z0-9_.\-]+)"', r.text))
        for h in handles:
            out["channels"].append({"yt_handle": h,
                                    "url": f"https://www.youtube.com/{h}"})
        out["status"] = "ok"
    except Exception as e:
        out["status"] = f"{type(e).__name__}"
    return out


def youtube_channel_links(yt_handle: str) -> Dict[str, Any]:
    """
    Resolves a YouTube channel to its Instagram handle and subscriber count.

    Not every channel exposes its links (some render them only behind JS), so a
    miss is normal and is reported as such rather than guessed at.
    """
    out = {"status": "failed", "yt_handle": yt_handle, "instagram": [],
           "subscribers": None, "subscribers_precision": "rounded", "title": ""}
    h = yt_handle if yt_handle.startswith("@") else f"@{yt_handle}"
    for url in (f"https://www.youtube.com/{h}/about", f"https://www.youtube.com/{h}"):
        try:
            r = _plain_get(url)
            if r.status_code != 200:
                continue
            igs = set(re.findall(r'instagram\.com%2F([A-Za-z0-9_.]{3,30})', r.text))
            igs |= set(re.findall(r'instagram\.com/([A-Za-z0-9_.]{3,30})', r.text))
            cleaned = [x for x in (clean_handle(i) for i in igs) if x]
            t = re.search(r'<title>([^<]{0,80})', r.text)
            if t:
                out["title"] = t.group(1).replace(" - YouTube", "").strip()
            subs = re.search(r'([\d.,]+[KMB]?)\s+subscribers', r.text)
            if subs:
                tok = subs.group(1)
                out["subscribers"] = _yt_count(tok)
                out["subscribers_precision"] = "rounded" if re.search(r'[KMB]', tok) else "exact"
            if cleaned:
                out["instagram"] = sorted(set(cleaned))
                out["status"] = "ok"
                return out
            out["status"] = "no_instagram_link"
        except Exception as e:
            out["status"] = f"{type(e).__name__}"
        time.sleep(1.5)
    return out


def _yt_count(tok: str) -> int:
    s = tok.upper().replace(",", "")
    try:
        if s.endswith("M"):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith("K"):
            return int(float(s[:-1]) * 1_000)
        if s.endswith("B"):
            return int(float(s[:-1]) * 1_000_000_000)
        return int(float(s))
    except Exception:
        return 0


def build_youtube_grid(region: str = "kolkata",
                       categories: Optional[Iterable[str]] = None) -> List[str]:
    """Region x category YouTube search queries, with vlogger/creator suffixes."""
    cfg = REGIONS.get(region, REGIONS["kolkata"])
    cats = list(categories) if categories else list(CATEGORIES.keys())
    suffixes = ["vlogger", "creator", "youtuber"]
    queries = []
    for token in cfg["search_tokens"][:3]:
        for cat in cats:
            term = CATEGORIES[cat]["search_terms"][0]
            for suf in suffixes[:2]:
                queries.append(f"{token} {term} {suf}")
    for lang in cfg.get("language_hints", []):
        queries.append(f"{lang} lifestyle vlogger")
    return list(dict.fromkeys(queries))


# ==============================================================================
# S5 - HASHTAG SECTIONS
# ==============================================================================
def hashtag_authors(tag: str, tab: str = "recent", min_likes: int = 30) -> Dict[str, Any]:
    """Authors posting under one hashtag. Same shape as location_authors."""
    out = {"status": "failed", "users": []}
    try:
        r = _post(f"https://www.instagram.com/api/v1/tags/{tag}/sections/",
                  {"tab": tab, "page": "0"})
        if is_throttled(r):
            out["status"] = "throttled"
            return out
        if r.status_code != 200:
            out["status"] = f"http_{r.status_code}"
            return out
        seen = set()
        for section in (r.json() or {}).get("sections", []) or []:
            for m in (section.get("layout_content", {}) or {}).get("medias", []) or []:
                media = m.get("media", {})
                u = media.get("user", {})
                h = clean_handle(u.get("username"))
                likes = media.get("like_count") or 0
                if not h or h in seen or likes < min_likes:
                    continue
                seen.add(h)
                out["users"].append({
                    "username": h,
                    "full_name": u.get("full_name") or "",
                    "is_verified": bool(u.get("is_verified")),
                    "hashtag": tag,
                    "post_like_count": likes,
                })
        out["status"] = "ok"
    except Exception as e:
        out["status"] = f"{type(e).__name__}"
    return out


# ==============================================================================
# S2 - LLM CITATION HARVEST (quvoid/LLMxCitations)
# ==============================================================================
# LLMxCitations drives ChatGPT / Gemini / Perplexity through a logged-in
# Playwright profile and writes one row per cited URL. The response text sits in
# the FIRST row of each (prompt, platform) group, so parsing has to group rather
# than read every row. Handles are pulled from both the prose and the cited URLs.
#
# Workflow:
#   1. python core/discovery_sources.py prompts kolkata food fashion
#        -> writes llm_discovery_prompts.csv
#   2. copy it to the LLMxCitations checkout as prompts.csv and run:
#        python main.py --save-auth chatgpt          (once, to log in)
#        python main.py --platforms chatgpt,perplexity,gemini \
#                       --input prompts.csv --output output.csv
#   3. python core/discovery_sources.py ingest path/to/output.csv

LLM_PROMPT_TEMPLATES = [
    "Who are the top {n} Instagram {category} creators and influencers based in {region}? "
    "List each one's exact Instagram handle.",
    "List {n} {region} {category} content creators on Instagram with more than 10,000 followers, "
    "with their Instagram usernames.",
    "Which {region} {category} influencers do brands most often partner with on Instagram? "
    "Give their Instagram handles.",
    "Name {n} emerging or up-and-coming {category} creators from {region} on Instagram, "
    "with handles.",
]

IG_URL_RE = re.compile(r'instagram\.com/([A-Za-z0-9_.]{3,30})', re.I)
AT_HANDLE_RE = re.compile(r'@([A-Za-z0-9_.]{3,30})')


def generate_llm_prompts(region: str = "kolkata",
                         categories: Optional[Iterable[str]] = None,
                         n: int = 30,
                         out_path: str = LLM_PROMPTS_FILE) -> str:
    """Writes a prompts.csv for LLMxCitations, one row per template x category."""
    import csv
    label = REGIONS.get(region, REGIONS["kolkata"])["label"]
    cats = list(categories) if categories else list(CATEGORIES.keys())
    rows = []
    for cat in cats:
        cat_label = CATEGORIES[cat]["label"].split(" & ")[0].lower()
        for tpl in LLM_PROMPT_TEMPLATES:
            rows.append(tpl.format(n=n, category=cat_label, region=label))
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["prompt"])
        for r in rows:
            w.writerow([r])
    print(f"[LLM] Wrote {len(rows)} prompts for region '{region}' "
          f"across {len(cats)} categories -> {out_path}")
    return out_path


def ingest_llm_citations(csv_path: str) -> Dict[str, Any]:
    """
    Reads an LLMxCitations output.csv and extracts Instagram handles that the
    models named or cited. Handles are LEADS - they are not verified here and
    carry no follower count.
    """
    import csv
    out = {"status": "failed", "users": [], "prompts_seen": 0, "platforms": set()}
    if not os.path.exists(csv_path):
        out["status"] = "file_not_found"
        return out
    try:
        found: Dict[str, Dict[str, Any]] = {}
        seen_groups = set()
        with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                prompt = (row.get("prompt") or "").strip()
                platform = (row.get("platform") or "").strip()
                out["platforms"].add(platform)
                group = (prompt, platform)

                blobs = []
                # response_content is only populated on a group's first row
                if group not in seen_groups:
                    seen_groups.add(group)
                    out["prompts_seen"] += 1
                    blobs.append(row.get("response_content") or "")
                blobs.append(row.get("url") or "")

                for blob in blobs:
                    for m in IG_URL_RE.finditer(blob):
                        h = clean_handle(m.group(1))
                        if h:
                            rec = found.setdefault(h, {"username": h, "platforms": set(),
                                                       "prompts": set(), "via": set()})
                            rec["platforms"].add(platform)
                            rec["prompts"].add(prompt[:90])
                            rec["via"].add("cited_url")
                    for m in AT_HANDLE_RE.finditer(blob):
                        h = clean_handle(m.group(1))
                        if h:
                            rec = found.setdefault(h, {"username": h, "platforms": set(),
                                                       "prompts": set(), "via": set()})
                            rec["platforms"].add(platform)
                            rec["prompts"].add(prompt[:90])
                            rec["via"].add("response_text")

        for h, rec in found.items():
            out["users"].append({
                "username": h,
                "full_name": "",
                "is_verified": None,
                "llm_platforms": sorted(rec["platforms"]),
                "llm_platform_count": len(rec["platforms"]),
                "llm_prompt_count": len(rec["prompts"]),
                "llm_via": sorted(rec["via"]),
            })
        out["users"].sort(key=lambda x: -x["llm_platform_count"])
        out["platforms"] = sorted(out["platforms"])
        out["status"] = "ok"
    except Exception as e:
        out["status"] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


# ==============================================================================
# S7 - FREE DIRECTORY PAGES
# ==============================================================================
# The influencer-platform vendors publish free, un-gated SEO ranking pages
# ("Top 20 Kolkata Food Influencers on Instagram") that print real @handles.
# Verified reachable and handle-bearing: modash.io, socialveins.com, qoruz.com.
#
# Handles only. Those pages also show follower counts, engagement rate, fake
# follower percentage and audience-city breakdowns, but those are the vendor's
# own estimates - they are NOT live Instagram data and must never be merged into
# the exact-count columns. If they get collected later they belong in separate,
# source-attributed fields.
DIRECTORY_URL_TEMPLATES = [
    "https://www.modash.io/find-influencers/india/{region}/{category}",
    "https://www.modash.io/find-influencers/india/{region}",
    "https://socialveins.com/find-influencers/all-{category}-influencers-in-{region}-on-instagram",
    "https://qoruz.com/find-influencers/top-{category}-bloggers-{region}-instagram",
]


def directory_urls(region: str = "kolkata",
                   categories: Optional[Iterable[str]] = None) -> List[str]:
    cats = list(categories) if categories else list(CATEGORIES.keys())
    urls = []
    for tpl in DIRECTORY_URL_TEMPLATES:
        if "{category}" in tpl:
            for c in cats:
                urls.append(tpl.format(region=region, category=c))
        else:
            urls.append(tpl.format(region=region))
    return list(dict.fromkeys(urls))


def directory_candidates(page, url: str) -> Dict[str, Any]:
    """
    Harvests @handles out of a rendered free directory page.

    Needs a Playwright page because these pages are client-rendered. Extracts
    from the visible text only, so it does not depend on any one vendor's DOM
    structure surviving a redesign.
    """
    out = {"status": "failed", "users": [], "url": url}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        text = page.inner_text("body")
        if not text or len(text) < 400:
            out["status"] = "empty_or_gated"
            return out
        seen = set()
        for m in AT_HANDLE_RE.finditer(text):
            h = clean_handle(m.group(1))
            if h and h not in seen:
                seen.add(h)
                out["users"].append({"username": h, "full_name": "",
                                     "is_verified": None, "directory_url": url})
        out["status"] = "ok"
    except Exception as e:
        out["status"] = f"{type(e).__name__}"
    return out


# ==============================================================================
# S6 - HUB HEALTH CHECK (hub accounts rot; check before crawling)
# ==============================================================================
def seed_health_check(handles: Iterable[str], min_followers: int = 10_000,
                      pause: float = 1.5) -> Dict[str, Any]:
    """
    Resolves each seed and reports whether it is still worth crawling.
    A seed is only useful if it exists, is above the gate, and has chaining.
    """
    from core.profile_auditor import resolve_profile
    report = {"alive": [], "dead": [], "detail": {}, "checked_at": now_iso()}
    for h in handles:
        res = resolve_profile(h, want_rich=True)
        reason = "OK"
        ok = True
        if res["status"] == "404_NOT_FOUND":
            reason, ok = "GONE", False
        elif res["followers_precision"] == "unresolved":
            reason, ok = "UNRESOLVED", False
        elif "delete" in (res.get("name") or "").lower():
            reason, ok = "DELETED ACCOUNT", False
        elif res["followers"] < min_followers:
            reason, ok = f"BELOW GATE ({res['followers']:,})", False
        report["detail"][h] = {
            "pk": res.get("pk"), "followers": res.get("followers"),
            "precision": res.get("followers_precision"),
            "is_verified": res.get("is_verified"), "reason": reason,
        }
        (report["alive"] if ok else report["dead"]).append(h)
        time.sleep(pause)
    return report


# ==============================================================================
# CANDIDATE POOL - provenance and corroboration
# ==============================================================================
# Confidence comes from how many INDEPENDENT source families proposed the same
# handle. Two seeds in the chaining graph both suggesting an account is a much
# stronger signal than one hashtag hit, and a handle that a language model named
# AND that Instagram's own graph corroborates is about as good as a lead gets.
SOURCE_WEIGHTS = {
    "chaining": 3.0,
    "llm": 2.0,
    "directory": 2.0,
    "topsearch": 1.5,
    "geo": 1.5,
    "hashtag": 1.0,
    "hub": 1.0,
    "manual": 3.0,
}


class CandidatePool:
    """Accumulates candidates with provenance and saves incrementally to JSON."""

    def __init__(self, path: str = CANDIDATE_POOL_FILE):
        self.path = path
        self.items: Dict[str, Dict[str, Any]] = {}

    def load(self) -> "CandidatePool":
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Tolerate the old format: a bare list of handles.
                if isinstance(data, list):
                    for h in data:
                        ch = clean_handle(h)
                        if ch:
                            self.items[ch] = {"username": ch, "sources": {},
                                              "first_seen": now_iso()}
                elif isinstance(data, dict):
                    self.items = data.get("candidates", data)
            except Exception:
                pass
        return self

    def add(self, username: str, source: str, detail: Any = None,
            full_name: str = "", is_verified: Optional[bool] = None) -> bool:
        h = clean_handle(username)
        if not h:
            return False
        rec = self.items.setdefault(h, {
            "username": h, "full_name": full_name, "is_verified": is_verified,
            "sources": {}, "first_seen": now_iso(),
        })
        if full_name and not rec.get("full_name"):
            rec["full_name"] = full_name
        if is_verified is not None and rec.get("is_verified") is None:
            rec["is_verified"] = is_verified
        bucket = rec["sources"].setdefault(source, [])
        if detail is not None and detail not in bucket:
            bucket.append(detail)
        return True

    def confidence(self, username: str) -> float:
        rec = self.items.get(username, {})
        score = 0.0
        for src, details in (rec.get("sources") or {}).items():
            w = SOURCE_WEIGHTS.get(src, 1.0)
            # Repeated corroboration inside one family counts, with diminishing returns.
            score += w * (1 + 0.5 * max(0, len(details) - 1))
        if rec.get("is_verified"):
            score += 1.0
        return round(score, 2)

    def provenance_string(self, username: str) -> str:
        rec = self.items.get(username, {})
        parts = []
        for src in sorted((rec.get("sources") or {}).keys()):
            n = len(rec["sources"][src]) or 1
            parts.append(f"{src} x{n}" if n > 1 else src)
        return ", ".join(parts) if parts else "unknown"

    def ranked(self, exclude: Optional[Set[str]] = None) -> List[str]:
        """Handles ordered most-corroborated first, so the audit budget goes furthest."""
        exclude = exclude or set()
        cands = [h for h in self.items if h not in exclude]
        return sorted(cands, key=lambda h: -self.confidence(h))

    def save(self) -> None:
        tmp = f"{self.path}.tmp"
        payload = {"saved_at": now_iso(), "count": len(self.items), "candidates": self.items}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def __len__(self):
        return len(self.items)


# ==============================================================================
# BFS EXPANSION OVER THE CHAINING GRAPH
# ==============================================================================
def chaining_bfs(seed_handles: List[str], pool: CandidatePool, hops: int = 2,
                 max_seeds_per_hop: int = 12, pause: float = 2.5,
                 verified_only_seeds: bool = False,
                 on_progress=None) -> Dict[str, Any]:
    """
    Expands outward from seeds through Instagram's similar-accounts graph.

    Each hop resolves the pk for a seed, pulls its chained accounts into the
    pool with provenance, then uses the most-corroborated new accounts as the
    next hop's seeds. Accounts suggested by several seeds bubble to the top,
    which is exactly the regional cluster we want.
    """
    from core.profile_auditor import fetch_profile_html

    stats = {"hops": [], "throttled": False, "seeds_used": [], "started_at": now_iso()}
    frontier = [h for h in (clean_handle(s) for s in seed_handles) if h]
    visited: Set[str] = set()

    for hop in range(1, hops + 1):
        hop_stat = {"hop": hop, "seeds": 0, "new_candidates": 0, "throttled_seeds": 0}
        next_frontier_source: Set[str] = set()

        for seed in frontier[:max_seeds_per_hop]:
            if seed in visited:
                continue
            visited.add(seed)

            _, meta = fetch_profile_html(seed)
            pk = meta.get("pk")
            if not pk:
                time.sleep(pause)
                continue
            time.sleep(pause)

            chained = chaining_candidates(pk)
            if chained["status"] == "throttled":
                stats["throttled"] = True
                hop_stat["throttled_seeds"] += 1
                if on_progress:
                    on_progress(f"  hop {hop}: @{seed} -> session throttled on chaining")
                time.sleep(pause * 2)
                continue

            hop_stat["seeds"] += 1
            stats["seeds_used"].append(seed)
            added = 0
            for u in chained["users"]:
                if u["username"] in visited:
                    continue
                if pool.add(u["username"], "chaining", detail=seed,
                            full_name=u["full_name"], is_verified=u["is_verified"]):
                    added += 1
                if not verified_only_seeds or u["is_verified"]:
                    next_frontier_source.add(u["username"])
            hop_stat["new_candidates"] += added
            pool.save()                                  # incremental progress
            if on_progress:
                on_progress(f"  hop {hop}: @{seed} -> {len(chained['users'])} chained "
                            f"({added} new, pool={len(pool)})")
            time.sleep(pause)

        stats["hops"].append(hop_stat)
        # Next hop seeds itself from the accounts the graph corroborated most.
        frontier = [h for h in pool.ranked(exclude=visited) if h in next_frontier_source]
        if not frontier:
            break

    stats["finished_at"] = now_iso()
    return stats


# ==============================================================================
# CLI
# ==============================================================================
def _cli():
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        print("Commands:")
        print("  prompts <region> [category ...]   write llm_discovery_prompts.csv")
        print("  ingest <output.csv>              extract handles from LLMxCitations output")
        print("  grid <region> [category ...]     print the topsearch query grid")
        print("  health <handle> [handle ...]     health-check seed accounts")
        print("  places <region>                  resolve region place ids")
        return

    cmd = args[0]
    if cmd == "prompts":
        region = args[1] if len(args) > 1 else "kolkata"
        cats = args[2:] or None
        generate_llm_prompts(region, cats)

    elif cmd == "ingest":
        if len(args) < 2:
            print("usage: ingest <output.csv>")
            return
        res = ingest_llm_citations(args[1])
        print(f"status={res['status']} prompts={res['prompts_seen']} "
              f"platforms={res.get('platforms')}")
        print(f"handles extracted: {len(res['users'])}")
        for u in res["users"][:40]:
            print(f"  @{u['username']:28s} platforms={u['llm_platform_count']} "
                  f"prompts={u['llm_prompt_count']} via={','.join(u['llm_via'])}")

    elif cmd == "grid":
        region = args[1] if len(args) > 1 else "kolkata"
        grid = build_search_grid(region, args[2:] or None)
        print(f"{len(grid)} queries:")
        for q in grid:
            print("  ", q)

    elif cmd == "health":
        rep = seed_health_check(args[1:])
        print(f"ALIVE {len(rep['alive'])} | DEAD {len(rep['dead'])}")
        for h, d in rep["detail"].items():
            print(f"  {h:28s} followers={str(d['followers']):>9s} "
                  f"({d['precision']}) {d['reason']}")

    elif cmd == "places":
        region = args[1] if len(args) > 1 else "kolkata"
        for p in region_place_ids(region):
            print(f"  pk={p['pk']:20s} {p['name'][:50]:50s} query={p['query']}")

    else:
        print(f"unknown command: {cmd}")


if __name__ == "__main__":
    _cli()
