"""
================================================================================
KOLKATA & BENGAL CREATOR DISCOVERY ENGINE v4.0
(MULTI-SOURCE HARVEST + CORROBORATION RANKING + EXACT LIVE AUDIT)
================================================================================
PHASE 0  SEED HEALTH CHECK
         Hub accounts rot. Every seed is resolved before it is crawled, and
         dead / deleted / sub-gate seeds are dropped from the run instead of
         silently wasting the crawl budget.

PHASE 1  MULTI-SOURCE CANDIDATE HARVEST  (all free - see core/discovery_sources)
         S1 chaining graph BFS   Instagram's own similar-accounts recommender,
                                 expanded outward from verified regional seeds
         S2 LLM citations        handles ChatGPT / Perplexity / Gemini name for
                                 "top creators in <region>", harvested by
                                 quvoid/LLMxCitations (optional CSV input)
         S3 topsearch grid       region x category search queries
         S4 geo sections         authors of posts geotagged in the region
         S5 hashtag sections     authors posting under regional hashtags
         S6 hub crawl            legacy community-hub HTML harvest (health-checked)
         S7 directory pages      free un-gated vendor ranking pages (modash,
                                 socialveins, qoruz) - handles only, never their
                                 follower estimates

         Every candidate carries which sources proposed it. A corroboration
         score puts the accounts several independent sources agree on at the
         front of the audit queue.

PHASE 2  LIVE EXACT AUDIT  (core/profile_auditor)
         - EXACT follower count with its source and timestamp recorded.
           A rounded count is never presented as exact; it is labelled rounded,
           and near the 10K gate it is held back as BORDERLINE instead of being
           counted as a pass.
         - 10,000+ follower gate
         - regional authenticity from bio, Instagram's own category, and
           optional geo corroboration
         - structured contact email / phone, verified badge, IG category
         - tier assignment in plain text, no emojis

OUTPUTS  discovery_verified_creators.json    (saved after every creator)
         discovery_candidates_pool.json      (saved after every source)
         kolkata_creators_master_discovery.xlsx
================================================================================
"""

import sys, os, re, json, time
from datetime import datetime, timezone
from typing import Dict, List, Any, Set, Optional

from playwright.sync_api import sync_playwright
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.profile_auditor import (
    PLAYWRIGHT_COOKIES, MIN_FOLLOWERS, WEB_UA,
    resolve_profile, resolve_engagement, format_followers, now_iso,
)
from core.discovery_sources import (
    CandidatePool, REGIONS, CATEGORIES,
    chaining_bfs, build_search_grid, topsearch_candidates,
    region_place_ids, location_authors, hashtag_authors,
    ingest_llm_citations, seed_health_check,
    directory_urls, directory_candidates,
    category_of_bio, is_region_authentic, clean_handle,
)

DISCOVERY_OUTPUT_FILE = os.path.join(BASE_DIR, "discovery_verified_creators.json")
CANDIDATES_CACHE_FILE = os.path.join(BASE_DIR, "discovery_candidates_pool.json")
EXCEL_OUTPUT_FILE = os.path.join(BASE_DIR, "deliverables", "kolkata_creators_master_discovery.xlsx")
RUN_REPORT_FILE = os.path.join(BASE_DIR, "discovery_run_report.json")

# Community hubs used as BFS entry points. Health-checked at the start of every
# run - as of the last check 9 of the original 16 were gone, deleted, or had
# fallen under the gate, so the list is a starting guess, never a fixed truth.
HUB_ACCOUNTS = [
    "kolkatasutra", "calcuttacacophony", "ekolkataa", "stories.of.kolkata",
    "kolkata_chitrography", "kolkatafoodie", "whatkolkataeats",
    "thekolkatabuzz", "kolkatar_golpo", "the_bong_gastronomist",
    "themasala_trails", "unseen.kolkata", "kolkatic",
]

# Regional brands whose grids carry collab-tagged creators. Crawled by S6
# alongside the hubs - a brand that tags a creator has already vetted them.
BRAND_ACCOUNTS = [
    "sencogolddiamond", "pcchandra_jewellers", "flurys1927", "t2telegraph",
]


# ==============================================================================
# LEGACY HUB HTML HARVEST (S6)
# ==============================================================================
def extract_usernames_from_page(page) -> Set[str]:
    """Handles mentioned anywhere in a rendered page's JSON state or profile links."""
    handles = set()
    try:
        content = page.content()
        for m in re.findall(r'"username"\s*:\s*"([a-zA-Z0-9_\.]{3,28})"', content):
            h = clean_handle(m)
            if h:
                handles.add(h)
        for link in page.locator("a").all():
            href = link.get_attribute("href") or ""
            m = re.match(r'^/([a-zA-Z0-9_\.]{3,28})/?$', href)
            if m:
                h = clean_handle(m.group(1))
                if h:
                    handles.add(h)
    except Exception:
        pass
    return handles


def harvest_hubs(page, pool: CandidatePool, hubs: List[str], pause: float = 1.5) -> int:
    added = 0
    for idx, hub in enumerate(hubs, 1):
        print(f"  [{idx}/{len(hubs)}] hub @{hub} ...", end=" ", flush=True)
        try:
            page.goto(f"https://www.instagram.com/{hub}/",
                      wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2200)
            found = extract_usernames_from_page(page)
            new = 0
            for h in found:
                if h != hub and pool.add(h, "hub", detail=hub):
                    new += 1
            added += new
            print(f"{len(found)} handles ({new} new, pool={len(pool)})")
        except Exception as e:
            print(f"skipped ({type(e).__name__})")
        pool.save()
        time.sleep(pause)
    return added


# ==============================================================================
# PHASE 0 + 1
# ==============================================================================
def load_all_existing_handles() -> Set[str]:
    handles = set()
    files = [
        "master_142_creators_list.json",
        "deep_scan_verified_creators.json",
        "verified_new_bengal_network_creators.json",
        "verified_additional_bengal_creators.json",
        "discovery_verified_creators.json",
    ]
    for fn in files:
        fp = next((p for p in (os.path.join(BASE_DIR, fn), os.path.join(BASE_DIR, "data", fn))
                   if os.path.exists(p)), None)
        if not fp:
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = data.get("creators", [])
            for item in data:
                h = clean_handle(item.get("handle", "") if isinstance(item, dict) else item)
                if h:
                    handles.add(h)
        except Exception:
            pass
    return handles


def collect_candidates(page, pool: CandidatePool, region: str,
                       categories: Optional[List[str]],
                       sources: List[str], hops: int,
                       llm_csv: Optional[str],
                       target_pool_size: int) -> Dict[str, Any]:
    """Runs each enabled source, saving the pool after every one."""
    report: Dict[str, Any] = {"region": region, "sources": {}, "started_at": now_iso()}
    print("\n" + "=" * 72)
    print("PHASE 1: MULTI-SOURCE CANDIDATE HARVEST")
    print("=" * 72)

    # ---- S2 LLM citations (cheap, offline, do it first) --------------------
    if "llm" in sources and llm_csv:
        print(f"\n[S2] LLM citation harvest from {llm_csv}")
        res = ingest_llm_citations(llm_csv)
        added = 0
        for u in res["users"]:
            if pool.add(u["username"], "llm",
                        detail=",".join(u["llm_platforms"]) or "unknown"):
                added += 1
        pool.save()
        report["sources"]["llm"] = {"status": res["status"], "handles": len(res["users"]),
                                    "added": added, "platforms": res.get("platforms"),
                                    "prompts_seen": res.get("prompts_seen")}
        print(f"     status={res['status']} handles={len(res['users'])} "
              f"added={added} (pool={len(pool)})")
    elif "llm" in sources:
        print("\n[S2] LLM citation harvest skipped - no CSV supplied "
              "(run: python core/discovery_sources.py prompts kolkata)")
        report["sources"]["llm"] = {"status": "skipped_no_csv"}

    # ---- PHASE 0 seed health, then S1 chaining BFS -------------------------
    if "chaining" in sources:
        print(f"\n[PHASE 0] Health-checking {len(HUB_ACCOUNTS)} seed accounts")
        health = seed_health_check(HUB_ACCOUNTS, min_followers=MIN_FOLLOWERS)
        for h, d in health["detail"].items():
            flag = "OK  " if h in health["alive"] else "DROP"
            fc = f"{d['followers']:,}" if d.get("followers") else "-"
            print(f"  {flag} @{h:26s} {fc:>10s} ({d['precision']}) {d['reason']}")
        print(f"  seeds alive: {len(health['alive'])}/{len(HUB_ACCOUNTS)}")
        report["seed_health"] = health

        seeds = health["alive"]
        if not seeds:
            print("  [WARN] no healthy seeds - skipping chaining BFS")
            report["sources"]["chaining"] = {"status": "no_healthy_seeds"}
        else:
            print(f"\n[S1] Chaining-graph BFS over {len(seeds)} healthy seeds, {hops} hop(s)")
            stats = chaining_bfs(seeds, pool, hops=hops, on_progress=print)
            pool.save()
            report["sources"]["chaining"] = stats
            print(f"     pool after chaining: {len(pool)}")

    # ---- S3 topsearch grid -------------------------------------------------
    if "topsearch" in sources and len(pool) < target_pool_size:
        grid = build_search_grid(region, categories)
        print(f"\n[S3] Topsearch grid: {len(grid)} region x category queries")
        added, ok, throttled = 0, 0, 0
        for i, q in enumerate(grid, 1):
            if len(pool) >= target_pool_size:
                print(f"     pool target reached at query {i}/{len(grid)}")
                break
            res = topsearch_candidates(q)
            if res["status"] == "throttled":
                throttled += 1
                print(f"     [{i}/{len(grid)}] '{q}' -> session throttled, backing off")
                time.sleep(8)
                continue
            if res["status"] == "ok":
                ok += 1
                for u in res["users"]:
                    if pool.add(u["username"], "topsearch", detail=q,
                                full_name=u["full_name"], is_verified=u["is_verified"]):
                        added += 1
                        if u.get("retailer_signal"):
                            pool.items[u["username"]]["retailer_signal"] = True
            if i % 10 == 0:
                pool.save()
                print(f"     [{i}/{len(grid)}] pool={len(pool)}")
            time.sleep(2.0)
        pool.save()
        report["sources"]["topsearch"] = {"queries": len(grid), "ok": ok,
                                          "throttled": throttled, "added": added}
        print(f"     queries ok={ok} throttled={throttled} added={added} (pool={len(pool)})")

    # ---- S4 geo sections ---------------------------------------------------
    if "geo" in sources and len(pool) < target_pool_size:
        print("\n[S4] Geo sections: resolving region place ids")
        places = region_place_ids(region)
        print(f"     {len(places)} places resolved")
        added, geo_verified = 0, {}
        for i, p in enumerate(places[:12], 1):
            if len(pool) >= target_pool_size:
                break
            res = location_authors(p["pk"])
            if res["status"] == "throttled":
                print(f"     [{i}] {p['name'][:40]} -> throttled, backing off")
                time.sleep(8)
                continue
            for u in res["users"]:
                if pool.add(u["username"], "geo", detail=p["name"][:40],
                            full_name=u["full_name"], is_verified=u["is_verified"]):
                    added += 1
                geo_verified[u["username"]] = p["name"][:40]
            print(f"     [{i}] {p['name'][:40]:40s} -> {len(res['users'])} authors "
                  f"(pool={len(pool)})")
            pool.save()
            time.sleep(2.5)
        # Geo presence is an authenticity fact worth keeping on the record.
        for h, place in geo_verified.items():
            if h in pool.items:
                pool.items[h]["geo_verified_at"] = place
        pool.save()
        report["sources"]["geo"] = {"places": len(places), "added": added,
                                    "geo_verified": len(geo_verified)}

    # ---- S5 hashtag sections ----------------------------------------------
    if "hashtag" in sources and len(pool) < target_pool_size:
        cats = categories or list(CATEGORIES.keys())
        tags = []
        for c in cats:
            tags.extend(CATEGORIES[c]["hashtags"])
        tags = list(dict.fromkeys(tags))
        print(f"\n[S5] Hashtag sections: {len(tags)} regional hashtags")
        added = 0
        for i, t in enumerate(tags, 1):
            if len(pool) >= target_pool_size:
                break
            res = hashtag_authors(t)
            if res["status"] == "throttled":
                print(f"     [{i}] #{t} -> throttled, backing off")
                time.sleep(8)
                continue
            for u in res["users"]:
                if pool.add(u["username"], "hashtag", detail=t,
                            full_name=u["full_name"], is_verified=u["is_verified"]):
                    added += 1
            print(f"     [{i}] #{t:28s} -> {len(res['users'])} authors "
                  f"({res['status']}, pool={len(pool)})")
            pool.save()
            time.sleep(2.5)
        report["sources"]["hashtag"] = {"tags": len(tags), "added": added}

    # ---- S7 free directory pages ------------------------------------------
    if "directory" in sources and len(pool) < target_pool_size:
        urls = directory_urls(region, categories)
        print(f"\n[S7] Free directory pages: {len(urls)} vendor ranking pages")
        added, ok = 0, 0
        for i, url in enumerate(urls, 1):
            if len(pool) >= target_pool_size:
                break
            res = directory_candidates(page, url)
            if res["status"] == "ok":
                ok += 1
                for u in res["users"]:
                    if pool.add(u["username"], "directory", detail=url.split("//")[-1][:48]):
                        added += 1
            print(f"     [{i}/{len(urls)}] {res['status']:16s} "
                  f"{len(res['users']):3d} handles  {url.split('//')[-1][:58]}")
            pool.save()
            time.sleep(2.0)
        report["sources"]["directory"] = {"urls": len(urls), "ok": ok, "added": added}
        print(f"     pages ok={ok}/{len(urls)} added={added} (pool={len(pool)})")

    # ---- S6 hub crawl (legacy) --------------------------------------------
    if "hub" in sources and len(pool) < target_pool_size:
        alive_hubs = (report.get("seed_health", {}).get("alive") or HUB_ACCOUNTS)
        crawl_list = alive_hubs + BRAND_ACCOUNTS
        print(f"\n[S6] Legacy HTML harvest over {len(alive_hubs)} healthy hubs "
              f"+ {len(BRAND_ACCOUNTS)} regional brand grids")
        added = harvest_hubs(page, pool, crawl_list)
        report["sources"]["hub"] = {"pages": len(crawl_list), "added": added}

    report["finished_at"] = now_iso()
    report["pool_size"] = len(pool)
    print(f"\n[+] Candidate pool assembled: {len(pool)} handles")
    return report


# ==============================================================================
# PHASE 2: LIVE EXACT AUDIT
# ==============================================================================
def audit_candidate(page, handle: str, pool: Optional[CandidatePool] = None,
                    region: str = "kolkata", with_engagement: bool = False) -> Dict[str, Any]:
    """
    Live audit of one creator. Follower counts come from core.profile_auditor,
    which resolves them exactly and records where the number came from.
    """
    res = resolve_profile(handle, page=page, want_rich=True)

    h = res["raw_handle"]
    prov = pool.items.get(h, {}) if pool else {}
    res["discovery_sources"] = pool.provenance_string(h) if pool else "manual"
    res["discovery_confidence"] = pool.confidence(h) if pool else 0.0
    res["geo_verified_at"] = prov.get("geo_verified_at", "")
    res["retailer_signal"] = bool(prov.get("retailer_signal"))

    # Regional authenticity: bio, name, and Instagram's own category, plus geo
    # corroboration when a location section actually placed them in the region.
    blob = f"{res.get('name','')} {res.get('bio','')} {res.get('ig_category','')} {res.get('city_name','')}"
    res["is_kolkata_bengal"] = is_region_authentic(blob, region) or bool(res["geo_verified_at"])
    res["authenticity_basis"] = (
        "geo-tagged post" if res["geo_verified_at"]
        else ("bio / name keyword" if is_region_authentic(blob, region) else "none")
    )

    res["category"] = category_of_bio(res.get("bio", ""), res.get("ig_category", ""))

    if with_engagement and res.get("pk") and res.get("followers"):
        res["engagement"] = resolve_engagement(res["pk"], res["followers"])

    return res


# ==============================================================================
# EXCEL EXPORT (zero emojis anywhere in the workbook)
# ==============================================================================
# Creator bios are full of emojis and flag characters. They must not reach a
# workbook cell. Lone UTF-16 surrogates are stripped in the same pass because
# openpyxl raises UnicodeEncodeError on save when it meets one (they turn up in
# text that was sliced mid-emoji upstream).
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"      # emoji, pictographs, flags, symbols
    "☀-➿"              # misc symbols and dingbats
    "⬀-⯿"              # arrows and geometric shapes
    "←-⇿"              # arrows
    "-"              # private use
    "️︎"               # variation selectors
    "‍⃣"               # zero-width joiner, keycap
    "]+"
)
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def clean_cell(value):
    """Emoji-free, save-safe value for any workbook cell."""
    if not isinstance(value, str):
        return value
    out = _SURROGATE_RE.sub("", _EMOJI_RE.sub("", value))
    out = re.sub(r"\s{2,}", " ", out).strip(" |").strip()
    return out

def export_kolkata_workbook(creators_list: List[Dict[str, Any]],
                            filename: str = EXCEL_OUTPUT_FILE,
                            run_report: Optional[Dict[str, Any]] = None):
    wb = openpyxl.Workbook()
    ws = wb.active
    # openpyxl caps sheet titles at 31 characters - a longer one makes the file
    # unreadable in some spreadsheet apps.
    ws.title = "Kolkata Verified Creators 10K+"

    headers = [
        "S.No", "Handle", "Creator Name", "Followers", "Followers (Exact)",
        "Count Precision", "Count Source", "Resolved At (UTC)",
        "Tier", "Category", "IG Category", "Verified",
        "Engagement Rate %", "Bio Summary", "Email", "Phone",
        "Profile URL", "Authenticity", "Authenticity Basis",
        "Discovery Sources", "Corroboration Score",
    ]

    header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1A365D", end_color="1A365D", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    border_thin = Border(
        left=Side(style='thin', color='D5D8DC'), right=Side(style='thin', color='D5D8DC'),
        top=Side(style='thin', color='D5D8DC'), bottom=Side(style='thin', color='D5D8DC'),
    )

    for col, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = header_align
        c.border = border_thin

    sorted_creators = sorted(creators_list, key=lambda x: x.get("followers", 0), reverse=True)

    tier_fills = {
        "Mega (1M+)": "FADBD8",
        "Macro (500K-1M)": "FCF3CF",
        "Mid-Tier (100K-500K)": "D5F5E3",
        "Micro (10K-100K)": "E8F8F5",
    }
    # A rounded count is called out visually so nobody reads it as exact.
    precision_fills = {"exact": "E8F8F5", "rounded": "FDEBD0", "unresolved": "F2F3F4"}

    for idx, c in enumerate(sorted_creators, 1):
        eng = c.get("engagement") or {}
        row_vals = [
            idx,
            c.get("handle", ""),
            c.get("name", ""),
            format_followers(c.get("followers", 0)),
            c.get("followers", 0),
            c.get("followers_precision", "unresolved"),
            c.get("followers_source", "none"),
            c.get("resolved_at", ""),
            c.get("tier", ""),
            c.get("category", ""),
            c.get("ig_category", "") or "N/A",
            "Yes" if c.get("is_verified") else ("No" if c.get("is_verified") is False else "Unknown"),
            eng.get("engagement_rate_pct") if eng.get("available") else "Not available",
            (c.get("bio", "") or "")[:200],
            c.get("email", "N/A"),
            c.get("phone", "N/A"),
            c.get("profile_url", ""),
            "Verified Kolkata/Bengal" if c.get("is_kolkata_bengal") else "Indian Emerging",
            c.get("authenticity_basis", ""),
            c.get("discovery_sources", ""),
            c.get("discovery_confidence", 0),
        ]
        for col, val in enumerate(row_vals, 1):
            cell = ws.cell(row=idx + 1, column=col, value=clean_cell(val))
            cell.border = border_thin
            cell.alignment = Alignment(vertical="center")

        tier_str = c.get("tier", "")
        if tier_str in tier_fills:
            ws.cell(row=idx + 1, column=9).fill = PatternFill(
                start_color=tier_fills[tier_str], end_color=tier_fills[tier_str],
                fill_type="solid")
        prec = c.get("followers_precision", "unresolved")
        if prec in precision_fills:
            ws.cell(row=idx + 1, column=6).fill = PatternFill(
                start_color=precision_fills[prec], end_color=precision_fills[prec],
                fill_type="solid")

    col_widths = [6, 22, 26, 12, 15, 15, 26, 20, 22, 22, 18, 10, 16, 45, 28, 16, 38, 24, 20, 30, 14]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C2"

    # ---- Tab 2: Tier Summary ---------------------------------------------
    ws2 = wb.create_sheet("Tier Summary")
    tier_counts: Dict[str, int] = {}
    precision_counts: Dict[str, int] = {}
    for c in sorted_creators:
        tier_counts[c.get("tier", "Unknown")] = tier_counts.get(c.get("tier", "Unknown"), 0) + 1
        p = c.get("followers_precision", "unresolved")
        precision_counts[p] = precision_counts.get(p, 0) + 1

    for col, title in enumerate(["Audience Tier", "Creator Count"], 1):
        cc = ws2.cell(1, col, title)
        cc.font = header_font
        cc.fill = header_fill
    r = 2
    for t, count in sorted(tier_counts.items(), key=lambda x: -x[1]):
        ws2.cell(r, 1, t).border = border_thin
        ws2.cell(r, 2, count).border = border_thin
        r += 1

    r += 1
    for col, title in enumerate(["Follower Count Precision", "Creator Count"], 1):
        cc = ws2.cell(r, col, title)
        cc.font = header_font
        cc.fill = header_fill
    r += 1
    for p, count in sorted(precision_counts.items(), key=lambda x: -x[1]):
        ws2.cell(r, 1, p).border = border_thin
        ws2.cell(r, 2, count).border = border_thin
        r += 1

    ws2.column_dimensions['A'].width = 28
    ws2.column_dimensions['B'].width = 16

    # ---- Tab 3: Discovery Provenance -------------------------------------
    ws3 = wb.create_sheet("Discovery Provenance")
    for col, title in enumerate(["Discovery Source", "Creators Attributed"], 1):
        cc = ws3.cell(1, col, title)
        cc.font = header_font
        cc.fill = header_fill
    src_counts: Dict[str, int] = {}
    for c in sorted_creators:
        for part in (c.get("discovery_sources", "") or "").split(","):
            key = part.strip().split(" x")[0]
            if key:
                src_counts[key] = src_counts.get(key, 0) + 1
    r = 2
    for s, count in sorted(src_counts.items(), key=lambda x: -x[1]):
        ws3.cell(r, 1, s).border = border_thin
        ws3.cell(r, 2, count).border = border_thin
        r += 1

    if run_report:
        r += 1
        cc = ws3.cell(r, 1, "Run Notes")
        cc.font = header_font
        cc.fill = header_fill
        r += 1
        sh = run_report.get("seed_health", {})
        notes = [
            ("Run started (UTC)", run_report.get("started_at", "")),
            ("Candidate pool size", run_report.get("pool_size", 0)),
            ("Seeds alive", len(sh.get("alive", []))),
            ("Seeds dropped as dead or sub-gate", len(sh.get("dead", []))),
            ("Dropped seeds", ", ".join(sh.get("dead", []))),
        ]
        for k, v in notes:
            ws3.cell(r, 1, k).border = border_thin
            ws3.cell(r, 2, v).border = border_thin
            r += 1

    ws3.column_dimensions['A'].width = 36
    ws3.column_dimensions['B'].width = 60

    try:
        wb.save(filename)
    except PermissionError:
        alt = filename.replace(".xlsx", f"_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        wb.save(alt)
        filename = alt
        print(f"[EXCEL] Primary file was locked - saved to {alt}")
    print(f"[EXCEL] Master workbook saved to {filename} "
          f"({len(sorted_creators)} verified creators)")


# ==============================================================================
# ORCHESTRATION
# ==============================================================================
def run_discovery(target_new: int = 35,
                  region: str = "kolkata",
                  categories: Optional[List[str]] = None,
                  sources: Optional[List[str]] = None,
                  hops: int = 2,
                  llm_csv: Optional[str] = None,
                  target_pool_size: int = 400,
                  with_engagement: bool = False,
                  include_below_gate: bool = False):
    """
    target_new     how many NEW creators to add to the verified roster
    categories     subset of core.discovery_sources.CATEGORIES (default: all)
    sources        subset of ["chaining","llm","directory","topsearch","geo",
                              "hashtag","hub"]
    llm_csv        LLMxCitations output.csv to harvest handles from
    """
    sources = sources or ["chaining", "llm", "directory", "topsearch", "geo", "hashtag"]
    print("=" * 72)
    print(f"KOLKATA CREATOR DISCOVERY v4.0  (target: {target_new} new creators)")
    print(f"region={region}  categories={categories or 'all'}  sources={sources}  hops={hops}")
    print("=" * 72)

    existing_handles = load_all_existing_handles()
    print(f"[INFO] Known handles across previous rosters: {len(existing_handles)}")

    verified_creators: List[Dict[str, Any]] = []
    if os.path.exists(DISCOVERY_OUTPUT_FILE):
        try:
            with open(DISCOVERY_OUTPUT_FILE, "r", encoding="utf-8") as f:
                verified_creators = json.load(f)
            print(f"[INFO] Resumed {len(verified_creators)} verified creators.")
        except Exception:
            pass

    audited_handles = {clean_handle(c.get("handle", "")) for c in verified_creators}
    audited_handles.discard(None)

    pool = CandidatePool(CANDIDATES_CACHE_FILE).load()
    print(f"[INFO] Candidate pool restored: {len(pool)} handles")

    run_report: Dict[str, Any] = {}
    skipped: Dict[str, int] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=WEB_UA,
                                      viewport={"width": 1366, "height": 900})
        context.add_cookies(PLAYWRIGHT_COOKIES)
        page = context.new_page()

        run_report = collect_candidates(page, pool, region, categories, sources,
                                       hops, llm_csv, target_pool_size)

        # ---- PHASE 2 -------------------------------------------------------
        print("\n" + "=" * 72)
        print(f"PHASE 2: LIVE EXACT AUDIT ({MIN_FOLLOWERS:,}+ gate)")
        print("=" * 72)

        queue = [h for h in pool.ranked(exclude=audited_handles | existing_handles)]
        print(f"[INFO] Audit queue: {len(queue)} candidates, "
              f"most-corroborated first\n")

        new_verified = 0
        for i, handle in enumerate(queue, 1):
            if new_verified >= target_new:
                print(f"\n[INFO] Target of {target_new} new creators reached.")
                break

            conf = pool.confidence(handle)
            print(f"[{i}/{len(queue)}] @{handle} (score {conf}, "
                  f"{pool.provenance_string(handle)}) ...", end=" ", flush=True)

            res = audit_candidate(page, handle, pool=pool, region=region,
                                  with_engagement=with_engagement)

            keep = res["status"] == "VALID" and res["followers"] >= MIN_FOLLOWERS
            if include_below_gate and res["followers_precision"] != "unresolved":
                keep = True

            if keep:
                print(f"VERIFIED: {res['name']} | {format_followers(res['followers'])} "
                      f"({res['followers_precision']}) | {res['category']}")
                verified_creators.append(res)
                audited_handles.add(handle)
                new_verified += 1
                # Incremental save after every verified creator.
                tmp = f"{DISCOVERY_OUTPUT_FILE}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(verified_creators, f, indent=2, ensure_ascii=False)
                os.replace(tmp, DISCOVERY_OUTPUT_FILE)

                # Every verified regional creator is a good seed for the next run.
                pool.items.setdefault(handle, {})["verified_seed"] = True
                pool.save()
            else:
                reason = res["status"]
                skipped[reason.split(" (")[0]] = skipped.get(reason.split(" (")[0], 0) + 1
                print(f"SKIPPED ({reason})")

            time.sleep(1.2)

        browser.close()

    run_report["audited"] = {"new_verified": new_verified, "skip_reasons": skipped}
    run_report["roster_total"] = len(verified_creators)
    with open(RUN_REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(run_report, f, indent=2, ensure_ascii=False)

    # Pass the path explicitly - a default argument binds at import time, so
    # anything that redirects the module-level path would otherwise be ignored.
    export_kolkata_workbook(verified_creators, filename=EXCEL_OUTPUT_FILE,
                            run_report=run_report)

    print(f"\n[SUCCESS] Added {new_verified} new creators. "
          f"Roster total: {len(verified_creators)}")
    print(f"[SKIPS] {skipped}")
    print(f"[REPORT] {RUN_REPORT_FILE}")
    return verified_creators


def reaudit_roster(region: str = "kolkata", only_missing_provenance: bool = True,
                   with_engagement: bool = False) -> List[Dict[str, Any]]:
    """
    Re-resolves rows already in the roster so that legacy entries pick up an
    exact, provenance-stamped count.

    Rows are never deleted. A row that no longer holds up is annotated -
    region_review_needed for an account with no regional evidence, and the
    real status for one that has fallen under the gate or disappeared - and it
    is left in place for a human to decide on.
    """
    if not os.path.exists(DISCOVERY_OUTPUT_FILE):
        print("[REAUDIT] No roster file to re-audit.")
        return []
    with open(DISCOVERY_OUTPUT_FILE, "r", encoding="utf-8") as f:
        roster = json.load(f)

    pool = CandidatePool(CANDIDATES_CACHE_FILE).load()
    targets = [i for i, c in enumerate(roster)
               if not (only_missing_provenance and c.get("followers_precision"))]
    print("=" * 72)
    print(f"RE-AUDIT: {len(targets)} of {len(roster)} roster rows")
    print("=" * 72)

    flagged = {"below_gate": [], "gone": [], "no_region_evidence": [], "unresolved": []}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=WEB_UA,
                                      viewport={"width": 1366, "height": 900})
        context.add_cookies(PLAYWRIGHT_COOKIES)
        page = context.new_page()

        for n, i in enumerate(targets, 1):
            old = roster[i]
            handle = clean_handle(old.get("handle", ""))
            if not handle:
                continue
            old_followers = old.get("followers", 0)
            print(f"[{n}/{len(targets)}] @{handle} (was {old_followers:,}) ...",
                  end=" ", flush=True)

            res = audit_candidate(page, handle, pool=pool, region=region,
                                  with_engagement=with_engagement)
            # Carry forward anything the old row knew that the new one does not.
            for k, v in old.items():
                res.setdefault(k, v)

            if res["followers_precision"] == "unresolved":
                res["review_flag"] = "unresolved"
                flagged["unresolved"].append(handle)
            elif res["status"] == "404_NOT_FOUND":
                res["review_flag"] = "gone"
                flagged["gone"].append(handle)
            elif res["followers"] < MIN_FOLLOWERS:
                res["review_flag"] = "below_gate"
                flagged["below_gate"].append(handle)
            elif not res["is_kolkata_bengal"]:
                res["review_flag"] = "region_review_needed"
                flagged["no_region_evidence"].append(handle)
            else:
                res["review_flag"] = ""

            delta = res["followers"] - old_followers
            print(f"{res['followers']:,} ({res['followers_precision']}) "
                  f"delta={delta:+,} {res['review_flag'] or 'OK'}")

            roster[i] = res
            tmp = f"{DISCOVERY_OUTPUT_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(roster, f, indent=2, ensure_ascii=False)
            os.replace(tmp, DISCOVERY_OUTPUT_FILE)
            time.sleep(1.2)

        browser.close()

    export_kolkata_workbook(roster, filename=EXCEL_OUTPUT_FILE)
    print("\n[REAUDIT] Flags raised (rows kept, not deleted):")
    for k, v in flagged.items():
        print(f"  {k:22s} {len(v):3d}  {', '.join(v[:10])}{' ...' if len(v) > 10 else ''}")
    return roster


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--reaudit" in argv:
        reaudit_roster(only_missing_provenance="--all" not in argv,
                       with_engagement="--engagement" in argv)
        sys.exit(0)
    count = 35
    cats: Optional[List[str]] = None
    srcs: Optional[List[str]] = None
    llm: Optional[str] = None
    hops_arg = 2
    engagement = False

    for a in argv:
        if a.isdigit():
            count = int(a)
        elif a.startswith("--categories="):
            cats = [c.strip() for c in a.split("=", 1)[1].split(",") if c.strip()]
        elif a.startswith("--sources="):
            srcs = [c.strip() for c in a.split("=", 1)[1].split(",") if c.strip()]
        elif a.startswith("--llm-csv="):
            llm = a.split("=", 1)[1]
        elif a.startswith("--hops="):
            hops_arg = int(a.split("=", 1)[1])
        elif a == "--engagement":
            engagement = True

    run_discovery(target_new=count, categories=cats, sources=srcs,
                  hops=hops_arg, llm_csv=llm, with_engagement=engagement)
