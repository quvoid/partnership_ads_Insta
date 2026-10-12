"""
================================================================================
REGIONAL CREATOR ENGINE - ANY REGION, ANY CATEGORY, ANY CAMPAIGN
================================================================================
Fixes the volume ceiling by splitting the work in two, because the old pipeline
paid the expensive per-creator cost on the way to finding candidates at all:

  HARVEST  cheap and wide. Pages through locations and hashtags, walks the
           chaining graph, sweeps YouTube. No per-creator requests. Writes leads
           and evidence straight into creator_intelligence.db. Thousands of leads
           per run instead of a few hundred.

  AUDIT    expensive and narrow. Resolves EXACT follower counts for pending
           leads, most-corroborated first, within a budget you set. Resumable -
           the queue survives a stop, a throttle, or a new day.

  DELIVER  a query over everything ever harvested, exported as a workbook.
           A client brief is a filter, not a new scrape.

WHY THE OLD NUMBERS WERE SMALL
  Kolkata returned 170 creators and each Enamor state barely 90, for three
  reasons this engine removes:
    1. location and hashtag reads took only the FIRST page. One Kolkata place
       gives 61 authors on page 0 and 176 by page 2, still not exhausted.
    2. residence was judged on bio keywords, so every genuine local creator who
       does not type "Kolkata" in their bio was thrown away. Residence is now
       evidence: distinct real places in the region that they posted at.
    3. nothing accumulated between runs, so every client started from zero.

WORKED BRIEFS
  Britannia - Kolkata residents who did Durga Puja last year:
    python core/regional_engine.py harvest --region kolkata --campaign durga_puja_2025 --pages 6
    python core/regional_engine.py audit   --region kolkata --limit 400
    python core/regional_engine.py deliver --region kolkata --campaign durga_puja_2025 \
           --residence 2 --min 10000 --xlsx Britannia_Kolkata_Pujo_Creators.xlsx

  Enamor - four regions:
    for r in punjab hyderabad chennai mumbai; do
      python core/regional_engine.py harvest --region $r --pages 6
      python core/regional_engine.py audit --region $r --limit 400
      python core/regional_engine.py deliver --region $r --residence 2 --min 10000 \
             --xlsx Enamor_${r}_Creators.xlsx
    done

  Visa - a competitor's collaborators, already scanned, now queryable:
    python core/regional_engine.py deliver --brand <competitor> --min 10000 --xlsx Visa_Pool.xlsx
================================================================================
"""

import sys, os, re, json, time, glob
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Set

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from playwright.sync_api import sync_playwright

from core import creator_db as cdb
from core.profile_auditor import (
    PLAYWRIGHT_COOKIES, WEB_UA, MIN_FOLLOWERS,
    resolve_profile, resolve_engagement, format_followers, now_iso,
)
from core.discovery_sources import (
    REGIONS, CATEGORIES, CAMPAIGNS, campaign_window,
    CandidatePool, chaining_bfs, chaining_candidates,
    build_search_grid, topsearch_candidates,
    region_place_ids, location_sweep, hashtag_sweep,
    build_youtube_grid, youtube_search_channels, youtube_channel_links,
    verify_campaign_for_creator,
    ingest_llm_citations, directory_urls, directory_candidates,
    category_of_bio, is_region_authentic, clean_handle,
)

ALL_SOURCES = ["geo", "hashtag", "chaining", "topsearch", "youtube", "llm", "directory"]


def _region_cfg(region: str) -> Dict[str, Any]:
    if region not in REGIONS:
        raise SystemExit(f"unknown region '{region}'. known: {', '.join(REGIONS)}")
    return REGIONS[region]


# ==============================================================================
# HARVEST
# ==============================================================================
def harvest(region: str,
            categories: Optional[List[str]] = None,
            campaign: Optional[str] = None,
            sources: Optional[List[str]] = None,
            pages: int = 5,
            max_places: int = 500,
            chaining_hops: int = 2,
            min_likes: int = 0,
            llm_csv: Optional[str] = None,
            seeds: Optional[List[str]] = None) -> Dict[str, Any]:
    """Wide, cheap discovery. Everything found is written to the database."""
    cfg = _region_cfg(region)
    sources = sources or ALL_SOURCES
    conn = cdb.connect()
    cdb.register_region(conn, region, cfg["label"], cfg)

    report: Dict[str, Any] = {"region": region, "campaign": campaign,
                              "sources": {}, "started_at": now_iso()}
    print("=" * 76)
    print(f"HARVEST  region={region} ({cfg['label']})")
    print(f"         categories={categories or 'all'}  campaign={campaign or 'none'}")
    print(f"         sources={sources}  pages_per_surface={pages}")
    print("=" * 76)

    since_ts = until_ts = None
    if campaign:
        if campaign not in CAMPAIGNS:
            raise SystemExit(f"unknown campaign '{campaign}'. known: {', '.join(CAMPAIGNS)}")
        since_ts, until_ts = campaign_window(campaign)
        print(f"[campaign] {CAMPAIGNS[campaign]['label']}: "
              f"{CAMPAIGNS[campaign]['start']} to {CAMPAIGNS[campaign]['end']}")

    # ---- S4 GEO SWEEP: volume plus residence evidence ----------------------
    if "geo" in sources:
        conn.execute("CREATE TABLE IF NOT EXISTS swept_locations (place_pk TEXT PRIMARY KEY, "
                     "place_name TEXT, region TEXT, pages INTEGER, authors INTEGER, swept_at TEXT)")
        conn.commit()
        already = {r[0] for r in conn.execute(
            "SELECT place_pk FROM swept_locations WHERE region=? AND pages>=?", (region, pages))}
        print(f"\n[S4 GEO] resolving places for {region} "
              f"({len(cfg['place_queries'])} place queries)")
        places = region_place_ids(region)
        todo = [p for p in places if p["pk"] not in already][:max_places]
        print(f"         {len(places)} places resolved, {len(already)} already swept at this "
              f"depth, sweeping {len(todo)}")
        leads = obs = 0
        for i, p in enumerate(todo, 1):
            print(f"  [{i}/{len(todo)}] {p['name'][:46]:46s}", flush=True)
            res = location_sweep(p["pk"], pages=pages, min_likes=min_likes,
                                 on_progress=print)
            if res["status"] != "ok":
                print(f"        {res['status']} - backing off")
                time.sleep(8 if res["status"] == "throttled" else 2)
                continue
            for o in res["observations"]:
                cdb.add_candidate_only(conn, o["username"], "geo",
                                       detail=p["name"][:60], region=region)
                cdb.add_geo_evidence(conn, o["username"], region, p["pk"], p["name"],
                                     o["taken_at"], o["like_count"])
                obs += 1
            conn.execute("INSERT OR REPLACE INTO swept_locations VALUES (?,?,?,?,?,?)",
                         (p["pk"], p["name"][:120], region, res["pages_read"],
                          res["distinct_authors"], now_iso()))
            conn.commit()
            leads += res["distinct_authors"]
            print(f"        {res['distinct_authors']} distinct authors over "
                  f"{res['pages_read']} pages (exhausted={res['exhausted']})")
            time.sleep(2)
        report["sources"]["geo"] = {"places_swept": len(todo), "already_swept": len(already),
                                    "author_hits": leads, "geo_observations": obs}

    # ---- S5 HASHTAG SWEEP: campaign proof when a window is given -----------
    if "hashtag" in sources:
        if campaign:
            tags = CAMPAIGNS[campaign]["hashtags"]
            print(f"\n[S5 HASHTAG] {len(tags)} campaign hashtags, window enforced")
        else:
            cats = categories or list(CATEGORIES.keys())
            tags = list(dict.fromkeys(t for c in cats for t in CATEGORIES[c]["hashtags"]))
            print(f"\n[S5 HASHTAG] {len(tags)} category hashtags")
        # For a PAST campaign the recent tab is nearly useless - it returns this
        # year's posts. The top tab is ranked by engagement, so a past festival's
        # best posts are still reachable. Sweep both and let the counts show it.
        tabs = ["top", "recent"] if campaign else ["recent"]
        in_win = leads = out_win = 0
        for i, t in enumerate(tags, 1):
            for tab in tabs:
                res = hashtag_sweep(t, pages=pages, tab=tab, min_likes=min_likes,
                                    since_ts=since_ts, until_ts=until_ts)
                if res["status"] != "ok":
                    print(f"  [{i}/{len(tags)}] #{t:24s} [{tab}] {res['status']} - backing off")
                    time.sleep(8 if res["status"] == "throttled" else 2)
                    continue
                for o in res["observations"]:
                    cdb.add_candidate_only(conn, o["username"], "hashtag",
                                           detail=f"#{t}", region=region)
                    if campaign:
                        cdb.add_campaign_evidence(conn, o["username"], campaign, "hashtag",
                                                  f"#{t}", o["taken_at"], o["code"],
                                                  o["like_count"])
                        in_win += 1
                conn.commit()
                leads += res["distinct_authors"]
                out_win += res["out_of_window"]
                print(f"  [{i}/{len(tags)}] #{t:24s} [{tab:6s}] "
                      f"{res['distinct_authors']:4d} authors over {res['pages_read']} pages"
                      + (f", {res['in_window']} in window / {res['out_of_window']} outside"
                         if campaign else ""))
                time.sleep(2)
        report["sources"]["hashtag"] = {"tags": len(tags), "tabs": tabs,
                                        "author_hits": leads,
                                        "campaign_evidence": in_win,
                                        "out_of_window": out_win}
        if campaign and in_win == 0:
            print("  [NOTE] no in-window posts from the sweep. For a campaign this far "
                  "back, per-creator verification during audit is the reliable route:")
            print(f"         python core/regional_engine.py audit --region {region} "
                  f"--verify-campaign {campaign}")

    # ---- campaign locations (pandals and the like) -------------------------
    if campaign and CAMPAIGNS[campaign].get("place_queries") and "geo" in sources:
        from core.discovery_sources import _get, ANDROID_UA
        from urllib.parse import quote
        pq = CAMPAIGNS[campaign]["place_queries"]
        print(f"\n[S4 CAMPAIGN GEO] {len(pq)} campaign venues")
        hits = 0
        for i, q in enumerate(pq, 1):
            try:
                r = _get(f"https://www.instagram.com/api/v1/fbsearch/places/?query={quote(q)}")
                if r.status_code != 200:
                    time.sleep(2)
                    continue
                items = (r.json() or {}).get("items", [])[:2]
            except Exception:
                time.sleep(2)
                continue
            time.sleep(2)
            for item in items:
                loc = item.get("location", {})
                pk = str(loc.get("pk") or "")
                if not pk:
                    continue
                res = location_sweep(pk, pages=max(2, pages // 2),
                                     since_ts=since_ts, until_ts=until_ts)
                if res["status"] != "ok":
                    time.sleep(6)
                    continue
                for o in res["observations"]:
                    cdb.add_candidate_only(conn, o["username"], "geo",
                                           detail=loc.get("name", "")[:60], region=region)
                    cdb.add_geo_evidence(conn, o["username"], region, pk,
                                         loc.get("name", ""), o["taken_at"], o["like_count"])
                    cdb.add_campaign_evidence(conn, o["username"], campaign, "location",
                                              loc.get("name", ""), o["taken_at"],
                                              o["code"], o["like_count"])
                    hits += 1
                conn.commit()
                print(f"  [{i}/{len(pq)}] {loc.get('name','')[:44]:44s} "
                      f"{res['distinct_authors']:3d} authors in window")
                time.sleep(2)
        report["sources"]["campaign_geo"] = {"venues": len(pq), "evidence": hits}

    # ---- S1 CHAINING: precision expansion ----------------------------------
    if "chaining" in sources:
        seed_list = seeds or _pick_seeds(conn, region, limit=14)
        if not seed_list:
            print("\n[S1 CHAINING] no verified seeds for this region yet - "
                  "run an audit first, then re-harvest")
            report["sources"]["chaining"] = {"status": "no_seeds"}
        else:
            print(f"\n[S1 CHAINING] BFS from {len(seed_list)} seeds, {chaining_hops} hop(s)")
            print(f"              seeds: {', '.join('@' + s for s in seed_list[:8])}")
            pool = CandidatePool(os.path.join(BASE_DIR,
                                              f"candidates_{region}.json")).load()
            stats_ = chaining_bfs(seed_list, pool, hops=chaining_hops, on_progress=print)
            for h, rec in pool.items.items():
                for detail in (rec.get("sources", {}).get("chaining") or ["seed"]):
                    cdb.add_candidate_only(conn, h, "chaining", detail=str(detail),
                                           region=region)
            conn.commit()
            report["sources"]["chaining"] = stats_

    # ---- S3 TOPSEARCH ------------------------------------------------------
    if "topsearch" in sources:
        grid = build_search_grid(region, categories)
        print(f"\n[S3 TOPSEARCH] {len(grid)} region x category queries")
        added = ok = 0
        for i, q in enumerate(grid, 1):
            res = topsearch_candidates(q)
            if res["status"] == "throttled":
                print(f"  [{i}/{len(grid)}] '{q}' throttled - backing off")
                time.sleep(8)
                continue
            if res["status"] == "ok":
                ok += 1
                for u in res["users"]:
                    cdb.add_candidate_only(conn, u["username"], "topsearch",
                                           detail=q, region=region)
                    added += 1
            if i % 15 == 0:
                conn.commit()
                print(f"  [{i}/{len(grid)}] {added} lead hits so far")
            time.sleep(1.8)
        conn.commit()
        report["sources"]["topsearch"] = {"queries": len(grid), "ok": ok, "hits": added}

    # ---- S8 YOUTUBE --------------------------------------------------------
    if "youtube" in sources:
        grid = build_youtube_grid(region, categories)
        print(f"\n[S8 YOUTUBE] {len(grid)} search queries")
        chans: Dict[str, None] = {}
        for i, q in enumerate(grid, 1):
            res = youtube_search_channels(q)
            if res["status"] == "ok":
                for c in res["channels"]:
                    chans.setdefault(c["yt_handle"], None)
            print(f"  [{i}/{len(grid)}] '{q[:44]:44s}' -> {len(res['channels'])} channels "
                  f"({len(chans)} unique)")
            time.sleep(2)
        print(f"  resolving Instagram links for {len(chans)} channels")
        linked = 0
        for i, ch in enumerate(list(chans)[:80], 1):
            res = youtube_channel_links(ch)
            if res["status"] == "ok" and res["instagram"]:
                for ig in res["instagram"][:2]:
                    cdb.add_candidate_only(conn, ig, "youtube", detail=ch, region=region)
                    cdb.add_cross_platform(conn, ig, "youtube", res["yt_handle"] and
                                           f"https://www.youtube.com/{res['yt_handle']}",
                                           res.get("title", ""), res.get("subscribers"),
                                           res.get("subscribers_precision", "rounded"))
                    linked += 1
                print(f"  [{i}] {ch:28s} -> IG {res['instagram'][:2]} "
                      f"subs={res.get('subscribers')}")
            conn.commit()
            time.sleep(1.5)
        report["sources"]["youtube"] = {"queries": len(grid), "channels": len(chans),
                                        "instagram_links": linked}

    # ---- S2 LLM CITATIONS --------------------------------------------------
    if "llm" in sources and llm_csv:
        print(f"\n[S2 LLM] ingesting {llm_csv}")
        res = ingest_llm_citations(llm_csv)
        for u in res["users"]:
            cdb.add_candidate_only(conn, u["username"], "llm",
                                   detail=",".join(u["llm_platforms"]), region=region)
        conn.commit()
        report["sources"]["llm"] = {"status": res["status"], "handles": len(res["users"])}
        print(f"         {len(res['users'])} handles ({res['status']})")

    # ---- S7 DIRECTORY ------------------------------------------------------
    if "directory" in sources:
        urls = directory_urls(region, categories)
        print(f"\n[S7 DIRECTORY] {len(urls)} vendor ranking pages")
        added = 0
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(user_agent=WEB_UA, viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            for i, url in enumerate(urls, 1):
                res = directory_candidates(page, url)
                if res["status"] == "ok":
                    for u in res["users"]:
                        cdb.add_candidate_only(conn, u["username"], "directory",
                                               detail=url.split("//")[-1][:60], region=region)
                        added += 1
                print(f"  [{i}/{len(urls)}] {res['status']:16s} {len(res['users']):3d} "
                      f"{url.split('//')[-1][:52]}")
                conn.commit()
                time.sleep(1.5)
            b.close()
        report["sources"]["directory"] = {"urls": len(urls), "hits": added}

    report["finished_at"] = now_iso()
    s = cdb.stats(conn)
    report["db_after"] = {k: v for k, v in s.items() if not isinstance(v, dict)}
    _save_report(report, f"harvest_{region}" + (f"_{campaign}" if campaign else ""))

    print("\n" + "=" * 76)
    print("HARVEST COMPLETE")
    print("=" * 76)
    print(f"  creators in db          {s['creators_total']:,}")
    print(f"  pending audit           {s['creators_pending_audit']:,}")
    print(f"  exact counts resolved   {s['creators_exact']:,}")
    print(f"  geo evidence rows       {s['geo_evidence']:,}")
    print(f"  campaign evidence rows  {s['campaign_evidence']:,}")
    print(f"  leads for this region   {s['by_region'].get(region, 0):,}")
    print(f"\n  next: python core/regional_engine.py audit --region {region} --limit 400")
    conn.close()
    return report


def _pick_seeds(conn, region: str, limit: int = 14) -> List[str]:
    """
    Best verified creators in the region so far, as chaining seeds.

    Must require real geo_evidence, not just any observation tagged with the
    region. A seed picked by "any observation" once pulled in Bollywood
    celebrities that a backfill had mislabelled, and the chaining graph then
    faithfully returned more celebrities from around them (fame clusters with
    fame) - 1,751 downstream observations, entirely wrong. Instagram's
    similar-accounts graph is only as good as the seed you hand it.
    """
    rows = conn.execute("""
        SELECT c.handle FROM creators c
        WHERE c.followers_precision='exact' AND c.followers >= ?
          AND (c.review_flag IS NULL OR c.review_flag='')
          AND EXISTS (SELECT 1 FROM geo_evidence g
                        WHERE g.handle=c.handle AND g.region=?)
        ORDER BY c.followers DESC LIMIT ?
    """, (MIN_FOLLOWERS, region, limit)).fetchall()
    return [r["handle"] for r in rows]


def _save_report(report: Dict[str, Any], name: str) -> None:
    path = os.path.join(BASE_DIR, f"{name}_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"[REPORT] {path}")


# ==============================================================================
# AUDIT
# ==============================================================================
def audit_pending(region: Optional[str] = None, limit: int = 300,
                  with_engagement: bool = False, min_sources: int = 1,
                  require_geo: int = 0, pause: float = 1.2,
                  verify_campaign: Optional[str] = None,
                  has_campaign: Optional[str] = None,
                  min_post_likes: int = 0) -> Dict[str, Any]:
    """
    Resolves EXACT counts for pending leads, most-corroborated first.

    Resumable: a lead stays PENDING_AUDIT until it resolves, so stopping or being
    throttled costs nothing. Below-gate and unresolvable leads are recorded as
    such so they are never re-audited pointlessly.
    """
    conn = cdb.connect()
    where = ["c.audit_status = 'PENDING_AUDIT'"]
    params: Dict[str, Any] = {"lim": limit}
    if region:
        params["region"] = region
        where.append("EXISTS (SELECT 1 FROM observations o "
                     "WHERE o.handle=c.handle AND o.region=:region)")
    if require_geo > 0:
        params["rg"] = require_geo
        where.append("(SELECT COUNT(DISTINCT g.place_pk) FROM geo_evidence g "
                     "WHERE g.handle=c.handle) >= :rg")
    if min_sources > 1:
        params["ms"] = min_sources
        where.append("(SELECT COUNT(DISTINCT o.source) FROM observations o "
                     "WHERE o.handle=c.handle) >= :ms")
    if min_post_likes > 0:
        # A free size proxy that costs no requests. Measured on a Kolkata geo
        # sweep: 1,027 of 1,364 harvested handles had a best post under 30 likes -
        # ordinary people who happened to geotag the city. Accounts that clear the
        # 10K gate reliably clear a few hundred likes, so this drops most of the
        # audit budget before a single profile request is spent. Leads with no
        # post evidence at all (chaining, LLM, directory) are not penalised.
        params["mpl"] = min_post_likes
        where.append(
            "((SELECT MAX(g.like_count) FROM geo_evidence g WHERE g.handle=c.handle) >= :mpl"
            " OR (SELECT MAX(e.like_count) FROM campaign_evidence e WHERE e.handle=c.handle) >= :mpl"
            " OR (NOT EXISTS (SELECT 1 FROM geo_evidence g2 WHERE g2.handle=c.handle)"
            "     AND NOT EXISTS (SELECT 1 FROM campaign_evidence e2 WHERE e2.handle=c.handle)))")
    if has_campaign:
        # Spend the audit budget on leads that already carry campaign evidence -
        # for a festive brief those are the only rows that can ever qualify.
        params["hc"] = has_campaign
        where.append("EXISTS (SELECT 1 FROM campaign_evidence e "
                     "WHERE e.handle=c.handle AND e.campaign=:hc)")

    rows = conn.execute(f"""
        SELECT c.handle,
               (SELECT COUNT(DISTINCT o.source) FROM observations o
                  WHERE o.handle=c.handle) AS src,
               (SELECT COUNT(*) FROM observations o
                  WHERE o.handle=c.handle) AS obs,
               (SELECT COUNT(DISTINCT g.place_pk) FROM geo_evidence g
                  WHERE g.handle=c.handle) AS places,
               COALESCE((SELECT MAX(g.like_count) FROM geo_evidence g
                           WHERE g.handle=c.handle), 0) AS best_likes
        FROM creators c
        WHERE {' AND '.join(where)}
        ORDER BY src DESC, places DESC, best_likes DESC, obs DESC
        LIMIT :lim
    """, params).fetchall()

    print("=" * 76)
    print(f"AUDIT  region={region or 'all'}  queue={len(rows)}  "
          f"(ranked by corroboration, then residence evidence)")
    print("=" * 76)

    tally = {"verified": 0, "below_gate": 0, "unresolved": 0, "gone": 0,
             "errors": 0, "campaign_verified": 0}
    # Once the feed endpoint throttles, stop asking it on every creator.
    campaign_unavailable = {"flag": False, "reason": ""}
    if verify_campaign:
        print(f"  campaign verification: {verify_campaign} "
              f"({CAMPAIGNS[verify_campaign]['start']} to {CAMPAIGNS[verify_campaign]['end']})")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=WEB_UA, viewport={"width": 1366, "height": 900})
        ctx.add_cookies(PLAYWRIGHT_COOKIES)
        page = ctx.new_page()

        for i, r in enumerate(rows, 1):
            h = r["handle"]
            print(f"[{i}/{len(rows)}] @{h:26s} src={r['src']} places={r['places']} "
                  f"likes={r['best_likes']} ...", end=" ", flush=True)
            try:
                res = resolve_profile(h, page=page, want_rich=True)
                res["category"] = category_of_bio(res.get("bio", ""), res.get("ig_category", ""))
                if with_engagement and res.get("pk") and res.get("followers"):
                    res["engagement"] = resolve_engagement(res["pk"], res["followers"])

                if res["followers_precision"] == "unresolved":
                    res["status"] = res.get("status") or "UNRESOLVED"
                    tally["gone" if res["status"] == "404_NOT_FOUND" else "unresolved"] += 1
                elif res["followers"] < MIN_FOLLOWERS:
                    tally["below_gate"] += 1
                else:
                    tally["verified"] += 1

                cdb.upsert_creator(conn, res)
                line = (f"{res['followers']:,} ({res['followers_precision']}) "
                        f"{res['tier']} | {res['status']}")

                # Campaign participation is only worth checking for an account
                # that actually cleared the gate.
                if (verify_campaign and res["followers"] >= MIN_FOLLOWERS
                        and res.get("pk") and not campaign_unavailable["flag"]):
                    v = verify_campaign_for_creator(res["pk"], verify_campaign)
                    if v["status"] == "throttled":
                        campaign_unavailable["flag"] = True
                        campaign_unavailable["reason"] = v["reason"]
                        line += " | campaign check unavailable (feed throttled)"
                    elif v["matches"]:
                        for m in v["matches"]:
                            cdb.add_campaign_evidence(
                                conn, h, verify_campaign, m["evidence_type"],
                                m["evidence_ref"], m["taken_at"], m["code"],
                                m["like_count"])
                        conn.commit()
                        tally["campaign_verified"] += 1
                        line += f" | {verify_campaign}: {len(v['matches'])} post(s)"
                    else:
                        line += f" | {verify_campaign}: none in window"
                print(line)
            except Exception as e:
                tally["errors"] += 1
                print(f"ERROR {type(e).__name__}: {str(e)[:50]}")
            time.sleep(pause)
        browser.close()

    s = cdb.stats(conn)
    print("\n" + "=" * 76)
    if campaign_unavailable["flag"]:
        print(f"[WARN] campaign verification stopped early: {campaign_unavailable['reason']}")
        print("       Re-run the audit with --verify-campaign once the throttle clears; "
              "already-resolved creators are skipped.")
    print(f"AUDIT COMPLETE  {tally}")
    print(f"  exact counts in db: {s['creators_exact']:,}   "
          f"above 10K: {s['creators_above_10k']:,}   "
          f"still pending: {s['creators_pending_audit']:,}")
    conn.close()
    return tally


# ==============================================================================
# DELIVER
# ==============================================================================
def deliver(xlsx: Optional[str] = None, json_out: Optional[str] = None,
            brief: str = "", **filters) -> List[Dict[str, Any]]:
    conn = cdb.connect()
    rows = cdb.query_creators(conn, **filters)
    print(f"filters: {json.dumps(filters, ensure_ascii=False)}")
    print(f"matches: {len(rows)}\n")
    cdb._print_rows(rows)
    if json_out:
        json.dump(cdb.rows_to_dicts(rows), open(json_out, "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)
        print(f"\n[JSON] {json_out}")
    if xlsx:
        path = cdb.export_rows_xlsx(rows, xlsx,
                                    brief=brief or json.dumps(filters, ensure_ascii=False))
        print(f"[EXCEL] {path}")
    out = cdb.rows_to_dicts(rows)
    conn.close()
    return out


# ==============================================================================
# BACKFILL - pull every roster already produced into the database
# ==============================================================================
HANDLE_COL_HINTS = ["username", "handle", "instagram", "creator", "profile", "account"]

# A backfilled file only gets a region tag when its name says so. Tagging every
# old workbook with the default region once labelled 1,800 celebrity ambassadors
# from jewellery and electronics clients as Kolkata creators.
REGION_FILE_HINTS = {
    "kolkata": re.compile(r'kolkata|bengal|pujo|durga|bong', re.I),
    "punjab": re.compile(r'punjab|chandigarh|amritsar|ludhiana', re.I),
    "hyderabad": re.compile(r'hyderabad|telangana', re.I),
    "chennai": re.compile(r'chennai|madras|tamil', re.I),
    "mumbai": re.compile(r'mumbai|bombay|maharashtra', re.I),
    "delhi": re.compile(r'delhi|gurgaon|gurugram|noida|ncr', re.I),
    "bangalore": re.compile(r'bangalore|bengaluru|karnataka', re.I),
}


def _region_for_file(fn: str, default: str) -> str:
    for reg, rx in REGION_FILE_HINTS.items():
        if rx.search(fn):
            return reg
    return ""


def backfill(region_default: str = "kolkata", include_xlsx: bool = True,
             audit_after: bool = False) -> Dict[str, Any]:
    """
    Loads the existing JSON rosters and Excel masters into the database as leads,
    so nothing already paid for is lost. Counts from those files are treated as
    UNVERIFIED - a backfilled row stays PENDING_AUDIT until it is re-resolved
    exactly, because the old numbers came from the rounded og:description path.
    """
    conn = cdb.connect()
    for r, cfg in REGIONS.items():
        cdb.register_region(conn, r, cfg["label"], cfg)

    report = {"json_files": {}, "xlsx_files": {}, "started_at": now_iso()}

    json_targets = [
        ("discovery_verified_creators.json", region_default),
        ("master_142_creators_list.json", region_default),
        ("deep_scan_verified_creators.json", region_default),
        ("verified_new_bengal_network_creators.json", region_default),
        ("verified_additional_bengal_creators.json", region_default),
        ("new_kolkata_creators_discovered.json", region_default),
        ("raw_discovered_network_handles.json", region_default),
        ("discovery_candidates_pool.json", region_default),
    ]
    print("=" * 76)
    print("BACKFILL - existing rosters into creator_intelligence.db")
    print("=" * 76)
    # Rosters may sit at the root (local working files) or under data/ (tracked).
    def _find(fn: str) -> Optional[str]:
        for d in (BASE_DIR, os.path.join(BASE_DIR, "data")):
            fp = os.path.join(d, fn)
            if os.path.exists(fp):
                return fp
        return None

    for fn, reg in json_targets:
        fp = _find(fn)
        if not fp:
            continue
        n = 0
        try:
            data = json.load(open(fp, encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("candidates", data.get("creators", list(data.keys())))
            if isinstance(data, dict):
                data = list(data.keys())
            for item in data:
                h = clean_handle(item.get("handle") or item.get("username")
                                 if isinstance(item, dict) else item)
                if not h:
                    continue
                # A row that already carries provenance was resolved by the exact
                # ladder - keep it as a real creator instead of demoting it to a lead.
                file_reg = _region_for_file(fn, reg)
                if isinstance(item, dict) and item.get("followers_precision") in ("exact", "rounded"):
                    item.setdefault("raw_handle", h)
                    cdb.upsert_creator(conn, item)
                    cdb.add_observation(conn, h, "manual", fn, file_reg)
                else:
                    cdb.add_candidate_only(conn, h, "manual", detail=fn, region=file_reg)
                n += 1
            conn.commit()
        except Exception as e:
            report["json_files"][fn] = f"error: {type(e).__name__}"
            print(f"  {fn:52s} ERROR {type(e).__name__}")
            continue
        report["json_files"][fn] = n
        print(f"  {fn:52s} {n:5d} handles")

    if include_xlsx:
        import openpyxl
        xlsx_paths = (glob.glob(os.path.join(BASE_DIR, "*.xlsx"))
                      + glob.glob(os.path.join(BASE_DIR, "deliverables", "*.xlsx")))
        for fp in sorted(xlsx_paths):
            fn = os.path.basename(fp)
            if fn.startswith("~$"):
                continue
            try:
                wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
            except Exception:
                continue
            found = 0
            for ws in wb.worksheets:
                try:
                    header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
                except (StopIteration, Exception):
                    continue
                if not header:
                    continue
                idx = None
                for i, hv in enumerate(header):
                    if isinstance(hv, str) and any(k in hv.lower() for k in HANDLE_COL_HINTS):
                        idx = i
                        break
                if idx is None:
                    continue
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if idx >= len(row):
                        continue
                    h = clean_handle(row[idx])
                    if h:
                        cdb.add_candidate_only(conn, h, "manual", detail=fn,
                                               region=_region_for_file(fn, region_default))
                        found += 1
            wb.close()
            if found:
                conn.commit()
                report["xlsx_files"][fn] = found
                print(f"  {fn[:52]:52s} {found:5d} handles")

    s = cdb.stats(conn)
    report["db_after"] = {k: v for k, v in s.items() if not isinstance(v, dict)}
    report["finished_at"] = now_iso()
    _save_report(report, "backfill")
    print(f"\n  creators in db: {s['creators_total']:,}  "
          f"pending audit: {s['creators_pending_audit']:,}")
    print(f"  next: python core/regional_engine.py audit --limit 500")
    conn.close()
    if audit_after:
        audit_pending(limit=500)
    return report


# ==============================================================================
# CLI
# ==============================================================================
def _parse(args: List[str]) -> Dict[str, Any]:
    o: Dict[str, Any] = {}
    i = 0
    while i < len(args):
        a, nxt = args[i], (args[i + 1] if i + 1 < len(args) else None)
        if a == "--region":
            o["region"] = nxt; i += 2
        elif a == "--categories":
            o["categories"] = [x.strip() for x in nxt.split(",") if x.strip()]; i += 2
        elif a == "--campaign":
            o["campaign"] = nxt; i += 2
        elif a == "--sources":
            o["sources"] = [x.strip() for x in nxt.split(",") if x.strip()]; i += 2
        elif a == "--pages":
            o["pages"] = int(nxt); i += 2
        elif a == "--places":
            o["max_places"] = int(nxt); i += 2
        elif a == "--hops":
            o["chaining_hops"] = int(nxt); i += 2
        elif a == "--min-likes":
            o["min_likes"] = int(nxt); i += 2
        elif a == "--llm-csv":
            o["llm_csv"] = nxt; i += 2
        elif a == "--seeds":
            o["seeds"] = [x.strip().lstrip("@") for x in nxt.split(",") if x.strip()]; i += 2
        elif a == "--limit":
            o["limit"] = int(nxt); i += 2
        elif a == "--min":
            o["min_followers"] = int(nxt); i += 2
        elif a == "--max":
            o["max_followers"] = int(nxt); i += 2
        elif a == "--category":
            o["category"] = nxt; i += 2
        elif a == "--tier":
            o["tier"] = nxt; i += 2
        elif a == "--brand":
            o["brand"] = nxt; i += 2
        elif a == "--exclude-brand":
            o["exclude_brand"] = nxt; i += 2
        elif a == "--residence":
            o["min_distinct_places"] = int(nxt); i += 2
        elif a == "--sources-min":
            o["min_sources"] = int(nxt); i += 2
        elif a == "--platform":
            o["platform"] = nxt; i += 2
        elif a == "--min-engagement":
            o["min_engagement"] = float(nxt); i += 2
        elif a == "--xlsx":
            o["xlsx"] = nxt; i += 2
        elif a == "--json":
            o["json_out"] = nxt; i += 2
        elif a == "--brief":
            o["brief"] = nxt; i += 2
        elif a == "--days":
            o["days"] = int(nxt); i += 2
        elif a == "--pause":
            o["pause"] = float(nxt); i += 2
        elif a == "--require-geo":
            o["require_geo"] = int(nxt); i += 2
        elif a == "--verify-campaign":
            o["verify_campaign"] = nxt; i += 2
        elif a == "--has-campaign":
            o["has_campaign"] = nxt; i += 2
        elif a == "--min-post-likes":
            o["min_post_likes"] = int(nxt); i += 2
        elif a == "--has-email":
            o["has_email"] = True; i += 1
        elif a == "--verified":
            o["verified_only"] = True; i += 1
        elif a == "--allow-rounded":
            o["require_exact"] = False; i += 1
        elif a == "--engagement":
            o["with_engagement"] = True; i += 1
        elif a == "--no-xlsx-backfill":
            o["include_xlsx"] = False; i += 1
        else:
            raise SystemExit(f"unknown option: {a}")
    return o


def _cli():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        print("Commands:")
        print("  harvest --region R [--campaign C] [--categories a,b] [--sources ...]")
        print("          [--pages N] [--places N] [--hops N] [--llm-csv PATH] [--seeds a,b]")
        print("  audit   [--region R] [--limit N] [--sources-min N] [--require-geo N]")
        print("          [--engagement] [--verify-campaign C] [--has-campaign C]")
        print("          [--min-post-likes N]  free size pre-filter, drops ~75% of the queue")
        print("  deliver [--region R] [--campaign C] [--residence N] [--min N] [--max N]")
        print("          [--category C] [--brand B] [--has-email] [--verified] [--xlsx OUT]")
        print("  backfill [--region R] [--no-xlsx-backfill]")
        print("  deepscan [--region R] [--limit N] [--days 90]   90-day partnerships, metrics, category, email")
        print("  deepexport --region R --xlsx OUT.xlsx")
        print(f"\nRegions: {', '.join(REGIONS)}")
        print(f"Campaigns: {', '.join(CAMPAIGNS)}")
        print(f"Categories: {', '.join(CATEGORIES)}")
        return

    cmd, opts = args[0], _parse(args[1:])

    if cmd == "harvest":
        if "region" not in opts:
            raise SystemExit("harvest needs --region")
        harvest(**opts)
    elif cmd == "audit":
        audit_pending(region=opts.get("region"), limit=opts.get("limit", 300),
                      with_engagement=opts.get("with_engagement", False),
                      min_sources=opts.get("min_sources", 1),
                      require_geo=opts.get("require_geo", 0),
                      verify_campaign=opts.get("verify_campaign"),
                      has_campaign=opts.get("has_campaign"),
                      min_post_likes=opts.get("min_post_likes", 0))
    elif cmd == "deliver":
        opts.pop("categories", None)
        opts.pop("sources", None)
        deliver(**opts)
    elif cmd == "backfill":
        backfill(region_default=opts.get("region", "kolkata"),
                 include_xlsx=opts.get("include_xlsx", True))
    elif cmd == "deepscan":
        from core.creator_deep_scan import run as deep_run
        deep_run(opts.get("region"), limit=opts.get("limit", 200),
                 days=opts.get("days", 90), pause=opts.get("pause", 1.5))
    elif cmd == "deepexport":
        from core.creator_deep_scan import export as deep_export
        deep_export(opts.get("region"), opts.get("xlsx", "Creator_Deep_Scan.xlsx"))
    else:
        raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    _cli()
