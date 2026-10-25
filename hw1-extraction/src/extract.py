"""Витягування структурованих даних із тексту поста через локальну модель (Ollama).

Один виклик `extract()` робить усе, що вимагає ДЗ для кроків 1-3:
  1. читає версію промпту з prompts/<версія>.txt;
  2. відправляє пост у модель зі structured output (JSON Schema з src/schema.py);
  3. перевіряє відповідь за схемою Pydantic;
  4. якщо відповідь невалідна, робить ОДНУ спробу виправлення, далі статус "failed";
  5. логує модель, версію промпту, параметри семплінгу, час і токени;
  6. дописує результат у results/runs.jsonl.

Мережеві помилки й таймаути повторюються (retry), одночасно йде лише один виклик,
бо відеокарта одна.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from pydantic import ValidationError

from src import config
from src.schema import ExtractedPost, json_schema

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
RESULTS_FILE = ROOT / "results" / "runs.jsonl"
GOLDEN_FILE = ROOT / "data" / "golden.json"

OLLAMA_URL = config.OLLAMA_URL
DEFAULT_MODEL = config.OLLAMA_MODEL
NUM_CTX = 8192
TIMEOUT_S = 300
NETWORK_RETRIES = 2

_gpu_lock = threading.Lock()
_file_lock = threading.Lock()


# ---------- промпти ----------

def list_prompts() -> list[dict]:
    """Версії промптів: [{version, label, text}], мітка береться з заголовків CHANGELOG.md."""
    labels: dict[str, str] = {}
    changelog = PROMPTS_DIR / "CHANGELOG.md"
    if changelog.exists():
        for m in re.finditer(r"^##\s+(v\d+)\s*[—-]\s*(.+)$", changelog.read_text(encoding="utf-8"), re.M):
            labels[m.group(1)] = m.group(2).strip()
    out = []
    for path in sorted(PROMPTS_DIR.glob("v*.txt"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)):
        out.append({"version": path.stem, "label": labels.get(path.stem, ""), "text": path.read_text(encoding="utf-8").strip()})
    return out


def load_prompt(version: str) -> str:
    if not re.fullmatch(r"v\d+", version or ""):
        raise ValueError(f"Невірна версія промпту: {version!r}")
    path = PROMPTS_DIR / f"{version}.txt"
    if not path.exists():
        raise ValueError(f"Немає файлу prompts/{version}.txt")
    return path.read_text(encoding="utf-8").strip()


# ---------- виклик моделі ----------

def _chat(model: str, messages: list[dict], options: dict) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "format": json_schema(),
        "think": False,
        "stream": False,
        "options": options,
    }
    last: Exception | None = None
    for attempt in range(NETWORK_RETRIES + 1):
        try:
            resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=TIMEOUT_S)
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Ollama недоступна ({OLLAMA_URL}): {last}")


def _validate(raw: str) -> tuple[dict | None, list[str]]:
    """Повертає (дані, помилки). Помилки порожні, якщо відповідь валідна."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"не JSON: {exc}"]
    try:
        return ExtractedPost.model_validate(data).model_dump(mode="json"), []
    except ValidationError as exc:
        return None, [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in exc.errors()]


def extract(
    text: str,
    prompt_version: str = "v2",
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
    top_p: float = 0.9,
    meta: dict | None = None,
) -> dict:
    """Витягує ExtractedPost з тексту. `meta` (channel, post_id, post_url) потрапляє в лог."""
    system = load_prompt(prompt_version)
    options = {"temperature": temperature, "top_p": top_p, "num_ctx": NUM_CTX}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]

    attempts = []
    output = None
    t_total = time.perf_counter()
    with _gpu_lock:
        for n in (1, 2):  # спроба 1 - основна, спроба 2 - єдине виправлення
            t0 = time.perf_counter()
            reply = _chat(model, messages, options)
            raw = reply["message"]["content"]
            output, errors = _validate(raw)
            attempts.append({
                "n": n, "raw": raw, "errors": errors,
                "latency_s": round(time.perf_counter() - t0, 2),
                "prompt_tokens": reply.get("prompt_eval_count"), "output_tokens": reply.get("eval_count"),
            })
            if not errors:
                break
            if n == 1:
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "The JSON above failed validation:\n- " + "\n- ".join(errors[:8])
                     + "\nReturn the corrected JSON object only."},
                ]
    status = "failed" if output is None else ("valid" if len(attempts) == 1 else "repaired")

    result = {
        "run_id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **(meta or {}),
        "provider": "ollama",
        "model": model,
        "prompt_version": prompt_version,
        "options": options,
        "status": status,
        "output": output,
        "attempts": attempts,
        "latency_s": round(time.perf_counter() - t_total, 2),
        "cost_usd": 0.0,
        "request": {"system": system, "user": text},
    }
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock, RESULTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def load_runs(channel: str | None = None, post_id: int | None = None) -> list[dict]:
    """Усі збережені прогони (нові першими), за потреби лише для одного поста."""
    if not RESULTS_FILE.exists():
        return []
    runs = []
    with RESULTS_FILE.open(encoding="utf-8") as f:
        for line in f:
            try:
                run = json.loads(line)
            except json.JSONDecodeError:
                continue
            if channel and run.get("channel") != channel:
                continue
            if post_id is not None and run.get("post_id") != post_id:
                continue
            runs.append(run)
    runs.sort(key=lambda r: r["created_at"], reverse=True)
    return runs


# ---------- порівняння з golden set ----------

def load_golden() -> dict[tuple[str, int], dict]:
    """(канал, id поста) -> еталонна розмітка та її статус."""
    if not GOLDEN_FILE.exists():
        return {}
    out = {}
    for g in json.loads(GOLDEN_FILE.read_text(encoding="utf-8")):
        m = re.search(r"t\.me/([^/]+)/(\d+)", g["url"])
        if m:
            out[(m.group(1), int(m.group(2)))] = {"label": g["label"], "status": g["status"]}
    return out


def _norm(value: str) -> str:
    """Для порівняння назв: без регістру, лапок і розділових знаків ('DSRG "Rusich"' == 'DSRG Rusich')."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(value))).strip().casefold()


def compare_to_golden(pred: dict | None, gold: dict) -> list[dict]:
    """Порівняння по полях: скаляри точно, списки за коефіцієнтом Жаккара по нормалізованих рядках."""
    rows = []
    for field in ("post_type", "language", "event_date", "call_to_action"):
        p = pred.get(field) if pred else None
        rows.append({"field": field, "pred": p, "gold": gold[field], "score": 1.0 if p == gold[field] else 0.0})

    def names(data: dict | None, field: str) -> set[str]:
        if not data:
            return set()
        items = data.get(field) or []
        return {_norm(i["name"] if isinstance(i, dict) else i) for i in items}

    for field in ("equipment", "organizations", "locations"):
        p, g = names(pred, field), names(gold, field)
        score = 1.0 if not p and not g else len(p & g) / len(p | g)
        rows.append({"field": field, "pred": sorted(p), "gold": sorted(g), "score": round(score, 2)})

    p_money = sorted((m["amount_min"], m["amount_max"], m["currency"]) for m in (pred or {}).get("money", []))
    g_money = sorted((m["amount_min"], m["amount_max"], m["currency"]) for m in gold["money"])
    rows.append({"field": "money", "pred": p_money, "gold": g_money, "score": 1.0 if p_money == g_money else 0.0})
    return rows
