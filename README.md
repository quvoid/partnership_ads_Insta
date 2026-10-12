# Brand partnership-post scanner (Instagram)

Lists every partnership post for a brand in a date window, from the brand's
**Reels tab + Posts grid** (this is the code used for the GIVA /
Palmonas scan, 1 Sep - 12 Oct 2026).

## Setup
1. Python 3.11+, then: `pip install curl_cffi openpyxl playwright`
2. Copy `.env.example` to `.env` and fill in YOUR OWN Instagram cookies from a
   logged-in browser (DevTools -> Application -> Cookies -> instagram.com).
   Never commit `.env` or share it.

## Run (from this folder)
```bash
# 1. scan - window dates inclusive
WIN_START=2026-09-01 WIN_END=2026-10-12 DIWALI_OUT=giva_palmonas_30d.json \
  python data/jewellery_adlib/diwali_scan.py giva.co palmonas_official

# 2. build the Excel -> deliverables/Giva_Palmonas_Partnership_Posts_....xlsx
python data/jewellery_adlib/build_partnership_30d.py
```
On Windows PowerShell set the variables first, e.g. `$env:WIN_START="2026-09-01"`.

For other brands: pass their handles to step 1, and edit `NAMES` / `SUB` at the
top of `build_partnership_30d.py` (SUB = the brand's own sub-account prefixes,
so they are not counted as partners).

## Notes
- Resumable: results are saved after every brand; rerunning skips finished ones.
- If Instagram throttles (HTTP 429 / feedback_required) the scan pauses, retries,
  and marks the brand INCOMPLETE rather than recording zero posts.
- Run from one machine per Instagram session at a time.
