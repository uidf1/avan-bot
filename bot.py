import asyncio
import os
import re
import httpx
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- БЕЗОПАСНОЕ ЧТЕНИЕ КЛЮЧЕЙ ---
# Теперь ключи берутся из переменных окружения Bothost (или .env)
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
AVAN_API_KEY = os.getenv("AVAN_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Проверка, что ключи вообще переданы
if not TELEGRAM_BOT_TOKEN or not AVAN_API_KEY or ADMIN_ID == 0:
    raise ValueError("Не заданы переменные окружения BOT_TOKEN, AVAN_API_KEY или ADMIN_ID!")

AVAN_BASE_URL = "https://api.avan.market/v1"

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()


class BuyStates(StatesGroup):
  confirming = State()


WEATHER_MAP = {
    "ash": "Genuine Weather Ash",
    "rain": "Genuine Weather Rain",
    "snow": "Genuine Weather Snow",
    "moon": "Genuine Weather Moonbeam",
    "pest": "Genuine Weather Pestilence",
    "harvest": "Genuine Weather Harvest",
    "spring": "Genuine Weather Spring",
    "aurora": "Genuine Weather Aurora",
    "sirocco": "Genuine Weather Sirocco",
    "test": "Genuine Wyvern Hatchling"
}


def parse_trade_link(url: str):
  partner_match = re.search(r"partner=(\d+)", url)
  token_match = re.search(r"token=([a-zA-Z0-9_-]+)", url)

  if partner_match and token_match:
    return partner_match.group(1), token_match.group(1)
  return None, None


async def get_usd_to_rub() -> float:
  # внутренний курс авана выше биржевого на ~4.3%
  avan_markup = 1.043

  try:
    async with httpx.AsyncClient() as client:
      res = await client.get("https://open.er-api.com/v6/latest/USD", timeout=5.0)
      if res.status_code == 200:
        forex_rate = float(res.json()["rates"].get("RUB", 83.08))
        return forex_rate * avan_markup
  except Exception:
    pass

  # запасной курс, если api валют временно недоступен
  return 86.65



async def get_item_id(item_name: str) -> tuple[str | None, float | None]:
  headers = {"Authorization": f"Bearer {AVAN_API_KEY}"}
  params = {"game": 570, "names[]": item_name}
  all_items = []

  async with httpx.AsyncClient() as client:
    while True:
      res = await client.get(
          f"{AVAN_BASE_URL}/market/search", params=params, headers=headers
      )
      if res.status_code != 200:
        break

      body = res.json()
      data = body.get("data", [])
      all_items.extend(data)

      # если лотов больше 200, догружаем следующую страницу через курсор
      next_page = body.get("meta", {}).get("next_page")
      if not next_page:
        break
      params["next_page"] = next_page

  if not all_items:
    return None, None

  # берем самый дешевый лот из всего списка вместо первого попавшегося data[0]
  cheapest = min(all_items, key=lambda item: float(item["price"]))
  return cheapest["id"], float(cheapest["price"])



@dp.message(Command("start"))
async def cmd_start(message: types.Message):
  if message.from_user.id != ADMIN_ID:
    await message.answer(
        f"доступ запрещен! твой telegram id: `{message.from_user.id}`",
        parse_mode="Markdown",
    )
    return

  await message.answer(
      "Салам! Бот с подтверждением и проверкой статуса готов.\n\n"
      "Формат команды:\n"
      "`/buy ash https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc`",
      parse_mode="Markdown",
  )


@dp.message(Command("buy"))
async def cmd_buy(message: types.Message, state: FSMContext):
  if message.from_user.id != ADMIN_ID:
    await message.answer(
        f"доступ запрещен! твой telegram id: `{message.from_user.id}`",
        parse_mode="Markdown",
    )
    return

  raw_text = message.text.replace("\n", " ")
  args = raw_text.split()

  if len(args) < 3:
    await message.answer(
        "Ошибка! Формат: `/buy <код_погоды> <трейд_ссылка>`",
        parse_mode="Markdown",
    )
    return

  weather_code = args[1].lower()
  trade_link = args[2]

  item_name = WEATHER_MAP.get(weather_code, weather_code)

  partner, token = parse_trade_link(trade_link)
  if not partner or not token:
    await message.answer("Ошибка: не удалось распарсить трейд-ссылку!")
    return

  # сохраняем ссылку заранее, чтобы потом передать ее в мониторинг
  await state.update_data(trade_link=trade_link)

  msg = await message.answer(
      f"Ищу `{item_name}` на Avan Market...", parse_mode="Markdown"
  )

  # тут должна быть обновленная get_item_id, которая ищет самую дешевую шмотку
  skin_id, price_usd = await get_item_id(item_name)
  if not skin_id:
    # если не нашли — предлагаем начать мониторинг
    builder = InlineKeyboardBuilder()
    builder.button(text="👀 Отслеживать появление", callback_data=f"track_{weather_code}")
    await msg.edit_text(
        f"Предмет `{item_name}` не найден на маркете!",
        reply_markup=builder.as_markup()
    )
    return

  rate = await get_usd_to_rub()
  price_rub = price_usd * rate

  # сохраняем цену в рублях и убираем + 0.5 из max_price
  await state.update_data(
      skin_id=skin_id,
      partner=partner,
      token=token,
      max_price=price_usd,
      price_rub=price_rub,
      item_name=item_name,
  )
  await state.set_state(BuyStates.confirming)

  builder = InlineKeyboardBuilder()
  builder.button(text="✅ Купить", callback_data="confirm_buy")
  builder.button(text="❌ Отмена", callback_data="cancel_buy")
  builder.adjust(2)

  await msg.edit_text(
      f"📦 Товар: *{item_name}*\n"
      f"💵 Цена: *${price_usd:.2f}* (~*{price_rub:.2f} ₽*)\n\n"
      f"Точно покупаем?",
      parse_mode="Markdown",
      reply_markup=builder.as_markup(),
  )


@dp.callback_query(BuyStates.confirming, F.data == "confirm_buy")
async def process_confirm(callback: types.CallbackQuery, state: FSMContext):
  data = await state.get_data()
  await state.clear()

  skin_id = data["skin_id"]
  partner = data["partner"]
  token = data["token"]
  max_price = data["max_price"]
  item_name = data["item_name"]
  price_rub = data.get("price_rub", 0.0)

  await callback.message.edit_text(
      f"Отправляю трейд для `{item_name}`...", parse_mode="Markdown"
  )

  headers = {
      "Authorization": f"Bearer {AVAN_API_KEY}",
      "Content-Type": "application/json",
  }
  payload = {
      "id": skin_id,
      "partner": partner,
      "token": token,
      "max_price": max_price,
  }

  async with httpx.AsyncClient() as client:
    try:
      buy_res = await client.post(
          f"{AVAN_BASE_URL}/market/buy",
          json=payload,
          headers=headers,
          timeout=35.0,
      )
      res_json = buy_res.json()

      if buy_res.status_code == 200:
        p_id = res_json.get("data", {}).get("purchase_id", "N/A")

        builder = InlineKeyboardBuilder()
        builder.button(
            text="🔄 Проверить статус", callback_data=f"check_{p_id}"
        )

        await callback.message.edit_text(
            f"✅ **Успешно! Трейд отправлен.**\n\n"
            f"📦 Товар: `{item_name}`\n"
            f"💵 Куплено за: **${max_price:.2f}** (~**{price_rub:.2f} ₽**)\n"
            f"🆔 ID покупки: `{p_id}`",
            parse_mode="Markdown",
            reply_markup=builder.as_markup(),
        )
      else:
        err_msg = res_json.get("message", "Неизвестная ошибка")
        code = res_json.get("code", "")
        await callback.message.edit_text(
            f"Ошибка Avan ({buy_res.status_code}): {err_msg} [{code}]"
        )

    except Exception as e:
      await callback.message.edit_text(
          f"Сбой выполнения: `{str(e)}`", parse_mode="Markdown"
      )
          
@dp.callback_query(BuyStates.confirming, F.data == "cancel_buy")
async def process_cancel(callback: types.CallbackQuery, state: FSMContext):
  await state.clear()
  await callback.message.edit_text("Покупка отменена.")


@dp.callback_query(F.data.startswith("check_"))
async def process_check_status(callback: types.CallbackQuery):
  p_id = callback.data.split("_", 1)[1]
  headers = {"Authorization": f"Bearer {AVAN_API_KEY}"}

  params = {"purchase_ids[]": p_id}

  async with httpx.AsyncClient() as client:
    try:
      res = await client.get(
          f"{AVAN_BASE_URL}/market/info",
          headers=headers,
          params=params,
          timeout=15.0,
      )

      if res.status_code == 200:
        data = res.json().get("data", [])

        if data:
          order_info = data[0]
          skin_info = order_info.get("skin", {})

          raw_status = str(skin_info.get("status", "0"))
          item_name = skin_info.get("name", "Предмет")
          steam_offer = skin_info.get("steam_trade_offer_id", "—")
          return_reason = skin_info.get("return_reason")

          status_map = {
              "0": "⏳ Обрабатывается (трейд ещё не отправлен)",
              "2": "📤 Отправлен! Ждёт принятия покупателем",
              "3": "✅ Успешно принят! Скин передан",
              "6": (
                  f"❌ Отменён ({return_reason or 'trade_canceled'})"
              ),
              "7": "❌ Отклонён получателем",
              "8": "❌ Предмет недоступен для передачи",
              "10": "❌ Не удалось создать трейд",
          }

          status_text = status_map.get(raw_status, f"Код: {raw_status}")

          msg = (
              f"📦 {item_name}\n"
              f"📊 Статус: {status_text}\n"
              f"🤝 Trade Offer ID: {steam_offer}"
          )
          await callback.answer(msg, show_alert=True)
        else:
          await callback.answer("Заказ не найден.", show_alert=True)
      else:
        await callback.answer(
            f"Ошибка API ({res.status_code})", show_alert=True
        )
    except Exception as e:
      await callback.answer(f"Ошибка запроса: {str(e)}", show_alert=True)

# словарь для хранения активных задач мониторинга
monitoring_tasks = {}

@dp.callback_query(F.data.startswith("track_"))
async def process_track(callback: types.CallbackQuery, state: FSMContext):
  weather_code = callback.data.split("_", 1)[1]
  item_name = WEATHER_MAP.get(weather_code, weather_code)

  # защита от случайного запуска нескольких одинаковых задач
  if weather_code in monitoring_tasks:
    await callback.answer("уже отслеживаю этот предмет!", show_alert=True)
    return

  data = await state.get_data()
  trade_link = data.get("trade_link")

  if not trade_link:
    await callback.answer("ошибка: не найдена трейд-ссылка.", show_alert=True)
    return

  await callback.message.edit_text(
      f"👀 Начал отслеживать `{item_name}`.\n"
      f"Буду проверять каждые 30 секунд. Как только появится — пришлю уведомление!",
      parse_mode="Markdown"
  )

  # запускаем асинхронный цикл проверки в фоне
  task = asyncio.create_task(monitor_item(callback.from_user.id, weather_code, item_name, trade_link))
  monitoring_tasks[weather_code] = task

async def monitor_item(user_id: int, weather_code: str, item_name: str, trade_link: str):
  while True:
    await asyncio.sleep(30)
    try:
      skin_id, price_usd = await get_item_id(item_name)
      if skin_id:
        rate = await get_usd_to_rub()
        price_rub = price_usd * rate

        msg = (
            f"🚨 **ПРЕДМЕТ ПОЯВИЛСЯ НА МАРКЕТЕ!** 🚨\n\n"
            f"📦 Товар: `{item_name}`\n"
            f"💵 Цена: **${price_usd:.2f}** (~**{price_rub:.2f} ₽**)\n\n"
            f"Для покупки нажми или скопируй команду:\n"
            f"`/buy {weather_code} {trade_link}`"
        )
        await bot.send_message(user_id, msg, parse_mode="Markdown")

        # удаляем задачу после успеха и завершаем цикл
        if weather_code in monitoring_tasks:
          del monitoring_tasks[weather_code]
        break
    except Exception as e:
        # игнорируем сетевые ошибки, чтобы цикл не упал
        pass

async def main():
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
