"""Локальний веб-інтерфейс: завантаження постів Telegram-каналів, вижимка, перегляд.

Дані беруться з публічної веб-версії каналів (https://t.me/s/<канал>) через
scripts/scrape_telegram.py. Сервер нічого не вигадує: кожен пост має посилання на
оригінал і час завантаження, а журнал оновлення показує кожну відкриту адресу.

  python app/server.py                 # http://127.0.0.1:8765
  python app/server.py --port 9000
  python app/server.py --host 0.0.0.0  # доступ з телефона в тій самій Wi-Fi мережі

Увага: --host 0.0.0.0 відкриває інтерфейс усім у вашій мережі, входу немає.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import scrape_telegram as tg  # noqa: E402
from src import extract as ex  # noqa: E402
from build_corpus import normalize  # noqa: E402
from discover_channels import TOPICS  # noqa: E402

DATA = ROOT / "data"
CHANNELS_FILE = DATA / "channels.json"
STATE_FILE = DATA / "state.json"
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

OLLAMA_URL = ex.OLLAMA_URL
OLLAMA_MODEL = ex.DEFAULT_MODEL

DEFAULT_CHANNELS = [
    ("ghost_of_novorossia", "ПриZрак Новороссии"),
    ("military_engineer", "Инженерный Z фронт"),
    ("reihatori", "Лина Булатникова: помощь фронту, дроны «Мурз»"),
    ("surf_noise1", "РОКОТ"),
    ("makers2fron", "Умельцы-Фронту"),
    ("vrachivyneodni_channel", "ВРАЧИ, ВЫ НЕ ОДНИ"),
    ("interbrigady2022", "Интербригады 2022"),
    ("ekipadobrovolec", "«Доброволец» Экип Центр"),
    ("signalman_diary", "Дневник связиста Кибы"),
]

app = Flask(__name__, static_folder=None)
file_lock = threading.Lock()


# ---------- конфіг і стан ----------

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_channels() -> list[dict]:
    with file_lock:
        channels = read_json(CHANNELS_FILE, None)
        if channels is None:
            channels = [{"username": u, "title": t, "enabled": True} for u, t in DEFAULT_CHANNELS]
            write_json(CHANNELS_FILE, channels)
        return channels


def save_channels(channels: list[dict]) -> None:
    with file_lock:
        write_json(CHANNELS_FILE, channels)


def update_state(channel: str, **fields) -> None:
    with file_lock:
        state = read_json(STATE_FILE, {})
        state.setdefault(channel, {}).update(fields)
        write_json(STATE_FILE, state)


def valid_username(name: str) -> bool:
    # Ім'я потрапляє в шляхи файлів, тому перевіряємо суворо.
    return bool(USERNAME_RE.match(name or ""))


# ---------- пости ----------

_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def parse_views(value: str | None) -> int:
    if not value:
        return 0
    s = value.strip().upper().replace(" ", "")
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def parse_date(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def get_posts(channel: str) -> list[dict]:
    """Пости каналу з прибраними персональними даними; кеш скидається при зміні файлу."""
    path = tg.source_path(channel)
    if path is None:
        return []
    mtime = path.stat().st_mtime
    with _cache_lock:
        cached = _cache.get(channel)
        if cached and cached[0] == mtime:
            return cached[1]
    posts = []
    for p in tg.load_raw(channel):
        c = tg.clean_post(p)
        posts.append({
            "channel": channel,
            "id": c["id"],
            "url": c["url"],
            "date": c["date"],
            "text": c["text"],
            "views": c.get("views"),
            "views_num": parse_views(c.get("views")),
            "forwarded_from": c.get("forwarded_from"),
            "has_photo": c.get("has_photo", False),
            "has_video": c.get("has_video", False),
            "document": c.get("document"),
            "links": c.get("links", []),
            "fetched_at": c.get("fetched_at"),
        })
    with _cache_lock:
        _cache[channel] = (mtime, posts)
    return posts


def enabled_channels(requested: str | None = None) -> list[str]:
    names = [c["username"] for c in load_channels() if c.get("enabled", True)]
    if requested:
        wanted = [n for n in requested.split(",") if valid_username(n)]
        names = [n for n in wanted if n in {c["username"] for c in load_channels()}]
    return names


def topic_counts(texts: list[str]) -> dict[str, int]:
    lowered = [t.lower() for t in texts]
    return {topic: sum(1 for t in lowered if any(r in t for r in roots)) for topic, roots in TOPICS.items()}


# ---------- завантаження (фонова задача) ----------

job_lock = threading.Lock()
job: dict = {"id": 0, "running": False, "started_at": None, "finished_at": None, "log": [], "results": {}, "channels": []}


def job_log(message: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')}  {message}"
    with job_lock:
        job["log"].append(line)
        del job["log"][:-1500]


def run_job(channels: list[str], days: int) -> None:
    try:
        for ch in channels:
            job_log(f"=== {ch}")
            started = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                result = tg.update_channel(ch, days=days, delay=1.0, log=job_log)
                job_log(f"✓ {ch}: нових {result['added']}, оновлено {result['refreshed']}, всього {result['total']}")
                update_state(ch, last_run_at=started, last_result=result, last_error=None)
                with job_lock:
                    job["results"][ch] = result
            except Exception as exc:  # один канал не повинен зупиняти решту
                job_log(f"✗ {ch}: {type(exc).__name__}: {exc}")
                update_state(ch, last_run_at=started, last_error=f"{type(exc).__name__}: {exc}"[:300])
                with job_lock:
                    job["results"][ch] = {"error": str(exc)[:300]}
    finally:
        with job_lock:
            job["running"] = False
            job["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        job_log("Готово")


# ---------- API ----------

@app.get("/")
def index():
    return send_from_directory(Path(__file__).parent / "static", "index.html")


@app.get("/api/channels")
def api_channels():
    state = read_json(STATE_FILE, {})
    out = []
    for c in load_channels():
        posts = get_posts(c["username"])
        dates = [p["date"] for p in posts if p["date"]]
        st = state.get(c["username"], {})
        out.append({
            **c,
            "stored": len(posts),
            "with_text": sum(1 for p in posts if p["text"]),
            "oldest_post": min(dates) if dates else None,
            "newest_post": max(dates) if dates else None,
            "last_run_at": st.get("last_run_at"),
            "last_result": st.get("last_result"),
            "last_error": st.get("last_error"),
        })
    return jsonify(out)


@app.post("/api/channels")
def api_add_channel():
    name = ((request.get_json(silent=True) or {}).get("username") or "").strip().lstrip("@")
    name = re.sub(r"^https?://t\.me/(s/)?", "", name).split("/")[0]
    if not valid_username(name):
        return jsonify(error="Невірне ім'я. Потрібен username каналу, напр. military_engineer"), 400
    channels = load_channels()
    if any(c["username"].lower() == name.lower() for c in channels):
        return jsonify(error="Цей канал уже в списку"), 409
    try:
        resp = requests.get(f"https://t.me/s/{name}", headers=tg.HEADERS, timeout=20, allow_redirects=False)
    except requests.RequestException as exc:
        return jsonify(error=f"Telegram недоступний: {exc}"[:200]), 502
    title_node = BeautifulSoup(resp.text, "html.parser").select_one(".tgme_channel_info_header_title") if resp.status_code == 200 else None
    if not title_node:
        return jsonify(error="Це не публічний канал: у нього немає веб-версії t.me/s/…"), 404
    channels.append({"username": name, "title": title_node.get_text(" ", strip=True), "enabled": True})
    save_channels(channels)
    return jsonify(ok=True), 201


@app.put("/api/channels/<username>")
def api_toggle_channel(username):
    channels = load_channels()
    for c in channels:
        if c["username"] == username:
            c["enabled"] = bool((request.get_json(silent=True) or {}).get("enabled", True))
            save_channels(channels)
            return jsonify(ok=True)
    return jsonify(error="Не знайдено"), 404


@app.delete("/api/channels/<username>")
def api_delete_channel(username):
    channels = load_channels()
    kept = [c for c in channels if c["username"] != username]
    if len(kept) == len(channels):
        return jsonify(error="Не знайдено"), 404
    save_channels(kept)  # файли з даними не видаляємо
    return jsonify(ok=True)


@app.post("/api/update")
def api_update():
    body = request.get_json(silent=True) or {}
    known = {c["username"] for c in load_channels()}
    channels = [n for n in (body.get("channels") or []) if n in known]
    if not channels:
        return jsonify(error="Не обрано жодного каналу"), 400
    days = max(1, min(int(body.get("days") or 30), 365))
    with job_lock:
        if job["running"]:
            return jsonify(error="Оновлення вже триває"), 409
        job.update(id=job["id"] + 1, running=True, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   finished_at=None, log=[], results={}, channels=channels)
    threading.Thread(target=run_job, args=(channels, days), daemon=True).start()
    return jsonify(ok=True, id=job["id"])


@app.get("/api/job")
def api_job():
    with job_lock:
        return jsonify(job)


@app.get("/api/posts")
def api_posts():
    channels = enabled_channels(request.args.get("channels")) if request.args.get("channels") else [c["username"] for c in load_channels()]
    q = (request.args.get("q") or "").strip().lower()
    days = float(request.args.get("days") or 0)
    sort = request.args.get("sort", "date")
    limit = max(1, min(int(request.args.get("limit") or 30), 200))
    offset = max(0, int(request.args.get("offset") or 0))
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None

    items = []
    for ch in channels:
        for p in get_posts(ch):
            if not p["text"]:
                continue
            if since and (parse_date(p["date"]) or since) < since:
                continue
            if q and q not in p["text"].lower():
                continue
            items.append(p)
    items.sort(key=(lambda p: p["views_num"]) if sort == "views" else (lambda p: p["date"] or ""), reverse=True)
    return jsonify(total=len(items), items=items[offset:offset + limit])


TECH_ROOTS = [root for topic, roots in TOPICS.items() if topic != "fundraising" for root in roots]


def is_tech(text: str) -> bool:
    """Грубий фільтр: у тексті є ключове слово про техніку (дрони, зв'язок, РЕБ, авто, оптика…)."""
    lowered = text.lower()
    return any(root in lowered for root in TECH_ROOTS)


def collect_digest_posts(days: float, channels: list[str], tech_only: bool = False) -> tuple[list[dict], dict]:
    """Пости за період без дублікатів (репостів між каналами) + статистика дублів."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    posts = []
    for ch in channels:
        for p in get_posts(ch):
            d = parse_date(p["date"])
            if d and d >= since and p["text"] and (not tech_only or is_tech(p["text"])):
                posts.append(p)
    posts.sort(key=lambda p: p["date"])
    seen: set[str] = set()
    for p in posts:
        norm = normalize(p["text"])
        p["duplicate"] = False
        if len(norm) >= 40:
            if norm in seen:
                p["duplicate"] = True
            seen.add(norm)
    return posts, {"all": len(posts), "duplicates": sum(1 for p in posts if p["duplicate"])}


def snippet(p: dict, n: int = 280) -> dict:
    text = re.sub(r"\s+", " ", p["text"]).strip()
    return {"channel": p["channel"], "id": p["id"], "url": p["url"], "date": p["date"], "views": p["views"],
            "views_num": p["views_num"], "text": text[:n] + ("…" if len(text) > n else ""),
            "forwarded_from": p["forwarded_from"]}


@app.get("/api/digest")
def api_digest():
    days = float(request.args.get("days") or 1)
    channels = enabled_channels(request.args.get("channels"))
    tech_only = request.args.get("tech_only") == "1"
    posts, dup = collect_digest_posts(days, channels, tech_only)
    unique = [p for p in posts if not p["duplicate"]]

    per_channel = []
    for ch in channels:
        own = [p for p in unique if p["channel"] == ch]
        topics = topic_counts([p["text"] for p in own])
        title = next((c["title"] for c in load_channels() if c["username"] == ch), ch)
        per_channel.append({
            "username": ch, "title": title, "posts": len(own),
            "views_total": sum(p["views_num"] for p in own),
            "reposts": sum(1 for p in own if p["forwarded_from"]),
            "topics": topics,
            "top": [snippet(p) for p in sorted(own, key=lambda p: p["views_num"], reverse=True)[:3]],
        })
    per_channel.sort(key=lambda c: c["posts"], reverse=True)

    dates = [p["date"] for p in posts]
    return jsonify(
        days=days, posts=len(unique), duplicates=dup["duplicates"],
        from_date=min(dates) if dates else None, to_date=max(dates) if dates else None,
        topics=topic_counts([p["text"] for p in unique]),
        top=[snippet(p) for p in sorted(unique, key=lambda p: p["views_num"], reverse=True)[:10]],
        channels=per_channel,
    )


SUMMARY_SYSTEM = (
    "Ти перекладач і стислювач текстів. Нижче пронумеровані пости з Telegram-каналів (російською). "
    "Для КОЖНОГО поста напиши рівно один рядок УКРАЇНСЬКОЮ у форматі: [номер] що саме сказано в пості (до 25 слів). "
    "Правила: один рядок на один пост, у порядку номерів; нічого не об'єднуй і не повторюй; "
    "передавай лише те, що є в тексті цього поста, назви, числа й моделі переписуй як є; "
    "якщо в пості немає конкретики (привітання, емоції), напиши «без конкретики». "
    "Жодних вступів, заголовків і висновків."
)


@app.post("/api/digest/summary")
def api_digest_summary():
    body = request.get_json(silent=True) or {}
    days = float(body.get("days") or 1)
    channels = enabled_channels(",".join(body.get("channels") or []) or None)
    posts, _ = collect_digest_posts(days, channels, bool(body.get("tech_only")))
    pool = [p for p in posts if not p["duplicate"] and len(p["text"]) >= 80]
    top = sorted(pool, key=lambda p: p["views_num"], reverse=True)[:12]
    if not top:
        return jsonify(error="За цей період немає постів для вижимки. Спершу натисніть «Оновити»."), 404

    sources = [{"n": i, "url": p["url"], "channel": p["channel"], "date": p["date"]} for i, p in enumerate(top, 1)]
    numbered = "\n\n".join(
        f"[{i}] ({p['channel']}, {p['date'][:10]})\n{re.sub(chr(10) + '+', chr(10), p['text'])[:700]}" for i, p in enumerate(top, 1)
    )
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": numbered + "\n\nТепер напиши по одному рядку на кожен пост. Мова відповіді: УКРАЇНСЬКА (не російська)."},
        ],
        "think": False, "stream": False,
        "options": {"temperature": 0.1, "repeat_penalty": 1.1, "num_ctx": 8192, "num_predict": 900},
    }
    t0 = time.perf_counter()
    try:
        resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=300)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return jsonify(error=f"Локальна модель недоступна ({OLLAMA_URL}): {exc}"[:300]), 503
    return jsonify(
        summary=resp.json()["message"]["content"].strip(), sources=sources, model=OLLAMA_MODEL,
        seconds=round(time.perf_counter() - t0, 1), posts_used=len(top),
    )


# ---------- етап 2: пост -> JSON ----------

def find_post(channel: str, post_id: int) -> dict | None:
    if not valid_username(channel) or channel not in {c["username"] for c in load_channels()}:
        return None
    return next((p for p in get_posts(channel) if p["id"] == post_id), None)


def decorate_run(run: dict, golden: dict) -> dict:
    """Додає порівняння з еталоном, якщо для цього поста є розмітка golden set."""
    gold = golden.get((run.get("channel"), run.get("post_id")))
    if gold:
        rows = ex.compare_to_golden(run.get("output"), gold["label"])
        run["golden"] = {"status": gold["status"], "rows": rows, "score": round(sum(r["score"] for r in rows) / len(rows), 3)}
    return run


@app.get("/api/prompts")
def api_prompts():
    return jsonify(prompts=ex.list_prompts(), model=ex.DEFAULT_MODEL, models=ex.list_models())


@app.get("/api/sources")
def api_sources():
    kind = request.args.get("kind", "golden")
    golden = ex.load_golden()
    run_counts: dict[tuple, int] = {}
    for r in ex.load_runs():
        key = (r.get("channel"), r.get("post_id"))
        run_counts[key] = run_counts.get(key, 0) + 1

    if kind == "golden":
        posts = [find_post(ch, pid) for ch, pid in sorted(golden)]
    elif kind == "dataset":
        ids = []
        with (DATA / "dataset.jsonl").open(encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                m = re.search(r"t\.me/([^/]+)/(\d+)", row["url"])
                if m:
                    ids.append((m.group(1), int(m.group(2))))
        posts = [find_post(ch, pid) for ch, pid in ids]
    else:  # recent: свіжі пости про техніку без репостів між каналами
        recent, _ = collect_digest_posts(3, [c["username"] for c in load_channels()], tech_only=True)
        posts = [p for p in reversed(recent) if not p["duplicate"] and len(p["text"]) >= 60][:40]

    items = []
    for p in posts:
        if not p:
            continue
        key = (p["channel"], p["id"])
        items.append({
            "channel": p["channel"], "id": p["id"], "date": p["date"],
            "snippet": re.sub(r"\s+", " ", p["text"])[:130],
            "golden": key in golden, "runs": run_counts.get(key, 0),
        })
    return jsonify(items)


@app.get("/api/post")
def api_post():
    post = find_post(request.args.get("channel", ""), int(request.args.get("id") or 0))
    if not post:
        return jsonify(error="Пост не знайдено серед збережених"), 404
    return jsonify(post)


@app.get("/api/extractions")
def api_extractions():
    channel, post_id = request.args.get("channel", ""), int(request.args.get("id") or 0)
    golden = ex.load_golden()
    return jsonify([decorate_run(r, golden) for r in ex.load_runs(channel, post_id)])


@app.post("/api/extract")
def api_extract():
    body = request.get_json(silent=True) or {}
    channel, post_id = body.get("channel", ""), int(body.get("id") or 0)
    post = find_post(channel, post_id)
    if not post:
        return jsonify(error="Пост не знайдено серед збережених"), 404
    model = str(body.get("model") or ex.DEFAULT_MODEL)
    if model not in ex.MODELS:
        return jsonify(error=f"Модель {model} не в списку OLLAMA_MODELS (.env)"), 400
    try:
        run = ex.extract(
            post["text"], str(body.get("prompt_version") or "v2"), model=model,
            temperature=min(max(float(body.get("temperature", 0)), 0.0), 2.0),
            top_p=min(max(float(body.get("top_p", 0.9)), 0.0), 1.0),
            meta={"channel": channel, "post_id": post_id, "post_url": post["url"]},
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)[:300]), 503
    return jsonify(decorate_run(run, ex.load_golden()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    load_channels()
    print(f"Відкрийте http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
