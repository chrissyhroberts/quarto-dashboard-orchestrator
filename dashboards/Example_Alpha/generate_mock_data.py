from pathlib import Path
import csv

root = Path(__file__).resolve().parent
(root / "data").mkdir(exist_ok=True)
(root / "exports").mkdir(exist_ok=True)

rows = [
    ["case_id", "site", "days_open", "status"],
    ["A-001", "North", "3", "follow up"],
    ["A-002", "South", "7", "review"],
    ["A-003", "North", "1", "follow up"],
]
with (root / "exports" / "open_cases.csv").open("w", newline="", encoding="utf-8") as f:
    csv.writer(f).writerows(rows)

review = [
    ["case_id", "issue", "priority"],
    ["A-002", "missing confirmation", "high"],
    ["A-004", "date requires review", "medium"],
]
with (root / "exports" / "data_review.csv").open("w", newline="", encoding="utf-8") as f:
    csv.writer(f).writerows(review)

(root / "data" / "README.txt").write_text("Placeholder transient data generated for the example.\n")
