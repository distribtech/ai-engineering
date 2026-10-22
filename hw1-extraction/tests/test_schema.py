"""Перевірка схеми на трьох розмічених постах і на навмисно невалідних відповідях.

Запуск:  python -m tests.test_schema   (або pytest, якщо встановлено)
"""

from __future__ import annotations

from pydantic import ValidationError

from src.schema import ExtractedPost

# #3536: нумерований список з кількостями та одиницями
POST_3536 = {
    "post_type": "delivery_report",
    "language": "ru",
    "summary": "Автор доставив у 204 СпН «Ахмат» партію засобів польового зв'язку: телефони, кабель і щогли.",
    "equipment": [
        {"name": "телефон офисный", "category": "phone", "manufacturer": None, "quantity": 10, "unit": "шт"},
        {"name": "изд. Тапикатор", "category": "phone", "manufacturer": None, "quantity": 10, "unit": "шт"},
        {"name": "ТА-57", "category": "phone", "manufacturer": None, "quantity": 10, "unit": "шт"},
        {"name": "кабель полевой П-274м", "category": "cable", "manufacturer": None, "quantity": 6, "unit": "км"},
        {"name": "мачты сборные", "category": "antenna_mast", "manufacturer": "Призрак Новороссии", "quantity": 12, "unit": "шт"},
    ],
    "organizations": ['204 СПН "Ахмат"', "Призрак Новороссии"],
    "locations": [],
    "event_date": None,
    "money": [],
    "call_to_action": True,
}

# #2593: вакансія з діапазоном зарплати
POST_2593 = {
    "post_type": "vacancy",
    "language": "ru",
    "summary": "Шукають монтажників-збирачів електроніки в лабораторію в Москві з зарплатою 80–120 тис. руб. і бронюванням.",
    "equipment": [],
    "organizations": [],
    "locations": ["Москва"],
    "event_date": None,
    "money": [{"amount_min": 80000, "amount_max": 120000, "currency": "RUB", "purpose": "salary"}],
    "call_to_action": True,
}

# #4442: 43 символи, майже всі поля порожні
POST_4442 = {
    "post_type": "personal",
    "language": "ru",
    "summary": "Неякісне пальне вивело з ладу новий генератор.",
    "equipment": [{"name": "генератор", "category": "power", "manufacturer": None, "quantity": None, "unit": None}],
    "organizations": [],
    "locations": [],
    "event_date": None,
    "money": [],
    "call_to_action": False,
}


def test_valid_examples():
    for data in (POST_3536, POST_2593, POST_4442):
        ExtractedPost.model_validate(data)


def _expect_invalid(data: dict, reason: str) -> None:
    try:
        ExtractedPost.model_validate(data)
    except ValidationError:
        return
    raise AssertionError(f"мало бути невалідно: {reason}")


def test_invalid_examples():
    _expect_invalid({**POST_2593, "post_type": "job"}, "значення поза enum")
    _expect_invalid({k: v for k, v in POST_2593.items() if k != "locations"}, "пропущене обов'язкове поле")
    _expect_invalid({**POST_2593, "extra_field": 1}, "зайве поле")
    _expect_invalid(
        {**POST_2593, "money": [{"amount_min": 120000, "amount_max": 80000, "currency": "RUB", "purpose": "salary"}]},
        "amount_max < amount_min",
    )
    _expect_invalid(
        {**POST_3536, "equipment": [{"name": "ТА-57", "category": "phone", "manufacturer": None, "quantity": 0, "unit": "шт"}]},
        "quantity = 0",
    )
    _expect_invalid({**POST_4442, "event_date": "12 октября"}, "дата не в ISO")


def test_categories_grouped():
    from src.schema import CATEGORY_GROUP, EquipmentCategory

    values = {c.value for c in EquipmentCategory}
    assert values == set(CATEGORY_GROUP), values ^ set(CATEGORY_GROUP)
    _expect_invalid(
        {**POST_3536, "equipment": [{"name": "Mavic 3", "category": "drone", "manufacturer": None, "quantity": None, "unit": None}]},
        "застаріла категорія 'drone' (v1)",
    )
    ExtractedPost.model_validate(
        {**POST_3536, "equipment": [{"name": "Mavic 3T", "category": "drone_multirotor", "manufacturer": "DJI", "quantity": 2, "unit": "шт"}]}
    )


def test_dedupe():
    post = ExtractedPost.model_validate({**POST_2593, "locations": ["Москва", " москва ", "Курская"]})
    assert post.locations == ["Москва", "Курская"], post.locations


if __name__ == "__main__":
    test_valid_examples()
    test_invalid_examples()
    test_categories_grouped()
    test_dedupe()
    print("OK: 3 валідні приклади, 6 невалідних відхилено, категорії v2 згруповані, дедуплікація працює")
