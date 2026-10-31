"""Прогін матриці «модель × версія промпту × семплінг» і метрики для звіту.

Спільний для `python main.py` і сторінки «Порівняння» у вебі.

Набори текстів:
  dataset   - 30 постів data/dataset.jsonl (серед них 10 golden): на них валідність, час, вартість;
  golden    - 10 постів з еталоном data/golden.json: на них точність по полях;
  injection - 3 пости golden set з дописаними інструкціями (data/injection.json): чи модель їх виконує.

Кеш: якщо в results/runs.jsonl уже є прогін з тією самою моделлю, хешем промпту, семплінгом
і текстом, він не повторюється (stretch-завдання ДЗ: кешування відповідей). --no-cache вимикає.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from src import config, llm
from src import extract as ex

ROOT = Path(__file__).resolve().parent.parent
DATASET_FILE = ROOT / "data" / "dataset.jsonl"
INJECTION_FILE = ROOT / "data" / "injection.json"
METRICS_FILE = ROOT / "results" / "metrics.json"
FIELDS = ["post_type", "language", "event_date", "call_to_action", "equipment", "organizations", "locations", "money"]
PROVIDER_THREADS = 4


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _channel_id(url: str) -> tuple[str, int]:
    m = re.search(r"t\.me/([^/]+)/(\d+)", url)
    return m.group(1), int(m.group(2))


# ---------- тексти ----------

def load_posts(dataset: str = "dataset") -> list[dict]:
    """Пости для прогону: [{kind, channel, post_id, url, text, label?, injection?}]."""
    golden = {_channel_id(g["url"]): g for g in json.loads(ex.GOLDEN_FILE.read_text(encoding="utf-8"))}
    posts = []
    if dataset == "golden":
        for (ch, pid), g in golden.items():
            posts.append({"kind": "golden", "channel": ch, "post_id": pid, "url": g["url"], "text": g["text"], "label": g["label"]})
    else:
        for line in DATASET_FILE.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            ch, pid = _channel_id(row["url"])
            g = golden.get((ch, pid))
            posts.append({"kind": "golden" if g else "dataset", "channel": ch, "post_id": pid, "url": row["url"],
                          "text": g["text"] if g else row["text"], "label": g["label"] if g else None})
    for inj in json.loads(INJECTION_FILE.read_text(encoding="utf-8")):
        ch, pid = _channel_id(inj["url"])
        posts.append({"kind": "injection", "channel": ch, "post_id": pid, "url": inj["url"], "text": inj["text"],
                      "label": inj["label"], "injection": inj["injection"]})
    return posts


# ---------- план і кеш ----------

@dataclass
class Job:
    model: str
    prompt: str
    sampling: str           # назва пресету або "provider_default"
    temperature: float
    top_p: float
    post: dict

    @property
    def provider(self) -> str:
        return llm.provider_of(self.model)


def plan(models: list[str], prompts: list[str], samplings: list[str], dataset: str = "dataset") -> list[Job]:
    exp = config.load_experiments()
    posts = load_posts(dataset)
    jobs = []
    for model in models:
        # Моделі, що не приймають temperature/top_p, проганяємо один раз: семплінг у них свій.
        variants = samplings if llm.supports_sampling(model) else ["provider_default"]
        for prompt in prompts:
            for s in variants:
                preset = exp["sampling"].get(s, {"temperature": 0.0, "top_p": 1.0})
                for post in posts:
                    jobs.append(Job(model, prompt, s, preset["temperature"], preset["top_p"], post))
    return jobs


def cache_key(model: str, prompt_hash: str, sampling: str, sha: str, effort: str | None = None) -> tuple:
    return (model, prompt_hash, sampling, sha, effort)


def load_cache() -> dict[tuple, dict]:
    """Останній прогін для кожного ключа (модель, хеш промпту, семплінг, текст)."""
    cache: dict[tuple, dict] = {}
    for run in sorted(ex.load_runs(), key=lambda r: r["created_at"]):
        if run.get("prompt_hash") and run.get("text_sha"):
            cache[cache_key(run["model"], run["prompt_hash"], run.get("sampling", ""), run["text_sha"],
                            run.get("reasoning_effort"))] = run
    return cache


def job_key(job: Job, hashes: dict[str, str]) -> tuple:
    return cache_key(job.model, hashes[job.prompt], job.sampling, text_sha(job.post["text"]), llm.reasoning_effort(job.model))


# ---------- прогін ----------

@dataclass
class Progress:
    total: int = 0
    done: int = 0
    cached: int = 0
    failed_calls: int = 0
    skipped: dict = field(default_factory=dict)   # модель -> причина
    current: str = ""
    log: list = field(default_factory=list)

    def write(self, line: str):
        self.log.append(line)
        del self.log[:-300]


def run_jobs(jobs: list[Job], use_cache: bool = True, progress: Progress | None = None,
             stop: threading.Event | None = None, log=print) -> Progress:
    progress = progress or Progress()
    stop = stop or threading.Event()
    hashes = {p["version"]: p["hash"] for p in ex.list_prompts()}
    cache = load_cache() if use_cache else {}

    todo: list[Job] = []
    for job in jobs:
        if job.model in progress.skipped:
            continue
        ok, why = llm.is_available(job.model)
        if not ok:
            progress.skipped[job.model] = why
            log(f"пропускаю {job.model}: {why}")
            continue
        if use_cache and job_key(job, hashes) in cache:
            progress.cached += 1
            continue
        todo.append(job)
    progress.total = len(todo)
    log(f"До прогону {len(todo)} викликів, з кешу {progress.cached}")
    lock = threading.Lock()

    def one(job: Job):
        if stop.is_set():
            return
        p = job.post
        progress.current = f"{job.model} · {job.prompt} · {job.sampling} · #{p['post_id']}"
        try:
            run = ex.extract(p["text"], job.prompt, model=job.model, temperature=job.temperature, top_p=job.top_p,
                             sampling=job.sampling, meta={
                                 "channel": p["channel"], "post_id": p["post_id"], "post_url": p["url"],
                                 "kind": p["kind"], "text_sha": text_sha(p["text"]),
                             })
            msg = f"{progress.current}: {run['status']}, {run['latency_s']} с"
        except Exception as exc:  # noqa: BLE001 — один збій не зупиняє прогін
            with lock:
                progress.failed_calls += 1
            msg = f"{progress.current}: ПОМИЛКА {str(exc)[:160]}"
        with lock:
            progress.done += 1
            progress.write(msg)
        log(f"[{progress.done}/{progress.total}] {msg}")

    local = [j for j in todo if j.provider == "ollama"]
    remote = [j for j in todo if j.provider != "ollama"]
    # Локальні — по одному (одна відеокарта), провайдерські — паралельно з ними.
    with ThreadPoolExecutor(max_workers=PROVIDER_THREADS) as pool:
        futures = [pool.submit(one, j) for j in remote]
        for j in local:
            if stop.is_set():
                break
            one(j)
        for f in futures:
            f.result()
    progress.current = ""
    return progress


# ---------- метрики ----------

def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 2)


def compute_metrics(jobs: list[Job]) -> dict:
    """Метрики по кожній конфігурації «модель × промпт × семплінг» з прогонів у runs.jsonl."""
    hashes = {p["version"]: p["hash"] for p in ex.list_prompts()}
    cache = load_cache()
    groups: dict[tuple, list[tuple[Job, dict | None]]] = {}
    for job in jobs:
        groups.setdefault((job.model, job.prompt, job.sampling), []).append((job, cache.get(job_key(job, hashes))))

    configs = []
    for (model, prompt, sampling), items in groups.items():
        runs = [(j, r) for j, r in items if r]
        if not runs:
            continue
        main = [(j, r) for j, r in runs if j.post["kind"] != "injection"]
        gold = [(j, r) for j, r in runs if j.post["kind"] == "golden"]
        inj = [(j, r) for j, r in runs if j.post["kind"] == "injection"]
        field_scores = {f: [] for f in FIELDS}
        invented = 0
        errors = []
        per_post = {}
        for j, r in gold:
            rows = ex.compare_to_golden(r.get("output"), j.post["label"])
            per_post[j.post["post_id"]] = {row["field"]: row["score"] for row in rows}
            for row in rows:
                field_scores[row["field"]].append(row["score"])
                invented += bool(row.get("invented"))
                if row["score"] < 1:
                    errors.append({"post_id": j.post["post_id"], "url": j.post["url"], "field": row["field"],
                                   "pred": row["pred"], "gold": row["gold"], "score": row["score"],
                                   "run_id": r["run_id"], "status": r["status"]})
        latencies = [r["latency_s"] for _, r in main]
        costs = [r.get("cost_usd") or 0 for _, r in main]
        statuses = [r["status"] for _, r in main]
        fields_avg = {f: round(statistics.mean(v), 3) if v else None for f, v in field_scores.items()}
        scored = [v for v in fields_avg.values() if v is not None]
        configs.append({
            "model": model, "provider": llm.provider_of(model), "prompt": prompt, "prompt_hash": hashes.get(prompt),
            "sampling": sampling,
            "n": len(main), "expected": sum(1 for j, _ in items if j.post["kind"] != "injection"),
            "valid_pct": round(100 * statuses.count("valid") / len(main), 1) if main else None,
            "repaired": statuses.count("repaired"), "failed": statuses.count("failed"),
            "truncated": sum(1 for _, r in main if r.get("truncated")),
            "accuracy": round(statistics.mean(scored), 3) if scored else None,
            "fields": fields_avg,
            "golden_n": len(gold),
            "invented": invented,
            "grounding_warnings": sum(len(r.get("grounding") or []) for _, r in main),
            "latency_p50": _pct(latencies, 0.5), "latency_p95": _pct(latencies, 0.95),
            "cost_per_1000": round(statistics.mean(costs) * 1000, 4) if costs else None,
            "cost_source": main[-1][1].get("cost_source") if main else None,
            "injection_followed": sum(ex.injection_followed(r.get("output"), j.post["injection"]) for j, r in inj),
            "injection_n": len(inj),
            "per_post": per_post,
            "errors": errors,
        })
    configs.sort(key=lambda c: (c["provider"], c["model"], c["prompt"], c["sampling"]))
    return {"configs": configs, "regressions": regressions(configs)}


def regressions(configs: list[dict]) -> list[dict]:
    """Що нова версія промпту зламала з того, що попередня робила правильно (та сама модель і семплінг)."""
    by = {(c["model"], c["sampling"], c["prompt"]): c for c in configs}
    out = []
    for c in configs:
        n = int(c["prompt"][1:])
        prev = by.get((c["model"], c["sampling"], f"v{n - 1}"))
        if not prev:
            continue
        broke, fixed = [], []
        for pid, scores in c["per_post"].items():
            before = prev["per_post"].get(pid, {})
            for f, s in scores.items():
                if f in before and before[f] == 1 and s < 1:
                    broke.append({"post_id": pid, "field": f})
                elif f in before and before[f] < 1 and s == 1:
                    fixed.append({"post_id": pid, "field": f})
        out.append({"model": c["model"], "sampling": c["sampling"], "from": prev["prompt"], "to": c["prompt"],
                    "broke": broke, "fixed": fixed})
    return out


def save_metrics(metrics: dict) -> Path:
    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    METRICS_FILE.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return METRICS_FILE


# ---------- таблиці (markdown) ----------

def _f(v, fmt="{:.2f}"):
    return "—" if v is None else fmt.format(v)


def markdown_tables(metrics: dict) -> str:
    rows = ["| Модель | Промпт | Семплінг | Точність (golden) | Валідних з 1-ї спроби | Виправлено / failed | Вигадано | Попередж. фактів | Injection | p50, с | p95, с | $ / 1000 постів |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in metrics["configs"]:
        cost_note = {"provider": "", "tokens": " (за токенами)", "electricity": " (ел.)"}.get(c["cost_source"], "")
        rows.append(f"| `{c['model']}` | {c['prompt']} | {c['sampling']} | {_f(c['accuracy'])} | "
                    f"{_f(c['valid_pct'], '{:.0f}%')} ({c['n']}/{c['expected']}) | {c['repaired']} / {c['failed']} | "
                    f"{c['invented']} | {c['grounding_warnings']} | {c['injection_followed']}/{c['injection_n']} | "
                    f"{_f(c['latency_p50'], '{:.1f}')} | {_f(c['latency_p95'], '{:.1f}')} | "
                    f"{_f(c['cost_per_1000'], '{:.3f}')}{cost_note} |")
    fields = ["| Модель | Промпт | Семплінг | " + " | ".join(FIELDS) + " |", "|---|---|---|" + "---|" * len(FIELDS)]
    for c in metrics["configs"]:
        fields.append(f"| `{c['model']}` | {c['prompt']} | {c['sampling']} | "
                      + " | ".join(_f(c["fields"].get(f)) for f in FIELDS) + " |")
    regs = ["| Модель | Семплінг | Зміна | Зламалось | Виправилось |", "|---|---|---|---|---|"]
    for r in metrics["regressions"]:
        broke = ", ".join(f"#{b['post_id']} {b['field']}" for b in r["broke"]) or "—"
        regs.append(f"| `{r['model']}` | {r['sampling']} | {r['from']} → {r['to']} | {broke} | {len(r['fixed'])} |")
    return ("### Зведена таблиця\n\n" + "\n".join(rows)
            + "\n\n### Точність по полях (golden set, 10 постів)\n\n" + "\n".join(fields)
            + "\n\n### Регресії між версіями промпту\n\n" + "\n".join(regs) + "\n")
