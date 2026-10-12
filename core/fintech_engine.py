"""
================================================================================
CORE MODULE: FINTECH 53-BRAND 2-YEAR 4-TIER COLLABORATION ENGINE
================================================================================
Runs collaboration discovery and 4-tier evaluation across 53 Indian FinTech brands:
  - 6 Segments: Payments, WealthTech, Neobanks, Digital Lending, InsurTech, B2B SaaS
  - 2-Year Deep Scan window (Aug 2024 - Aug 2026)
  - Evaluates Meta Paid Partnership Toggle ON/OFF
  - Evaluates Boosted Ad Spend
  - Generates 3-Tab Master Excel: India_Fintech_4Tier_Partnership_Master.xlsx
================================================================================
"""

import sys, os, json, time
from typing import Dict, List, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from scrape_fintech_segment_partnerships import (
    FINTECH_SEGMENTS,
    scrape_brand_collabs,
    run_segment_pipeline,
    export_all_segments_workbook,
)

CACHE_FILE = os.path.join(BASE_DIR, "fintech_collabs_cache.json")

def run_fintech_scan(segments: List[str] = None, dry_run: bool = False):
    """Executes the FinTech scan for specified or all segments."""
    print("=" * 70)
    print("FINTECH 4-TIER 2-YEAR COLLABORATION ENGINE")
    print("=" * 70)

    target_segments = {}
    if segments:
        for seg in segments:
            for k, v in FINTECH_SEGMENTS.items():
                if seg.lower() in k.lower():
                    target_segments[k] = v
    else:
        target_segments = FINTECH_SEGMENTS

    total_brands = sum(len(b) for b in target_segments.values())
    print(f"Target Segments: {len(target_segments)}")
    print(f"Total Brands:    {total_brands}")
    print(f"Cache Path:      {CACHE_FILE}")
    print("=" * 70)

    if dry_run:
        print("[DRY RUN] Would scan the following segments and brands:")
        for seg, brands in target_segments.items():
            print(f"  Segment: {seg} ({len(brands)} brands)")
            for b in brands:
                print(f"    - @{b['handle']} ({b.get('brand', b['handle'])})")
        return

    # Run the segment pipeline
    run_segment_pipeline()
    print("Exporting updated workbook...")
    export_all_segments_workbook()
    print("Done! Master workbook updated.")


if __name__ == "__main__":
    is_dry = "--dry-run" in sys.argv
    run_fintech_scan(dry_run=is_dry)
