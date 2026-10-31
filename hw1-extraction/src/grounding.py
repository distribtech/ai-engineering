"""Перевірка, що LLM не вигадала й не спотворила ідентифікатори (схема v3).

У схемі v3 гаманці, картки, контакти й імена — головна цінність: за точним значенням
видно, що той самий гаманець чи акаунт трапляється в різних каналах. Модель може
«виправити» символ у гаманці, дописати домен, переставити цифри або вигадати
@-ім'я, якого в тексті немає. Pydantic таке не ловить: рядок валідний за схемою,
але хибний. Тому кожне значення звіряємо з текстом поста.

Рівні:
  error   - значення немає в тексті (вигадане або спотворене). Таке поле не можна
            використовувати для зв'язування каналів.
  warning - точного збігу немає, але є схожий (імена у відмінках, регістр тощо);
            варто подивитись очима.

  from src.grounding import check_grounding
  issues = check_grounding(post_text, extracted_dict)
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# Формати криптогаманців: якщо модель повернула гаманець не цього формату, він точно зіпсований.
WALLET_FORMATS = {
    "ETH/ERC20/BSC": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "BTC": re.compile(r"^(?:bc1[0-9a-z]{25,60}|[13][1-9A-HJ-NP-Za-km-z]{25,34})$"),
    "TRON/TRC20": re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),
    "TON": re.compile(r"^(?:UQ|EQ)[A-Za-z0-9_-]{46}$"),
    "XMR": re.compile(r"^[48][0-9AB][1-9A-HJ-NP-Za-km-z]{93}$"),
}
MASK_RE = re.compile(r"^\[[A-Z_]+\]$")


@dataclass
class Issue:
    level: str   # error | warning
    field: str   # напр. payment_details[1].identifier
    value: str
    reason: str


def _compact(s: str) -> str:
    """Без пробілів, дефісів і крапок: '2202 2083-8835' і '220220838835' - одне й те саме."""
    return re.sub(r"[\s\-.()]", "", s)


def _in_text(value: str, text: str, case_sensitive: bool) -> bool:
    if case_sensitive:
        return value in text or _compact(value) in _compact(text)
    return value.casefold() in text.casefold() or _compact(value).casefold() in _compact(text).casefold()


def _name_in_text(name: str, text: str) -> bool:
    """Ім'я у називному відмінку може стояти в тексті в іншому відмінку: шукаємо основу кожного слова."""
    low = text.casefold()
    words = [w for w in re.findall(r"\w+", name.casefold()) if w not in ("позывной", "позивний")]
    return bool(words) and all((w[:-2] if len(w) > 4 else w) in low for w in words)


def wallet_format(identifier: str) -> str | None:
    for name, rx in WALLET_FORMATS.items():
        if rx.match(identifier):
            return name
    return None


def check_grounding(text: str, data: dict) -> list[Issue]:
    issues: list[Issue] = []

    for i, p in enumerate(data.get("payment_details") or []):
        ident = (p.get("identifier") or "").strip()
        field = f"payment_details[{i}].identifier"
        if p.get("method") == "crypto_wallet" and not MASK_RE.match(ident):
            # Гаманець - лише точний збіг з урахуванням регістру: один символ = інший гаманець.
            if ident not in text:
                issues.append(Issue("error", field, ident, "гаманця немає в тексті (вигаданий або спотворений)"))
            if not wallet_format(ident):
                issues.append(Issue("error", field, ident, "не схоже на адресу жодної відомої мережі"))
        elif ident and not _in_text(ident, text, case_sensitive=True):
            issues.append(Issue("error", field, ident, "значення немає в тексті"))
        holder = p.get("holder")
        if holder and not _name_in_text(holder, text):
            issues.append(Issue("warning", f"payment_details[{i}].holder", holder, "імені власника не знайдено в тексті"))

    for i, c in enumerate(data.get("contacts") or []):
        value = (c.get("value") or "").strip()
        # @-імена в Telegram не залежать від регістру, решту теж звіряємо без регістру.
        if value and not _in_text(value, text, case_sensitive=False):
            bare = value.lstrip("@").removeprefix("https://").removeprefix("t.me/")
            level = "warning" if bare and _in_text(bare, text, case_sensitive=False) else "error"
            issues.append(Issue(level, f"contacts[{i}].value", value,
                                "у тексті інший запис (без @ або посилання)" if level == "warning" else "контакту немає в тексті"))

    for i, person in enumerate(data.get("persons") or []):
        name = (person.get("name") or "").strip()
        if name and not _name_in_text(name, text):
            issues.append(Issue("warning", f"persons[{i}].name", name, "імені не знайдено в тексті"))

    return issues


def as_dicts(issues: list[Issue]) -> list[dict]:
    return [asdict(x) for x in issues]
