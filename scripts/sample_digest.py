"""Render a digest from synthetic data, to review the report format without API keys.

Writes to data/sample.db and reports/sample-digest.md. Never touches monitor.db.
    .venv/bin/python -m scripts.sample_digest
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import report, store
from src.config import load_rubric

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(timezone.utc)

BRANDS = [{"name": "Allbirds", "channel_id": "sample"}, {"name": "Rothy's", "channel_id": "sample"}]

# (brand, days_old, title, views, likes, comments, values, confidence, themes)
UGC = dict(format="ugc", hook_type="problem", angle="problem_solution", offer="none",
           production_level="low", thumbnail_treatment="face")
STUDIO = dict(format="studio_product", hook_type="demo", angle="quality_durability", offer="none",
              production_level="high", thumbnail_treatment="product_only")
THEMES = {"recurring_themes": ["fit after a month of wear"],
          "questions": ["durability on wet pavement", "whether sizing runs small"],
          "objections": ["price vs. competitors"], "praise": ["comfort out of the box"],
          "overall_sentiment": "mixed"}


def main():
    db = ROOT / "data" / "sample.db"
    db.unlink(missing_ok=True)
    conn = store.connect(db)
    rng = random.Random(7)
    rubric = load_rubric()
    i = 0

    def add(brand, age, title, views, values, conf="high", themes=None):
        nonlocal i
        i += 1
        vid = f"s{i:03d}"
        store.insert_video(conn, {"video_id": vid, "brand": brand, "title": title,
                                  "published_at": (NOW - timedelta(days=age)).isoformat()})
        store.save_metrics(conn, vid, views, int(views * rng.uniform(.02, .05)),
                           int(views * rng.uniform(.002, .01)))
        if values:
            store.save_classification(conn, vid, values, conf, "sample", rubric["version"])
        if themes is not None:
            store.save_comment_themes(conn, vid, themes)

    # Allbirds history: studio-heavy, baseline ~40k; recent swing toward UGC.
    for d in range(60, 16, -4):
        add("Allbirds", d, f"Tree Runner — studio spot {d}", rng.randint(30_000, 50_000), STUDIO)
    add("Allbirds", 15, "I walked 20k steps in these", 41_000, UGC)
    add("Allbirds", 13, "My feet hurt every day until…", 38_000, UGC)
    add("Allbirds", 10, "Why my running shoes kept falling apart", 94_000, UGC, themes=THEMES)
    add("Allbirds", 9, "Wool Runner Mizzle — product film", 17_000, STUDIO)
    add("Allbirds", 8, "Our founders on carbon labels", 36_000,
        dict(UGC, format="talking_head", hook_type="founder_story", angle="sustainability"), conf="low")
    add("Allbirds", 3, "Commute test: 3 weeks in the rain", 22_000, UGC, themes=THEMES)
    add("Allbirds", 1, "Fall drop — 20% off this weekend", 6_000,
        dict(STUDIO, offer="discount", hook_type="bold_claim"))

    # Rothy's: steady, testimonial-led, one outperformer.
    for d in range(55, 14, -5):
        add("Rothy's", d, f"Customer story #{d}", rng.randint(8_000, 12_000),
            dict(UGC, format="testimonial", hook_type="social_proof", angle="sustainability",
                 production_level="mid"))
    add("Rothy's", 11, "Machine-washable flats: 100 washes later", 27_000,
        dict(STUDIO, format="montage", hook_type="before_after", thumbnail_treatment="before_after"),
        themes={"questions": ["colour fading after washing"], "praise": ["how new they still look"],
                "objections": [], "recurring_themes": [], "overall_sentiment": "positive"})
    add("Rothy's", 2, "The Point, reimagined", 3_100, None)
    store.quarantine(conn, f"s{i:03d}", "classify", "sample: unparseable after retry")
    conn.commit()

    md = report.build_digest(conn, rubric, BRANDS, NOW)
    out = ROOT / "reports" / "sample-digest.md"
    out.write_text("> **Sample digest — synthetic data**, generated to review the format.\n\n" + md)
    print(md)


if __name__ == "__main__":
    main()
