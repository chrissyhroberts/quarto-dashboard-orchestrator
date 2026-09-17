from pathlib import Path
import csv

root = Path(__file__).resolve().parent
(root / "exports").mkdir(exist_ok=True)

with (root / "exports" / "exceptions.csv").open("w", newline="", encoding="utf-8") as f:
    csv.writer(f).writerows([
        ["item_id", "exception", "owner"],
        ["B-101", "late", "team 1"],
        ["B-102", "incomplete", "team 2"],
    ])

with (root / "exports" / "weekly_summary.csv").open("w", newline="", encoding="utf-8") as f:
    csv.writer(f).writerows([
        ["metric", "value"],
        ["received", "42"],
        ["complete", "38"],
        ["open", "4"],
    ])
