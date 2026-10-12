"""
================================================================================
CORE MODULE: PAID MEDIA AD SPEND & BOOST DETECTION ENGINE
================================================================================
Evaluates video performance metrics to identify paid ad spend (boosted reels).
Calculates:
  - Like-to-view percentage
  - View-to-follower multiplier
  - Boost status classification
  - Diagnostic reason explaining why ad spend was detected
================================================================================
"""

import sys, os, json
from typing import Dict, Any

def evaluate_boost(views: int, likes: int, followers: int) -> Dict[str, Any]:
    """Classifies whether a video was boosted with paid media ad spend."""
    if views <= 0:
        return {
            "is_boosted": False,
            "boost_status": "Standard Organic",
            "like_to_view_pct": "0.00%",
            "view_to_follower_multiplier": "0.0x",
            "boost_reason": "No view metrics available"
        }

    like_pct = (likes / views) * 100 if views > 0 else 0
    multiplier = (views / followers) if followers > 0 else 0

    is_boosted = False
    status = "Standard Organic"
    reason = "Normal organic engagement pattern"

    if views >= 500_000 and like_pct < 0.35:
        is_boosted = True
        status = "Heavily Boosted"
        reason = f"High view count ({views:,}) with sub-0.35% like rate ({like_pct:.2f}%) indicates active paid video ads campaign"
    elif views >= 1_000_000 and like_pct < 0.60:
        is_boosted = True
        status = "Heavily Boosted"
        reason = f"Outlier scale ({views:,} views) with suppressed like rate ({like_pct:.2f}%) indicates paid media amplification"
    elif multiplier >= 10.0 and like_pct < 0.50:
        is_boosted = True
        status = "Likely Boosted"
        reason = f"View-to-follower multiplier ({multiplier:.1f}x) exceeds creator baseline with low like rate ({like_pct:.2f}%)"
    elif multiplier >= 25.0:
        status = "Viral Organic"
        reason = f"Massive reach multiplier ({multiplier:.1f}x) with organic engagement"

    return {
        "is_boosted": is_boosted,
        "boost_status": status,
        "like_to_view_pct": f"{like_pct:.2f}%",
        "view_to_follower_multiplier": f"{multiplier:.1f}x",
        "boost_reason": reason
    }


if __name__ == "__main__":
    v = int(sys.argv[1]) if len(sys.argv) > 1 else 1_250_000
    l = int(sys.argv[2]) if len(sys.argv) > 2 else 2_500
    f = int(sys.argv[3]) if len(sys.argv) > 3 else 45_000
    res = evaluate_boost(v, l, f)
    print("=" * 60)
    print("BOOST DETECTION DIAGNOSTIC")
    print("=" * 60)
    print(f"Views:       {v:,}")
    print(f"Likes:       {l:,}")
    print(f"Followers:   {f:,}")
    print(f"Like Rate:   {res['like_to_view_pct']}")
    print(f"Multiplier:  {res['view_to_follower_multiplier']}")
    print(f"Status:      {res['boost_status']}")
    print(f"Reason:      {res['boost_reason']}")
    print("=" * 60)
