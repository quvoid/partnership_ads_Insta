# -*- coding: utf-8 -*-
"""Partnership posts in the last 30 days for given brands, from giva_palmonas_30d.json
(Reels tab + Posts grid). A post counts when someone other than the brand is
involved: the creator posted it with the brand as co-author, a co-author, a tagged account,
or a paid-partnership sponsor."""
import json, os, sys
from datetime import datetime, timezone, timedelta
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from core.kolkata_engine import clean_cell

IST = timezone(timedelta(hours=5, minutes=30))
SRC = ROOT + "/" + os.environ.get("PB_SRC", "giva_palmonas_30d.json")
OUT = ROOT + "/deliverables/" + os.environ.get("PB_OUT", "Giva_Palmonas_Partnership_Posts_01Sep-12Oct2026.xlsx")
PERIOD = os.environ.get("PB_PERIOD", "1 Sep - 12 Oct 2026")
NAMES = dict(x.split("=", 1) for x in os.environ["PB_BRANDS"].split(",")) if os.environ.get("PB_BRANDS") else {"giva.co": "GIVA", "palmonas_official": "Palmonas"}
SUB = {h: (h.split(".")[0].split("_")[0],) for h in NAMES}       # own sub-accounts are not partners
SRC_LABEL = {"reels_tab": "Reels tab", "posts_grid": "Posts grid"}
HDR = PatternFill("solid", fgColor="1F3864"); W = Font(bold=True, color="FFFFFF"); B = Font(bold=True)
WRAP = Alignment(wrap_text=True, vertical="top"); LINK = Font(color="0563C1", underline="single")

d = json.load(open(SRC, encoding="utf-8"))
wb = openpyxl.Workbook(); ov = wb.active; ov.title = "Overview"
ov_rows = []
for h, disp in NAMES.items():
    r = d.get(h) or {}
    allp = (r.get("reels") or []) + ((r.get("grid") or {}).get("posts") or [])
    own = lambda u: (u or "").lower() == h or any((u or "").lower().startswith(s) for s in SUB[h])
    merged = {}
    for p in allp:
        m = merged.setdefault(p["code"], dict(p, found_in=set()))
        m["found_in"].add(SRC_LABEL.get(p.get("source"), p.get("source")))
        for k in ("coauthors", "tagged", "sponsors"):
            m[k] = list(dict.fromkeys((m.get(k) or []) + (p.get(k) or [])))
        m["is_paid_partnership"] = m.get("is_paid_partnership") or p.get("is_paid_partnership")
        if not m.get("views") and p.get("views"):
            m["views"] = p["views"]
    rows = []
    for p in sorted(merged.values(), key=lambda x: -(x.get("taken_at") or 0)):
        partners = [u for u in ([] if own(p.get("creator")) else [p.get("creator")]) + p["coauthors"] + p["sponsors"] + p["tagged"]
                    if u and not own(u)]
        partners = list(dict.fromkeys(partners))
        if not partners:
            continue
        # Every label below comes from Instagram's own post fields, never from the caption:
        # coauthor_producers (Collab, "X and 1 other"), sponsor_tags (Paid partnership), usertags (tagged on the media).
        collab = [u for u in p["coauthors"] if not own(u)] or (not own(p.get("creator")) and any(own(x) for x in p["coauthors"]))
        if p.get("is_paid_partnership"):
            how = "Paid partnership (sponsor label)"
        elif collab and own(p.get("creator")):
            how = "Collab - brand post with creator as co-author"
        elif collab:
            how = "Collab - creator post with brand as co-author"
        else:
            how = "Tagged on the video/photo only (no Collab)"
        rows.append([datetime.fromtimestamp(p["taken_at"], IST).strftime("%Y-%m-%d"), p.get("kind"), "@" + (p.get("creator") or ""),
                     ", ".join("@" + u for u in partners), how, "ON" if p.get("is_paid_partnership") else "OFF",
                     ", ".join("@" + s for s in p["sponsors"]) or "-", p.get("views") or ("n/a (photo)" if p.get("kind") != "Reel" else "not shown"),
                     "hidden" if p.get("likes_hidden") else p.get("likes"), p.get("comments"), ", ".join(sorted(p["found_in"])),
                     p["post_url"], p.get("caption_full") or p.get("caption") or ""])
    ws = wb.create_sheet(disp)
    ws.cell(1, 1, f"{disp} (@{h}) - partnership posts, {PERIOD}: {len(rows)} posts. "
                  f"Sources checked: Reels tab and Posts grid.").font = B
    ws.cell(2, 1, "All partner data comes from Instagram's post data, not captions: 'Collab' = the account is a co-author "
                  "(shown as 'X and 1 other'); 'Tagged only' = tagged on the media, no Collab; Paid Label ON = Meta's 'Paid partnership' label.")
    cols = ["#", "Date", "Format", "Posted By", "Partner(s)", "How", "Paid Label", "Sponsor", "Views", "Likes", "Comments",
            "Found In", "Post Link", "Caption"]
    for c, (t, w) in enumerate(zip(cols, [5, 11, 9, 24, 34, 30, 9, 18, 11, 9, 9, 22, 40, 80]), 1):
        x = ws.cell(4, c, t); x.font = W; x.fill = HDR; ws.column_dimensions[get_column_letter(c)].width = w
    for i, r_ in enumerate(rows, 1):
        for c, v in enumerate([i] + r_, 1):
            cell = ws.cell(4 + i, c, clean_cell(v) if isinstance(v, str) else v); cell.alignment = WRAP
            if c == 13:
                cell.hyperlink = v; cell.font = LINK
    ws.freeze_panes = None
    if rows:
        ws.auto_filter.ref = f"A4:N{4 + len(rows)}"
    # ---- one row per Collab / paid-partner influencer per post, with exact followers + engagement
    FOL = json.load(open(ROOT + "/collab_followers.json", encoding="utf-8")) if os.path.exists(ROOT + "/collab_followers.json") else {}
    ci = []
    for p in sorted(merged.values(), key=lambda x: -(x.get("taken_at") or 0)):
        if own(p.get("creator")):
            infl = [u for u in p["coauthors"] + p["sponsors"] if not own(u)]
        elif any(own(x) for x in p["coauthors"]) or p.get("is_paid_partnership"):
            infl = [p.get("creator")]
        else:
            infl = []
        for u in dict.fromkeys(infl):
            f = (FOL.get(u) or {}).get("followers")
            v, lk, cm = p.get("views"), (None if p.get("likes_hidden") else p.get("likes")), p.get("comments") or 0
            ci.append(["@" + u, f if f else "not resolved", v if v else ("n/a (photo)" if p.get("kind") != "Reel" else "not shown"),
                       lk if lk is not None else "hidden", cm,
                       round(lk / v * 100, 2) if (lk is not None and v) else "-",
                       round(((lk or 0) + cm) / f * 100, 2) if (f and lk is not None) else "-",
                       datetime.fromtimestamp(p["taken_at"], IST).strftime("%Y-%m-%d"), p.get("kind"),
                       "Paid partnership" if p.get("is_paid_partnership") else "Collab (co-author)", p["post_url"]])
    wi = wb.create_sheet((disp + " - Collab Influencers")[:31])
    wi.cell(1, 1, f"{disp} - Collab influencers ({PERIOD}): {len({x[0] for x in ci})} unique influencers, {len(ci)} posts").font = B
    wi.cell(2, 1, "Followers = exact count read from the creator's Instagram profile. Like-to-View % = likes / views. "
                  "Creator ER% = (likes + comments) / followers. Only Collab co-authors and paid-partnership creators - tagged-only accounts excluded.")
    icols = ["Influencer", "Followers", "Views / Plays", "Likes", "Comments", "Like-to-View %", "Creator ER%", "Post Date", "Format", "Type", "Post Link"]
    for c, (t, w) in enumerate(zip(icols, [26, 13, 13, 11, 11, 13, 12, 12, 9, 20, 42]), 1):
        x = wi.cell(4, c, t); x.font = W; x.fill = HDR; wi.column_dimensions[get_column_letter(c)].width = w
    for i, r_ in enumerate(ci, 5):
        for c, v in enumerate(r_, 1):
            cell = wi.cell(i, c, v)
            if c in (2, 3, 4, 5) and isinstance(v, int): cell.number_format = "#,##0"
            if c == 11: cell.hyperlink = v; cell.font = LINK
    if ci:
        wi.auto_filter.ref = f"A4:K{4 + len(ci)}"
    notes = "; ".join(x for x in [r.get("note"), (r.get("grid") or {}).get("note")] if x)
    ov_rows.append([disp, "@" + h, len(rows), sum(1 for x in rows if x[5] == "ON"), sum(1 for x in rows if x[1] == "Reel"),
                    len({p for x in rows for p in x[3].split(", ")}), notes or "Complete"])
ov.cell(1, 1, f"Partnership posts - {PERIOD}").font = Font(bold=True, size=13)
for c, (t, w) in enumerate(zip(["Brand", "Instagram", "Partnership Posts", "Paid-Label Posts", "Reels", "Distinct Partners", "Scan Status"],
                               [14, 22, 12, 12, 8, 12, 40]), 1):
    x = ov.cell(3, c, t); x.font = W; x.fill = HDR; ov.column_dimensions[get_column_letter(c)].width = w
for i, r_ in enumerate(ov_rows, 4):
    for c, v in enumerate(r_, 1):
        ov.cell(i, c, v)
wb.save(OUT)
print(OUT); [print(r_) for r_ in ov_rows]
