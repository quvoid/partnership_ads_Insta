# -*- coding: utf-8 -*-
"""Every reel on a brand's Reels tab inside a past window (Diwali 2025: 30 Sep - 1 Nov 2025).

Unlike walk_reels_tab() this keeps the brand's OWN reels too (they are the ads), and records
every collaborator (co-authors, tagged accounts, paid-partnership sponsor). The Reels tab is
newest-first, so it pages back from today until a whole page is older than the window.
Resumable: results per brand in diwali2025_reels.json, written after every brand.

  python data/jewellery_adlib/scan_brand_posts.py grtjewels tanishqjewellery ...
"""
import json, os, sys, time
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from core import ig_web
from core.creator_deep_scan import fetch_tokens
from core.brand_collab_scan import _record

IST = timezone(timedelta(hours=5, minutes=30))
_d = lambda v, dflt: datetime.strptime(os.environ[v], "%Y-%m-%d").replace(tzinfo=IST) if os.environ.get(v) else dflt
START = int(_d("WIN_START", datetime(2025, 9, 30, tzinfo=IST)).timestamp())
END = int((_d("WIN_END", datetime(2025, 11, 1, tzinfo=IST)) + timedelta(days=1)).timestamp())   # end date inclusive
OUT = ROOT + "/" + os.environ.get("DIWALI_OUT", "diwali2025_reels.json")
GRID_ONLY = bool(os.environ.get("GRID_ONLY"))
MAX_PAGES = 250


def load():
    try:
        return json.load(open(OUT, encoding="utf-8"))
    except Exception:
        return {}


def save(d):
    json.dump(d, open(OUT + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)


def scan(brand):
    tok = fetch_tokens(brand)
    pk = str(tok["pk"])
    cands, cursor, pages, note = [], None, 0, ""
    while pages < MAX_PAGES:
        media, nxt, err = ig_web.reels_tab_page(brand, pk, tok, cursor)
        pages += 1
        if media is None:
            time.sleep(30)
            media, nxt, err = ig_web.reels_tab_page(brand, pk, tok, cursor)
            if media is None:
                note = f"reels tab refused ({err}) on page {pages} - INCOMPLETE"
                break
        tss = [ig_web.ts_from_pk(m.get("pk") or 0) for m in media]
        for m, ts in zip(media, tss):
            if START - 3600 <= ts < END + 3600:
                cands.append(m)
        if pages % 10 == 0:
            print(f"  {brand}: page {pages}, at {datetime.fromtimestamp(min(tss or [0]), IST):%Y-%m-%d}, {len(cands)} in window", flush=True)
        if not nxt or (tss and max(tss) < START):
            break
        cursor = nxt
        time.sleep(1.5)
    out = []
    for m in cands:
        it = ig_web.post_web_info(m.get("code") or "")
        merged = dict(m)
        if it:
            merged.update(it)
        else:
            merged["taken_at"] = ig_web.ts_from_pk(m.get("pk") or 0)
        if not merged.get("play_count") and m.get("play_count"):
            merged["play_count"] = m["play_count"]
        if not (START <= (merged.get("taken_at") or 0) < END):
            continue
        rec = _record(merged, brand, "reels_tab", merged.get("user") or {})
        rec["detail"] = bool(it)
        rec["caption_full"] = ((merged.get("caption") or {}).get("text") or "") if isinstance(merged.get("caption"), dict) else ""
        out.append(rec)
        time.sleep(1.5)
    print(f"{brand}: {pages} pages, {len(out)} reels in window {note}", flush=True)
    return {"pk": pk, "pages": pages, "note": note, "reels": out}


def scan_grid(brand):
    """Every post on the Posts grid in the window - photos and carousels too (all creatives)."""
    from core.creator_deep_scan import _graphql, POSTS_DOC_ID, CONN_DOC_ID, _RELAY, _parse_conn, _DATA
    tok = fetch_tokens(brand)
    out, cursor, pages, note = [], None, 0, ""
    while pages < MAX_PAGES:
        if cursor is None:
            r = _graphql(brand, "PolarisProfilePostsQuery", POSTS_DOC_ID, {"data": dict(_DATA), "username": brand, **_RELAY}, tok)
        else:
            r = _graphql(brand, "PolarisProfilePostsTabContentQuery_connection", CONN_DOC_ID,
                         {"after": cursor, "before": None, "data": dict(_DATA), "first": 12, "last": None,
                          "username": brand, **_RELAY}, tok)
        pages += 1
        if r.status_code != 200:
            time.sleep(30); note = f"grid HTTP {r.status_code} on page {pages} - INCOMPLETE"; break
        nodes, pi = _parse_conn(r)
        tss = [n.get("taken_at") or 0 for n in nodes if not n.get("timeline_pinned_user_ids")]
        for n in nodes:
            if START <= (n.get("taken_at") or 0) < END:
                rec = _record(n, brand, "posts_grid", n.get("user") or {})
                rec["caption_full"] = ((n.get("caption") or {}).get("text") or "") if isinstance(n.get("caption"), dict) else ""
                out.append(rec)
        if pages % 10 == 0:
            print(f"  {brand} grid: page {pages}, at {datetime.fromtimestamp(min(tss or [0]), IST):%Y-%m-%d}, {len(out)} in window", flush=True)
        cursor = pi.get("end_cursor")
        if not pi.get("has_next_page") or not cursor or (tss and max(tss) < START):
            break
        time.sleep(1.5)
    print(f"{brand} grid: {pages} pages, {len(out)} posts in window {note}", flush=True)
    return {"pages": pages, "note": note, "posts": out}


if __name__ == "__main__":
    d = load()
    for b in sys.argv[1:]:
        try:
            d.setdefault(b, {})
            if not GRID_ONLY and not ("reels" in d[b] and not d[b].get("note")):
                g = (d.get(b) or {}).get("grid")
                d[b] = scan(b)
                if g: d[b]["grid"] = g
                save(d)
            if not (d[b].get("grid") and not d[b]["grid"].get("note")):
                d[b]["grid"] = scan_grid(b)
                save(d)
        except Exception as e:
            print(b, "FAILED", type(e).__name__, e, flush=True)
