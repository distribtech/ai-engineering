"""Спільний інтерфейс до моделей: локальна Ollama і провайдерські моделі через OpenRouter.

Решта коду (extract.py, main.py, веб) бачить лише одне:

    reply = llm.chat(model, messages, schema, temperature=0, top_p=1)
    reply.content, reply.prompt_tokens, reply.output_tokens, reply.finish_reason, reply.cost_usd

Модель вибирається рядком:
    "gemma4:31b"                     -> Ollama (локально)
    "openrouter:openai/gpt-4o-mini"  -> OpenRouter (OpenAI-сумісний API, ключ OPENROUTER_API_KEY у .env)

Обидва клієнти просять structured output за JSON Schema, мають timeout і повтори
(мережеві помилки, 429, 5xx) і повертають finish_reason, щоб помітити обрізану відповідь.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass

import requests

from src import config

TIMEOUT_S = 300
RETRIES = 3            # повтори після першої спроби
MAX_OUTPUT_TOKENS = 2048          # локально: лише сама відповідь (think=False)
PROVIDER_MAX_TOKENS = 8000        # провайдер: міркування моделі теж рахуються в ліміт
NUM_CTX = 8192
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_PREFIX = "openrouter:"


_caps_cache: dict[str, set[str]] = {}


def openrouter_params(model: str) -> set[str]:
    """Параметри, які модель приймає на OpenRouter (публічний каталог /models, кешується в пам'яті)."""
    provider_model = model.removeprefix(OPENROUTER_PREFIX)
    if not _caps_cache:
        try:
            resp = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
            resp.raise_for_status()
            for m in resp.json()["data"]:
                _caps_cache[m["id"]] = set(m.get("supported_parameters") or [])
        except requests.RequestException:
            return {"temperature", "top_p", "response_format", "structured_outputs"}
    return _caps_cache.get(provider_model, set())


def supports_sampling(model: str) -> bool:
    """Чи можна задати temperature/top_p (деякі моделі-«міркувальники» їх не приймають)."""
    if provider_of(model) == "ollama":
        return True
    return {"temperature", "top_p"} <= openrouter_params(model)


@dataclass
class Reply:
    content: str
    prompt_tokens: int | None
    output_tokens: int | None
    finish_reason: str | None      # "stop" | "length" | ...
    latency_s: float
    cost_usd: float | None         # точна вартість від провайдера, якщо він її повертає
    provider: str
    provider_model: str
    sampling_applied: bool = True  # False: модель не приймає temperature/top_p, діють її налаштування
    reasoning_effort: str | None = None  # рівень міркування провайдерської моделі


class LLMError(RuntimeError):
    pass


def provider_of(model: str) -> str:
    return "openrouter" if model.startswith(OPENROUTER_PREFIX) else "ollama"


def is_available(model: str) -> tuple[bool, str]:
    """Чи можна зараз викликати модель, і якщо ні — чому."""
    if provider_of(model) == "openrouter":
        return (True, "") if config.OPENROUTER_API_KEY else (False, "немає OPENROUTER_API_KEY у .env")
    try:
        resp = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        resp.raise_for_status()
    except requests.RequestException:
        return False, f"Ollama недоступна ({config.OLLAMA_URL})"
    names = {m["name"] for m in resp.json().get("models", [])}
    names |= {n.removesuffix(":latest") for n in names}
    return (True, "") if model in names else (False, f"модель не встановлена: ollama pull {model}")


def _post_with_retry(url: str, payload: dict, headers: dict | None = None) -> dict:
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_S)
            if resp.status_code == 429 or resp.status_code >= 500:
                last = LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(float(resp.headers.get("Retry-After") or 2 ** (attempt + 1)))
                continue
            if resp.status_code >= 400:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
            time.sleep(2 ** (attempt + 1))
    raise LLMError(f"Не вдалося після {RETRIES + 1} спроб: {last}")


# ---------- Ollama ----------

def _ollama(model: str, messages: list[dict], schema: dict, temperature: float, top_p: float) -> Reply:
    t0 = time.perf_counter()
    data = _post_with_retry(f"{config.OLLAMA_URL}/api/chat", {
        "model": model,
        "messages": messages,
        "format": schema,
        "think": False,
        "stream": False,
        "options": {"temperature": temperature, "top_p": top_p, "num_ctx": NUM_CTX, "num_predict": MAX_OUTPUT_TOKENS},
    })
    return Reply(
        content=data["message"]["content"],
        prompt_tokens=data.get("prompt_eval_count"),
        output_tokens=data.get("eval_count"),
        finish_reason=data.get("done_reason"),
        latency_s=round(time.perf_counter() - t0, 2),
        cost_usd=None,
        provider="ollama",
        provider_model=model,
    )


# ---------- OpenRouter ----------

# Ключові слова JSON Schema, яких strict structured output OpenAI не приймає.
_UNSUPPORTED = {"minLength", "maxLength", "default", "title"}


def strict_schema(schema: dict) -> dict:
    """JSON Schema з Pydantic -> форма для strict structured output OpenAI.

    - $ref розгортаються на місці: strict mode не дозволяє поруч із $ref інших ключів,
      а нам потрібен description поля (у ньому правила для моделі);
    - прибираються minLength/maxLength/default/title (їх перевіряє Pydantic після відповіді).
    """
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return walk(merged)
        return {k: walk(v) for k, v in node.items() if k not in _UNSUPPORTED and k != "$defs"}

    return walk(schema)


def _openrouter(model: str, messages: list[dict], schema: dict, temperature: float, top_p: float) -> Reply:
    if not config.OPENROUTER_API_KEY:
        raise LLMError("немає OPENROUTER_API_KEY у .env")
    provider_model = model.removeprefix(OPENROUTER_PREFIX)
    sampling = supports_sampling(model)
    effort = reasoning_effort(model)
    t0 = time.perf_counter()
    data = _post_with_retry(OPENROUTER_URL, {
        "model": provider_model,
        "messages": messages,
        **({"temperature": temperature, "top_p": top_p} if sampling else {}),
        **({"reasoning": {"effort": effort, "exclude": True}} if effort else {}),
        "max_tokens": PROVIDER_MAX_TOKENS,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "extracted_post", "strict": True, "schema": strict_schema(schema)}},
        # лише ті провайдери, що підтримують усі параметри (зокрема structured output)
        "provider": {"require_parameters": True},
        "usage": {"include": True},
    }, headers={
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "X-Title": "hw1-extraction",
    })
    if "choices" not in data:
        raise LLMError(f"Неочікувана відповідь OpenRouter: {str(data)[:300]}")
    choice = data["choices"][0]
    usage = data.get("usage") or {}
    return Reply(
        content=choice["message"].get("content") or "",
        prompt_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        finish_reason=choice.get("finish_reason"),
        latency_s=round(time.perf_counter() - t0, 2),
        cost_usd=usage.get("cost"),
        provider="openrouter",
        provider_model=provider_model,
        sampling_applied=sampling,
        reasoning_effort=effort,
    )


def reasoning_effort(model: str) -> str | None:
    """Рівень міркування з experiments.json (provider_reasoning_effort), якщо модель його приймає."""
    if provider_of(model) != "openrouter":
        return None
    effort = config.load_experiments().get("provider_reasoning_effort")
    return effort if effort and "reasoning" in openrouter_params(model) else None


def chat(model: str, messages: list[dict], schema: dict, temperature: float = 0.0, top_p: float = 1.0) -> Reply:
    fn = _openrouter if provider_of(model) == "openrouter" else _ollama
    return fn(model, messages, schema, temperature, top_p)


# ---------- вартість ----------

def cost_usd(model: str, reply_or_tokens, latency_s: float | None = None) -> tuple[float, str]:
    """Вартість одного виклику і як її пораховано.

    Провайдер: точна сума від OpenRouter, інакше токени x ціна з experiments.json.
    Локальна модель: оцінка електроенергії = час x потужність GPU x тариф (experiments.json).
    """
    exp = config.load_experiments()
    if isinstance(reply_or_tokens, Reply):
        r = reply_or_tokens
        if r.cost_usd is not None:
            return float(r.cost_usd), "provider"
        p_in, p_out, latency_s = r.prompt_tokens or 0, r.output_tokens or 0, r.latency_s
    else:
        p_in, p_out = reply_or_tokens
    if provider_of(model) == "openrouter":
        price = exp["prices_usd_per_1m"].get(model)
        if price is None:
            return 0.0, "no_price"
        return (p_in * price[0] + p_out * price[1]) / 1_000_000, "tokens"
    local = exp["local_cost"]
    return (latency_s or 0) / 3600 * local["gpu_power_w"] / 1000 * local["usd_per_kwh"], "electricity"
