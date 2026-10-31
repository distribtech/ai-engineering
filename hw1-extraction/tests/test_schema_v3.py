"""Перевірка схеми v3 (src/schema_v3.py) на ручних прикладах.

Запуск:  python -m tests.test_schema_v3
"""

from __future__ import annotations

import copy

from pydantic import ValidationError

from src.schema_v3 import ExtractedPostV3

# Пост #915 з golden set (реклама щогли «Аргус-М»), розмічений за v3.
POST_915 = {
    "post_type": "advertisement",
    "language": "ru",
    "summary": "Московський завод «Аргус-М» пропонує недорогі телескопічні щогли висотою до 12 м для ретрансляторів і камер спостереження.",
    "equipment": [{
        "name": "мачта телескопическая", "category": "antenna_mast", "manufacturer": "Аргус-М",
        "quantity": None, "unit": None, "status": "for_sale", "recipient": None,
        "specs": [
            {"parameter": "высота", "value": "до 12 м"},
            {"parameter": "длина в сложенном виде", "value": "не более 2.2 м"},
            {"parameter": "время установки", "value": "не более 5-10 минут"},
        ],
    }],
    "technical_content": ["specs"],
    "organizations": [{"name": "Аргус-М", "type": "company", "role": "supplier"}],
    "persons": [],
    "locations": ["Москва"],
    "contacts": [{"type": "telegram_user", "value": "[USER]", "purpose": "для заказов"}],
    "payment_details": [],
    "money": [],
    "event_date": None,
    "call_to_action": True,
}

# Вигаданий пост-збір: гаманець і картка не справжні.
FUNDRAISING = {
    "post_type": "fundraising",
    "language": "ru",
    "summary": "Автор збирає 120 тис. рублів на два тепловізори для підрозділу «Кот».",
    "equipment": [{
        "name": "тепловизор", "category": "optics", "manufacturer": None, "quantity": 2, "unit": "шт",
        "status": "needed", "recipient": "позывной Кот", "specs": [],
    }],
    "technical_content": [],
    "organizations": [],
    "persons": [{"name": "Кот", "role": "military"}, {"name": "Иван П.", "role": "fundraiser"}],
    "locations": [],
    "contacts": [{"type": "telegram_bot", "value": "@example_help_bot", "purpose": "по вопросам сбора"}],
    "payment_details": [
        {"method": "bank_card", "provider": "Сбербанк", "identifier": "[CARD]", "holder": "Иван П."},
        {"method": "crypto_wallet", "provider": "USDT TRC20", "identifier": "TExampleWalletAddressNotReal000000", "holder": None},
    ],
    "money": [{"amount_min": 120000, "amount_max": None, "currency": "RUB", "purpose": "fundraising"}],
    "event_date": None,
    "call_to_action": True,
}


def _bad(base: dict, mutate) -> dict:
    data = copy.deepcopy(base)
    mutate(data)
    return data


INVALID = [
    ("невідомий стан техніки", _bad(FUNDRAISING, lambda d: d["equipment"][0].update(status="stolen"))),
    ("організація рядком, а не об'єктом", _bad(POST_915, lambda d: d.update(organizations=["Аргус-М"]))),
    ("невідомий спосіб оплати", _bad(FUNDRAISING, lambda d: d["payment_details"][0].update(method="cash"))),
    ("порожній identifier", _bad(FUNDRAISING, lambda d: d["payment_details"][1].update(identifier=""))),
    ("зайве поле", _bad(POST_915, lambda d: d.update(wallets=[]))),
    ("пропущено обов'язкове поле", _bad(POST_915, lambda d: d.pop("persons"))),
    ("невідомий тип технічного вмісту", _bad(POST_915, lambda d: d.update(technical_content=["photo"]))),
]


def test_valid():
    for data in (POST_915, FUNDRAISING):
        ExtractedPostV3.model_validate(data)


def test_invalid():
    for name, data in INVALID:
        try:
            ExtractedPostV3.model_validate(data)
        except ValidationError:
            continue
        raise AssertionError(f"мало бути невалідним: {name}")


def test_dedupe():
    data = copy.deepcopy(POST_915)
    data["organizations"].append({"name": "аргус-м", "type": "company", "role": "mentioned"})
    data["technical_content"] = ["specs", "specs", "instruction"]
    post = ExtractedPostV3.model_validate(data)
    assert [o.name for o in post.organizations] == ["Аргус-М"]
    assert post.technical_content == ["specs", "instruction"]


if __name__ == "__main__":
    test_valid()
    test_invalid()
    test_dedupe()
    print(f"OK: 2 валідні приклади, {len(INVALID)} невалідних відхилено, дедуплікація працює")
