"""Следит за новыми квартирами на balder.se и шлёт их в Telegram.

Без внешних зависимостей (только стандартная библиотека Python 3.9+).
Состояние (уже виденные объекты) хранится в seen.json рядом со скриптом.

Переменные окружения:
  TELEGRAM_BOT_TOKEN  токен бота от @BotFather
  TELEGRAM_CHAT_ID    id чата, куда слать сообщения
  DRY_RUN=1           ничего не отправлять, только печатать в консоль
"""
import hashlib
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Публичный поисковый ключ, он же используется самим сайтом в браузере.
SEARCH_URL = (
    "https://search.balder.se/multi_search"
    "?x-typesense-api-key=yq3N83M%2BhBBSMsj48%2FzW3ZSgA7rloVAhrtVR5J3g4Dk%3D"
)

# ───────────────────────── НАСТРОЙКИ ─────────────────────────
# True  = следить за всем Stockholms län
# False = только за районами из списка MUNICIPALITIES ниже
WHOLE_COUNTY = True
COUNTY = "stockholm"
MUNICIPALITIES = [
    "stockholm > sundbyberg",
    "stockholm > järfälla",
    "stockholm > botkyrka",  # Tumba; написание по аналогии, проверяется сообщением при запуске
]
# Можно менять эти настройки в любой момент: при изменении фильтра бот
# тихо запомнит текущие объекты и не завалит тебя старыми квартирами.
# ──────────────────────────────────────────────────────────────

PER_PAGE = 250
MAX_PAGES = 10
STATE_FILE = Path(__file__).with_name("seen.json")
TZ = ZoneInfo("Europe/Stockholm")


def location_filter() -> str:
    if WHOLE_COUNTY:
        return f"(location.county:=[`{COUNTY}`])"
    munis = ",".join(f"`{m}`" for m in MUNICIPALITIES)
    return f"(location.municipality:=[{munis}])"


def filter_signature() -> str:
    """Короткий отпечаток текущего фильтра, чтобы заметить его смену."""
    return hashlib.sha1(location_filter().encode("utf-8")).hexdigest()[:12]


def area_label() -> str:
    if WHOLE_COUNTY:
        return f"весь {COUNTY.capitalize()}s län"
    return ", ".join(m.split(" > ")[-1] for m in MUNICIPALITIES)


def build_payload(page: int) -> dict:
    return {
        "searches": [
            {
                "collection": "market_object",
                "q": "*",
                "query_by": "street,title,objectNumber",
                "sort_by": "sort_weight:desc,createdAt:desc",
                "filter_by": (
                    "objectTypeGroup:housing && (language:sv || isMissingTranslation:=true)"
                    " && objectType:rental"
                    f" && {location_filter()}"
                ),
                "page": page,
                "per_page": PER_PAGE,
            }
        ]
    }


def fetch_page(page: int) -> dict:
    req = urllib.request.Request(
        SEARCH_URL,
        data=json.dumps(build_payload(page)).encode("utf-8"),
        headers={"Content-Type": "application/json", "Origin": "https://www.balder.se"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    result = data["results"][0]
    if "hits" not in result:  # Typesense вернул ошибку внутри результата
        raise RuntimeError(f"Неожиданный ответ поиска: {str(result)[:300]}")
    return result


def fetch_objects() -> list[dict]:
    docs: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        result = fetch_page(page)
        hits = result["hits"]
        docs.extend(h["document"] for h in hits)
        if len(docs) >= result.get("found", 0) or len(hits) < PER_PAGE:
            break
    return docs


def first(value, default=None):
    """Поля rentOrFee / size / numberOfRooms приходят списками."""
    if isinstance(value, list):
        return value[0] if value else default
    return value if value is not None else default


def listing_url(doc: dict) -> str:
    return f"https://www.balder.se/bostader/{doc['sourceId']}"


def format_message(doc: dict) -> str:
    rent = first(doc.get("rentOrFee"))
    rent_txt = f"{rent:,}".replace(",", " ") + " kr/мес" if rent else "цена не указана"
    place = doc.get("territory") or doc.get("city", "")
    lines = [
        f"🏠 <b>Новая квартира · {html.escape(place)}</b>",
        f"{html.escape(doc.get('street', ''))} — {html.escape(doc.get('title', ''))}",
        f"{rent_txt} · этаж {doc.get('floor', '?')} · въезд {doc.get('access', '?')}",
        f"Объект {html.escape(doc.get('objectNumber', ''))}",
    ]
    if doc.get("publishedAt"):
        pub = datetime.fromtimestamp(doc["publishedAt"], TZ).strftime("%d.%m")
        lines.append(f"Опубликовано: {pub}")
    lines.append(f'<a href="{html.escape(listing_url(doc))}">Открыть объявление</a>')
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    if os.environ.get("DRY_RUN"):
        print("--- DRY RUN ---\n" + text + "\n")
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    body = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def load_state():
    """Возвращает (сигнатура_фильтра, множество_id) или (None, None) при первом запуске.

    Старый формат файла (простой список id) считается «неизвестным фильтром»,
    поэтому при первом запуске новой версии бот тихо перезапоминает объекты.
    """
    if not STATE_FILE.exists():
        return None, None
    raw = json.loads(STATE_FILE.read_text())
    if isinstance(raw, dict):
        return raw.get("filter"), set(raw.get("ids", []))
    return None, None


def save_state(ids: set) -> None:
    STATE_FILE.write_text(
        json.dumps({"filter": filter_signature(), "ids": sorted(ids)}, indent=1)
    )


def breakdown(docs: list[dict]) -> str:
    counts = Counter(
        (d.get("location", {}).get("municipality") or "?").split(" > ")[-1] for d in docs
    )
    return ", ".join(f"{name}: {n}" for name, n in counts.most_common(15))


def main() -> int:
    docs = fetch_objects()
    current = {d["sourceId"]: d for d in docs}
    saved_filter, seen = load_state()

    if seen is None or saved_filter != filter_signature():
        # первый запуск или смена фильтра: запоминаем текущее, не спамим
        save_state(set(current))
        send_telegram(
            f"✅ Слежение обновлено. Область: {area_label()}.\n"
            f"Сейчас объектов: {len(current)}.\n"
            f"По районам: {breakdown(docs) or '—'}.\n"
            "Буду писать о новых."
        )
        return 0

    new_ids = [i for i in current if i not in seen]
    # старые сначала, чтобы свежие пришли последними
    new_docs = sorted((current[i] for i in new_ids), key=lambda d: d.get("createdAt", 0))
    sent = set()
    try:
        for doc in new_docs:
            send_telegram(format_message(doc))
            sent.add(doc["sourceId"])
    finally:
        # запоминаем только то, что реально отправили, остальное повторим в след. раз
        save_state(seen | sent | (set(current) - set(new_ids)))
    print(f"Объектов: {len(current)}, новых: {len(new_docs)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (urllib.error.URLError, RuntimeError, KeyError, json.JSONDecodeError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)
