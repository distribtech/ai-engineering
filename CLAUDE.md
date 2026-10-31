# Контекст для Claude: курс AI Engineering, ДЗ1

Спілкуйся з користувачем **українською**. Комітити й пушити лише тоді, коли користувач попросить.

## Що це за репозиторій
- Матеріали курсу AI Engineering (папки `01 …`, `02 …`, `03 …`, PDF) і домашні завдання.
- **ДЗ1** — у `hw1-extraction/`: сервіс, який витягує структуровані дані з «брудних» текстів за схемою, порівнює провайдерську й локальну моделі та обґрунтовує вибір. Повний текст завдання: `01 …/Домашнє завдання 1.docx`. Дедлайн **27.10, 23:59**.
- `tools/tg-collector/` — окремий веб-застосунок (Flask, порт 8770) для завантаження **сирих** постів публічних Telegram-каналів за період у папки `output/<папка>/<канал>/`. Не залежить від ДЗ1, PII не маскує, `output/` не в git. Детально — його `README.md`.
- `tools/robotdreams-video-extension/` — розширення Chrome для завантаження відео уроків. Це копія оригіналу з WSL основного комп'ютера (`~/projects/robotdreams-video-extension`).
- `03 …/ai-engineeering-2/` — чужий клон репозиторію курсу (`andbilous/ai-engineeering-2`) з власним `.git`. **Не комітити** в цей репозиторій.
- Репозиторій приватний: `github.com/distribtech/ai-engineering`. Перед здачею ментор має отримати доступ (зробити публічним або додати collaborator).

## Дані ДЗ1
- Джерело: публічні Telegram-канали російських волонтерів, які постачають техніку армії (OSINT). Пости збираються з веб-версії `t.me/s/<канал>`, без акаунта.
- Канали: `data/channels.json` (`signalman_diary` + 8 знайдених «сніжним комом»: `ghost_of_novorossia`, `military_engineer`, `reihatori`, `surf_noise1`, `makers2fron`, `vrachivyneodni_channel`, `interbrigady2022`, `ekipadobrovolec`).
- `data/raw/` (сирі пости) і `data/corpus.jsonl` — **не в git**. У git є `data/<канал>_clean.jsonl`: ті самі пости з масками PII (`[PHONE]`, `[CARD]`, `[WALLET]`, `[USER]`, `[TG_LINK]`, `[PAY_LINK]`, `[EMAIL]`). Якщо `data/raw/` немає, код читає clean-файли.
- Робочий набір: `data/dataset.jsonl` (30 постів з `signalman_diary`, відбір у `scripts/select_dataset.py`).
- Golden set: `data/golden.json`, 10 постів, **усі перевірені людиною** (`status: reviewed`, рішення в `reviewer_notes`).

## Код
| Що | Де | Запуск |
|---|---|---|
| Збір / докачування постів | `scripts/scrape_telegram.py` | `python scripts/scrape_telegram.py <канал> --update` |
| Веб-інтерфейс: канали, вижимка, пости, вкладка JSON (пост і JSON поруч) | `app/server.py`, `app/static/index.html` | `python app/server.py` → http://127.0.0.1:8765 |
| Схема Pydantic (9 полів, 22 типи техніки) | `src/schema.py` → `schema.json` | `python -m src.schema` |
| Схема v3 OSINT (13 полів: реквізити, контакти, люди, ТТХ, стан техніки); у пайплайн ще не підключена | `src/schema_v3.py` → `schema_v3.json` | `python -m src.schema_v3`, `python -m tests.test_schema_v3` |
| Вилучення: Ollama, structured output, перевірка схемою, 1 спроба виправлення, retry/timeout, лог | `src/extract.py` → `results/runs.jsonl` | через вкладку JSON або `ex.extract(...)` |
| Налаштування (адреса Ollama, модель, `.env`) | `src/config.py`, `.env.example` | |
| Промпти з версіями | `prompts/v1.txt`, `prompts/v2.txt`, `prompts/CHANGELOG.md` | нова версія — новий файл, старі не змінювати |
| Тести | `tests/test_schema.py`, `tests/test_schema_v3.py`, `tests/test_golden.py` | `python -m tests.test_schema`, `python -m tests.test_schema_v3`, `python -m tests.test_golden` |

Усе запускається з папки `hw1-extraction/`. Детально — `hw1-extraction/README.md`.

## Правила розмітки (погоджені з користувачем, записані в описах полів схеми)
- `quantity` — лише за явним числом («экземпляр», «новый генератор» → `null`); `unit` `шт` без крапки.
- `manufacturer` — лише якщо названо, зокрема як частина моделі (`TYT-7500` → `TYT`); країна — не виробник.
- `equipment` — конкретні вироби; конкретний предмет поста рахується й без моделі («убило новый генератор»), загальні згадки («генераторы в больницах», «паяльные станции» в умовах роботи) — ні.
- `organizations` — усі названі (включно з НАТО), у називному відмінку, без емодзі, з виправленими опечатками, компанії короткою назвою (`Аргус-М`).
- `locations` — називний відмінок, **завжди російською** (`Mariupol` → `Мариуполь`); місце виробництва теж (`производства Украины` → `Украина`, `Московский завод` → `Москва`); станції метро не беремо.
- `event_date` — лише якщо дату можна прочитати повністю (рік можна з назви заходу), для діапазону — перший день.
- `post_type`: подяка → `other`, анонс заходу → `event`, репліка без фактів → `personal`.
- Порівняння з еталоном ігнорує регістр, лапки й розділові знаки.

## Заміри на зараз (`qwen3:8b`, temperature 0, golden set)
- v1: збіг з еталоном 0.59; v2: 0.81; 10/10 валідних з першої спроби у v2; p50 ≈ 6.5 с на Quadro P4000.
- Найслабші поля у v2: `organizations` 0.63, `locations` 0.58 — кандидати на промпт v3.
- Застереження: правила v2 і еталон писав той самий автор; два пости golden set бачили під час написання v2.

## Схема v3 (OSINT), рішення користувача 31.10
- ДЗ1 робимо в `hw1-extraction` на даних, скопійованих з TG Collector. **Імена, позивні, @-імена й криптогаманці не маскуємо** (публічні дані). Картки, телефони й e-mail лишаються замаскованими.
- Імена й гаманці тут — результат розвідки, тож їх не приховуємо, а перевіряємо, що LLM їх не спотворила й не вигадала: `src/grounding.py` (`check_grounding`), тест `python -m tests.test_grounding`.
- Наступні кроки: перестати маскувати гаманці й @-імена в `scrape_telegram.py`, переразмітити golden set під v3, промпт v3, перемкнути `extract.py`.

## Що лишилось по ДЗ1
1. Провайдерська модель через API зі structured output (ключ у `.env`, не в git) і **спільний інтерфейс** з Ollama.
2. Семплінг у конфігу, порівняти ≥ 2 конфігурації.
3. Скрипт метрик: точність по полях на golden set, % валідних, latency p50/p95, вартість на 1000 документів; прогін усіх 30 текстів.
4. `REPORT.md`: таблиця «модель × промпт × семплінг», розбір 3–5 помилок, вибір для продакшну.
5. Запуск однією командою (`python main.py` або `make run`), README для перевіряючого.

## Оточення
- **Основний ПК** (Windows, Quadro P4000 8 GB): Windows-Ollama на **11435** (`OLLAMA_HOST=127.0.0.1:11435`), модель `qwen3:8b`. Ще одна Ollama 0.32.5 у WSL на **11434** — її використовують агенти OpenClaw і Hermes, **не зупиняти**.
- **Ubuntu-ПК з RTX 5090 32 GB** (цей, `/home/administrator/ai-engineering`): Ollama на 11434 з `gemma4:31b`, `gemma3:4b`, `gemma4:31b-32k`; `qwen3:8b` **немає**. У локальному `.env`: `OLLAMA_MODEL=gemma4:31b`, `OLLAMA_MODELS=gemma4:31b,gemma3:4b,qwen3:8b`. Віртуальне середовище — `hw1-extraction/.venv`. Push — через SSH (`origin` = `git@github.com:distribtech/ai-engineering.git`).
- **Ноутбук / інший ПК**: Ollama на стандартному 11434 (`ollama pull qwen3:8b`, або менша модель через `OLLAMA_MODEL` у `.env`).
- **Синхронізація:** `git pull` перед роботою, push після. Кнопку «Оновити» каналів тиснути лише на одному комп'ютері — інакше конфлікти у великих clean-файлах.
