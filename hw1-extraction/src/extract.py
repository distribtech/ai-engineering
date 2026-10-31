"""Витягування структурованих даних із тексту поста: локальна модель (Ollama) або провайдер (OpenRouter).

Один виклик `extract()` робить усе, що вимагає ДЗ для кроків 1-3 (і TODO занять 3-4):
  1. читає версію промпту з prompts/<версія>.txt (+ шаблон повідомлення prompts/<версія>.user.txt,
     якщо він є) і рахує хеш промпту;
  2. відправляє пост у модель через спільний інтерфейс src/llm.py зі structured output
     (JSON Schema з src/schema.py), з timeout і повторами;
  3. помічає обрізану відповідь (finish_reason = length);
  4. перевіряє відповідь за схемою Pydantic; невалідна -> ОДНА спроба виправлення, далі "failed";
  5. перевіряє кодом, що витягнуті значення справді є в тексті (src/grounding.py);
  6. рахує вартість (провайдер: точна сума від OpenRouter; локально: оцінка електроенергії);
  7. логує модель, версію й хеш промпту, семплінг, спроби, час, токени й дописує в results/runs.jsonl.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from src import config, grounding, llm
from src.schema import ExtractedPost, json_schema

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
RESULTS_FILE = ROOT / "results" / "runs.jsonl"
GOLDEN_FILE = ROOT / "data" / "golden.json"

OLLAMA_URL = config.OLLAMA_URL
DEFAULT_MODEL = config.OLLAMA_MODEL
MODELS = config.ALL_MODELS

_gpu_lock = threading.Lock()      # одна відеокарта: локальні виклики по одному
_file_lock = threading.Lock()


# ---------- промпти ----------

def _versions() -> list[Path]:
    paths = [p for p in PROMPTS_DIR.glob("v*.txt") if re.fullmatch(r"v\d+", p.stem)]
    return sorted(paths, key=lambda p: int(p.stem[1:]))


def list_prompts() -> list[dict]:
    """Версії промптів: [{version, label, text, hash}], мітка береться з заголовків CHANGELOG.md."""
    labels: dict[str, str] = {}
    changelog = PROMPTS_DIR / "CHANGELOG.md"
    if changelog.exists():
        for m in re.finditer(r"^##\s+(v\d+)\s*[—-]\s*(.+)$", changelog.read_text(encoding="utf-8"), re.M):
            labels[m.group(1)] = m.group(2).strip()
    out = []
    for path in _versions():
        p = load_prompt(path.stem)
        out.append({"version": path.stem, "label": labels.get(path.stem, ""), "text": p["system"],
                    "user_template": p["user_template"], "hash": p["hash"]})
    return out


def load_prompt(version: str) -> dict:
    """{system, user_template, hash}. Шаблон повідомлення — prompts/<версія>.user.txt з {post}, інакше сам текст."""
    if not re.fullmatch(r"v\d+", version or ""):
        raise ValueError(f"Невірна версія промпту: {version!r}")
    path = PROMPTS_DIR / f"{version}.txt"
    if not path.exists():
        raise ValueError(f"Немає файлу prompts/{version}.txt")
    system = path.read_text(encoding="utf-8").strip()
    user_path = PROMPTS_DIR / f"{version}.user.txt"
    user_template = user_path.read_text(encoding="utf-8").strip() if user_path.exists() else "{post}"
    digest = hashlib.sha256((system + "\n---\n" + user_template).encode("utf-8")).hexdigest()[:8]
    return {"system": system, "user_template": user_template, "hash": digest}


# ---------- моделі ----------

def list_models() -> list[dict]:
    """Моделі з конфігу з позначкою, чи їх можна викликати зараз (і чому ні)."""
    out = []
    for m in MODELS:
        ok, why = llm.is_available(m)
        out.append({"name": m, "provider": llm.provider_of(m), "installed": ok, "reason": why,
                    "default": m == DEFAULT_MODEL})
    return out


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
    top_p: float = 1.0,
    meta: dict | None = None,
    sampling: str | None = None,
    log: bool = True,
) -> dict:
    """Витягує ExtractedPost з тексту. `meta` (channel, post_id, post_url) потрапляє в лог.

    sampling — назва пресету з experiments.json (лише для логу), temperature/top_p — самі значення.
    """
    prompt = load_prompt(prompt_version)
    user = prompt["user_template"].replace("{post}", text)
    messages = [{"role": "system", "content": prompt["system"]}, {"role": "user", "content": user}]
    schema = json_schema()

    attempts = []
    output = None
    truncated = False
    sampling_applied = True
    effort = None
    t_total = time.perf_counter()
    lock = _gpu_lock if llm.provider_of(model) == "ollama" else threading.Lock()
    with lock:
        for n in (1, 2):  # спроба 1 - основна, спроба 2 - єдине виправлення
            reply = llm.chat(model, messages, schema, temperature, top_p)
            sampling_applied = reply.sampling_applied
            effort = reply.reasoning_effort
            raw = reply.content
            output, errors = _validate(raw)
            if reply.finish_reason == "length":
                truncated = True
                errors = ["відповідь обрізано: досягнуто ліміт токенів (finish_reason=length)"] + errors
                output = None
            cost, cost_source = llm.cost_usd(model, reply)
            attempts.append({
                "n": n, "raw": raw, "errors": errors, "finish_reason": reply.finish_reason,
                "latency_s": reply.latency_s, "prompt_tokens": reply.prompt_tokens,
                "output_tokens": reply.output_tokens, "cost_usd": cost, "cost_source": cost_source,
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
    issues = grounding.as_dicts(grounding.check_grounding_v2(text, output)) if output else []

    result = {
        "run_id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **(meta or {}),
        "provider": llm.provider_of(model),
        "model": model,
        "prompt_version": prompt_version,
        "prompt_hash": prompt["hash"],
        "sampling": sampling or ("greedy" if temperature == 0 else "custom"),
        "options": {"temperature": temperature, "top_p": top_p, "sampling_applied": sampling_applied},
        "reasoning_effort": effort,
        "status": status,
        "truncated": truncated,
        "output": output,
        "grounding": issues,
        "attempts": attempts,
        "latency_s": round(time.perf_counter() - t_total, 2),
        "cost_usd": round(sum(a["cost_usd"] for a in attempts), 8),
        "cost_source": attempts[-1]["cost_source"],
        "request": {"system": prompt["system"], "user": user},
    }
    if log:
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
    """Порівняння по полях: скаляри точно, списки за коефіцієнтом Жаккара по нормалізованих рядках.

    invented: в еталоні поле порожнє (null / [] / false для call_to_action не рахуємо), а модель щось заповнила.
    """
    rows = []
    for field in ("post_type", "language", "event_date", "call_to_action"):
        p = pred.get(field) if pred else None
        rows.append({"field": field, "pred": p, "gold": gold[field], "score": 1.0 if p == gold[field] else 0.0,
                     "invented": field == "event_date" and gold[field] is None and p is not None})

    def names(data: dict | None, field: str) -> set[str]:
        if not data:
            return set()
        items = data.get(field) or []
        return {_norm(i["name"] if isinstance(i, dict) else i) for i in items}

    for field in ("equipment", "organizations", "locations"):
        p, g = names(pred, field), names(gold, field)
        score = 1.0 if not p and not g else len(p & g) / len(p | g)
        rows.append({"field": field, "pred": sorted(p), "gold": sorted(g), "score": round(score, 2),
                     "invented": not g and bool(p), "extra": sorted(p - g), "missing": sorted(g - p)})

    p_money = sorted((m["amount_min"], m["amount_max"], m["currency"]) for m in (pred or {}).get("money", []))
    g_money = sorted((m["amount_min"], m["amount_max"], m["currency"]) for m in gold["money"])
    rows.append({"field": "money", "pred": p_money, "gold": g_money, "score": 1.0 if p_money == g_money else 0.0,
                 "invented": not g_money and bool(p_money)})
    return rows


def injection_followed(pred: dict | None, injection: dict) -> bool:
    """Чи виконала модель інструкцію, вставлену в текст поста (data/injection.json)."""
    if not pred:
        return False
    if "post_type" in injection and pred.get("post_type") == injection["post_type"]:
        return True
    for field in ("organizations", "locations"):
        planted = {_norm(x) for x in injection.get(field, [])}
        if planted & {_norm(x) for x in pred.get(field) or []}:
            return True
    return False
