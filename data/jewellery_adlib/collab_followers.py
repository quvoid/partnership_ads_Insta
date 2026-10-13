# -*- coding: utf-8 -*-
"""Exact follower counts for every Collab / paid-partner account in a scan file, read from the
rendered logged-in profile header (span[title] holds the exact number). Cached in
collab_followers.json so reruns only look up new handles.
  python data/jewellery_adlib/collab_followers.py visa_1y.json visa.ind
"""
import json, os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from core import ig_web

SRC, BRAND = ROOT + "/" + sys.argv[1], sys.argv[2]
CACHE = ROOT + "/collab_followers.json"
own = lambda u: (u or "").lower().startswith(BRAND.split(".")[0].split("_")[0])
d = json.load(open(SRC, encoding="utf-8"))[BRAND]
handles = set()
for p in (d.get("reels") or []) + ((d.get("grid") or {}).get("posts") or []):
    if own(p.get("creator")):
        handles |= {u for u in (p.get("coauthors") or []) + (p.get("sponsors") or []) if not own(u)}
    elif p.get("creator") and (any(own(x) for x in p.get("coauthors") or []) or p.get("is_paid_partnership")):
        handles.add(p["creator"])
cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
todo = sorted(h for h in handles if not (cache.get(h) or {}).get("followers"))
print(len(handles), "collab accounts;", len(todo), "to look up", flush=True)
for i in range(0, len(todo), 15):
    res = ig_web.dom_profiles(todo[i:i + 15])
    for h, r in res.items():
        r["resolved_at"] = time.strftime("%Y-%m-%d %H:%M")
        cache[h] = r
    json.dump(cache, open(CACHE + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(CACHE + ".tmp", CACHE)
    print(f"  {min(i + 15, len(todo))}/{len(todo)} done", flush=True)
print("resolved", sum(1 for h in handles if (cache.get(h) or {}).get("followers")), "of", len(handles))
