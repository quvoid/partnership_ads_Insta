# Brand partnership-post scanner (Instagram)

Lists every creator partnership for a brand in a date window, from the brand's
**Reels tab + Posts grid**, with exact creator follower counts and engagement.

## Setup
1. Python 3.11+, then: `pip install curl_cffi openpyxl playwright` and `playwright install chromium`
2. Copy `.env.example` to `.env` and fill in YOUR OWN Instagram cookies from a
   logged-in browser (DevTools -> Application -> Cookies -> instagram.com).
   Never commit `.env` or share it.

## Run (from this folder) - example: GIVA, 1 Apr - 13 Oct 2026
```bash
# 1. scan the Reels tab + Posts grid (window dates inclusive)
WIN_START=2026-04-01 WIN_END=2026-10-13 DIWALI_OUT=giva.json \
  python data/jewellery_adlib/scan_brand_posts.py giva.co

# 2. re-fetch any reel whose details Instagram refused (rate limit), via the mobile endpoint
python data/jewellery_adlib/refetch_details.py giva.json

# 3. exact follower counts for every Collab / paid creator
python data/jewellery_adlib/collab_followers.py giva.json giva.co

# 4. build the Excel -> deliverables/<PB_OUT>
PB_SRC=giva.json PB_OUT=GIVA_Partnerships.xlsx PB_BRANDS="giva.co=GIVA" \
PB_PERIOD="1 Apr - 13 Oct 2026" python data/jewellery_adlib/build_partnership_excel.py
```
On Windows PowerShell set each variable first, e.g. `$env:WIN_START="2026-04-01"`.

## How the scraping works
| Step | Source (Instagram backend, never the caption) | What it gives |
|---|---|---|
| Reels tab | GraphQL `PolarisProfileReelsTabContentQuery` (what instagram.com runs) | every reel incl. creator-owned Collab reels that never reach the grid |
| Posts grid | GraphQL `PolarisProfilePostsQuery` | photos, carousels, reels with full post fields |
| Post detail | `/p/{code}/` page JSON, fallback **`i.instagram.com/api/v1/media/{id}/info/`** (mobile app endpoint) | author, co-authors, tags, paid label, views |
| Followers | rendered logged-in profile header, `span[title]` | **exact** follower count (not the rounded "125K") |

Post date comes from the post itself; the window is applied before any detail call.

**Partnership classification** (all from Instagram's post fields):
- `coauthor_producers` -> **Collab** ("Brand and 1 other")
- `is_paid_partnership` -> **Paid partnership** label. Counted as paid even when no
  sponsor is named (when the brand is already a co-author Instagram shows only
  "Paid partnership", without "with Brand").
- `usertags` -> tagged on the media only (no Collab) - shown separately

**Metrics**: Like-to-View % = likes / views; Creator ER% = (likes + comments) / followers.

## Rate limits
Instagram throttles per endpoint, not per account. When the `/p/{code}/` page returns
HTTP 429, `refetch_details.py` switches to the mobile media-info endpoint, which keeps
answering. Profile follower counts are read from the rendered page because the
`users/{id}/info` endpoint is often throttled. Results are saved after every brand /
batch, so reruns resume. Run one machine per Instagram session at a time.
