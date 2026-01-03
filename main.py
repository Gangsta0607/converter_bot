import asyncio
import logging
import os
import re
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

# Загружаем переменные из .env (если есть)
load_dotenv()

# --- Конфигурация ---
API_TOKEN = os.getenv("BOT_TOKEN")
BASE_API = "https://api.nbrb.by/exrates/rates"
CURRENCIES_API = "https://api.nbrb.by/exrates/currencies"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Регулярка теперь поддерживает минус: "-100 usd to eur"
REQ_RE = re.compile(
    r"^\s*(?P<amount>[-+]?\d+(?:[.,]\d+)?)\s+(?P<from>[A-Z]{3})\s+to\s+(?P<to>[A-Z]{3})\s*$",
    re.I,
)

# Кэш
cache = {"data": None, "last_date": None}


def is_cache_expired():
    """Сброс кэша в полночь и после обновления курсов в 11:05"""
    if not cache["data"]:
        return True
    now = datetime.now()
    last = cache["last_date"]

    if now.date() > last.date():
        return True
    if now.time() > time(11, 5) and last.time() < time(11, 5):
        return True
    return False


async def get_rates(session):
    """Получение курсов с игнорированием ошибок SSL"""
    if is_cache_expired():
        try:
            # ssl=False решает проблему с сертификатами НБ РБ
            async with session.get(f"{BASE_API}?periodicity=0", ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    cache["data"] = {i["Cur_Abbreviation"]: i for i in data}
                    cache["last_date"] = datetime.now()
                    logging.info("Курсы валют обновлены")
        except Exception as e:
            logging.error(f"Ошибка при обновлении курсов: {e}")
    return cache["data"]


def fmt(d: Decimal) -> str:
    """Форматирование вывода чисел"""
    q = Decimal("0.01") if d >= 1 else Decimal("0.000001")
    return str(d.quantize(q, ROUND_HALF_UP)).rstrip("0").rstrip(".")


@dp.message(Command("currencies"))
async def cmd_currencies(message: Message):
    async with aiohttp.ClientSession() as session:
        rates = await get_rates(session)

    if not rates:
        return await message.answer("Ошибка получения данных от API.")

    text = "<b>Доступные валюты (НБ РБ):</b>\n\n<code>BYN</code> - Белорусский рубль\n"
    for code in sorted(rates.keys()):
        text += f"<code>{code}</code> - {rates[code]['Cur_Name']}\n"

    for i in range(0, len(text), 4096):
        await message.answer(text[i : i + 4096])


@dp.message(Command("convert"), F.text)
@dp.message(F.text.regexp(REQ_RE))
async def convert_handler(message: Message):
    # Убираем команду, если она есть
    text = message.text.replace("/convert", "").strip()
    if not text:
        return await message.answer(
            "Формат: <code>100</code> или <code>100 USD to EUR</code>"
        )

    cur_from, cur_to, amount = None, None, None

    # 1. Быстрая логика: только число (BYN <-> RUB)
    try:
        quick_amount = Decimal(text.replace(",", "."))
        if quick_amount == 0:
            return await message.answer("Сумма не может быть нулем.")

        if quick_amount > 0:
            cur_from, cur_to = "BYN", "RUB"
            amount = quick_amount
        else:
            cur_from, cur_to = "RUB", "BYN"
            amount = abs(quick_amount)

    except Exception:
        # 2. Стандартная логика: парсинг "100 USD to EUR"
        match = REQ_RE.match(text)
        if not match:
            return await message.answer(
                "Используй: <code>/convert 100</code> или <code>/convert 100 USD to EUR</code>"
            )

        amount = Decimal(match.group("amount").replace(",", "."))
        cur_from = match.group("from").upper()
        cur_to = match.group("to").upper()

    if cur_from == cur_to:
        return await message.answer(f"Результат: {fmt(amount)} {cur_to}")

    async with aiohttp.ClientSession() as session:
        rates = await get_rates(session)
        if not rates:
            return await message.answer("Не удалось загрузить курсы валют.")

        # Добавляем базовую валюту в список для расчета
        rates["BYN"] = {"Cur_OfficialRate": 1, "Cur_Scale": 1}

        if cur_from not in rates or cur_to not in rates:
            return await message.answer("Валюта не найдена. Список кодов: /currencies")

        # Расчет стоимости 1 единицы каждой валюты в BYN
        val_from = Decimal(str(rates[cur_from]["Cur_OfficialRate"])) / Decimal(
            str(rates[cur_from]["Cur_Scale"])
        )
        val_to = Decimal(str(rates[cur_to]["Cur_OfficialRate"])) / Decimal(
            str(rates[cur_to]["Cur_Scale"])
        )

        # Конвертация
        res = (amount * val_from) / val_to

        await message.answer(
            f"<b>{fmt(amount)} {cur_from} = {fmt(res)} {cur_to}</b>\n\n"
            f"<i>Курс: 1 {cur_from} = {fmt(val_from / val_to)} {cur_to}</i>"
        )


async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")
