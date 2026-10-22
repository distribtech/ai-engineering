"""Схема структурованих даних, які LLM витягує з поста Telegram-каналу.

Усі поля обов'язкові: відсутнє значення - це null або порожній список, а не
пропущений ключ. Так вимагає strict structured output (OpenAI), і так само
працює з Anthropic, Gemini та Ollama (format=<JSON Schema>).

Описи полів (description) йдуть у JSON Schema і, відповідно, у запит до моделі,
тому вони англійською й містять правила нормалізації, за якими розмічено golden set.

Експорт JSON Schema:
  python -m src.schema            -> schema.json
"""

from __future__ import annotations

import json
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PostType(str, Enum):
    DELIVERY_REPORT = "delivery_report"      # звіт про закупівлю / передачу обладнання
    EQUIPMENT_REVIEW = "equipment_review"    # огляд, обговорення, несправність техніки
    NEWS = "news"                            # новина, інцидент, подія у світі
    EVENT = "event"                          # анонс або звіт про захід, навчання, виставку
    ANNOUNCEMENT = "announcement"            # оголошення каналу: реліз, оновлення, службове
    ADVERTISEMENT = "advertisement"          # реклама продукту чи компанії
    VACANCY = "vacancy"                      # вакансія, набір людей
    REQUEST = "request"                      # прохання щось знайти, передати, обміняти
    GUIDE = "guide"                          # інструкція, правила, пояснення
    PERSONAL = "personal"                    # особисті думки без фактів
    HUMOR = "humor"
    OTHER = "other"


class Language(str, Enum):
    RU = "ru"
    UK = "uk"
    EN = "en"
    MIXED = "mixed"


class EquipmentCategory(str, Enum):
    """Тип техніки. v2: дрони розбито на підтипи, додано мережі, IT, захист, спорядження, медицину, озброєння."""

    # зв'язок
    RADIO = "radio"                        # радіостанції, ретранслятори, MANET-модулі
    PHONE = "phone"                        # польові та стаціонарні телефони
    ANTENNA_MAST = "antenna_mast"          # антени, щогли
    CABLE = "cable"                        # кабелі, лінії, котушки
    SATELLITE = "satellite"                # супутникові термінали, Starlink
    NETWORK = "network"                    # роутери, комутатори, Wi-Fi мости, mesh-вузли
    # БПЛА
    DRONE_FPV = "drone_fpv"                # FPV-дрони
    DRONE_MULTIROTOR = "drone_multirotor"  # квадрокоптери-розвідники (Mavic, Matrice, Autel), важкі мультикоптери
    DRONE_FIXED_WING = "drone_fixed_wing"  # літаки-розвідники, «крила»
    DRONE_COMPONENT = "drone_component"    # мотори, відеопередавачі, пульти, окуляри, АКБ і рами для дронів
    # РЕБ / розвідка
    EW_SIGINT = "ew_sigint"                # РЕБ, РЕР, детектори дронів, радари
    OPTICS = "optics"                      # тепловізори, нічні приціли, далекоміри, біноклі
    # забезпечення
    POWER = "power"                        # генератори, АКБ (не для дронів), повербанки, зарядні, сонячні панелі
    VEHICLE = "vehicle"                    # автомобілі, мотоцикли, квадроцикли та запчастини до них
    IT_HARDWARE = "it_hardware"            # ноутбуки, планшети, смартфони, флешки
    SOFTWARE = "software"                  # застосунки, месенджери, прошивки, VPN
    TOOLS = "tools"                        # інструменти, паяльники, ремкомплекти, витратні матеріали
    # особовий склад
    PROTECTIVE_GEAR = "protective_gear"    # бронежилети, шоломи, плитники, балістичні окуляри
    FIELD_GEAR = "field_gear"              # одяг, взуття, розвантаження, спальники, намети, окопні свічки
    MEDICAL = "medical"                    # аптечки, турнікети, перев'язувальні, медобладнання, ліки
    # озброєння
    WEAPON_RELATED = "weapon_related"      # зброя, боєприпаси, скиди, детонатори та аксесуари до них
    OTHER = "other"


# Групи для аналітики (у JSON Schema не потрапляють, модель їх не заповнює).
CATEGORY_GROUP: dict[str, str] = {
    "radio": "comms", "phone": "comms", "antenna_mast": "comms", "cable": "comms",
    "satellite": "comms", "network": "comms",
    "drone_fpv": "drones", "drone_multirotor": "drones", "drone_fixed_wing": "drones", "drone_component": "drones",
    "ew_sigint": "ew_recon", "optics": "ew_recon",
    "power": "support", "vehicle": "support", "it_hardware": "support", "software": "support", "tools": "support",
    "protective_gear": "personnel", "field_gear": "personnel", "medical": "personnel",
    "weapon_related": "weapons",
    "other": "other",
}

CATEGORY_DESCRIPTION = (
    "Equipment type. "
    "radio: radios, repeaters, MANET modules; phone: field/landline phones; antenna_mast: antennas, masts; "
    "cable: cables, field lines, reels; satellite: satellite terminals, Starlink; network: routers, switches, Wi-Fi bridges, mesh nodes; "
    "drone_fpv: FPV drones; drone_multirotor: recon quadcopters (Mavic, Matrice, Autel) and heavy multirotors; "
    "drone_fixed_wing: fixed-wing UAVs; drone_component: motors, video transmitters, controllers, goggles, drone batteries, frames; "
    "ew_sigint: EW/jammers, SIGINT, drone detectors, radars; optics: thermal and night sights, rangefinders, binoculars; "
    "power: generators, batteries not for drones, power banks, chargers, solar panels; vehicle: cars, motorcycles, ATVs and parts; "
    "it_hardware: laptops, tablets, smartphones, flash drives; software: apps, messengers, firmware, VPN; "
    "tools: tools, soldering, repair kits, consumables; protective_gear: body armor, helmets, plate carriers, ballistic glasses; "
    "field_gear: clothing, boots, chest rigs, sleeping bags, tents, trench candles; medical: first aid kits, tourniquets, dressings, medical devices, medicines; "
    "weapon_related: weapons, ammunition, drone drop munitions, detonators and their accessories; other: anything else."
)


class Currency(str, Enum):
    RUB = "RUB"
    USD = "USD"
    EUR = "EUR"
    UAH = "UAH"
    OTHER = "OTHER"


class MoneyPurpose(str, Enum):
    PRICE = "price"                    # ціна товару
    SALARY = "salary"                  # зарплата
    PURCHASE_TOTAL = "purchase_total"  # скільки витрачено на закупівлю
    FUNDRAISING = "fundraising"        # ціль або стан збору коштів
    OTHER = "other"


class _Strict(BaseModel):
    # extra="forbid" -> additionalProperties: false у JSON Schema (потрібно для strict mode)
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, use_enum_values=True)


class EquipmentItem(_Strict):
    name: str = Field(
        min_length=1,
        description="Model or item name as written in the text, without quantity, e.g. 'ТА-57', 'TYT-7500', 'кабель полевой П-274м'.",
    )
    category: EquipmentCategory = Field(description=CATEGORY_DESCRIPTION)
    manufacturer: Optional[str] = Field(
        description="Manufacturer or brand if explicitly stated (e.g. 'Silvus', 'Аргус-М'), otherwise null. Do not guess.",
    )
    quantity: Optional[float] = Field(
        gt=0,
        description="Number of items or amount only if a number is written explicitly (e.g. 10 for '10 шт.', 6 for '6 км'); 'a generator' or 'экземпляр' -> null.",
    )
    unit: Optional[str] = Field(
        description="Unit for quantity: 'шт' for pieces, or the unit from the text ('км', 'м', 'кг'). Null if quantity is null.",
    )


class Money(_Strict):
    amount_min: float = Field(
        ge=0,
        description="Amount as a plain number: '14300 рублей' -> 14300, '80к' -> 80000, '1м' -> 1000000. For a range, the lower bound.",
    )
    amount_max: Optional[float] = Field(
        ge=0,
        description="Upper bound for a range ('от 80к до 120к' -> 120000), otherwise null.",
    )
    currency: Currency = Field(description="Currency; 'р', 'руб', 'к рублей' -> RUB.")
    purpose: MoneyPurpose = Field(description="What the amount refers to.")

    @model_validator(mode="after")
    def _range_order(self) -> "Money":
        if self.amount_max is not None and self.amount_max < self.amount_min:
            raise ValueError("amount_max must be >= amount_min")
        return self


class ExtractedPost(_Strict):
    post_type: PostType = Field(description="Main purpose of the post. Pick one.")
    language: Language = Field(
        description="Main language of the text. Russian with English technical terms is still 'ru'; use 'mixed' only when whole sentences are in different languages.",
    )
    summary: str = Field(
        min_length=1,
        max_length=300,
        description="One-sentence summary in Ukrainian, up to 300 characters.",
    )
    equipment: list[EquipmentItem] = Field(
        description="Concrete equipment, devices or software products mentioned. Empty list if none. Do not include generic words like 'связь' or 'оборудование'.",
    )
    organizations: list[str] = Field(
        description=(
            "Companies, military units, projects, volunteer groups, alliances and channels named in the text, "
            "spelled as in the text but in nominative case, without emoji (e.g. 'БЮРО 1440', '204 СПН \"Ахмат\"'). "
            "Exclude social platforms mentioned only as links or hashtags (Telegram, VK, Rutube, Дзен, MAX). Empty list if none."
        ),
    )
    locations: list[str] = Field(
        description="Geographic places (countries, regions, cities, districts) in nominative case and full form: 'в Мск' -> 'Москва'. Empty list if none.",
    )
    event_date: Optional[date] = Field(
        description=(
            "Date of the event the post is about, YYYY-MM-DD. Only if day, month and year can be read from the text "
            "(the year may come from context such as an event name 'Дронница-2026'). For a range use the start date. "
            "Relative dates ('сегодня', 'вчера') -> null."
        ),
    )
    money: list[Money] = Field(description="Money amounts mentioned. Empty list if none.")
    call_to_action: bool = Field(
        description="True if the post asks readers to do something: write to a bot or DM, apply, register, repost, send or donate.",
    )

    @field_validator("organizations", "locations")
    @classmethod
    def _dedupe(cls, values: list[str]) -> list[str]:
        seen: dict[str, str] = {}
        for v in values:
            v = v.strip()
            if v and v.casefold() not in seen:
                seen[v.casefold()] = v
        return list(seen.values())


def json_schema() -> dict:
    return ExtractedPost.model_json_schema()


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "schema.json"
    out.write_text(json.dumps(json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON Schema -> {out.name}")
