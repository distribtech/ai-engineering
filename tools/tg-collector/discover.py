"""Пошук нових каналів «сніжним комом» від уже завантажених у TG Collector.

  .venv/bin/python discover.py                       # кандидати з усіх папок output/
  .venv/bin/python discover.py --min-subs 30000 --min-channels 2

1. З постів кожного каналу (output/<папка>/<канал>/posts.jsonl) бере посилання t.me/<ім'я> (текст і прев'ю)
   та @згадки. Для кандидата рахує: у скількох постах і в скількох різних каналах його згадано,
   окремо від ворожих і дружніх каналів (сторона — з config.json).
2. Кандидатів, згаданих щонайменше в --min-channels каналах або --min-posts постах, перевіряє через
   t.me/s/<ім'я>: чи це публічний канал, підписники, тематика й мова останніх ~20 постів.
3. Лишає великі (>= --min-subs) і тематичні канали та пише звіт discover_report.json і таблицю в консоль.

Сторона кандидата — лише підказка (хто згадує + мова). Українські канали теж посилаються на російські,
щоб їх викривати, тож сторону треба підтвердити вручну.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

import scraper as tg

ROOT = Path(__file__).resolve().parent
LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?=[/?#\s]|$)", re.I)
MENTION_RE = re.compile(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_]{3,31})")
SERVICE = {"joinchat", "addstickers", "addemoji", "addlist", "share", "iv", "proxy", "socks", "boost", "contact"}

# Корені слів російською й українською. «Техніка» — те, заради чого шукаємо.
TECH = {
    "дрони": ["дрон", "бпла", "fpv", "фпв", "мавік", "мавик", "mavic", "квадрокоптер", "коптер", "matrice", "autel", "крило", "крыло", "ударн"],
    "РЕБ/РЕР": ["рэб", "реб", "рэр", "рер", "детектор", "глушил", "подавител", "сканер"],
    "зв'язок": ["связ", "зв'яз", "звяз", "радиостанц", "радіостанц", "рації", "рации", "рация", "ретрансл", "антен", "starlink", "старлінк", "старлинк", "mesh"],
    "оптика": ["тепловиз", "тепловіз", "ночник", "приціл", "прицел", "монокуляр", "пнв"],
    "техніка": ["автомоб", "пікап", "пикап", "броньов", "броньован", "бтр", "бмп", "танк", "артилер", "гармат", "миномет", "міномет"],
}
FUNDRAISING = ["сбор", "збір", "збираємо", "собираем", "реквизит", "реквізит", "закуп", "передали", "отчет", "отчёт", "звіт", "доставили"]
NEWS_HINTS = ["срочно", "терміново", "новости", "новини", "breaking", "мнение", "думка"]


def parse_subs(value: str | None) -> int:
    if not value:
        return 0
    s = value.strip().upper().replace(" ", "").replace(",", ".")
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def load_sources() -> tuple[dict, dict[str, str]]:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    out_root = Path(cfg["output_dir"])
    out_root = out_root if out_root.is_absolute() else ROOT / out_root
    sides = {c["username"].lower(): c.get("side", "unknown") for c in cfg["channels"]}
    posts = {}
    for c in cfg["channels"]:
        f = out_root / c["folder"] / c["username"] / "posts.jsonl"
        if f.exists():
            posts[c["username"].lower()] = tg.read_jsonl(f)
    return posts, sides


def collect(posts: dict, sides: dict) -> dict[str, dict]:
    cand: dict[str, dict] = collections.defaultdict(lambda: {"posts": 0, "by": collections.Counter(), "side_posts": collections.Counter()})
    for src, rows in posts.items():
        for p in rows:
            urls = list(p.get("links") or [])
            if p.get("link_preview"):
                urls.append(p["link_preview"].get("url") or "")
            names = {m.group(1).lower() for u in urls for m in LINK_RE.finditer(u)}
            names |= {m.group(1).lower() for m in MENTION_RE.finditer(p.get("text") or "")}
            for n in names - SERVICE - {src}:
                if n in sides or n.endswith("bot"):
                    continue
                c = cand[n]
                c["posts"] += 1
                c["by"][src] += 1
                c["side_posts"][sides.get(src, "unknown")] += 1
    return cand


def ua_share(texts: list[str]) -> float:
    joined = " ".join(texts).lower()
    ua = sum(joined.count(ch) for ch in "іїєґ")
    ru = sum(joined.count(ch) for ch in "ыэъё")
    return ua / (ua + ru) if ua + ru else 0.5


def profile(f: tg.Fetcher, username: str) -> dict | None:
    info, posts = f.page(username)
    if not posts and not info["title"]:
        return None
    texts = [p["text"] for p in posts if p["text"]]
    low = [t.lower() for t in texts]
    tech = {k: sum(any(w in t for w in words) for t in low) for k, words in TECH.items()}
    return {
        "title": info["title"], "description": info["description"], "subscribers": info["subscribers"],
        "subs": parse_subs(info["subscribers"]), "recent_posts": len(texts),
        "tech_posts": sum(any(w in t for words in TECH.values() for w in words) for t in low),
        "tech_topics": {k: v for k, v in tech.items() if v},
        "fundraising_posts": sum(any(w in t for w in FUNDRAISING) for t in low),
        "news_posts": sum(any(w in t for w in NEWS_HINTS) for t in low),
        "ua_share": round(ua_share(texts), 2),
        "last_date": max((p["date"] or "")[:10] for p in posts) if posts else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-channels", type=int, default=2, help="кандидат має бути згаданий щонайменше в N різних каналах…")
    ap.add_argument("--min-posts", type=int, default=4, help="…або щонайменше в N постах")
    ap.add_argument("--min-subs", type=int, default=30000, help="«великий» канал: підписників не менше")
    ap.add_argument("--min-tech", type=int, default=3, help="з ~20 останніх постів про техніку щонайменше N")
    ap.add_argument("--limit", type=int, default=150, help="скільки кандидатів перевіряти (за частотою згадок)")
    args = ap.parse_args()

    posts, sides = load_sources()
    cand = collect(posts, sides)
    ranked = sorted((n for n, c in cand.items() if len(c["by"]) >= args.min_channels or c["posts"] >= args.min_posts),
                    key=lambda n: (len(cand[n]["by"]), cand[n]["posts"]), reverse=True)[:args.limit]
    print(f"Джерел: {len(posts)} каналів, кандидатів: {len(cand)}, перевіряю {len(ranked)}")
    f = tg.Fetcher(delay=0.8, log=lambda m: None)
    found = []
    for i, n in enumerate(ranked, 1):
        c = cand[n]
        try:
            prof = profile(f, n)
        except Exception as exc:  # noqa: BLE001
            prof = None
            print(f"  @{n}: помилка {exc}")
        row = {"username": n, "mentions_posts": c["posts"], "mentioned_by": dict(c["by"]),
               "mentions_by_side": dict(c["side_posts"]), "public": prof is not None, **(prof or {})}
        if prof:
            hostile, friendly = c["side_posts"].get("hostile", 0), c["side_posts"].get("friendly", 0)
            lang = "uk" if prof["ua_share"] >= 0.6 else ("ru" if prof["ua_share"] <= 0.4 else "mixed")
            row["lang"] = lang
            row["side_hint"] = ("friendly" if lang == "uk" and friendly >= hostile else
                                "hostile" if lang == "ru" and hostile >= friendly else "unknown")
            row["big"] = prof["subs"] >= args.min_subs
            row["tech"] = prof["tech_posts"] >= args.min_tech
        found.append(row)
        if i % 20 == 0:
            print(f"  перевірено {i}/{len(ranked)}")

    keep = sorted((r for r in found if r.get("big") and r.get("tech")), key=lambda r: -r["subs"])
    (ROOT / "discover_report.json").write_text(json.dumps({"params": vars(args), "selected": keep, "all": found},
                                                          ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nВеликих (>= {args.min_subs}) і про техніку (>= {args.min_tech} з ~20 постів): {len(keep)}\n")
    for r in keep:
        topics = ", ".join(f"{k} {v}" for k, v in sorted(r["tech_topics"].items(), key=lambda x: -x[1]))
        print(f"{r['side_hint']:8} @{r['username']:26} {r['subscribers']:>7}  мова {r['lang']:5} "
              f"техніка {r['tech_posts']:2}/{r['recent_posts']}  збори {r['fundraising_posts']:2}  "
              f"згадок {r['mentions_posts']:3} у {len(r['mentioned_by'])} кан. ({r['mentions_by_side']})  "
              f"{(r['title'] or '')[:40]}  [{topics}]")


if __name__ == "__main__":
    main()
