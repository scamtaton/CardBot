"""
Телеграм-бот с карточками.

Установка:  pip install aiogram
Запуск:     BOT_TOKEN=123:ABC python card_bot.py
Тексты лежат в texts.py, картинки - в папке cards/.
"""
import asyncio
import html
import json
import os
import random
import re
from pathlib import Path

import texts

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"  # тут хранится прогресс каждого игрока

# ---------------- НАСТРОЙКИ ШАНСОВ ----------------

# (ключ редкости, базовый вес в %)
RARITIES = [
    ("rare",       50),
    ("super_rare", 35),
    ("epic",       10),
    ("mythic",      5),
    ("legendary",   1),
    ("requiem",     1),
]

BASE_RARITY = "rare"  # у неё шанс не растет, она "заполняет остаток"
BOOST = 0.10          # на сколько растет вес за каждую крутку без этой редкости
                      # (0.10 = +10% от базового веса за крутку)
MAX_MULT = 10.0       # потолок: вес не вырастет больше чем в 10 раз от базового

# ---------------- КАРТОЧКИ ----------------
# Карточки подхватываются САМИ из папки cards/ по имени файла:
#
#     Название.Редкость.jpg      например:  Papyrus.Rare.jpg
#
# Редкость: Rare, Super_Rare, Epic, Mythic, Legendary, Requiem
# (можно и по-русски: Редкий, Эпический и т.д.). Регистр не важен.
# Кода трогать не нужно: закинул файл в cards/ и перезапустил бота.
CARDS_DIR = BASE_DIR / "cards"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]", "", s).lower()


def scan_cards() -> dict:
    aliases = {}
    for key, _ in RARITIES:
        aliases[_norm(key)] = key
        aliases[_norm(texts.RARITY_NAMES[key])] = key

    cards: dict = {}
    if not CARDS_DIR.exists():
        return cards
    for path in sorted(CARDS_DIR.iterdir()):
        if path.suffix.lower() not in IMAGE_EXT:
            continue
        name, dot, rar = path.stem.rpartition(".")
        key = aliases.get(_norm(rar)) if dot else None
        if not name or key is None:
            print(f"[!] Файл пропущен: {path.name} (нужно Название.Редкость.jpg)")
            continue
        cards.setdefault(key, []).append((path.stem, path))
    return cards


def card_name(card_id: str) -> str:
    # своё название можно задать в texts.CARD_NAMES, иначе берется из имени файла
    if card_id in texts.CARD_NAMES:
        return texts.CARD_NAMES[card_id]
    return html.escape(card_id.rpartition(".")[0].replace("_", " "))


CARDS = scan_cards()
for _k, _v in CARDS.items():
    print(f"Загружено карточек [{_k}]: {len(_v)}")

# ---------------- ЛОГИКА ----------------

_rng = random.SystemRandom()  # криптостойкий генератор, предсказать нельзя


def _load() -> dict:
    try:
        return json.loads(DATA_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save() -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(DATA, ensure_ascii=False), "utf-8")
    tmp.replace(DATA_FILE)  # атомарно, файл не побьется при вылете


DATA = _load()


def get_user(uid: int) -> dict:
    # miss[ключ] = сколько круток подряд не выпадала эта редкость
    return DATA.setdefault(str(uid), {"rolls": 0, "miss": {}})


def current_weights(user: dict) -> dict:
    weights = {}
    for key, base in RARITIES:
        if key == BASE_RARITY:
            weights[key] = base
        else:
            mult = min(1 + user["miss"].get(key, 0) * BOOST, MAX_MULT)
            weights[key] = base * mult
    return weights


def current_chances(user: dict) -> dict:
    """Реальные шансы игрока прямо сейчас, в процентах."""
    w = current_weights(user)
    total = sum(w.values())
    return {k: v / total * 100 for k, v in w.items()}


def roll(user: dict):
    """Крутка. Возвращает (ключ редкости, (id, путь) или None)."""
    w = current_weights(user)
    keys = list(w)
    rarity = _rng.choices(keys, weights=[w[k] for k in keys])[0]

    user["rolls"] += 1
    for key, _ in RARITIES:
        if key == BASE_RARITY:
            continue
        user["miss"][key] = 0 if key == rarity else user["miss"].get(key, 0) + 1

    pool = CARDS.get(rarity, [])
    return rarity, (_rng.choice(pool) if pool else None)


# ---------------- TELEGRAM ----------------
from aiogram import Bot, Dispatcher, F  # noqa: E402
from aiogram.filters import Command, CommandStart  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

dp = Dispatcher()
FILE_IDS: dict = {}  # кэш: после первой отправки фото берется с серверов Telegram

KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text=texts.BTN_MORE, callback_data="roll")]]
)


async def send_card(target: Message, user_id: int) -> None:
    user = get_user(user_id)
    rarity, card = roll(user)
    _save()

    emoji = texts.RARITY_EMOJIS[rarity]
    rname = texts.RARITY_NAMES[rarity]

    if card is None:
        await target.answer(
            texts.NO_CARD_CAPTION.format(emoji=emoji, rarity=rname),
            reply_markup=KB,
            parse_mode="HTML",
        )
        return

    card_id, path = card
    caption = texts.CARD_CAPTION.format(
        emoji=emoji, rarity=rname, name=card_name(card_id)
    )
    photo = FILE_IDS.get(card_id) or FSInputFile(path)
    msg = await target.answer_photo(
        photo, caption=caption, reply_markup=KB, parse_mode="HTML"
    )
    FILE_IDS[card_id] = msg.photo[-1].file_id


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(texts.START)


@dp.message(Command("card"))
async def card(m: Message):
    await send_card(m, m.from_user.id)


@dp.callback_query(F.data == "roll")
async def more(c: CallbackQuery):
    await send_card(c.message, c.from_user.id)
    await c.answer()


@dp.message(Command("chances"))
async def chances(m: Message):
    ch = current_chances(get_user(m.from_user.id))
    lines = [
        texts.CHANCES_LINE.format(
            emoji=texts.RARITY_EMOJIS[k], rarity=texts.RARITY_NAMES[k], chance=ch[k]
        )
        for k, _ in RARITIES
    ]
    await m.answer(
        texts.CHANCES_HEADER + "\n" + "\n".join(lines) + "\n\n" + texts.CHANCES_FOOTER,
        parse_mode="HTML",
    )


async def main():
    bot = Bot(os.environ["BOT_TOKEN"])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
