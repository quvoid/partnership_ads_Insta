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
SRC = ROOT + "/giva_palmonas_30d.json"
OUT = ROOT + "/deliverables/Giva_Palmonas_Partnership_Posts_01Sep-12Oct2026.xlsx"
NAMES = {"giva.co": "GIVA", "palmonas_official": "Palmonas"}
SUB = {"giva.co": ("giva",), "palmonas_official": ("palmonas",)}       # own sub-accounts are not partners
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
        if not own(p.get("creator")) and h not in [x.lower() for x in p["coauthors"] + p["sponsors"]] and not p.get("is_paid_partnership"):
            how = "Creator post tagging the brand"
        elif own(p.get("creator")):
            how = "Brand post with co-author / tagged creator"
        else:
            how = "Creator post, brand as co-author"
        rows.append([datetime.fromtimestamp(p["taken_at"], IST).strftime("%Y-%m-%d"), p.get("kind"), "@" + (p.get("creator") or ""),
                     ", ".join("@" + u for u in partners), how, "ON" if p.get("is_paid_partnership") else "OFF",
                     ", ".join("@" + s for s in p["sponsors"]) or "-", p.get("views") or ("n/a (photo)" if p.get("kind") != "Reel" else "not shown"),
                     "hidden" if p.get("likes_hidden") else p.get("likes"), p.get("comments"), ", ".join(sorted(p["found_in"])),
                     p["post_url"], p.get("caption_full") or p.get("caption") or ""])
    ws = wb.create_sheet(disp)
    ws.cell(1, 1, f"{disp} (@{h}) - partnership posts, 1 Sep - 12 Oct 2026: {len(rows)} posts. "
                  f"Sources checked: Reels tab and Posts grid.").font = B
    ws.cell(2, 1, "Paid Label ON = Meta's 'Paid partnership' label.")
    cols = ["#", "Date", "Format", "Posted By", "Partner(s)", "How", "Paid Label", "Sponsor", "Views", "Likes", "Comments",
            "Found In", "Post Link", "Caption"]
    for c, (t, w) in enumerate(zip(cols, [5, 11, 9, 24, 34, 30, 9, 18, 11, 9, 9, 22, 40, 80]), 1):
        x = ws.cell(4, c, t); x.font = W; x.fill = HDR; ws.column_dimensions[get_column_letter(c)].width = w
    for i, r_ in enumerate(rows, 1):
        for c, v in enumerate([i] + r_, 1):
            cell = ws.cell(4 + i, c, clean_cell(v) if isinstance(v, str) else v); cell.alignment = WRAP
            if c == 13:
                cell.hyperlink = v; cell.font = LINK
    ws.freeze_panes = "C5"
    if rows:
        ws.auto_filter.ref = f"A4:N{4 + len(rows)}"
    notes = "; ".join(x for x in [r.get("note"), (r.get("grid") or {}).get("note")] if x)
    ov_rows.append([disp, "@" + h, len(rows), sum(1 for x in rows if x[5] == "ON"), sum(1 for x in rows if x[1] == "Reel"),
                    len({p for x in rows for p in x[3].split(", ")}), notes or "Complete"])
ov.cell(1, 1, "Partnership posts - since 1 Sep (1 Sep - 12 Oct 2026)").font = Font(bold=True, size=13)
for c, (t, w) in enumerate(zip(["Brand", "Instagram", "Partnership Posts", "Paid-Label Posts", "Reels", "Distinct Partners", "Scan Status"],
                               [14, 22, 12, 12, 8, 12, 40]), 1):
    x = ov.cell(3, c, t); x.font = W; x.fill = HDR; ov.column_dimensions[get_column_letter(c)].width = w
for i, r_ in enumerate(ov_rows, 4):
    for c, v in enumerate(r_, 1):
        ov.cell(i, c, v)
wb.save(OUT)
print(OUT); [print(r_) for r_ in ov_rows]
