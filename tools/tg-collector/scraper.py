"""Завантаження постів публічних Telegram-каналів з веб-версії t.me/s/<канал>.

Акаунт і API-ключі не потрібні. Працює лише з публічними каналами
(у приватних і в груп веб-версії немає).

Як знаходимо період:
  - найстаріші пости:  t.me/s/<канал>?after=1
  - найновіші пости:   t.me/s/<канал>
  - сторінка «до id»:  t.me/s/<канал>?before=<id>  (~20 постів з меншими id)
Id постів зростають з часом, тому кінець періоду знаходимо бінарним пошуком по id,
а далі гортаємо назад, поки не дійдемо до початку періоду.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://t.me/s/{channel}"
HEADERS = {"User-Agent": "Mozilla/5.0 (tg-collector; public channel archiver)"}
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
BG_URL_RE = re.compile(r"background-image:\s*url\(['\"]?([^'\")]+)['\"]?\)")


class Stopped(Exception):
    """Користувач натиснув «Зупинити»."""


# ---------- посилання на канал ----------

def parse_channel_ref(ref: str) -> str:
    """'https://t.me/name', 't.me/s/name/123', '@name', 'name' -> 'name'.

    Кидає ValueError з поясненням, якщо це не публічний канал.
    """
    s = (ref or "").strip()
    if not s:
        raise ValueError("порожній рядок")
    if s.startswith("@"):
        s = s[1:]
    elif "t.me/" in s or "telegram.me/" in s:
        if "://" not in s:
            s = "https://" + s
        parts = [p for p in urlparse(s).path.split("/") if p]
        if parts and parts[0] == "s":
            parts = parts[1:]
        if not parts:
            raise ValueError(f"{ref}: у посиланні немає імені каналу")
        if parts[0] in ("addlist", "joinchat") or parts[0].startswith("+"):
            raise ValueError(f"{ref}: це запрошення в папку або приватний чат — веб-версія їх не відкриває, додайте канали по одному")
        s = parts[0]
    if not USERNAME_RE.match(s):
        raise ValueError(f"{ref}: не схоже на ім'я публічного каналу")
    return s


# ---------- розбір сторінки ----------

def _text(node) -> str:
    for br in node.find_all("br"):
        br.replace_with("\n")
    return node.get_text().strip()


def parse_page(html: str, channel: str) -> tuple[dict, list[dict]]:
    """Повертає (інформація про канал, пости на сторінці)."""
    soup = BeautifulSoup(html, "html.parser")

    def one(sel):
        n = soup.select_one(sel)
        return n.get_text(" ", strip=True) if n else None

    info = {
        "title": one(".tgme_channel_info_header_title"),
        "description": one(".tgme_channel_info_description"),
        "subscribers": None,
    }
    for counter in soup.select(".tgme_channel_info_counter"):
        kind = counter.select_one(".counter_type")
        if kind and "subscriber" in kind.get_text():
            info["subscribers"] = counter.select_one(".counter_value").get_text(strip=True)

    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        post_id = int(msg["data-post"].split("/")[-1])

        # Власний текст поста, а не цитата з reply-блоку.
        text_node = None
        for node in msg.select("div.tgme_widget_message_text"):
            if not node.find_parent(class_="tgme_widget_message_reply"):
                text_node = node
                break

        links: list[str] = []
        if text_node:
            for a in text_node.select("a[href]"):
                href = a["href"]
                if href.startswith(("http://", "https://")) and href not in links:
                    links.append(href)

        photo_urls = []
        for wrap in msg.select(".tgme_widget_message_photo_wrap"):
            m = BG_URL_RE.search(wrap.get("style", ""))
            if m and m.group(1) not in photo_urls:
                photo_urls.append(m.group(1))

        preview = None
        pnode = msg.select_one("a.tgme_widget_message_link_preview")
        if pnode:
            def ptxt(sel):
                n = pnode.select_one(sel)
                return n.get_text(" ", strip=True) if n else None
            preview = {
                "url": pnode.get("href"),
                "site": ptxt(".link_preview_site_name"),
                "title": ptxt(".link_preview_title"),
                "description": ptxt(".link_preview_description"),
            }

        doc = None
        dnode = msg.select_one(".tgme_widget_message_document")
        if dnode:
            t = dnode.select_one(".tgme_widget_message_document_title")
            e = dnode.select_one(".tgme_widget_message_document_extra")
            doc = {"title": t.get_text(strip=True) if t else None, "size": e.get_text(strip=True) if e else None}

        time_node = msg.select_one("a.tgme_widget_message_date time")
        views = msg.select_one("span.tgme_widget_message_views")
        fwd = msg.select_one(".tgme_widget_message_forwarded_from_name")
        posts.append({
            "id": post_id,
            "url": f"https://t.me/{channel}/{post_id}",
            "date": time_node["datetime"] if time_node else None,
            "text": _text(text_node) if text_node else "",
            "views": views.get_text(strip=True) if views else None,
            "forwarded_from": (fwd.get_text(strip=True) or None) if fwd else None,
            "has_photo": bool(photo_urls),
            "has_video": bool(msg.select_one(".tgme_widget_message_video_player")),
            "photo_urls": photo_urls,
            "links": links,
            "link_preview": preview,
            "document": doc,
        })
    return info, posts


# ---------- завантаження ----------

class Fetcher:
    def __init__(self, delay: float = 1.0, log=print, stop: threading.Event | None = None):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay = delay
        self.log = log
        self.stop = stop or threading.Event()
        self._last = 0.0

    def _wait(self, delay: float):
        pause = delay - (time.monotonic() - self._last)
        while pause > 0:
            if self.stop.is_set():
                raise Stopped()
            time.sleep(min(pause, 0.2))
            pause = delay - (time.monotonic() - self._last)
        if self.stop.is_set():
            raise Stopped()

    def get(self, url: str, binary: bool = False, retries: int = 3, delay: float | None = None):
        for attempt in range(1, retries + 1):
            self._wait(self.delay if delay is None else delay)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 10 * attempt))
                    self.log(f"  Telegram просить зачекати (429): {wait} с")
                    self._last = time.monotonic() + wait
                    continue
                resp.raise_for_status()
                return resp.content if binary else resp.text
            except requests.RequestException as exc:
                if attempt == retries:
                    raise
                self.log(f"  помилка ({exc}), спроба {attempt + 1}/{retries}")
                self._last = time.monotonic() + 3 * attempt
        raise RuntimeError(f"Не вдалося завантажити {url}")

    def page(self, channel: str, before: int | None = None, after: int | None = None):
        url = BASE_URL.format(channel=channel)
        if before is not None:
            url += f"?before={before}"
        elif after is not None:
            url += f"?after={after}"
        return parse_page(self.get(url), channel)


def channel_info(f: Fetcher, channel: str) -> dict:
    """Назва, підписники, перший і останній пост каналу (2 запити)."""
    info, newest = f.page(channel)
    if not newest and not info["title"]:
        raise ValueError(f"@{channel}: канал не знайдено або він не публічний")
    _, oldest = f.page(channel, after=1)
    dated_new = [p for p in newest if p["date"]]
    dated_old = [p for p in oldest if p["date"]] or dated_new
    last = max(dated_new, key=lambda p: p["id"]) if dated_new else None
    first = min(dated_old, key=lambda p: p["id"]) if dated_old else None
    return {
        "username": channel,
        "title": info["title"] or channel,
        "description": info["description"],
        "subscribers": info["subscribers"],
        "first_id": first and first["id"],
        "first_date": first and first["date"][:10],
        "last_id": last and last["id"],
        "last_date": last and last["date"][:10],
        "checked_at": now_iso(),
    }


def find_before_id(f: Fetcher, channel: str, last_id: int, date_to: str) -> int:
    """Найменший id поста, новішого за date_to (YYYY-MM-DD, включно).

    Сторінка ?before=<цей id> починається з останнього поста періоду.
    """
    lo, hi = 0, last_id + 1  # пости з id <= lo — не новіші за date_to; пост hi — новіший
    while hi - lo > 1:
        mid = (lo + hi) // 2
        _, posts = f.page(channel, before=mid + 1)
        posts = [p for p in posts if p["date"]]
        if not posts:
            lo = mid
            continue
        newer = [p["id"] for p in posts if p["date"][:10] > date_to]
        older = [p["id"] for p in posts if p["date"][:10] <= date_to]
        if not newer:
            lo = mid
        else:
            hi = min(newer)
            if older:
                lo = max(older)
                break
        f.log(f"  шукаю кінець періоду: id між {lo} і {hi}")
    return hi


# ---------- збереження ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def folder_stats(folder: Path) -> dict | None:
    posts_file = folder / "posts.jsonl"
    if not posts_file.exists():
        return None
    count, dates = 0, []
    with posts_file.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                count += 1
                d = json.loads(line).get("date")
                if d:
                    dates.append(d[:10])
    media = folder / "media"
    return {
        "posts": count,
        "from": min(dates) if dates else None,
        "to": max(dates) if dates else None,
        "photos": sum(1 for _ in media.iterdir()) if media.exists() else 0,
    }


# Один замок на всі posts.jsonl: перегляд не читає файл посеред запису.
FILE_LOCK = threading.RLock()


def read_meta(folder: Path) -> dict:
    meta_file = folder / "channel.json"
    if not meta_file.exists():
        return {}
    return json.loads(meta_file.read_text(encoding="utf-8"))


def attach_photos(folder: Path, p: dict) -> None:
    files = [f"media/{p['id']}_{i}.jpg" for i in range(1, len(p.get("photo_urls") or []) + 1)]
    files = [x for x in files if (folder / x).exists()]
    if files:
        p["photos"] = files
    else:
        p.pop("photos", None)


class ChannelTask:
    """Завантаження одного каналу кроками, щоб кілька каналів можна було качати по черзі.

    start()        - інформація про канал і пошук кінця періоду
    step()         - одна сторінка (~20 постів); повертає False, коли період пройдено
    photo_queue()  - фото, яких ще немає на диску
    save()         - дописати зібране в posts.jsonl (викликається періодично й наприкінці)

    Файли в out_dir:
      posts.jsonl  - усі зібрані пости каналу (нові додаються, повтори оновлюються)
      channel.json - опис каналу та історія завантажень
      media/       - фото <id>_<n>.jpg
    """

    SAVE_EVERY = 25  # сторінок

    def __init__(self, f: Fetcher, channel: str, out_dir: Path, date_from=None, date_to=None, photos=False):
        self.f, self.channel, self.out_dir = f, channel, out_dir
        self.date_from, self.date_to, self.photos = date_from, date_to, photos
        self.got: dict[int, dict] = {}
        self.unsaved = 0
        self.pages = 0
        self.before: int | None = None
        self.oldest: str | None = None
        self.done = False
        self.info: dict | None = None
        self.added = 0
        self.photos_done = 0
        self.photos_total = 0

    def log(self, msg: str):
        self.f.log(f"@{self.channel}: {msg}")

    def start(self) -> dict:
        self.info = info = channel_info(self.f, self.channel)
        if info["last_id"] is None:
            self.log("у каналі немає постів")
            self.done = True
            return info
        if self.date_to and self.date_to >= (info["last_date"] or ""):
            self.date_to = None
        if self.date_from and self.date_from <= (info["first_date"] or ""):
            self.date_from = None
        if self.date_to:
            self.log(f"шукаю останній пост до {self.date_to} включно")
            self.before = find_before_id(self.f, self.channel, info["last_id"], self.date_to)
        self.span_hi = self.before or info["last_id"] + 1
        self.span_lo = info["first_id"] or 1
        return info

    @property
    def progress(self) -> float:
        if self.done:
            return 1.0
        if self.before is None or self.info is None or not self.pages:
            return 0.0
        return min((self.span_hi - self.before) / max(self.span_hi - self.span_lo, 1), 1.0)

    def step(self) -> bool:
        if self.done:
            return False
        _, posts = self.f.page(self.channel, before=self.before)
        posts = [p for p in posts if p["id"] not in self.got and (self.before is None or p["id"] < self.before)]
        if not posts:
            self.done = True
        else:
            self.pages += 1
            self.before = min(p["id"] for p in posts)
            reached_from = False
            for p in posts:
                d = (p["date"] or "")[:10]
                if self.date_to and d > self.date_to:
                    continue
                if self.date_from and d and d < self.date_from:
                    reached_from = True
                    continue
                self.got[p["id"]] = p
                self.unsaved += 1
            self.oldest = min((p["date"] or "")[:10] for p in posts)
            self.log(f"сторінка {self.pages}: до {self.oldest}, зібрано {len(self.got)}")
            if reached_from or self.before <= 1:
                self.done = True
        if self.done or self.pages % self.SAVE_EVERY == 0:
            self.save()
        return not self.done

    def save(self, final: bool = False) -> None:
        """Зливає зібране з posts.jsonl. Поле photos — фото, які вже лежать у media/."""
        if not self.unsaved and not final:
            return
        with FILE_LOCK:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            posts_file = self.out_dir / "posts.jsonl"
            existing = {p["id"]: p for p in read_jsonl(posts_file)}
            fetched_at = now_iso()
            for p in self.got.values():
                p.setdefault("fetched_at", fetched_at)
                old = existing.get(p["id"])
                if old is None:
                    self.added += 1
                existing[p["id"]] = p
            for p in existing.values():
                attach_photos(self.out_dir, p)
            write_jsonl(posts_file, sorted(existing.values(), key=lambda p: p["id"]))
            self.unsaved = 0

    def photo_queue(self) -> list[tuple[str, Path]]:
        if not self.photos:
            return []
        media = self.out_dir / "media"
        queue = []
        for pid in sorted(self.got):
            for i, url in enumerate(self.got[pid].get("photo_urls") or [], 1):
                target = media / f"{pid}_{i}.jpg"
                if not target.exists():
                    queue.append((url, target))
        return queue

    def download_photo(self, url: str, target: Path, delay: float) -> bool:
        target.parent.mkdir(exist_ok=True)
        try:
            data = self.f.get(url, binary=True, delay=delay)
        except Stopped:
            raise
        except Exception as exc:  # noqa: BLE001 — одне фото не має зупиняти все
            self.log(f"фото {target.name} не завантажилось: {exc}")
            return False
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        self.photos_done += 1
        return True

    def finish(self) -> dict:
        self.save(final=True)
        info = self.info or {}
        with FILE_LOCK:
            meta = read_meta(self.out_dir)
            meta.update(info)
            meta.setdefault("downloads", []).append({
                "at": now_iso(),
                "from": self.date_from or info.get("first_date"),
                "to": self.date_to or info.get("last_date"),
                "complete": self.done,
                "posts": len(self.got), "added": self.added,
                "photos": self.photos_done if self.photos else None,
            })
            write_json(self.out_dir / "channel.json", meta)
        return {"posts": len(self.got), "added": self.added, "photos": self.photos_done, "complete": self.done}
