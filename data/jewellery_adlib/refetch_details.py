# -*- coding: utf-8 -*-
"""Re-fetch post details for reels whose first detail lookup failed (creator == None)."""
import json, os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from core import ig_web
from core.brand_collab_scan import _record, _media_id
from curl_cffi import requests
from core.profile_auditor import COOKIES, ANDROID_UA, IG_APP_ID


def media_info(code):
    r = requests.get(f'https://i.instagram.com/api/v1/media/{_media_id(code)}/info/', headers={'User-Agent': ANDROID_UA, 'x-ig-app-id': IG_APP_ID},
                     cookies=COOKIES, impersonate='chrome120', timeout=25)
    if r.status_code == 200 and r.text.startswith('{'):
        items = r.json().get('items') or []
        return items[0] if items else None
    return None
F = ROOT + "/" + sys.argv[1]
d = json.load(open(F, encoding="utf-8"))
for b, res in d.items():
    todo = [p for p in res.get("reels", []) if not p.get("creator")]
    print(b, len(todo), "reels to re-fetch", flush=True)
    ok = 0
    for i, p in enumerate(todo, 1):
        it = None
        for attempt in range(3):
            it = media_info(p["code"])
            if it: break
            time.sleep(20 * (attempt + 1))
        if it:
            n = _record(dict(it, play_count=it.get("play_count") or p.get("views")), b, "reels_tab", it.get("user") or {})
            n["caption_full"] = ((it.get("caption") or {}).get("text") or "") if isinstance(it.get("caption"), dict) else ""
            n["detail"] = True
            p.clear(); p.update(n); ok += 1
        if i % 20 == 0:
            print(f"  {i}/{len(todo)} ({ok} ok)", flush=True)
            json.dump(d, open(F + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1); os.replace(F + ".tmp", F)
        time.sleep(3)
    print(b, "re-fetched", ok, "of", len(todo), flush=True)
json.dump(d, open(F + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1); os.replace(F + ".tmp", F)
