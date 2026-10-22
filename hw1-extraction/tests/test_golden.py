"""Перевірка golden set після ручних правок.

- кожна розмітка валідна за схемою ExtractedPost;
- id збігаються з golden_candidate у data/dataset.jsonl, текст не змінено;
- status лише 'draft' або 'reviewed'.

Запуск:  python -m tests.test_golden
"""

from __future__ import annotations

import json
from pathlib import Path

from src.schema import ExtractedPost

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "data" / "golden.json"
DATASET = ROOT / "data" / "dataset.jsonl"


def load():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    with DATASET.open(encoding="utf-8") as f:
        dataset = {r["id"]: r for r in map(json.loads, f)}
    return golden, dataset


def test_golden():
    golden, dataset = load()

    expected = sorted(i for i, r in dataset.items() if r["golden_candidate"])
    assert sorted(g["id"] for g in golden) == expected, "id у golden.json не збігаються з golden_candidate"

    for g in golden:
        assert g["status"] in ("draft", "reviewed"), f"#{g['id']}: невідомий status {g['status']!r}"
        assert g["text"] == dataset[g["id"]]["text"], f"#{g['id']}: текст відрізняється від dataset.jsonl"
        ExtractedPost.model_validate(g["label"])


if __name__ == "__main__":
    test_golden()
    golden, _ = load()
    reviewed = sum(g["status"] == "reviewed" for g in golden)
    print(f"OK: {len(golden)} розміток валідні; перевірено вручну: {reviewed}/{len(golden)}")
