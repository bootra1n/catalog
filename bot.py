import asyncio
import logging
import re
import httpx
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8682129121:AAHdVFK-RIYbnDrqCID5h37txKIrom6MTHI"
GROUP_ID = -1003874974918
MY_USERNAME = "predicement"
SHEET_ID = "1TcXdTn25cY6HIL2zQ9lkx5-nIaA6l123rZnMvm-H20w"

CATEGORIES = {
    "👕 Одежда":       "1202122575",
    "👟 Обувь":        "1457470483",
    "👗 Женское":      "897161751",
    "💍 Аксессуары":   "279758272",
    "🧥 Куртки":       "1679202227",
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- FSM ---
class OrderState(StatesGroup):
    waiting_size = State()

class SearchState(StatesGroup):
    browsing = State()  # листаем результаты поиска

# --- КАТАЛОГ ПО КАТЕГОРИЯМ ---
CATALOG: dict[str, list] = {cat: [] for cat in CATEGORIES}

# --- ОЧИСТКА НАЗВАНИЯ ---
def apply_markup(price_str: str) -> str:
    """Умножает цену на 1.5 и округляет до целого."""
    try:
        # Убираем $ и пробелы, берём число
        cleaned = price_str.replace("$", "").replace("＄", "").strip()
        value = float(cleaned)
        new_value = round(value * 1.5)
        return f"${new_value}"
    except Exception:
        return price_str  # если не число — возвращаем как есть
    """Убирает 'XX colorways', лишние цифры и мусор — оставляет только название товара."""
    name = raw.strip()
    # Убираем строки типа "38 colorways", "24 colorways" и т.д.
    name = re.sub(r'\d+\s*colorways?', '', name, flags=re.IGNORECASE)
    # Убираем одиночные числа в начале/конце
    name = re.sub(r'^\d+\s*', '', name)
    name = re.sub(r'\s*\d+$', '', name)
    # Убираем лишние пробелы и переносы
    name = re.sub(r'\s+', ' ', name).strip()
    return name

# --- ПРАВИЛЬНЫЙ CSV ПАРСЕР (поддерживает многострочные ячейки) ---
def parse_csv(text: str) -> list[list[str]]:
    """Парсит CSV корректно включая ячейки с переносами строк внутри кавычек."""
    rows = []
    current_row = []
    current_cell = ""
    in_quotes = False

    i = 0
    while i < len(text):
        ch = text[i]

        if ch == '"':
            if in_quotes and i + 1 < len(text) and text[i + 1] == '"':
                # Экранированная кавычка ""
                current_cell += '"'
                i += 2
                continue
            in_quotes = not in_quotes
        elif ch == ',' and not in_quotes:
            current_row.append(current_cell.strip())
            current_cell = ""
        elif ch == '\n' and not in_quotes:
            current_row.append(current_cell.strip())
            rows.append(current_row)
            current_row = []
            current_cell = ""
        elif ch == '\r':
            pass  # игнорируем \r
        else:
            current_cell += ch

        i += 1

    if current_cell or current_row:
        current_row.append(current_cell.strip())
        rows.append(current_row)

    return rows

# --- ЗАГРУЗКА ОДНОГО ЛИСТА ---
async def load_sheet(category: str, gid: str) -> list:
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        f"/export?format=csv&gid={gid}"
    )
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(url)
        if response.status_code != 200:
            logging.error(f"Ошибка загрузки {category}: {response.status_code}")
            return []

        rows = parse_csv(response.text)
        logging.info(f"{category}: строк в CSV = {len(rows)}")

        # Первая строка — заголовок (Item Name, Image, LINK, Price USD, Price CNY, ...)
        # Данные с строки 1 (индекс 1)
        items = []
        for row in rows[1:]:
            # Дополняем до 12 колонок
            while len(row) < 12:
                row.append("")

            def c(idx):
                return row[idx].strip() if idx < len(row) else ""

            # Ряд 1: A(0)=название, B(1)=фото(пусто), C(2)=ссылка, D(3)=цена$, K(10)=фото
            name1_raw = c(0)
            link1     = c(2)
            price1    = c(3)
            photo1    = c(10)

            if name1_raw and link1.startswith("http"):
                items.append({
                    "name":  clean_name(name1_raw),
                    "photo": photo1,
                    "link":  link1,
                    "price": apply_markup(price1),
                })

            # Ряд 2: F(5)=название, H(7)=ссылка, I(8)=цена$, L(11)=фото
            name2_raw = c(5)
            link2     = c(7)
            price2    = c(8)
            photo2    = c(11)

            if name2_raw and link2.startswith("http"):
                items.append({
                    "name":  clean_name(name2_raw),
                    "photo": photo2,
                    "link":  link2,
                    "price": apply_markup(price2),
                })

        # Убираем дубликаты по ссылке
        seen_links = set()
        unique_items = []
        for item in items:
            if item["link"] not in seen_links:
                seen_links.add(item["link"])
                unique_items.append(item)
        items = unique_items

        logging.info(f"{category}: загружено {len(items)} товаров (после дедупликации)")
        return items

    except Exception as e:
        logging.error(f"Исключение при загрузке {category}: {e}")
        return []

# --- ЗАГРУЗКА ВСЕХ КАТЕГОРИЙ ---
async def load_all_catalogs():
    global CATALOG
    tasks = {cat: load_sheet(cat, gid) for cat, gid in CATEGORIES.items()}
    results = await asyncio.gather(*tasks.values())
    for cat, items in zip(tasks.keys(), results):
        CATALOG[cat] = items
    total = sum(len(v) for v in CATALOG.values())
    logging.info(f"Всего загружено: {total} товаров")

# --- КЛАВИАТУРЫ ---
def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="🔍 Поиск")],
            [KeyboardButton(text="💬 Связь")]
        ],
        resize_keyboard=True
    )

def get_categories_keyboard():
    buttons = [[InlineKeyboardButton(text=cat, callback_data=f"category_{cat}")]
               for cat in CATEGORIES]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_catalog_keyboard(category: str, index: int):
    items = CATALOG.get(category, [])
    total = len(items)
    if total == 0:
        return None
    prev_idx = (index - 1) % total
    next_idx = (index + 1) % total
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️", callback_data=f"nav_{category}_{prev_idx}"),
            InlineKeyboardButton(text=f"{index + 1} / {total}", callback_data="noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"nav_{category}_{next_idx}")
        ],
        [InlineKeyboardButton(text="🛒 Заказать", callback_data=f"order_{category}_{index}")],
        [InlineKeyboardButton(text="◀️ К категориям", callback_data="back_categories")]
    ])

def get_search_keyboard(index: int, total: int):
    prev_idx = (index - 1) % total
    next_idx = (index + 1) % total
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️", callback_data=f"search_{prev_idx}"),
            InlineKeyboardButton(text=f"{index + 1} / {total}", callback_data="noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"search_{next_idx}")
        ],
        [InlineKeyboardButton(text="🛒 Заказать", callback_data=f"search_order_{index}")],
        [InlineKeyboardButton(text="◀️ К категориям", callback_data="back_categories")]
    ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Выкуплено",  callback_data=f"st_bought_{user_id}"),
            InlineKeyboardButton(text="🚛 В пути",     callback_data=f"st_shipping_{user_id}")
        ],
        [
            InlineKeyboardButton(text="📦 На складе",  callback_data=f"st_stock_{user_id}"),
            InlineKeyboardButton(text="❌ Отказ",      callback_data=f"st_cancel_{user_id}")
        ]
    ])

# --- ПОКАЗ ТОВАРА ---
async def show_item(target, category: str, index: int, edit: bool = False):
    items = CATALOG.get(category, [])
    if not items:
        text = f"😔 В категории *{category}* пока нет товаров."
        await target.message.answer(text, parse_mode="Markdown")
        return

    item = items[index]
    kb = get_catalog_keyboard(category, index)
    caption = f"*{item['name']}*\n\n💰 {item['price']}"
    photo = item.get("photo", "")

    # Всегда работаем через message
    msg = target.message if hasattr(target, 'message') else target

    try:
        if edit:
            if photo:
                await msg.edit_media(
                    media=types.InputMediaPhoto(media=photo, caption=caption, parse_mode="Markdown"),
                    reply_markup=kb
                )
            else:
                await msg.edit_text(caption, reply_markup=kb, parse_mode="Markdown")
        else:
            if photo:
                await msg.answer_photo(photo=photo, caption=caption, reply_markup=kb, parse_mode="Markdown")
            else:
                await msg.answer(caption, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"show_item error: {e}")
        if photo:
            await msg.answer_photo(photo=photo, caption=caption, reply_markup=kb, parse_mode="Markdown")
        else:
            await msg.answer(caption, reply_markup=kb, parse_mode="Markdown")

# --- ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "👑 *XV ARCHIVE* — Добро пожаловать!\n\nВыбери действие:",
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "🛍 Каталог")
async def open_catalog(message: types.Message):
    await message.answer(
        "Выбери категорию:",
        reply_markup=get_categories_keyboard()
    )

# --- ВЫБОР КАТЕГОРИИ ---
@dp.callback_query(F.data.startswith("category_"))
async def select_category(callback: CallbackQuery):
    category = callback.data.replace("category_", "")
    if not CATALOG.get(category):
        await callback.answer("Загружаю...", show_alert=False)
        CATALOG[category] = await load_sheet(category, CATEGORIES[category])

    await show_item(callback, category, 0, edit=False)
    await callback.answer()

# --- НАВИГАЦИЯ ---
@dp.callback_query(F.data.startswith("nav_"))
async def navigate(callback: CallbackQuery):
    # nav_{category}_{index}
    parts = callback.data.split("_", 2)
    category = parts[1]
    index = int(parts[2])
    await show_item(callback, category, index, edit=True)
    await callback.answer()

@dp.callback_query(F.data == "back_categories")
async def back_to_categories(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Выбери категорию:", reply_markup=get_categories_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()

# --- ЗАКАЗАТЬ ---
@dp.callback_query(F.data.startswith("order_"))
async def order_item(callback: CallbackQuery, state: FSMContext):
    # order_{category}_{index}
    parts = callback.data.split("_", 2)
    category = parts[1]
    index = int(parts[2])
    item = CATALOG[category][index]

    await state.set_state(OrderState.waiting_size)
    await state.update_data(item=item)

    await callback.message.answer(
        f"📏 Вы выбрали *{item['name']}*\n\n"
        f"Укажите ваш размер (например: M, L, XL, 42, 44...):",
        parse_mode="Markdown"
    )
    await callback.answer()

# --- ПОЛУЧЕНИЕ РАЗМЕРА ---
@dp.message(OrderState.waiting_size)
async def receive_size(message: types.Message, state: FSMContext):
    data = await state.get_data()
    item = data["item"]
    size = message.text.strip()
    user = message.from_user
    username = f"@{user.username}" if user.username else "нет юзернейма"

    await state.clear()

    await message.answer(
        f"✅ *Заказ принят!*\n\n"
        f"📌 {item['name']}\n"
        f"📏 Размер: {size}\n\n"
        f"Продавец скоро свяжется с вами.",
        parse_mode="Markdown"
    )

    order_text = (
        f"🛍 *НОВЫЙ ЗАКАЗ*\n\n"
        f"👤 Клиент: {user.full_name} ({username})\n"
        f"🆔 ID клиента: `{user.id}`\n\n"
        f"📌 *{item['name']}*\n"
        f"📏 Размер: {size}\n"
        f"💰 Цена: {item['price']}\n"
        f"🛒 [Ссылка Mulebuy]({item['link']})"
    )
    await bot.send_message(GROUP_ID, order_text, parse_mode="Markdown",
                           reply_markup=get_status_keyboard(user.id))

# --- ПОИСК ---
@dp.message(F.text == "🔍 Поиск")
async def search_prompt(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("🔍 Введите название товара или бренда:")

@dp.message(F.chat.type == "private")
async def handle_text(message: types.Message, state: FSMContext):
    menu = ["🛍 Каталог", "🔍 Поиск", "💬 Связь"]
    if message.text in menu:
        return
    current_state = await state.get_state()
    if current_state == OrderState.waiting_size:
        return

    query = message.text.lower().strip()
    if not query:
        return

    all_items = []
    for items in CATALOG.values():
        all_items.extend(items)

    results = [item for item in all_items if query in item["name"].lower()]

    if not results:
        await message.answer(
            f"😔 По запросу *«{message.text}»* ничего не найдено.",
            parse_mode="Markdown"
        )
        return

    # Сохраняем результаты в FSM
    await state.set_state(SearchState.browsing)
    await state.update_data(results=results, query=message.text)

    # Показываем первый результат
    await show_search_item(message, results, 0)

async def show_search_item(target, results: list, index: int, edit: bool = False):
    item = results[index]
    total = len(results)
    kb = get_search_keyboard(index, total)
    caption = (
        f"🔍 *{total} результатов*\n\n"
        f"*{item['name']}*\n"
        f"💰 {item['price']}"
    )
    photo = item.get("photo", "")
    msg = target.message if hasattr(target, "message") else target

    try:
        if edit:
            if photo:
                await msg.edit_media(
                    media=types.InputMediaPhoto(media=photo, caption=caption, parse_mode="Markdown"),
                    reply_markup=kb
                )
            else:
                await msg.edit_text(caption, reply_markup=kb, parse_mode="Markdown")
        else:
            if photo:
                await msg.answer_photo(photo=photo, caption=caption, reply_markup=kb, parse_mode="Markdown")
            else:
                await msg.answer(caption, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"show_search_item error: {e}")
        if photo:
            await msg.answer_photo(photo=photo, caption=caption, reply_markup=kb, parse_mode="Markdown")
        else:
            await msg.answer(caption, reply_markup=kb, parse_mode="Markdown")

# --- НАВИГАЦИЯ ПО РЕЗУЛЬТАТАМ ПОИСКА ---
@dp.callback_query(F.data.startswith("search_"), SearchState.browsing)
async def navigate_search(callback: CallbackQuery, state: FSMContext):
    data_str = callback.data  # search_{index} или search_order_{index}

    if data_str.startswith("search_order_"):
        index = int(data_str.replace("search_order_", ""))
        state_data = await state.get_data()
        results = state_data.get("results", [])
        if not results or index >= len(results):
            await callback.answer("Ошибка")
            return
        item = results[index]
        await state.set_state(OrderState.waiting_size)
        await state.update_data(item=item)
        await callback.message.answer(
            f"📏 Вы выбрали *{item['name']}*\n\nУкажите ваш размер (M, L, XL, 42...):",
            parse_mode="Markdown"
        )
        await callback.answer()
        return

    index = int(data_str.replace("search_", ""))
    state_data = await state.get_data()
    results = state_data.get("results", [])
    if not results:
        await callback.answer("Результаты устарели, выполните поиск заново")
        return
    await show_search_item(callback, results, index, edit=True)
    await callback.answer()

# --- СТАТУСЫ ---
@dp.callback_query(F.data.startswith("st_"))
async def process_status(callback: CallbackQuery):
    parts = callback.data.split("_")
    action = parts[1]
    user_id = int(parts[2])

    status_map = {
        "bought":   "✅ Выкуплено",
        "shipping": "🚛 В пути",
        "stock":    "📦 На складе",
        "cancel":   "❌ Отмена / Нет в наличии"
    }
    new_status = status_map.get(action, "—")

    try:
        await bot.send_message(
            user_id,
            f"🔔 *Обновление по вашему заказу!*\n\nНовый статус: `{new_status}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Ошибка уведомления: {e}")

    current = callback.message.text or ""
    base = current.split("\n\n📍")[0]
    updated = f"{base}\n\n📍 *СТАТУС:* {new_status}"
    kb = None if action == "cancel" else get_status_keyboard(user_id)
    await callback.message.edit_text(updated, parse_mode="Markdown", reply_markup=kb)
    await callback.answer(f"Статус: {new_status}")

# --- ОТВЕТ ИЗ ГРУППЫ ---
@dp.message(F.chat.id == GROUP_ID, F.reply_to_message)
async def reply_to_client(message: types.Message):
    source = message.reply_to_message.text or message.reply_to_message.caption or ""
    match = re.search(r"ID клиента: `(\d+)`", source)
    if not match:
        return
    user_id = int(match.group(1))
    try:
        if message.text:
            await bot.send_message(user_id,
                f"✉️ *Ответ от продавца XV ARCHIVE:*\n\n{message.text}",
                parse_mode="Markdown")
        elif message.photo:
            await bot.send_photo(user_id, message.photo[-1].file_id,
                caption=f"✉️ *Ответ от продавца XV ARCHIVE:*\n\n{message.caption or ''}",
                parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# --- СВЯЗЬ ---
@dp.message(F.text == "💬 Связь")
async def contact(message: types.Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Написать менеджеру", url=f"https://t.me/{MY_USERNAME}")
    ]])
    await message.answer("Есть вопросы? Напиши нам напрямую:", reply_markup=markup)

# --- ЗАПУСК ---
async def main():
    await load_all_catalogs()
    total = sum(len(v) for v in CATALOG.values())
    print(f"🚀 БОТ XV ARCHIVE ЗАПУЩЕН! Товаров: {total}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
