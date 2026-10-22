"""Збирає тексти статей за посиланнями з постів каналу.

Бере посилання (`links` і `link_preview.url`) з data/<channel>_clean.jsonl, відкидає
відео, файлообмінники й магазини, завантажує лише HTML-сторінки та витягує з них
основний текст через trafilatura (без меню, реклами й коментарів).

Файли (PDF, архіви, apk тощо) скрипт НЕ завантажує: Content-Type перевіряється
до читання тіла відповіді, а розмір сторінки обмежено.

  python scripts/fetch_articles.py signalman_diary
  python scripts/fetch_articles.py signalman_diary --limit 5      # швидка перевірка

Результат: data/<channel>_articles.jsonl - по рядку на URL, включно з невдалими
(поле error), щоб було видно, що й чому не зібралося.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import charset_normalizer
import requests
import trafilatura

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape_telegram import _host, clean_pii  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"User-Agent": "Mozilla/5.0 (course homework scraper)"}
MAX_BYTES = 5 * 1024 * 1024

# Тексту статті там немає: відео, файлообмінники, магазини, сторінки соцмереж.
SKIP_HOSTS = (
    "rutube.ru", "youtube.com", "youtu.be", "vkvideo.ru", "video.oper.ru", "dzen.ru", "max.ru", "vk.com",
    "disk.yandex.ru", "yadi.sk", "drive.google.com", "cloud.mail.ru", "mega.nz", "dropbox.com",
    "aliexpress.ru", "aliexpress.com", "ozon.ru", "wildberries.ru", "market.yandex.ru", "avito.ru",
)
SKIP_EXTENSIONS = (".pdf", ".zip", ".rar", ".7z", ".apk", ".exe", ".doc", ".docx", ".xls", ".xlsx",
                   ".djvu", ".torrent", ".mp4", ".mp3", ".jpg", ".jpeg", ".png", ".gif")


def is_article_candidate(url: str) -> bool:
    host = _host(url)
    if any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
        return False
    path = url.split("?", 1)[0].lower()
    return not path.endswith(SKIP_EXTENSIONS)


def normalize_url(url: str) -> str:
    """Кириличний домен у вигляді '%D0%B1...' -> punycode (xn--...), інакше requests його не знайде."""
    parts = urlsplit(url)
    host = unquote(parts.netloc)
    if host != parts.netloc:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return url
        return urlunsplit(parts._replace(netloc=host))
    return url


def collect_urls(posts: list[dict]) -> dict[str, list[int]]:
    """URL -> id постів, де він згадується (у порядку першої появи)."""
    urls: dict[str, list[int]] = {}
    for p in posts:
        candidates = list(p.get("links", []))
        if p.get("link_preview"):
            candidates.append(p["link_preview"]["url"])
        for url in candidates:
            url = url and normalize_url(url)
            if url and is_article_candidate(url):
                ids = urls.setdefault(url, [])
                if p["id"] not in ids:
                    ids.append(p["id"])
    return urls


def fetch_html(session: requests.Session, url: str, timeout: int = 20) -> tuple[str, str]:
    """Повертає (final_url, html). Кидає ValueError, якщо це не HTML або сторінка завелика."""
    with session.get(url, timeout=timeout, stream=True, allow_redirects=True) as resp:
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "").lower()
        if "html" not in ctype:
            raise ValueError(f"не HTML: {ctype or 'Content-Type відсутній'}")
        body = b""
        for chunk in resp.iter_content(64 * 1024):
            body += chunk
            if len(body) > MAX_BYTES:
                raise ValueError(f"сторінка більша за {MAX_BYTES // 1024 // 1024} MB")
        # requests підставляє iso-8859-1, якщо charset не вказано; тоді визначаємо кодування за байтами.
        encoding = resp.encoding if resp.encoding and resp.encoding.lower() != "iso-8859-1" else None
        if encoding is None:
            best = charset_normalizer.from_bytes(body).best()
            encoding = best.encoding if best else "utf-8"
        return resp.url, body.decode(encoding, errors="replace")


def process(session: requests.Session, url: str, post_ids: list[int], retries: int) -> dict:
    row = {
        "url": url,
        "final_url": None,
        "host": _host(url),
        "source_posts": post_ids,
        "title": None,
        "date": None,
        "text": "",
        "chars": 0,
        "error": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for attempt in range(1, retries + 1):
        try:
            final_url, html = fetch_html(session, url)
            row["final_url"] = final_url
            extracted = trafilatura.bare_extraction(html, url=final_url, include_comments=False, with_metadata=True)
            data = extracted.as_dict() if hasattr(extracted, "as_dict") else (extracted or {})
            text = (data.get("text") or "").strip()
            if not text:
                row["error"] = "текст не знайдено"
            row["title"] = clean_pii(data["title"]) if data.get("title") else None
            row["date"] = data.get("date")
            row["text"] = clean_pii(text)
            row["chars"] = len(row["text"])
            return row
        except ValueError as exc:  # не HTML / завелике - повторювати немає сенсу
            row["error"] = str(exc)
            return row
        except requests.RequestException as exc:
            if "NameResolutionError" in str(exc) or "getaddrinfo failed" in str(exc):
                # Повтор не допоможе: домен не резолвиться (заблокований у вашій мережі або не існує).
                row["error"] = "DNS: домен не знайдено (заблоковано у вашій мережі або сайт не існує)"
                return row
            row["error"] = f"{type(exc).__name__}: {exc}"[:300]
            if attempt < retries:
                time.sleep(2 * attempt)
        except Exception as exc:  # одна зламана сторінка не повинна зупиняти весь збір
            row["error"] = f"{type(exc).__name__}: {exc}"[:300]
            return row
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("channel", help="username каналу без @, напр. signalman_diary")
    parser.add_argument("--limit", type=int, default=None, help="обробити лише перші N посилань")
    parser.add_argument("--delay", type=float, default=1.0, help="пауза між запитами, секунд")
    parser.add_argument("--retries", type=int, default=2, help="спроб на мережеву помилку")
    args = parser.parse_args()

    source = ROOT / "data" / f"{args.channel}_clean.jsonl"
    with source.open(encoding="utf-8") as f:
        posts = [json.loads(line) for line in f]

    urls = collect_urls(posts)
    items = list(urls.items())[: args.limit] if args.limit else list(urls.items())
    print(f"Посилань-кандидатів: {len(urls)}, обробляю: {len(items)}")

    session = requests.Session()
    session.headers.update(HEADERS)

    rows = []
    for i, (url, post_ids) in enumerate(items, 1):
        row = process(session, url, post_ids, args.retries)
        rows.append(row)
        status = f"{row['chars']} симв." if not row["error"] else f"помилка: {row['error'][:80]}"
        print(f"[{i}/{len(items)}] {row['host']}: {status}")
        time.sleep(args.delay)

    out = ROOT / "data" / f"{args.channel}_articles.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    ok = [r for r in rows if r["chars"] > 0]
    print(f"\nГотово: {len(ok)}/{len(rows)} з текстом ({sum(r['chars'] for r in ok)} симв.) -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
