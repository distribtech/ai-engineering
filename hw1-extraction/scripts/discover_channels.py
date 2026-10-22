"""Шукає пов'язані Telegram-канали "сніжним комом" від уже зібраного каналу.

1. Бере всі посилання t.me/<username> з data/raw/<channel>.jsonl (посилання в
   тексті та превʼю) і рахує, у скількох постах згадано кожен username.
2. Для кандидатів, згаданих щонайменше в --min-posts постах, відкриває веб-версію
   t.me/s/<username>. Якщо її немає - це особистий акаунт, бот, група або приватний
   канал, і він позначається як not_public.
3. Для публічних каналів зберігає назву, опис, кількість підписників, дату
   останнього поста й тематичний профіль за ключовими словами в останніх ~20 постах.

  python scripts/discover_channels.py signalman_diary
  python scripts/discover_channels.py signalman_diary --min-posts 3 --limit 20

Результат: data/channels_<channel>.jsonl - по рядку на кандидата, відсортовано
за кількістю згадок.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape_telegram import HEADERS, clean_pii, fetch, parse_page  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

USERNAME_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?:[/?#]|$)", re.I)
SERVICE_NAMES = {"joinchat", "addstickers", "addemoji", "share", "iv", "proxy", "socks", "c", "s", "boost"}

# Тематичні словники (корені в нижньому регістрі, російською - мовою каналів).
TOPICS = {
    "drones": ["дрон", "бпла", "fpv", "фпв", "мавик", "mavic", "квадрокоптер", "коптер", "птичк", "крыло", "оператор бп", "matrice", "autel"],
    "ew_sigint": ["рэб", "рэр", "детектор", "обнаружител", "глушил", "подавител", "сканер"],
    "comms": ["связ", "радиостанц", "рации", "рация", "ретрансл", "антенн", "starlink", "старлинк", "mesh", "dmr"],
    "optics": ["тепловиз", "ночник", "прицел", "монокуляр", "nvg", "пнв"],
    "vehicles": ["автомоб", "автомаш", "машин", "буханк", "уаз", "пикап", "квадроцикл", "мотоцикл", "багги", " нива"],
    "medicine": ["аптечк", "турникет", "жгут", "медицин", "тактическая медиц", "перевяз"],
    "gear": ["бронежилет", "шлем", "разгрузк", "плитник", "экипировк", "снаряжени", "спальник", "палатк", "генератор", "окопн"],
    "fundraising": ["сбор", "реквизит", "закуп", "передали", "отчет", "отчёт", "гуманитар", "помощь фронту", "доставили"],
}


def collect_candidates(posts: list[dict], own: str) -> collections.Counter:
    """username -> кількість постів, де він згаданий."""
    counter: collections.Counter = collections.Counter()
    for p in posts:
        urls = list(p.get("links", []))
        if p.get("link_preview"):
            urls.append(p["link_preview"]["url"])
        names = set()
        for url in urls:
            m = USERNAME_RE.match(url or "")
            if m:
                name = m.group(1).lower()
                if name != own.lower() and name not in SERVICE_NAMES and not name.endswith("bot"):
                    names.add(name)
        counter.update(names)
    return counter


def topic_profile(texts: list[str]) -> dict[str, int]:
    """Скільки постів зачіпає кожну тему (пост рахується один раз на тему)."""
    profile = {}
    lowered = [t.lower() for t in texts]
    for topic, roots in TOPICS.items():
        profile[topic] = sum(1 for t in lowered if any(r in t for r in roots))
    return profile


def inspect_channel(session: requests.Session, username: str) -> dict:
    row: dict = {"username": username, "url": f"https://t.me/{username}", "status": None}
    resp = session.get(f"https://t.me/s/{username}", timeout=20, allow_redirects=False)
    # Для не-каналів (особисті акаунти, групи, приватні канали) t.me/s/ редиректить на t.me/<name>.
    if resp.status_code in (301, 302):
        row["status"] = "not_public"
        return row
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.select_one(".tgme_channel_info_header_title")
    if not title:
        row["status"] = "not_public"
        return row

    desc = soup.select_one(".tgme_channel_info_description")
    counters = {
        c.select_one(".counter_type").get_text(strip=True): c.select_one(".counter_value").get_text(strip=True)
        for c in soup.select(".tgme_channel_info_counter")
        if c.select_one(".counter_type") and c.select_one(".counter_value")
    }
    posts = parse_page(resp.text, username)
    texts = [p["text"] for p in posts if p["text"]]
    dates = [p["date"] for p in posts if p["date"]]

    row.update({
        "status": "public",
        "title": title.get_text(" ", strip=True),
        "description": clean_pii(desc.get_text("\n", strip=True)) if desc else None,
        "subscribers": counters.get("subscribers") or counters.get("subscriber"),
        "last_post_date": max(dates)[:10] if dates else None,
        "sample_posts": len(texts),
        "topics": topic_profile(texts),
    })
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("channel", help="канал-джерело, вже зібраний scrape_telegram.py")
    parser.add_argument("--min-posts", type=int, default=2, help="мінімум постів зі згадкою кандидата")
    parser.add_argument("--limit", type=int, default=None, help="перевірити лише N найчастіших кандидатів")
    parser.add_argument("--delay", type=float, default=1.5, help="пауза між запитами, секунд")
    args = parser.parse_args()

    raw_path = ROOT / "data" / "raw" / f"{args.channel}.jsonl"
    with raw_path.open(encoding="utf-8") as f:
        posts = [json.loads(line) for line in f]

    counter = collect_candidates(posts, args.channel)
    candidates = [(n, c) for n, c in counter.most_common() if c >= args.min_posts]
    if args.limit:
        candidates = candidates[: args.limit]
    print(f"Згадано username: {len(counter)}; перевіряю {len(candidates)} (>= {args.min_posts} постів)")

    session = requests.Session()
    session.headers.update(HEADERS)
    rows = []
    for i, (name, mentions) in enumerate(candidates, 1):
        try:
            row = inspect_channel(session, name)
        except requests.RequestException as exc:
            row = {"username": name, "url": f"https://t.me/{name}", "status": f"error: {exc}"[:200]}
        row["mentioned_in_posts"] = mentions
        rows.append(row)
        info = f"{row.get('title')} | {row.get('subscribers')} | {row.get('topics')}" if row["status"] == "public" else row["status"]
        print(f"[{i}/{len(candidates)}] {name}: {info}")
        time.sleep(args.delay)

    out = ROOT / "data" / f"channels_{args.channel}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    public = sum(r["status"] == "public" for r in rows)
    print(f"\nГотово: {public} публічних каналів з {len(rows)} -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
