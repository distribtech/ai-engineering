"""Схема v3: OSINT-розширення схеми v2 для постів волонтерських каналів.

Що додано до v2 (src/schema.py) і навіщо (детально — README, розділ «Схема v3»):
  - post_type: новий тип fundraising (збір коштів — найчастіший тип постів у цих каналах);
  - equipment[]: стан (потрібно / куплено / передано / в роботі / втрачено / продається),
    отримувач і технічні характеристики (specs);
  - organizations[]: тепер об'єкти з типом (військова частина, волонтерська група, компанія…)
    і роллю в пості (отримувач, постачальник, партнер, згадка) — щоб будувати ланцюжки
    «хто кому що постачає»;
  - persons[]: люди й позивні з роллю (автор, збирач коштів, військовий…);
  - contacts[]: Telegram-акаунти, боти, канали, телефони, пошта, сайти;
  - payment_details[]: картки, СБП, рахунки, криптогаманці, платіжні посилання —
    щоб бачити, що той самий гаманець чи картка трапляється в різних каналах;
  - technical_content[]: чи є в пості ТТХ, інструкція, тест, звіт про поломку, бойове застосування.

Персональні дані. Імена, позивні, @-імена Telegram і криптогаманці беремо як є: це публічна
інформація з відкритих каналів, і саме вона потрібна, щоб бачити, що той самий гаманець чи
акаунт трапляється в різних каналах. Номери карток, телефони й e-mail у clean-текстах
замасковані ([CARD], [PHONE], [EMAIL]); модель копіює маску як є і не вигадує номер.

Схему v2 не змінено: на ній розмічено й перевірено golden set і зроблено заміри v1/v2.
v3 запрацює в пайплайні після переразмітки golden set під нові поля.

Експорт JSON Schema:
  python -m src.schema_v3          -> schema_v3.json
"""

from __future__ import annotations

import json
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator

# Спільне з v2: мова, категорії техніки, валюта, суми.
from src.schema import CATEGORY_DESCRIPTION, EquipmentCategory, Language, Money, _Strict

SCHEMA_VERSION = "v3"


class PostType(str, Enum):
    FUNDRAISING = "fundraising"              # v3: збір коштів на конкретну закупівлю, стан збору, реквізити
    DELIVERY_REPORT = "delivery_report"      # звіт про закупівлю / передачу обладнання
    EQUIPMENT_REVIEW = "equipment_review"    # огляд, тест, обговорення, несправність техніки
    NEWS = "news"                            # новина, інцидент, подія у світі
    EVENT = "event"                          # анонс або звіт про захід, навчання, виставку
    ANNOUNCEMENT = "announcement"            # службове оголошення каналу
    ADVERTISEMENT = "advertisement"          # реклама продукту чи компанії
    VACANCY = "vacancy"                      # вакансія, набір людей
    REQUEST = "request"                      # прохання щось знайти, передати, обміняти (не гроші)
    GUIDE = "guide"                          # інструкція, правила, пояснення
    PERSONAL = "personal"                    # особисті думки без фактів
    HUMOR = "humor"
    OTHER = "other"


class EquipmentStatus(str, Enum):
    NEEDED = "needed"            # потрібно, збираємо кошти, шукаємо
    PURCHASED = "purchased"      # куплено, замовлено, в дорозі
    DELIVERED = "delivered"      # передано підрозділу чи людині
    IN_USE = "in_use"            # використовується, тестується, налаштовується
    DAMAGED_LOST = "damaged_lost"  # зламано, знищено, втрачено, захоплено
    FOR_SALE = "for_sale"        # пропонується до продажу, реклама
    MENTIONED = "mentioned"      # просто згадка, огляд без дії


class Spec(_Strict):
    parameter: str = Field(
        min_length=1,
        description="Parameter name in Russian, lower case, as in the text: 'высота', 'дальность', 'частота', 'емкость', 'вес'.",
    )
    value: str = Field(
        min_length=1,
        description="Value with unit and qualifiers as written: 'до 12 м', '433 МГц', '10000 мАч', 'не более 5-10 минут'.",
    )


class EquipmentItemV3(_Strict):
    name: str = Field(
        min_length=1,
        description="Model or item name as written in the text, without quantity, e.g. 'ТА-57', 'TYT-7500', 'кабель полевой П-274м'.",
    )
    category: EquipmentCategory = Field(description=CATEGORY_DESCRIPTION)
    manufacturer: Optional[str] = Field(
        description=(
            "Manufacturer or brand if explicitly stated (e.g. 'Silvus', 'Аргус-М'), including a brand that is part "
            "of the model name ('TYT-7500' -> 'TYT'), otherwise null. Do not guess; a country is not a manufacturer."
        ),
    )
    quantity: Optional[float] = Field(
        gt=0,
        description="Number of items or amount only if a number is written explicitly ('10 шт.' -> 10, '6 км' -> 6); otherwise null.",
    )
    unit: Optional[str] = Field(
        description="Unit for quantity: 'шт' for pieces, or the unit from the text ('км', 'м', 'кг'). Null if quantity is null.",
    )
    status: EquipmentStatus = Field(
        description=(
            "What happens to this item in the post. needed: money is being raised or the item is wanted; "
            "purchased: bought, ordered, on the way; delivered: handed over to a unit or person; "
            "in_use: used, tested, configured; damaged_lost: broken, destroyed, lost, captured; "
            "for_sale: offered or advertised; mentioned: anything else."
        ),
    )
    recipient: Optional[str] = Field(
        description=(
            "Unit, group or person the item is for or was handed to, as in organizations or persons "
            "('204 СПН \"Ахмат\"', 'позывной Кот'); null if not stated."
        ),
    )
    specs: list[Spec] = Field(
        description="Technical characteristics stated for this item (range, frequency, power, capacity, size, weight, setup time). Empty list if none.",
    )


class OrgType(str, Enum):
    MILITARY_UNIT = "military_unit"      # частина, підрозділ, батальйон, бригада, ПВК
    VOLUNTEER_GROUP = "volunteer_group"  # волонтерський проєкт, фонд, гуманітарна місія
    COMPANY = "company"                  # виробник, магазин, підприємство
    GOVERNMENT = "government"            # міністерство, адміністрація, державний орган, партія
    MEDIA = "media"                      # канал, ЗМІ, блогер-проєкт
    OTHER = "other"                      # альянси (НАТО), іноземні організації тощо


class OrgRole(str, Enum):
    RECIPIENT = "recipient"   # отримує техніку чи допомогу
    SUPPLIER = "supplier"     # виробляє, продає, постачає, збирає
    PARTNER = "partner"       # співпрацює, допомагає, проводить захід разом
    MENTIONED = "mentioned"   # просто згадується


class Organization(_Strict):
    name: str = Field(
        min_length=1,
        description=(
            "Name in nominative case, without emoji, obvious typos fixed ('Призрак Новоросии' -> 'Призрак Новороссии'); "
            "a company by its short name ('Московский завод осветительных установок Аргус-М' -> 'Аргус-М'). "
            "Exclude social platforms mentioned only as links (Telegram, VK, Rutube, Дзен, MAX)."
        ),
    )
    type: OrgType = Field(description="Kind of organization.")
    role: OrgRole = Field(description="Role in this post: recipient of help or equipment, supplier/manufacturer/fundraiser, partner, or just mentioned.")


class PersonRole(str, Enum):
    AUTHOR = "author"            # автор поста / каналу
    FUNDRAISER = "fundraiser"    # на кого збір, власник картки
    MILITARY = "military"        # військовий, командир, боєць
    RECIPIENT = "recipient"      # отримувач допомоги
    OTHER = "other"


class Person(_Strict):
    name: str = Field(
        min_length=1,
        description=(
            "Name, surname or call sign exactly as written, in nominative case ('Киба', 'Тимур Громов', 'позывной Кот'). "
            "Only people named in the text; not public figures mentioned in passing news."
        ),
    )
    role: PersonRole = Field(description="Role of the person in the post.")


class ContactType(str, Enum):
    TELEGRAM_USER = "telegram_user"
    TELEGRAM_BOT = "telegram_bot"
    TELEGRAM_CHANNEL = "telegram_channel"
    PHONE = "phone"
    EMAIL = "email"
    WEBSITE = "website"
    OTHER = "other"


class Contact(_Strict):
    type: ContactType = Field(description="Kind of contact. A username ending in 'bot' is telegram_bot.")
    value: str = Field(
        min_length=1,
        description=(
            "The contact exactly as in the text: '@username', 't.me/channel', 'example.ru'. If the text has a mask "
            "such as [PHONE], [USER], [TG_LINK] or [EMAIL], copy the mask unchanged and never invent the real value."
        ),
    )
    purpose: Optional[str] = Field(
        description="What to contact for, a few words in Russian as in the text ('для заказов', 'по вопросам сбора'); null if not stated.",
    )


class PaymentMethod(str, Enum):
    BANK_CARD = "bank_card"
    SBP_PHONE = "sbp_phone"          # Система быстрых платежей за номером телефону
    BANK_ACCOUNT = "bank_account"    # розрахунковий рахунок, реквізити юрособи
    CRYPTO_WALLET = "crypto_wallet"
    PAYMENT_LINK = "payment_link"    # ЮMoney, Boosty, CloudTips, QR-посилання тощо
    OTHER = "other"


class PaymentDetail(_Strict):
    method: PaymentMethod = Field(description="How the money is sent.")
    provider: Optional[str] = Field(
        description=(
            "Bank, service or crypto network as written, normalized: 'Сбербанк', 'Т-Банк', 'ЮMoney', 'USDT TRC20', 'BTC', 'TON'. "
            "Null if not stated."
        ),
    )
    identifier: str = Field(
        min_length=1,
        description=(
            "Card, phone, wallet address or link exactly as in the text, without spaces added or removed in the middle. "
            "If the text has a mask such as [CARD], [PHONE], [WALLET] or [PAY_LINK], copy the mask unchanged."
        ),
    )
    holder: Optional[str] = Field(
        description="Name of the recipient or card holder exactly as written ('Тимур Максимович Г.'), null if not stated.",
    )


class TechnicalContent(str, Enum):
    SPECS = "specs"                    # ТТХ: дальність, частоти, ємність, розміри
    INSTRUCTION = "instruction"        # інструкція, налаштування, прошивка, схема
    TEST_REVIEW = "test_review"        # тест, порівняння, огляд
    FAILURE_DAMAGE = "failure_damage"  # поломка, відмова, ураження
    COMBAT_USE = "combat_use"          # застосування на фронті, ефективність


class ExtractedPostV3(_Strict):
    post_type: PostType = Field(
        description="Main purpose of the post. Pick one. A post that mainly asks for money is fundraising even if it lists equipment.",
    )
    language: Language = Field(
        description="Main language of the text. Russian with English technical terms is still 'ru'; use 'mixed' only when whole sentences are in different languages.",
    )
    summary: str = Field(min_length=1, max_length=300, description="One-sentence summary in Ukrainian, up to 300 characters.")
    equipment: list[EquipmentItemV3] = Field(
        description=(
            "Concrete equipment, devices or software products mentioned. A specific item the post is about counts even "
            "without a model name ('убило новый генератор' -> 'генератор'); general mentions of equipment types "
            "('генераторы в больницах') and generic words like 'связь' or 'оборудование' do not. Empty list if none."
        ),
    )
    technical_content: list[TechnicalContent] = Field(
        description="Kinds of technical information present in the post. Empty list if none.",
    )
    organizations: list[Organization] = Field(description="Organizations named in the text. Empty list if none.")
    persons: list[Person] = Field(description="People and call signs named in the text. Empty list if none.")
    locations: list[str] = Field(
        description=(
            "Geographic places (countries, regions, cities, districts) in nominative case and full form, always in Russian: "
            "'в Мск' -> 'Москва', 'Mariupol' -> 'Мариуполь'. Include the place where equipment is made "
            "('производства Украины' -> 'Украина', 'Московский завод' -> 'Москва'). No metro stations. Empty list if none."
        ),
    )
    contacts: list[Contact] = Field(description="Ways to contact the author or others: Telegram users, bots, channels, phones, e-mails, websites. Not payment details. Empty list if none.")
    payment_details: list[PaymentDetail] = Field(description="Where to send money: cards, SBP phones, accounts, crypto wallets, payment links. Empty list if none.")
    money: list[Money] = Field(description="Money amounts mentioned. Empty list if none.")
    event_date: Optional[date] = Field(
        description=(
            "Date of the event the post is about, YYYY-MM-DD, only if day, month and year can be read from the text "
            "(the year may come from an event name such as 'Дронница-2026'). For a range use the start date. Relative dates -> null."
        ),
    )
    call_to_action: bool = Field(
        description="True if the post asks readers to do something: write to a bot or DM, apply, register, repost, send or donate.",
    )

    @field_validator("locations")
    @classmethod
    def _dedupe_locations(cls, values: list[str]) -> list[str]:
        seen: dict[str, str] = {}
        for v in values:
            v = v.strip()
            if v and v.casefold() not in seen:
                seen[v.casefold()] = v
        return list(seen.values())

    @field_validator("organizations")
    @classmethod
    def _dedupe_orgs(cls, values: list[Organization]) -> list[Organization]:
        seen: dict[str, Organization] = {}
        for o in values:
            seen.setdefault(o.name.casefold(), o)
        return list(seen.values())

    @field_validator("technical_content")
    @classmethod
    def _dedupe_flags(cls, values: list) -> list:
        return list(dict.fromkeys(values))


def json_schema() -> dict:
    return ExtractedPostV3.model_json_schema()


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "schema_v3.json"
    out.write_text(json.dumps(json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON Schema v3 -> {out.name}")
