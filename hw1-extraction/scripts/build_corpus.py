"""Зводить clean-файли кількох каналів в один корпус і позначає дублікати.

Канали активно репостять одне одного, тож той самий текст трапляється кілька
разів. Для аналітики це подвійний підрахунок. Скрипт нормалізує текст (регістр,
пробіли, мітки PII, посилання) і для однакових текстів позначає найраніший пост
як оригінал, а решту - duplicate_of=<channel>/<id> оригіналу.

  python scripts/build_corpus.py                       # усі data/*_clean.jsonl
  python scripts/build_corpus.py signalman_diary military_engineer

Результат: data/corpus.jsonl - пости всіх каналів з полями channel і duplicate_of.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MIN_CHARS_FOR_DEDUPE = 40  # короткі тексти ("Доброе утро") збігаються випадково - їх не зводимо


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\[(?:tg_link|pay_link|card|wallet|phone|email|user)\]", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\w]+", " ", text)
    return text.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("channels", nargs="*", help="канали; за замовчуванням усі data/*_clean.jsonl")
    args = parser.parse_args()

    channels = args.channels or sorted(p.name[: -len("_clean.jsonl")] for p in DATA.glob("*_clean.jsonl"))
    posts = []
    for ch in channels:
        with (DATA / f"{ch}_clean.jsonl").open(encoding="utf-8") as f:
            for line in f:
                p = json.loads(line)
                posts.append({"channel": ch, **p})

    # Оригінал - найраніший пост із таким текстом.
    posts.sort(key=lambda p: (p["date"] or "", p["channel"], p["id"]))
    first_seen: dict[str, str] = {}
    for p in posts:
        norm = normalize(p["text"])
        p["duplicate_of"] = None
        if len(norm) < MIN_CHARS_FOR_DEDUPE:
            continue
        key = hashlib.sha1(norm.encode("utf-8")).hexdigest()
        ref = f"{p['channel']}/{p['id']}"
        if key in first_seen:
            p["duplicate_of"] = first_seen[key]
        else:
            first_seen[key] = ref

    out = DATA / "corpus.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for p in posts:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    total = collections.Counter(p["channel"] for p in posts)
    dups = collections.Counter(p["channel"] for p in posts if p["duplicate_of"])
    print(f"{'канал':<26}{'постів':>8}{'дублікатів':>12}")
    for ch in channels:
        print(f"{ch:<26}{total[ch]:>8}{dups[ch]:>12}")
    print(f"\nУсього {len(posts)}, унікальних {len(posts) - sum(dups.values())} -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
