"""Налаштування з оточення та файлу .env (без сторонніх бібліотек).

Порядок для адреси Ollama:
  1. OLLAMA_URL   - повна адреса, напр. http://127.0.0.1:11435
  2. OLLAMA_HOST  - та сама змінна, з якої порт бере сама Ollama (127.0.0.1:11435)
  3. http://127.0.0.1:11434 - стандартний порт Ollama

Значення з .env не перекривають змінні, вже задані в оточенні.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


def ollama_url() -> str:
    if os.environ.get("OLLAMA_URL"):
        return os.environ["OLLAMA_URL"].rstrip("/")
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if host and host not in ("0.0.0.0", "0.0.0.0:11434"):
        return (host if host.startswith("http") else f"http://{host}").rstrip("/")
    return "http://127.0.0.1:11434"


OLLAMA_URL = ollama_url()
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

# Моделі, між якими можна вибирати у вкладці JSON і для замірів, через кому:
#   OLLAMA_MODELS=qwen3:8b,gemma4:31b
# Модель за замовчуванням (OLLAMA_MODEL) додається завжди й стоїть першою.
OLLAMA_MODELS = list(dict.fromkeys(
    [OLLAMA_MODEL] + [m.strip() for m in os.environ.get("OLLAMA_MODELS", "").split(",") if m.strip()]
))

# Провайдерські моделі через OpenRouter (OpenAI-сумісний API). Ключ — лише в .env.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODELS = [
    "openrouter:" + m.strip().removeprefix("openrouter:")
    for m in os.environ.get("OPENROUTER_MODELS", "openai/gpt-4o-mini").split(",") if m.strip()
]

# Усі моделі для вибору у вебі: локальні, потім провайдерські.
ALL_MODELS = OLLAMA_MODELS + OPENROUTER_MODELS

EXPERIMENTS_FILE = ROOT / "experiments.json"


def load_experiments() -> dict:
    """Конфіг замірів: моделі, версії промптів, пресети семплінгу, ціни (experiments.json)."""
    return json.loads(EXPERIMENTS_FILE.read_text(encoding="utf-8"))
