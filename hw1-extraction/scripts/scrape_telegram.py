"""Збирає пости публічного Telegram-каналу через веб-версію t.me/s/<channel>.

Акаунт, номер телефону та API-ключі не потрібні. Скрипт гортає сторінки
назад у часі (параметр ?before=<id>), поки пости не закінчаться, і зберігає:

  data/raw/<channel>.jsonl    - сирі тексти (НЕ комітити: можуть містити персональні дані)
  data/<channel>_clean.jsonl  - тексти з прибраними персональними даними

Приклад:
  python scripts/scrape_telegram.py signalman_diary
  python scripts/scrape_telegram.py signalman_diary --max-pages 3 --delay 1.5
  python scripts/scrape_telegram.py military_engineer --since 2025-10-22
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://t.me/s/{channel}"
HEADERS = {"User-Agent": "Mozilla/5.0 (course homework scraper)"}
ROOT = Path(__file__).resolve().parent.parent

# Порядок важливий: спершу посилання й email, потім @згадки, потім телефони.
PII_PATTERNS = [
    (re.compile(r"https?://(?:t\.me|telegram\.me)/\S+", re.I), "[TG_LINK]"),
    (re.compile(
        r"(?:https?://)?(?:[\w-]+\.)*(?:yoomoney\.ru|money\.yandex\.ru|tinkoff\.ru|tbank\.ru|donationalerts\.com"
        r"|boosty\.to|sberbank\.ru|qiwi\.com|qr\.nspk\.ru|donatepay\.ru|sponsr\.ru|mysbertips\.ru)\S*", re.I),
     "[PAY_LINK]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    # Криптогаманці: Ethereum/USDT-ERC20, Bitcoin, TRON (USDT-TRC20), Monero
    (re.compile(r"\b(?:0x[0-9a-fA-F]{40}|bc1[0-9a-z]{25,60}|[13][1-9A-HJ-NP-Za-km-z]{25,34}|T[1-9A-HJ-NP-Za-km-z]{33}|[48][0-9AB][1-9A-HJ-NP-Za-km-z]{93})\b"), "[WALLET]"),
    (re.compile(r"(?<![\w/])@[A-Za-z][A-Za-z0-9_]{3,}"), "[USER]"),
    (re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)"), "[CARD]"),
    (re.compile(r"(?<!\d)\+?\d[\d\s().-]{8,}\d(?!\d)"), "[PHONE]"),
]


def clean_pii(text: str) -> str:
    for pattern, repl in PII_PATTERNS:
        text = pattern.sub(repl, text)
    return text


# Посилання, які не потрапляють у clean-файл: Telegram (там бувають особисті акаунти)
# і платіжні сервіси (реквізити зборів).
DROP_LINK_HOSTS = (
    "t.me", "telegram.me", "yoomoney.ru", "money.yandex.ru", "tinkoff.ru", "tbank.ru",
    "donationalerts.com", "boosty.to", "sberbank.ru", "qiwi.com",
    "qr.nspk.ru", "donatepay.ru", "sponsr.ru", "mysbertips.ru",
)


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def keep_link(url: str | None) -> bool:
    if not url:
        return False
    host = _host(url)
    return not any(host == h or host.endswith("." + h) for h in DROP_LINK_HOSTS)


def clean_post(p: dict) -> dict:
    preview = p.get("link_preview")
    if preview and keep_link(preview.get("url")):
        preview = {k: (clean_pii(v) if isinstance(v, str) and k != "url" else v) for k, v in preview.items()}
    else:
        preview = None
    return {
        **p,
        "text": clean_pii(p["text"]),
        "forwarded_from": p["forwarded_from"] and clean_pii(p["forwarded_from"]),
        "links": [u for u in p.get("links", []) if keep_link(u)],
        "link_preview": preview,
    }


def extract_text(node) -> str:
    """Текст повідомлення з переносами рядків замість <br>."""
    for br in node.find_all("br"):
        br.replace_with("\n")
    return node.get_text().strip()


def parse_page(html: str, channel: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        post_id = int(msg["data-post"].split("/")[-1])

        # Беремо лише власний текст поста, а не цитату з reply-блоку.
        text_node = None
        for node in msg.select("div.tgme_widget_message_text"):
            if not node.find_parent(class_="tgme_widget_message_reply"):
                text_node = node
                break

        time_node = msg.select_one("a.tgme_widget_message_date time")
        views_node = msg.select_one("span.tgme_widget_message_views")
        fwd_node = msg.select_one(".tgme_widget_message_forwarded_from_name")

        # Посилання з тексту; '?q=#тег' - це пошук по хештегу всередині t.me, його пропускаємо.
        links: list[str] = []
        if text_node:
            for a in text_node.select("a[href]"):
                href = a["href"]
                if href.startswith(("http://", "https://")) and href not in links:
                    links.append(href)

        preview_node = msg.select_one("a.tgme_widget_message_link_preview")
        link_preview = None
        if preview_node:
            def _txt(sel):
                n = preview_node.select_one(sel)
                return n.get_text(" ", strip=True) if n else None
            link_preview = {
                "url": preview_node.get("href"),
                "site": _txt(".link_preview_site_name"),
                "title": _txt(".link_preview_title"),
                "description": _txt(".link_preview_description"),
            }

        # Веб-версія показує лише назву й розмір файлу, без посилання на завантаження.
        doc_node = msg.select_one(".tgme_widget_message_document")
        document = None
        if doc_node:
            title = doc_node.select_one(".tgme_widget_message_document_title")
            extra = doc_node.select_one(".tgme_widget_message_document_extra")
            document = {
                "title": title.get_text(strip=True) if title else None,
                "size": extra.get_text(strip=True) if extra else None,
            }

        posts.append({
            "id": post_id,
            "url": f"https://t.me/{channel}/{post_id}",
            "date": time_node["datetime"] if time_node else None,
            "text": extract_text(text_node) if text_node else "",
            "views": views_node.get_text(strip=True) if views_node else None,
            "forwarded_from": (fwd_node.get_text(strip=True) or None) if fwd_node else None,
            "has_photo": bool(msg.select_one(".tgme_widget_message_photo_wrap")),
            "has_video": bool(msg.select_one(".tgme_widget_message_video_player")),
            "has_document": document is not None,
            "links": links,
            "link_preview": link_preview,
            "document": document,
        })
    return posts


def fetch(session: requests.Session, url: str, retries: int = 3, timeout: int = 20) -> str:
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10 * attempt))
                print(f"  429 Too Many Requests, чекаю {wait} с", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            print(f"  помилка ({exc}), спроба {attempt + 1}/{retries}", file=sys.stderr)
            time.sleep(3 * attempt)
    raise RuntimeError(f"Не вдалося завантажити {url}")


def scrape(channel: str, max_pages: int | None, delay: float, since: str | None = None) -> list[dict]:
    """since - 'YYYY-MM-DD': зупинитися, щойно сторінка дійде до старіших постів."""
    session = requests.Session()
    session.headers.update(HEADERS)

    by_id: dict[int, dict] = {}
    before: int | None = None
    page = 0
    while max_pages is None or page < max_pages:
        url = BASE_URL.format(channel=channel)
        if before is not None:
            url += f"?before={before}"
        posts = parse_page(fetch(session, url), channel)
        posts = [p for p in posts if p["id"] not in by_id]
        if not posts:
            break
        page += 1
        before = min(p["id"] for p in posts)
        reached_since = False
        if since:
            reached_since = any(p["date"] and p["date"][:10] < since for p in posts)
            posts = [p for p in posts if not p["date"] or p["date"][:10] >= since]
        for p in posts:
            by_id[p["id"]] = p
        print(f"сторінка {page}: +{len(posts)} постів (всього {len(by_id)}), найстаріший id {before}")
        if before <= 1 or reached_since:
            break
        time.sleep(delay)

    return sorted(by_id.values(), key=lambda p: p["id"])


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("channel", help="username публічного каналу без @, напр. signalman_diary")
    parser.add_argument("--max-pages", type=int, default=None, help="обмежити кількість сторінок (по ~20 постів)")
    parser.add_argument("--delay", type=float, default=1.0, help="пауза між запитами, секунд (за замовчуванням 1.0)")
    parser.add_argument("--min-chars", type=int, default=1, help="у clean-файл потрапляють пости з текстом не коротшим за N символів")
    parser.add_argument("--clean-only", action="store_true", help="не завантажувати, а переочистити вже збережений raw-файл")
    parser.add_argument("--since", default=None, help="збирати лише пости з цієї дати, YYYY-MM-DD")
    args = parser.parse_args()

    raw_path = ROOT / "data" / "raw" / f"{args.channel}.jsonl"
    if args.clean_only:
        with raw_path.open(encoding="utf-8") as f:
            posts = [json.loads(line) for line in f]
    else:
        posts = scrape(args.channel, args.max_pages, args.delay, args.since)
        write_jsonl(raw_path, posts)

    clean = [clean_post(p) for p in posts if len(p["text"]) >= args.min_chars]
    clean_path = ROOT / "data" / f"{args.channel}_clean.jsonl"
    write_jsonl(clean_path, clean)

    print(f"\nГотово: {len(posts)} постів -> {raw_path.relative_to(ROOT)}")
    print(f"З текстом (>= {args.min_chars} симв.): {len(clean)} -> {clean_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
