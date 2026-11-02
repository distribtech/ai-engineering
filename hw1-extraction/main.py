"""Запуск ДЗ1 однією командою: тести -> прогін матриці -> метрики -> таблиці в REPORT.md.

  python main.py                              # усе з experiments.json, з кешем
  python main.py --models gemma4:31b --prompts v2 v3 --sampling greedy
  python main.py --set golden                 # лише 10 постів golden set (+ injection)
  python main.py --metrics-only               # нічого не викликати, перерахувати метрики з results/runs.jsonl
  python main.py --no-cache                   # прогнати заново, навіть якщо результат уже є

Моделі без ключа чи не встановлені в Ollama пропускаються з поясненням.
Результати: results/runs.jsonl (кожен виклик), results/metrics.json, таблиці в REPORT.md.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import config, runner  # noqa: E402

REPORT = ROOT / "REPORT.md"
START, END = "<!-- METRICS:START -->", "<!-- METRICS:END -->"


def run_tests() -> None:
    from tests import test_golden, test_grounding, test_privacy, test_schema, test_schema_v3
    for mod in (test_schema, test_schema_v3, test_golden, test_grounding, test_privacy):
        for name in dir(mod):
            if name.startswith("test_"):
                getattr(mod, name)()
    print("Тести: OK")


def update_report(tables: str) -> None:
    block = f"{START}\n<!-- Цей блок перезаписує `python main.py`. Не редагуйте вручну. -->\n\n{tables}\n{END}"
    text = REPORT.read_text(encoding="utf-8") if REPORT.exists() else f"# Звіт ДЗ1\n\n{START}\n{END}\n"
    if START in text:
        text = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, text, flags=re.S)
    else:
        text += "\n" + block + "\n"
    REPORT.write_text(text, encoding="utf-8")


def main() -> None:
    exp = config.load_experiments()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=exp["models"])
    ap.add_argument("--prompts", nargs="+", default=exp["prompts"])
    ap.add_argument("--sampling", nargs="+", default=list(exp["sampling"]))
    ap.add_argument("--set", default=exp.get("dataset", "dataset"), choices=["dataset", "golden"])
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--metrics-only", action="store_true")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--no-report", action="store_true", help="не чіпати REPORT.md")
    args = ap.parse_args()

    if not args.skip_tests:
        run_tests()
    jobs = runner.plan(args.models, args.prompts, args.sampling, args.set)
    if not args.metrics_only:
        progress = runner.run_jobs(jobs, use_cache=not args.no_cache)
        for model, why in progress.skipped.items():
            print(f"Пропущено {model}: {why}")
    metrics = runner.compute_metrics(jobs)
    path = runner.save_metrics(metrics)
    tables = runner.markdown_tables(metrics)
    print("\n" + tables)
    print(f"Метрики: {path.relative_to(ROOT)}")
    if not args.no_report:
        update_report(tables)
        print(f"Таблиці оновлено в {REPORT.name}")


if __name__ == "__main__":
    main()
