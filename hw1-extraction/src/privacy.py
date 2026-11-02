"""Правила приватності залежно від сторони каналу.

Сторону задає TG Collector (поле `side` каналу, дублюється в channel.json):

  hostile  - ворожий канал (збирає на армію РФ). Ідентифікатори — це і є результат розвідки:
             акаунти, боти, гаманці, реквізити, імена, позивні, підрозділи, напрямки, місця передачі
             техніки. Нічого не маскуємо; навпаки, intel_entities() збирає їх в окремий список.
  friendly - дружній канал (українські волонтери). Маскуємо все, що вказує на людей і на військових:
             реквізити й акаунти, імена й позивні, підрозділи, напрямки, населені пункти, місця передачі
             техніки. Назви фондів і компаній лишаємо: це публічні організації.
  unknown  - сторону ще не визначено: обробляємо як friendly (безпечніше помилитись у бік маски).

Два шари маскування для friendly:
  1. регулярні вирази (без моделі): картки, телефони, e-mail, гаманці, @-імена, посилання,
     номери військових частин, «N бригада», «… напрямок», «позивний …»;
  2. сутності, які знайшла LLM за схемою v3 (persons, organizations з type=military_unit,
     locations, frontline_directions, equipment[].recipient / delivery_place): замінюються
     і в тексті (з урахуванням відмінків), і в самому JSON.

Шар 2 залежить від якості моделі: що вона пропустила, те лишиться в тексті. Тому для
публічного репозиторію дружні тексти треба ще переглядати (див. README, «Сторона каналу»).
"""

from __future__ import annotations

import copy
import re

SIDES = ("hostile", "friendly", "unknown")

# Що робимо з кожним типом даних. "keep" — лишити як є, "mask" — замінити маскою.
POLICY: dict[str, dict[str, str]] = {
    "hostile": {
        "identifiers": "keep", "persons": "keep", "units": "keep",
        "locations": "keep", "directions": "keep", "delivery_places": "keep",
    },
    "friendly": {
        "identifiers": "mask", "persons": "mask", "units": "mask",
        "locations": "mask", "directions": "mask", "delivery_places": "mask",
    },
}
POLICY["unknown"] = POLICY["friendly"]

# ---------- шар 1: регулярні вирази ----------

IDENTIFIER_PATTERNS = [
    (re.compile(r"https?://(?:t\.me|telegram\.me)/\S+", re.I), "[TG_LINK]"),
    (re.compile(
        r"(?:https?://)?(?:[\w-]+\.)*(?:yoomoney\.ru|money\.yandex\.ru|tinkoff\.ru|tbank\.ru|donationalerts\.com"
        r"|boosty\.to|sberbank\.ru|qiwi\.com|qr\.nspk\.ru|donatepay\.ru|sponsr\.ru|mysbertips\.ru"
        r"|send\.monobank\.ua|monobank\.ua|privatbank\.ua|base\.monobank\.ua)\S*", re.I),
     "[PAY_LINK]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    (re.compile(r"\b(?:0x[0-9a-fA-F]{40}|bc1[0-9a-z]{25,60}|[13][1-9A-HJ-NP-Za-km-z]{25,34}"
                r"|T[1-9A-HJ-NP-Za-km-z]{33}|(?:UQ|EQ)[A-Za-z0-9_-]{46}|[48][0-9AB][1-9A-HJ-NP-Za-km-z]{93})\b"), "[WALLET]"),
    (re.compile(r"\bUA\d{2}(?:\s?\d){25}\b"), "[IBAN]"),
    (re.compile(r"(?<![\w/])@[A-Za-z][A-Za-z0-9_]{3,}"), "[USER]"),
    (re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)"), "[CARD]"),
    (re.compile(r"(?<!\d)\+?\d[\d\s().-]{8,}\d(?!\d)"), "[PHONE]"),
]

_UNIT_WORDS = (
    r"ОМБр|ОМПБр|ОШБр|ОДШБр|ДШБр|ОАеМБр|ОБрТрО|ОБрМП|ОМБ|ОШБ|ОМПБ|ТрО|СпН|ССО|ГУР|ПВК|"
    r"бригад\w*|батальйон\w*|батальон\w*|полк\w*|дивізі\w*|дивизи\w*|рот[аиуі]\b|роти\b|"
    r"окрем\w+\s+(?:\w+\s+){0,2}(?:бригад\w*|батальйон\w*|полк\w*)"
)
UNIT_PATTERNS = [
    # «47 ОМБр», «3-я штурмова бригада», «204 СПН»
    (re.compile(rf"\b\d{{1,4}}(?:\s*-?\s*(?:а|я|й|ї|ий|ої|го|ої|ой|ая))?\s+(?:[\w«»\"'-]+\s+){{0,2}}(?:{_UNIT_WORDS})", re.I), "[UNIT]"),
    # номер військової частини: «в/ч А1234», «військова частина А0000», «А7020»
    (re.compile(r"(?:в/ч|військов\w+\s+частин\w*|воинск\w+\s+част\w*)\s*[АA]?\s?\d{3,5}", re.I), "[UNIT]"),
    (re.compile(r"\b[АA]\d{4}\b"), "[UNIT]"),
]
DIRECTION_PATTERNS = [
    # «на Покровському напрямку», «Бахмутское направление»
    (re.compile(r"\b[А-ЯІЇЄҐA-Z][\w'-]*(?:ськ|цьк|зьк|ск|цк|зк)\w*\s+(?:напрям\w*|направлени\w*)", re.I), "[DIRECTION]"),
]
CALLSIGN_PATTERNS = [
    (re.compile(r"\b(позивн\w*|позывн\w*)\s+[«\"']?[\w-]+[»\"']?", re.I), r"\1 [PERSON]"),
]


def _apply(patterns, text: str, found: list, kind: str) -> str:
    def repl_factory(repl):
        def repl_fn(m):
            found.append({"type": kind, "value": m.group(0)})
            return m.expand(repl) if "\\" in repl else repl
        return repl_fn
    for rx, repl in patterns:
        text = rx.sub(repl_factory(repl), text)
    return text


# ---------- шар 2: сутності з відповіді LLM ----------

def _entities(data: dict | None) -> dict[str, set[str]]:
    """Значення з відповіді за схемою v3, згруповані за типом політики."""
    out = {"persons": set(), "units": set(), "locations": set(), "directions": set(), "delivery_places": set(),
           "identifiers": set()}
    if not data:
        return out
    for p in data.get("persons") or []:
        out["persons"].add(p.get("name", ""))
    for pay in data.get("payment_details") or []:
        out["identifiers"].add(pay.get("identifier", ""))
        if pay.get("holder"):
            out["persons"].add(pay["holder"])
    for c in data.get("contacts") or []:
        out["identifiers"].add(c.get("value", ""))
    for o in data.get("organizations") or []:
        if o.get("type") == "military_unit":
            out["units"].add(o.get("name", ""))
    for loc in data.get("locations") or []:
        out["locations"].add(loc)
    for d in data.get("frontline_directions") or []:
        out["directions"].add(d)
    for e in data.get("equipment") or []:
        if e.get("recipient"):
            out["units"].add(e["recipient"])
        if e.get("delivery_place"):
            out["delivery_places"].add(e["delivery_place"])
    return {k: {v.strip() for v in vals if v and v.strip() and not v.startswith("[")} for k, vals in out.items()}


MASK = {"persons": "[PERSON]", "units": "[UNIT]", "locations": "[LOCATION]", "directions": "[DIRECTION]",
        "delivery_places": "[LOCATION]", "identifiers": "[ID]"}


# Модель може повернути назву російською, а в тексті вона українською (і навпаки): «Иван» / «Івану».
_EQUIV = {"и": "[иіїы]", "і": "[иіїы]", "ї": "[иіїы]", "ы": "[иіы]", "е": "[еєэ]", "є": "[еєэ]", "э": "[еєэ]",
          "г": "[гґ]", "ґ": "[гґ]"}


def _stem_pattern(word: str) -> str:
    stem = word[:-2] if len(word) > 5 else (word[:-1] if len(word) > 3 else word)
    return "".join(_EQUIV.get(ch.lower(), re.escape(ch)) for ch in stem) + r"\w*"


def _entity_regex(value: str) -> re.Pattern:
    """Значення в називному відмінку -> шукаємо в тексті з будь-яким закінченням кожного слова."""
    parts = [_stem_pattern(w) for w in re.findall(r"\w+", value)]
    return re.compile(r"\b" + r"[\s\"'«».-]+".join(parts) + r"\b", re.I)


def _word_regexes(value: str) -> list[re.Pattern]:
    """Для людей: кожне слово імені окремо (ім'я і прізвище можуть стояти порізно)."""
    return [re.compile(r"\b" + _stem_pattern(w) + r"\b", re.I) for w in re.findall(r"\w+", value)
            if len(w) >= 4 and w.lower() not in ("позывной", "позивний")]


# ---------- публічні функції ----------

def policy(side: str | None) -> dict[str, str]:
    return POLICY.get(side or "unknown", POLICY["unknown"])


def mask_text(text: str, side: str | None, extracted: dict | None = None) -> tuple[str, list[dict]]:
    """Текст поста за політикою сторони. Повертає (текст, що саме замасковано)."""
    rules = policy(side)
    found: list[dict] = []
    if rules["identifiers"] == "mask":
        text = _apply(IDENTIFIER_PATTERNS, text, found, "identifiers")
    if rules["units"] == "mask":
        text = _apply(UNIT_PATTERNS, text, found, "units")
    if rules["directions"] == "mask":
        text = _apply(DIRECTION_PATTERNS, text, found, "directions")
    if rules["persons"] == "mask":
        text = _apply(CALLSIGN_PATTERNS, text, found, "persons")
    # Шар 2: довші значення першими, щоб «Покровське направлення» не розбилось на «Покровськ».
    ents = _entities(extracted)
    for kind in ("identifiers", "persons", "units", "directions", "delivery_places", "locations"):
        if rules.get(kind) != "mask":
            continue
        for value in sorted(ents[kind], key=len, reverse=True):
            regexes = [_entity_regex(value)] + (_word_regexes(value) if kind == "persons" else [])
            hit = False
            for rx in regexes:
                if rx.search(text):
                    hit = True
                    text = rx.sub(MASK[kind], text)
            if hit:
                found.append({"type": kind, "value": value})
    return text, found


def mask_extracted(data: dict | None, side: str | None) -> dict | None:
    """Відповідь за схемою v3 після політики сторони (для hostile — без змін)."""
    if data is None:
        return None
    rules = policy(side)
    d = copy.deepcopy(data)
    if rules["persons"] == "mask":
        for p in d.get("persons") or []:
            p["name"] = "[PERSON]"
    if rules["identifiers"] == "mask":
        for pay in d.get("payment_details") or []:
            pay["identifier"] = "[ID]"
            if pay.get("holder"):
                pay["holder"] = "[PERSON]"
        for c in d.get("contacts") or []:
            c["value"] = "[ID]"
    if rules["units"] == "mask":
        for o in d.get("organizations") or []:
            if o.get("type") == "military_unit":
                o["name"] = "[UNIT]"
        for e in d.get("equipment") or []:
            if e.get("recipient"):
                e["recipient"] = "[UNIT]"
    if rules["locations"] == "mask" and d.get("locations"):
        d["locations"] = ["[LOCATION]"] * len(d["locations"])
    if rules["directions"] == "mask" and d.get("frontline_directions"):
        d["frontline_directions"] = ["[DIRECTION]"] * len(d["frontline_directions"])
    if rules["delivery_places"] == "mask":
        for e in d.get("equipment") or []:
            if e.get("delivery_place"):
                e["delivery_place"] = "[LOCATION]"
    return d


def intel_entities(data: dict | None) -> dict[str, list]:
    """Для ворожих каналів: усе, що ідентифікує мережу постачання, одним списком (для зв'язування каналів)."""
    if not data:
        return {}
    return {
        "wallets": [p["identifier"] for p in data.get("payment_details") or [] if p.get("method") == "crypto_wallet"],
        "payment_details": [{k: p.get(k) for k in ("method", "provider", "identifier", "holder")} for p in data.get("payment_details") or []],
        "accounts": [c["value"] for c in data.get("contacts") or []],
        "persons": [p["name"] for p in data.get("persons") or []],
        "units": [o["name"] for o in data.get("organizations") or [] if o.get("type") == "military_unit"]
                 + [e["recipient"] for e in data.get("equipment") or [] if e.get("recipient")],
        "frontline_directions": list(data.get("frontline_directions") or []),
        "delivery_places": [e["delivery_place"] for e in data.get("equipment") or [] if e.get("delivery_place")],
        "locations": list(data.get("locations") or []),
    }
