"""Следит за новыми квартирами на balder.se и шлёт их в Telegram.

Без внешних зависимостей (только стандартная библиотека Python 3.9+).
Состояние (уже виденные объекты) хранится в seen.json рядом со скриптом.

Переменные окружения:
  TELEGRAM_BOT_TOKEN  токен бота от @BotFather
  TELEGRAM_CHAT_ID    id чата, куда слать сообщения
  DRY_RUN=1           ничего не отправлять, только печатать в консоль
"""
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Публичный поисковый ключ, он же используется самим сайтом в браузере.
SEARCH_URL = (
    "https://search.balder.se/multi_search"
    "?x-typesense-api-key=yq3N83M%2BhBBSMsj48%2FzW3ZSgA7rloVAhrtVR5J3g4Dk%3D"
)

# Районы, за которыми следим. Формат такой же, как в запросе сайта.
MUNICIPALITIES = ["stockholm > sundbyberg", "stockholm > järfälla"]

LIST_PAGE = (
    "https://www.balder.se/lediga-lagenheter?"
    + "&".join(
        "locationRefinements%5Blocation%5D%5Blocation.municipality%5D%5B{}%5D={}".format(
            i, urllib.parse.quote(m)
        )
        for i, m in enumerate(MUNICIPALITIES)
    )
)

STATE_FILE = Path(__file__).with_name("seen.json")
TZ = ZoneInfo("Europe/Stockholm")


def build_payload() -> dict:
    munis = ",".join(f"`{m}`" for m in MUNICIPALITIES)
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
                    f" && (location.municipality:=[{munis}])"
                ),
                "page": 1,
                "per_page": 100,
            }
        ]
    }


def fetch_objects() -> list[dict]:
    req = urllib.request.Request(
        SEARCH_URL,
        data=json.dumps(build_payload()).encode("utf-8"),
        headers={"Content-Type": "application/json", "Origin": "https://www.balder.se"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    result = data["results"][0]
    if "hits" not in result:  # Typesense вернул ошибку внутри результата
        raise RuntimeError(f"Неожиданный ответ поиска: {str(result)[:300]}")
    return [hit["document"] for hit in result["hits"]]


def first(value, default=None):
    """Поля rentOrFee / size / numberOfRooms приходят списками."""
    if isinstance(value, list):
        return value[0] if value else default
    return value if value is not None else default


def format_message(doc: dict) -> str:
    rent = first(doc.get("rentOrFee"))
    rent_txt = f"{rent:,}".replace(",", " ") + " kr/мес" if rent else "цена не указана"
    lines = [
        f"🏠 <b>Новая квартира · {html.escape(doc.get('territory') or doc.get('city', ''))}</b>",
        f"{html.escape(doc.get('street', ''))} — {html.escape(doc.get('title', ''))}",
        f"{rent_txt} · этаж {doc.get('floor', '?')} · въезд {doc.get('access', '?')}",
        f"Объект {html.escape(doc.get('objectNumber', ''))}",
    ]
    if doc.get("publishedAt"):
        pub = datetime.fromtimestamp(doc["publishedAt"], TZ).strftime("%d.%m")
        lines.append(f"Опубликовано: {pub}")
    lines.append(f'<a href="{html.escape(LIST_PAGE)}">Открыть список на balder.se</a>')
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    if os.environ.get("DRY_RUN"):
        print("--- DRY RUN ---\n" + text + "\n")
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    body = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def load_seen():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return None  # None = первый запуск


def save_seen(seen: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=1))


def main() -> int:
    docs = fetch_objects()
    current = {d["sourceId"]: d for d in docs}
    seen = load_seen()

    if seen is None:  # первый запуск: запоминаем текущее, не спамим
        save_seen(set(current))
        send_telegram(
            f"✅ Слежение запущено. Сейчас в {', '.join(m.split(' > ')[1] for m in MUNICIPALITIES)}"
            f" объектов: {len(current)}. Буду писать о новых."
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
        save_seen(seen | sent | (set(current) - set(new_ids)))
    print(f"Объектов: {len(current)}, новых: {len(new_docs)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (urllib.error.URLError, RuntimeError, KeyError, json.JSONDecodeError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)
