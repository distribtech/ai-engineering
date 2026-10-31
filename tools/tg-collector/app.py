"""TG Collector — веб-застосунок для завантаження сирих постів публічних Telegram-каналів.

  python app.py                  # http://127.0.0.1:8770
  python app.py --port 9000
  python app.py --host 0.0.0.0   # доступ з інших пристроїв у мережі (входу немає!)

Результат: <output>/<папка>/<канал>/posts.jsonl, channel.json, media/
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import scraper as tg

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
FOLDER_RE = re.compile(r"^[\w\- .]{1,60}$")

app = Flask(__name__, static_folder=None)
lock = threading.Lock()


# ---------- конфіг ----------

def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    cfg.setdefault("output_dir", "output")
    cfg.setdefault("channels", [])
    return cfg


def save_config(cfg: dict) -> None:
    tg.write_json(CONFIG_FILE, cfg)


def output_root(cfg: dict) -> Path:
    p = Path(cfg["output_dir"]).expanduser()
    return p if p.is_absolute() else ROOT / p


def channel_dir(cfg: dict, ch: dict) -> Path:
    return output_root(cfg) / ch["folder"] / ch["username"]


def clean_folder(name: str) -> str:
    name = (name or "").strip() or "default"
    if not FOLDER_RE.match(name) or name.strip(".") == "":
        raise ValueError("назва папки: лише літери, цифри, пробіл, _ - .")
    return name


# ---------- фонове завдання ----------

class Job:
    def __init__(self):
        self.running = False
        self.stop = threading.Event()
        self.log: list[str] = []
        self.progress = 0.0
        self.note = ""
        self.results: dict[str, dict] = {}

    def write(self, line: str):
        with lock:
            self.log.append(time.strftime("%H:%M:%S ") + line)
            del self.log[:-500]

    def state(self, since: int = 0) -> dict:
        with lock:
            return {
                "running": self.running, "progress": self.progress, "note": self.note,
                "log": self.log[since:], "log_len": len(self.log), "results": self.results,
            }


job = Job()


def run_job(usernames: list[str], date_from, date_to, photos: bool, delay: float):
    """Качає вибрані канали по черзі: сторінка з першого, сторінка з другого і т. д.

    Етапи: 1) інформація про кожен канал, 2) тексти по колу, 3) фото по колу.
    Що б не сталося (зупинка, помилка), зібране зберігається у finish().
    """
    f = tg.Fetcher(delay=delay, log=job.write, stop=job.stop)
    cfg = load_config()
    by_name = {c["username"]: c for c in cfg["channels"]}
    tasks: list[tg.ChannelTask] = []
    status = job.results

    def st(task, **fields):
        with lock:
            status[task.channel].update(fields)

    def text_progress():
        live = [t for t in tasks if status[t.channel]["stage"] != "помилка"]
        return sum(t.progress for t in live) / max(len(live), 1)

    try:
        # 1. інформація про канали
        for u in usernames:
            ch = by_name.get(u)
            if ch is None:
                continue
            task = tg.ChannelTask(f, u, channel_dir(cfg, ch), date_from, date_to, photos)
            status[u] = {"title": ch.get("title") or u, "folder": ch["folder"], "stage": "перевірка",
                         "posts": 0, "oldest": None, "photos_done": 0, "photos_total": 0, "error": None}
            job.note = f"Перевіряю @{u}"
            try:
                info = task.start()
                with lock:
                    cfg2 = load_config()
                    for c in cfg2["channels"]:
                        if c["username"] == u:
                            c.update({k: info[k] for k in ("title", "subscribers", "first_id", "first_date",
                                                           "last_id", "last_date", "checked_at")})
                    save_config(cfg2)
                tasks.append(task)
                st(task, stage="тексти" if not task.done else "готово", title=info["title"])
            except tg.Stopped:
                raise
            except Exception as exc:  # noqa: BLE001
                st(task, stage="помилка", error=str(exc))
                job.write(f"@{u}: ПОМИЛКА {exc}")

        # 2. тексти по колу
        active = [t for t in tasks if not t.done]
        if len(active) > 1:
            job.write(f"Качаю {len(active)} канали по черзі, по сторінці (~20 постів) з кожного")
        while active:
            for task in list(active):
                try:
                    more = task.step()
                except tg.Stopped:
                    raise
                except Exception as exc:  # noqa: BLE001
                    st(task, stage="помилка", error=str(exc))
                    job.write(f"@{task.channel}: ПОМИЛКА {exc} — збережено те, що встигли")
                    task.save()
                    more = False
                    active.remove(task)
                    continue
                st(task, posts=len(task.got), oldest=task.oldest)
                if not more:
                    active.remove(task)
                    st(task, stage="фото" if photos else "готово")
                job.progress = text_progress() * (0.5 if photos else 1.0)
                job.note = f"Тексти: {sum(len(t.got) for t in tasks)} постів, активних каналів {len(active)}"

        # 3. фото по колу
        if photos:
            queues = {t.channel: t.photo_queue() for t in tasks if status[t.channel]["stage"] == "фото"}
            for t in tasks:
                if t.channel in queues:
                    st(t, photos_total=len(queues[t.channel]))
                    if not queues[t.channel]:
                        st(t, stage="готово")
            total = sum(len(q) for q in queues.values())
            job.write(f"Фото: {total} нових файлів")
            done = 0
            photo_delay = max(0.2, delay / 4)
            by_task = {t.channel: t for t in tasks}
            while any(queues.values()):
                for u, q in queues.items():
                    if not q:
                        continue
                    url, target = q.pop(0)
                    task = by_task[u]
                    task.download_photo(url, target, photo_delay)
                    done += 1
                    st(task, photos_done=task.photos_done)
                    if not q:
                        st(task, stage="готово")
                    if done % 25 == 0 or done == total:
                        job.write(f"фото {done}/{total}")
                    job.progress = 0.5 + 0.5 * done / max(total, 1)
                    job.note = f"Фото: {done} з {total}"
        job.progress, job.note = 1.0, "Готово"
        job.write("Готово.")
    except tg.Stopped:
        job.note = "Зупинено"
        job.write("Зупинено. Вже завантажене збережено.")
    finally:
        for task in tasks:
            try:
                task.finish()
            except Exception as exc:  # noqa: BLE001
                job.write(f"@{task.channel}: не вдалося зберегти: {exc}")
            if status[task.channel]["stage"] not in ("готово", "помилка"):
                st(task, stage="зупинено")
        job.running = False


# ---------- API ----------

def channel_view(cfg: dict, ch: dict) -> dict:
    return {**ch, "stats": tg.folder_stats(channel_dir(cfg, ch)), "path": str(channel_dir(cfg, ch))}


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/api/state")
def api_state():
    cfg = load_config()
    return jsonify({
        "output_dir": cfg["output_dir"],
        "output_abs": str(output_root(cfg)),
        "channels": [channel_view(cfg, c) for c in cfg["channels"]],
    })


@app.post("/api/channels")
def api_add():
    body = request.get_json(force=True)
    try:
        folder = clean_folder(body.get("folder"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    refs = [r for r in re.split(r"[\s,]+", body.get("refs", "")) if r]
    added, errors = [], []
    f = tg.Fetcher(delay=0.5)
    for ref in refs:
        try:
            username = tg.parse_channel_ref(ref)
            cfg = load_config()
            if any(c["username"].lower() == username.lower() for c in cfg["channels"]):
                errors.append(f"@{username}: вже є у списку")
                continue
            info = tg.channel_info(f, username)
            with lock:
                cfg = load_config()
                cfg["channels"].append({**{k: v for k, v in info.items() if k != "description"}, "folder": folder})
                save_config(cfg)
            added.append(username)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    return jsonify({"added": added, "errors": errors})


@app.patch("/api/channels/<username>")
def api_edit(username):
    body = request.get_json(force=True)
    with lock:
        cfg = load_config()
        ch = next((c for c in cfg["channels"] if c["username"] == username), None)
        if ch is None:
            return jsonify({"error": "немає такого каналу"}), 404
        if "folder" in body:
            try:
                ch["folder"] = clean_folder(body["folder"])
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
        save_config(cfg)
    return jsonify({"ok": True})


@app.delete("/api/channels/<username>")
def api_delete(username):
    with lock:
        cfg = load_config()
        cfg["channels"] = [c for c in cfg["channels"] if c["username"] != username]
        save_config(cfg)
    return jsonify({"ok": True, "note": "файли на диску не видалено"})


@app.post("/api/settings")
def api_settings():
    body = request.get_json(force=True)
    out = (body.get("output_dir") or "").strip()
    if not out:
        return jsonify({"error": "вкажіть папку"}), 400
    with lock:
        cfg = load_config()
        cfg["output_dir"] = out
        save_config(cfg)
    return jsonify({"ok": True})


@app.post("/api/jobs")
def api_start():
    body = request.get_json(force=True)
    if job.running:
        return jsonify({"error": "завантаження вже йде"}), 409
    usernames = body.get("channels") or []
    if not usernames:
        return jsonify({"error": "не вибрано жодного каналу"}), 400
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    date_from = body.get("from") or None
    date_to = body.get("to") or None
    for d in (date_from, date_to):
        if d and not date_re.match(d):
            return jsonify({"error": f"дата {d}: потрібен формат YYYY-MM-DD"}), 400
    delay = min(max(float(body.get("delay", 1.0)), 0.3), 10.0)
    with lock:
        job.running = True
        job.stop.clear()
        job.log, job.results, job.progress, job.note = [], {}, 0.0, "Старт"
    threading.Thread(
        target=run_job, args=(usernames, date_from, date_to, bool(body.get("photos")), delay), daemon=True
    ).start()
    return jsonify({"ok": True})


@app.get("/api/jobs")
def api_job():
    return jsonify(job.state(int(request.args.get("since", 0))))


@app.post("/api/jobs/stop")
def api_stop():
    job.stop.set()
    return jsonify({"ok": True})


# ---------- перегляд (лише читання: сирі тексти не змінюємо) ----------

def find_channel(username: str):
    cfg = load_config()
    ch = next((c for c in cfg["channels"] if c["username"] == username), None)
    return (channel_dir(cfg, ch) if ch else None), ch


def load_posts(folder: Path) -> list[dict]:
    with tg.FILE_LOCK:
        return tg.read_jsonl(folder / "posts.jsonl")


@app.get("/api/posts/<username>")
def api_posts(username):
    folder, ch = find_channel(username)
    if folder is None:
        return jsonify({"error": "немає такого каналу"}), 404
    a = request.args
    q = (a.get("q") or "").strip().lower()
    d_from, d_to = a.get("from") or "", a.get("to") or ""
    posts = load_posts(folder)
    out = []
    for p in posts:
        d = (p.get("date") or "")[:10]
        if d_from and d < d_from or d_to and d > d_to:
            continue
        if q and q not in p.get("text", "").lower():
            continue
        if a.get("photos") == "1" and not p.get("photo_urls"):
            continue
        if a.get("empty") == "1" and p.get("text"):
            continue
        out.append(p)
    if a.get("order") != "asc":
        out.reverse()
    offset = max(int(a.get("offset", 0)), 0)
    limit = min(max(int(a.get("limit", 50)), 1), 200)
    return jsonify({
        "total": len(out), "all": len(posts), "offset": offset,
        "posts": out[offset:offset + limit],
    })


@app.get("/media/<username>/<name>")
def media(username, name):
    folder, _ = find_channel(username)
    if folder is None or not re.match(r"^\d+_\d+\.jpg$", name):
        return "", 404
    return send_from_directory(folder / "media", name)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()
    print(f"TG Collector: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
