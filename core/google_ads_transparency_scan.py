# -*- coding: utf-8 -*-
"""
================================================================================
GOOGLE ADS TRANSPARENCY CENTER SCAN - every creative an advertiser has shown
================================================================================
Opens https://adstransparency.google.com/advertiser/<AR id>?region=IN in headless
Chromium, scrolls to load every creative, and captures the site's own
SearchCreatives responses (the data the page renders from). Per creative:
creative id, format (Text / Image / Video), first shown, last shown, preview
link and, for image ads, the image URL. Also resolves "which advertiser" by
name and by website domain so a mall with no verified advertiser is reported
as such rather than guessed.

  python core/google_ads_transparency_scan.py --advertisers adv.json --xlsx OUT.xlsx
  adv.json: {"phoenix_millennium": {"advertiser_id": "AR...", "name": "..."}, "seasons_mall": {"domain": "seasonsmall.in"}}
================================================================================
"""
import sys, os, re, json, time
from datetime import datetime, timezone
from typing import Dict, Any, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
FORMAT = {1: "Text", 2: "Image", 3: "Video"}


def _parse_creatives(payload: Dict[str, Any], out: Dict[str, Dict[str, Any]]):
    for c in payload.get("1") or []:
        cid = c.get("2")
        if not cid or cid in out:
            continue
        prev = c.get("3") or {}
        img = ""
        if "3" in prev and isinstance(prev["3"], dict):
            m = re.search(r'src="([^"]+)"', prev["3"].get("2", "") or "")
            img = m.group(1) if m else ""
        first = (c.get("6") or {}).get("1"); last = (c.get("7") or {}).get("1")
        out[cid] = {
            "creative_id": cid, "advertiser_id": c.get("1"), "advertiser": c.get("12", ""),
            "format": FORMAT.get(c.get("4"), str(c.get("4"))),
            "first_shown": datetime.fromtimestamp(int(first), tz=timezone.utc).strftime("%Y-%m-%d") if first else "",
            "last_shown": datetime.fromtimestamp(int(last), tz=timezone.utc).strftime("%Y-%m-%d") if last else "",
            "image_url": img,
            "detail_url": f"https://adstransparency.google.com/advertiser/{c.get('1')}/creative/{cid}?region=IN",
        }


def _rpc(session, body: Dict[str, Any]):
    import urllib.parse
    return session.post("https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives?authuser=",
                        headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                                 "origin": "https://adstransparency.google.com", "referer": "https://adstransparency.google.com/",
                                 "x-same-domain": "1"},
                        data="f.req=" + urllib.parse.quote(json.dumps(body)), timeout=30)


def scan_advertiser(page, adv_id: str, log=print, max_pages: int = 80) -> Dict[str, Any]:
    """Pages the site's own SearchCreatives RPC (40 per page, continuation token)."""
    from curl_cffi import requests as cffi
    S = cffi.Session(impersonate="chrome120")
    found: Dict[str, Dict[str, Any]] = {}
    tok = None
    for _ in range(max_pages):
        body = {"2": 40, "3": {"13": {"1": [adv_id]}}, "7": {"1": 1, "2": 40, "3": 2356}}
        if tok:
            body["4"] = tok
        r = _rpc(S, body)
        if r.status_code != 200:
            log(f"    rpc HTTP {r.status_code}; stopping"); break
        j = r.json()
        before = len(found)
        _parse_creatives(j, found)
        tok = j.get("2")
        if not tok or len(found) == before:
            break
        time.sleep(0.5)
    legal = based = count_text = ""
    if page is not None:
        try:
            page.goto(f"https://adstransparency.google.com/advertiser/{adv_id}?region=IN&hl=en", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            header = page.evaluate("document.body.innerText.slice(0,1500)")
            count_text = (re.search(r"~?([\d,]+)\s+ads", header) or [None, ""])[1]
            legal = (re.search(r"Legal name:\s*(.+)", header) or [None, ""])[1].strip()
            based = (re.search(r"Based in:\s*(.+)", header) or [None, ""])[1].strip()
        except Exception:
            pass
    if not legal and found:
        legal = next(iter(found.values())).get("advertiser", "")
    log(f"    {adv_id}: site says ~{count_text or '?'} ads, captured {len(found)} creatives ({legal or ''})")
    return {"advertiser_id": adv_id, "legal_name": legal, "based_in": based, "count_text": count_text,
            "creatives": list(found.values()), "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def check_domain(page, domain: str, log=print) -> Dict[str, Any]:
    page.goto(f"https://adstransparency.google.com/?region=IN&hl=en&domain={domain}", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    txt = page.evaluate("document.body.innerText.slice(0,2000)")
    m = re.search(r"~?([\d,]+)\s+ads", txt)
    advs = re.findall(r"\n([^\n]{3,80})\nVerified\n", txt)
    log(f"    domain {domain}: {'~' + m.group(1) + ' ads' if m else 'no ads found'}; advertisers on page: {advs[:5]}")
    return {"domain": domain, "count_text": m.group(1) if m else "", "advertisers": advs[:10]}


def run(spec: Dict[str, Dict[str, str]], log=print) -> Dict[str, Any]:
    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 1000}, locale="en-GB")
        pg = ctx.new_page()
        for key, meta in spec.items():
            log(f"[{key}]")
            entry = {"name": meta.get("name", key), "advertisers": [], "domains": []}
            for adv in meta.get("advertiser_ids", []) or ([meta["advertiser_id"]] if meta.get("advertiser_id") else []):
                try:
                    entry["advertisers"].append(scan_advertiser(pg, adv, log))
                except Exception as e:
                    log(f"    ERROR {type(e).__name__}: {str(e)[:120]}")
                time.sleep(2)
            for dom in meta.get("domains", []):
                try:
                    entry["domains"].append(check_domain(pg, dom, log))
                except Exception as e:
                    log(f"    ERROR {type(e).__name__}: {str(e)[:120]}")
            if not entry["advertisers"] and not any(d["count_text"] for d in entry["domains"]):
                entry["note"] = meta.get("note", "No verified Google advertiser found for this account under its name or website.")
            out[key] = entry
        browser.close()
    return out


def export(results: Dict[str, Any], xlsx: str):
    import openpyxl
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter
    from core.kolkata_engine import clean_cell
    bold = Font(bold=True)
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Summary"
    ws.cell(1, 1, "Google Ads Transparency Center (India), captured " + datetime.now().strftime("%d %b %Y")).font = bold
    hdr = ["Account", "Google Advertiser (legal name)", "Advertiser ID", "Ads Shown By Google", "Creatives Captured",
           "Text", "Image", "Video", "Earliest First-Shown", "Latest Last-Shown", "Websites Checked", "Note"]
    for c, h in enumerate(hdr, 1):
        ws.cell(3, c, h).font = bold
    r = 4
    for key, e in results.items():
        if not e["advertisers"]:
            vals = [f"@{key}", "", "", "", 0, 0, 0, 0, "", "",
                    "; ".join(f"{d['domain']} ({'~' + d['count_text'] + ' ads' if d['count_text'] else 'none'})" for d in e["domains"]), e.get("note", "")]
            for c, v in enumerate(vals, 1):
                ws.cell(r, c, clean_cell(v))
            r += 1
        for a in e["advertisers"]:
            cs = a["creatives"]
            vals = [f"@{key}", a["legal_name"], a["advertiser_id"], f"~{a['count_text']}" if a["count_text"] else "", len(cs),
                    sum(1 for c in cs if c["format"] == "Text"), sum(1 for c in cs if c["format"] == "Image"), sum(1 for c in cs if c["format"] == "Video"),
                    min((c["first_shown"] for c in cs if c["first_shown"]), default=""), max((c["last_shown"] for c in cs if c["last_shown"]), default=""),
                    "; ".join(f"{d['domain']} ({'~' + d['count_text'] + ' ads' if d['count_text'] else 'none'})" for d in e["domains"]), e.get("note", "")]
            for c, v in enumerate(vals, 1):
                ws.cell(r, c, clean_cell(v))
            r += 1
    for i, w in enumerate([24, 44, 26, 14, 12, 8, 8, 8, 16, 16, 40, 60], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for key, e in results.items():
        for a in e["advertisers"]:
            t = wb.create_sheet(re.sub(r'[\[\]\*\?/\\:]', '', f"{key}")[:31] if len(e["advertisers"]) == 1 else re.sub(r'[\[\]\*\?/\\:]', '', f"{key} {a['advertiser_id'][-6:]}")[:31])
            t.cell(1, 1, f"@{key} - {a['legal_name']} ({a['advertiser_id']}) - Google Ads Transparency Center, India").font = bold
            cols = ["Creative ID", "Format", "First Shown", "Last Shown", "Image URL", "Creative Page"]
            for c, h in enumerate(cols, 1):
                t.cell(3, c, h).font = bold
            cs = sorted(a["creatives"], key=lambda c: (c["last_shown"], c["first_shown"]), reverse=True)
            for i, c in enumerate(cs, 4):
                for j, v in enumerate([c["creative_id"], c["format"], c["first_shown"], c["last_shown"], c["image_url"], c["detail_url"]], 1):
                    t.cell(i, j, clean_cell(v)).alignment = Alignment(vertical="top")
            for i, w in enumerate([26, 8, 12, 12, 70, 90], 1):
                t.column_dimensions[get_column_letter(i)].width = w

    m = wb.create_sheet("Method")
    notes = [("Source", "Google Ads Transparency Center, region India. Public data, no login."),
             ("Advertiser matching", "Google lists ads by verified legal entity, not by Instagram handle. Each account was searched by name variants and by website; where a verified advertiser exists it is named. Phoenix malls advertise under The Phoenix Mills Limited (parent, all malls nationally) and Island Star Mall Developers Pvt Ltd (the Pune entity)."),
             ("Creative", "One row per creative Google has shown. Format is Text (Search), Image (Display) or Video (YouTube). First/Last Shown are the dates Google reports for that creative."),
             ("What is not here", "Google publishes no spend, impressions, keywords or targeting for commercial ads. Ad text for Text and Video creatives is rendered inside Google's preview frame; open the Creative Page link to see it."),
             ("Accounts with no advertiser", "Seasons Mall, The Pavillion, Amanora Mall and KOPA have no verified Google advertiser and no ads found under their websites: they are not running Google ads in India, or run them through an agency account under a different legal name.")]
    m.cell(1, 1, "Field").font = bold; m.cell(1, 2, "Definition").font = bold
    for i, (k, v) in enumerate(notes, 2):
        m.cell(i, 1, k); m.cell(i, 2, v).alignment = Alignment(wrap_text=True, vertical="top")
    m.column_dimensions["A"].width = 30; m.column_dimensions["B"].width = 120
    path = xlsx if (os.sep in xlsx or "/" in xlsx) else os.path.join(BASE_DIR, "deliverables", xlsx)
    try:
        wb.save(path)
    except PermissionError:
        path = path.replace(".xlsx", f"_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"); wb.save(path)
    print(f"[EXCEL] {path}")
    return path


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    spec = json.load(open(a[a.index("--advertisers") + 1], encoding="utf-8"))
    xlsx = a[a.index("--xlsx") + 1] if "--xlsx" in a else "Google_Ads_Transparency.xlsx"
    res = run(spec)
    json.dump(res, open(os.path.join(BASE_DIR, "google_ads_cache.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    export(res, xlsx)
