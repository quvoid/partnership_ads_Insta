# -*- coding: utf-8 -*-
"""
================================================================================
GOOGLE ADS CREATIVE CONTENT - what each Google ad actually says
================================================================================
Reads google_ads_cache.json (from google_ads_transparency_scan.py) and, for every
creative, fetches the ad itself:

  Text  -> Google renders the ad as an image; OCR it locally (RapidOCR) to recover
           headline / description / display URL
  Image -> banner image URL, plus OCR of any text on it
  Video -> the YouTube id from the preview payload, then the public title and
           channel via YouTube oEmbed

Also records how many variations the creative has and which regions it ran in.
Progress is saved atomically to google_ads_content.json after every batch, so the
run is resumable; OCR output is stripped of lone surrogates before writing.

  python core/google_ads_creative_content.py --xlsx OUT.xlsx [--limit N]
================================================================================
"""
import sys, os, re, json, time, io, urllib.parse
from typing import Dict, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.stdout.reconfigure(encoding="utf-8")

from curl_cffi import requests as cffi

CACHE = os.path.join(BASE_DIR, "google_ads_cache.json")
OUT_JSON = os.path.join(BASE_DIR, "google_ads_content.json")
S = cffi.Session(impersonate="chrome120")
_ocr = None
_SURR = re.compile("[\ud800-\udfff]")


def _clean(o):
    """OCR can emit lone surrogates, which json cannot encode. Strip them."""
    if isinstance(o, str):
        return _SURR.sub("", o)
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def _save(done: Dict[str, Any]):
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_clean(done), f, ensure_ascii=True)
    os.replace(tmp, OUT_JSON)


def ocr(img_bytes: bytes) -> str:
    global _ocr
    try:
        if _ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr = RapidOCR()
        import numpy as np
        from PIL import Image
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        if im.width < 400:                      # upscale small banners so small type resolves
            im = im.resize((im.width * 3, im.height * 3))
        res, _ = _ocr(np.array(im))
        lines = [r[1].strip() for r in (res or []) if r[1] and float(r[2]) > 0.4]
        return " | ".join(lines)
    except Exception as e:
        return f"(ocr failed: {type(e).__name__})"


def rpc(svc: str, body: Dict[str, Any]):
    return S.post(f"https://adstransparency.google.com/anji/_/rpc/{svc}?authuser=",
                  headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                           "origin": "https://adstransparency.google.com",
                           "referer": "https://adstransparency.google.com/", "x-same-domain": "1"},
                  data="f.req=" + urllib.parse.quote(json.dumps(body)), timeout=30)


def youtube_meta(vid: str) -> Dict[str, str]:
    try:
        r = S.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json", timeout=20)
        if r.status_code == 200:
            j = r.json()
            return {"title": j.get("title", ""), "channel": j.get("author_name", "")}
    except Exception:
        pass
    return {"title": "", "channel": ""}


def enrich(c: Dict[str, Any]) -> Dict[str, Any]:
    out = {"regions": "", "variations": 0, "youtube_id": "", "youtube_url": "", "video_title": "",
           "video_channel": "", "ad_text_ocr": "", "image_url": c.get("image_url", "")}
    try:
        r = rpc("LookupService/GetCreativeById", {"1": c["advertiser_id"], "2": c["creative_id"], "5": {"1": 1, "2": 2356}})
        if r.status_code != 200:
            return out
        d = r.json().get("1", {})
        out["regions"] = ", ".join(str(x.get("1")) for x in d.get("17", []))
        variations = d.get("5", [])
        out["variations"] = len(variations)
        imgs, yts = [], []
        for v in variations:
            p = v.get("1") or {}
            q = v.get("3") or {}
            if q.get("2"):
                m = re.search(r'src="([^"]+)"', q["2"])
                if m:
                    imgs.append(m.group(1))
            if p.get("4") and "content.js" in p["4"]:
                try:
                    t = S.get(p["4"], headers={"referer": "https://adstransparency.google.com/"}, timeout=30).text
                    yts += re.findall(r'ytimg\.com/vi/([\w-]{11})', t)
                    yts += re.findall(r'"videoId":"([\w-]{11})"', t)
                    yts += re.findall(r'youtube\.com/(?:embed/|watch\?v=)([\w-]{11})', t)
                    imgs += re.findall(r'(https://tpc\.googlesyndication\.com/(?:archive/)?simgad/\d+)', t)
                except Exception:
                    pass
        yts = list(dict.fromkeys(yts))
        if yts:
            out["youtube_id"] = yts[0]
            out["youtube_url"] = f"https://www.youtube.com/watch?v={yts[0]}"
            meta = youtube_meta(yts[0])
            out["video_title"], out["video_channel"] = meta["title"], meta["channel"]
        imgs = list(dict.fromkeys(imgs))
        if not out["image_url"] and imgs:
            out["image_url"] = imgs[0]
        if out["image_url"]:
            try:
                out["ad_text_ocr"] = ocr(S.get(out["image_url"], timeout=30).content)
            except Exception as e:
                out["ad_text_ocr"] = f"(image fetch failed: {type(e).__name__})"
    except Exception as e:
        out["ad_text_ocr"] = f"(lookup failed: {type(e).__name__})"
    return out


def run(limit: int = 0):
    cache = json.load(open(CACHE, encoding="utf-8"))
    done = {}
    if os.path.exists(OUT_JSON) and os.path.getsize(OUT_JSON):
        try:
            done = json.load(open(OUT_JSON, encoding="utf-8"))
        except Exception:
            done = {}
    todo = [(k, a, c) for k, e in cache.items() for a in e.get("advertisers", [])
            for c in a["creatives"] if c["creative_id"] not in done]
    if limit:
        todo = todo[:limit]
    print(f"[content] {len(todo)} creatives to enrich ({len(done)} already done)", flush=True)
    t0 = time.time()
    for i, (k, a, c) in enumerate(todo, 1):
        try:
            done[c["creative_id"]] = enrich(c)
        except Exception as e:
            done[c["creative_id"]] = {"ad_text_ocr": f"(error {type(e).__name__})", "variations": 0,
                                      "youtube_url": "", "video_title": "", "video_channel": "",
                                      "image_url": c.get("image_url", ""), "regions": "", "youtube_id": ""}
        if i % 20 == 0 or i == len(todo):
            _save(done)
            el = time.time() - t0
            print(f"  {i}/{len(todo)} ({el/60:.0f} min, ~{el/i*(len(todo)-i)/60:.0f} min left)", flush=True)
        time.sleep(0.4)
    _save(done)
    return cache, done


def export(cache, content, xlsx: str):
    import openpyxl
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter
    from core.kolkata_engine import clean_cell
    from core.google_ads_transparency_scan import export as base_export
    bold = Font(bold=True)
    path = base_export(cache, xlsx)          # Summary + Method, then swap in richer per-advertiser tabs
    wb = openpyxl.load_workbook(path)
    for key, e in cache.items():
        for a in e.get("advertisers", []):
            name = key if len(e["advertisers"]) == 1 else f"{key} {a['advertiser_id'][-6:]}"
            name = re.sub(r'[\[\]\*\?/\\:]', '', name)[:31]
            if name in wb.sheetnames:
                del wb[name]
            t = wb.create_sheet(name)
            t.cell(1, 1, f"@{key} - {a['legal_name']} ({a['advertiser_id']}) - Google Ads Transparency Center, India").font = bold
            cols = ["Creative ID", "Format", "First Shown", "Last Shown", "Variations",
                    "Ad Text (read from the rendered ad)", "Video Title", "Video Channel",
                    "YouTube URL", "Image URL", "Creative Page"]
            for c, h in enumerate(cols, 1):
                t.cell(3, c, h).font = bold
            cs = sorted(a["creatives"], key=lambda c: (c["last_shown"], c["first_shown"]), reverse=True)
            for i, c in enumerate(cs, 4):
                x = content.get(c["creative_id"], {})
                vals = [c["creative_id"], c["format"], c["first_shown"], c["last_shown"], x.get("variations", ""),
                        x.get("ad_text_ocr", ""), x.get("video_title", ""), x.get("video_channel", ""),
                        x.get("youtube_url", ""), x.get("image_url") or c.get("image_url", ""), c["detail_url"]]
                for j, v in enumerate(vals, 1):
                    t.cell(i, j, clean_cell(v)).alignment = Alignment(vertical="top", wrap_text=(j == 6))
            for i, w in enumerate([26, 8, 12, 12, 10, 80, 44, 24, 44, 60, 80], 1):
                t.column_dimensions[get_column_letter(i)].width = w
            t.freeze_panes = "C4"

    ws = wb["Summary"]
    hdr = [c.value for c in ws[3]]
    if "Creatives With Ad Text / Video Title" not in hdr:
        col = len(hdr) + 1
        ws.cell(3, col, "Creatives With Ad Text / Video Title").font = bold
        ws.column_dimensions[get_column_letter(col)].width = 18
        r = 4
        for key, e in cache.items():
            if not e.get("advertisers"):
                r += 1
                continue
            for a in e["advertisers"]:
                n = 0
                for c in a["creatives"]:
                    x = content.get(c["creative_id"], {})
                    txt = (x.get("ad_text_ocr") or "").strip()
                    if (txt and not txt.startswith("(")) or x.get("video_title"):
                        n += 1
                ws.cell(r, col, n)
                r += 1
    m = wb["Method"]
    m.append(["Ad Text (read from the rendered ad)",
              "Google serves Text and Display ads as rendered images, so the wording is recovered with local OCR. "
              "Line breaks appear as ' | '. Treat it as close but not exact: small type and stylised fonts can be misread."])
    m.append(["Video Title / Channel",
              "For Video creatives, the YouTube video behind the ad, with its public title and channel name."])
    m.append(["Variations",
              "How many creative variations Google groups under this one creative id."])
    wb.save(path)
    print(f"[EXCEL] {path}")
    return path


if __name__ == "__main__":
    a = sys.argv[1:]
    lim = int(a[a.index("--limit") + 1]) if "--limit" in a else 0
    xlsx = a[a.index("--xlsx") + 1] if "--xlsx" in a else "Google_Ads_Transparency.xlsx"
    cache, content = run(lim)
    export(cache, content, xlsx)
