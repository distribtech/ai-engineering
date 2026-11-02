"""Перевірка src/privacy.py: дружні тексти деперсоналізуються, ворожі лишаються як є.

Запуск:  python -m tests.test_privacy
Усі імена, номери й гаманці в тесті вигадані.
"""

from __future__ import annotations

from src.privacy import intel_entities, mask_extracted, mask_text

FRIENDLY = (
    "Передали 5 дронів для 47 ОМБр на Покровському напрямку, отримував позивний Сокіл. "
    "Дякуємо Івану Петренку! Збір: картка 5375 4141 0000 1111, монобанка https://send.monobank.ua/jar/ABC123, "
    "питання до @example_volunteer. Наступна передача — Краматорськ, в/ч А1234."
)
EXTRACTED = {
    "persons": [{"name": "Иван Петренко", "role": "fundraiser"}],
    "organizations": [{"name": "Фонд Сергія Притули", "type": "volunteer_group", "role": "supplier"},
                      {"name": "47 ОМБр", "type": "military_unit", "role": "recipient"}],
    "locations": ["Краматорськ"],
    "frontline_directions": ["Покровський напрямок"],
    "equipment": [{"name": "дрон", "recipient": "47 ОМБр", "delivery_place": "Покровський напрямок"}],
    "contacts": [{"type": "telegram_user", "value": "@example_volunteer", "purpose": None}],
    "payment_details": [{"method": "bank_card", "provider": "monobank", "identifier": "5375 4141 0000 1111", "holder": None}],
}

LEAKS = ["47 ОМБр", "Покровськ", "Сокіл", "Петренк", "5375", "monobank.ua/jar", "@example_volunteer", "Краматорськ", "А1234"]


def test_friendly_text_masked():
    text, found = mask_text(FRIENDLY, "friendly", EXTRACTED)
    for leak in LEAKS:
        assert leak not in text, f"витік у дружньому тексті: {leak!r}\n{text}"
    assert "дронів" in text and "Передали" in text, "звичайний текст має лишитись"
    assert {f["type"] for f in found} >= {"identifiers", "units", "directions", "persons", "locations"}


def test_unknown_is_friendly():
    assert mask_text(FRIENDLY, "unknown", EXTRACTED)[0] == mask_text(FRIENDLY, "friendly", EXTRACTED)[0]


def test_hostile_text_untouched():
    text, found = mask_text(FRIENDLY, "hostile", EXTRACTED)
    assert text == FRIENDLY and found == []


def test_friendly_json_masked_but_funds_kept():
    d = mask_extracted(EXTRACTED, "friendly")
    assert d["persons"][0]["name"] == "[PERSON]"
    assert [o["name"] for o in d["organizations"]] == ["Фонд Сергія Притули", "[UNIT]"]
    assert d["frontline_directions"] == ["[DIRECTION]"] and d["locations"] == ["[LOCATION]"]
    assert d["equipment"][0]["recipient"] == "[UNIT]" and d["equipment"][0]["delivery_place"] == "[LOCATION]"
    assert d["payment_details"][0]["identifier"] == "[ID]" and d["contacts"][0]["value"] == "[ID]"
    assert EXTRACTED["persons"][0]["name"] == "Иван Петренко", "оригінал не має змінюватись"


def test_hostile_intel():
    intel = intel_entities(EXTRACTED)
    assert intel["units"] == ["47 ОМБр", "47 ОМБр"]
    assert intel["frontline_directions"] == ["Покровський напрямок"]
    assert intel["accounts"] == ["@example_volunteer"]
    assert mask_extracted(EXTRACTED, "hostile") == EXTRACTED


if __name__ == "__main__":
    test_friendly_text_masked()
    test_unknown_is_friendly()
    test_hostile_text_untouched()
    test_friendly_json_masked_but_funds_kept()
    test_hostile_intel()
    print("OK: дружній текст і JSON без людей, підрозділів, напрямків і місць; фонди лишились; ворожий — без змін, intel зібрано")
