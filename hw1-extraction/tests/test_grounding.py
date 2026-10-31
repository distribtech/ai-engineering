"""Перевірка src/grounding.py: вигадані й спотворені ідентифікатори мають ловитися.

Запуск:  python -m tests.test_grounding
Гаманці й номери в тесті вигадані.
"""

from __future__ import annotations

from src.grounding import check_grounding

ETH = "0x" + "ab12" * 10
TRON = "T" + "Abc1" * 8 + "X"
TEXT = f"""Собираем на тепловизоры для бойцов позывного Кота.
Карта Сбербанк [CARD] Ивана Петрова. USDT TRC20: {TRON}
ETH: {ETH}
По вопросам: @Example_Help_bot"""

GOOD = {
    "payment_details": [
        {"method": "bank_card", "provider": "Сбербанк", "identifier": "[CARD]", "holder": "Иван Петров"},
        {"method": "crypto_wallet", "provider": "USDT TRC20", "identifier": TRON, "holder": None},
        {"method": "crypto_wallet", "provider": "ETH", "identifier": ETH, "holder": None},
    ],
    "contacts": [{"type": "telegram_bot", "value": "@example_help_bot", "purpose": None}],  # інший регістр - ок
    "persons": [{"name": "Кот", "role": "military"}, {"name": "Иван Петров", "role": "fundraiser"}],  # відмінки - ок
}


def levels(data):
    return [(i.level, i.field) for i in check_grounding(TEXT, data)]


def test_good():
    assert levels(GOOD) == [], levels(GOOD)


def test_distorted_wallet():
    bad = {"payment_details": [{"method": "crypto_wallet", "provider": "ETH", "identifier": ETH[:-1] + "c", "holder": None}]}
    assert ("error", "payment_details[0].identifier") in levels(bad)


def test_wallet_case_changed():
    bad = {"payment_details": [{"method": "crypto_wallet", "provider": "USDT TRC20", "identifier": TRON.lower(), "holder": None}]}
    assert ("error", "payment_details[0].identifier") in levels(bad)


def test_invented_contact_and_person():
    bad = {
        "contacts": [{"type": "telegram_user", "value": "@invented_user", "purpose": None}],
        "persons": [{"name": "Сергей Сидоров", "role": "other"}],
    }
    assert levels(bad) == [("error", "contacts[0].value"), ("warning", "persons[0].name")]


def test_unmasked_card():
    bad = {"payment_details": [{"method": "bank_card", "provider": None, "identifier": "2202 0000 1111 2222", "holder": None}]}
    assert ("error", "payment_details[0].identifier") in levels(bad)


if __name__ == "__main__":
    test_good()
    test_distorted_wallet()
    test_wallet_case_changed()
    test_invented_contact_and_person()
    test_unmasked_card()
    print("OK: правильні значення проходять; спотворений гаманець, змінений регістр, вигадані контакт, ім'я й «розмаскована» картка ловляться")
