import asyncio
import json
import os
import logging
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # секунды

# ===== ПОСТЫ ДЛЯ ОТСЛЕЖИВАНИЯ =====
TRACKED_POSTS = [
    # Айфоны
    "BigSaleApple/12854",
    "BigSaleApple/12472",
    "BigSaleApple/12471",
    "BigSaleApple/12470",
    "BigSaleApple/12468",
    "BigSaleApple/12466",
    # Маки
    "BigSaleApple/12463",
    "BigSaleApple/12464",
    "BigSaleApple/12459",
    "BigSaleApple/12460",
    "BigSaleApple/12455",
    "BigSaleApple/12456",
    # Айпады
    "BigSaleApple/12328",
    "BigSaleApple/12304",
    "BigSaleApple/12255",
    "BigSaleApple/12266",
    # Эирподс
    "BigSaleApple/12252",
]
# ===================================

STATE_FILE = "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TG_LIMIT = 4096


async def send_long(target, text: str, **kwargs):
    """Отправляет сообщение, разбивая на части если длиннее TG_LIMIT."""
    while text:
        chunk = text[:TG_LIMIT]
        text = text[TG_LIMIT:]
        await target(chunk, parse_mode="Markdown", **kwargs)


# ========== КЛАВИАТУРА ==========

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Получить все посты", callback_data="get_all")],
        [InlineKeyboardButton(text="🔄 Проверить изменения", callback_data="check_changes")],
    ])

# ========== ХРАНИЛИЩЕ ==========

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ========== ПАРСИНГ ==========

def extract_emoji(node) -> str:
    """Извлекает символ эмодзи из <tg-emoji> или <i class='emoji'>."""
    b = node.find("b")
    return b.get_text() if b else node.get_text()


def html_to_text(elem) -> str:
    """Конвертирует HTML элемент в текст, сохраняя ссылки."""
    result = []
    for node in elem.children:
        if isinstance(node, str):
            result.append(node)
        elif node.name == "br":
            result.append("\n")
        elif node.name == "tg-emoji":
            result.append(extract_emoji(node))
        elif node.name == "i" and "emoji" in (node.get("class") or []):
            result.append(extract_emoji(node))
        elif node.name == "a":
            href = node.get("href", "")
            inner = html_to_text(node)
            if href and inner.strip():
                result.append(f"[{inner}]({href})")
            else:
                result.append(inner)
        elif node.name == "code":
            result.append(f"`{node.get_text()}`")
        else:
            result.append(html_to_text(node))
    return "".join(result)


async def fetch_post_text(session: aiohttp.ClientSession, post_path: str) -> str | None:
    channel, post_id_str = post_path.split("/")
    post_id = int(post_id_str)
    url = f"https://t.me/s/{channel}?before={post_id + 1}"

    try:
        async with session.get(
            url, headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            if resp.status != 200:
                logger.warning(f"HTTP {resp.status} для {url}")
                return None

            html = await resp.text()
            soup = BeautifulSoup(html, "lxml")

            post_wrap = soup.find("div", attrs={"data-post": f"{channel}/{post_id_str}"})

            if not post_wrap:
                logger.warning(f"Пост {post_path} не найден на странице {url}")
                return None

            text_elem = post_wrap.find("div", class_="tgme_widget_message_text")
            if text_elem:
                return html_to_text(text_elem).strip()

            return "[медиа без текста]"

    except asyncio.TimeoutError:
        logger.error(f"Таймаут: {url}")
        return None
    except Exception as e:
        logger.error(f"Ошибка {post_path}: {e}")
        return None


async def fetch_all_posts() -> list[dict]:
    async with aiohttp.ClientSession() as session:
        async def fetch_one(post_path):
            channel, post_id = post_path.split("/")
            text = await fetch_post_text(session, post_path)
            return {
                "path": post_path,
                "post_id": post_id,
                "url": f"https://t.me/{channel}/{post_id}",
                "text": text,
            }

        return await asyncio.gather(*[fetch_one(p) for p in TRACKED_POSTS])


async def check_posts(notify: bool = True) -> tuple[int, int]:
    global CHAT_ID
    state = load_state()
    changed_count = 0
    error_count = 0

    results = await fetch_all_posts()

    for item in results:
        if item["text"] is None:
            error_count += 1
            continue

        prev_text = state.get(item["path"])

        if prev_text != item["text"]:
            changed_count += 1
            state[item["path"]] = item["text"]

            if notify and CHAT_ID:
                if prev_text is None:
                    msg = f"📋 Пост #{item['post_id']} — первоначальное сохранение\n\n{item['text']}\n\n🔗 {item['url']}"
                else:
                    msg = f"⚠️ ЦЕНА ИЗМЕНИЛАСЬ — Пост #{item['post_id']}\n\n📝 Новый текст:\n{item['text']}\n\n🔗 {item['url']}"
                try:
                    await send_long(
                        lambda text: bot.send_message(CHAT_ID, text, disable_web_page_preview=True),
                        msg
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки: {e}")

    save_state(state)
    logger.info(f"Проверка: изменений={changed_count}, ошибок={error_count}")
    return changed_count, error_count


# ========== КОМАНДЫ ==========

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    global CHAT_ID
    if not CHAT_ID:
        CHAT_ID = str(message.chat.id)
        logger.info(f"CHAT_ID установлен: {CHAT_ID}")

    await message.answer(
        f"✅ Бот-мониторинг цен запущен!\n\n"
        f"🔍 Постов: {len(TRACKED_POSTS)}\n"
        f"⏱ Авто-проверка каждые {CHECK_INTERVAL // 60} мин\n\n"
        f"Нажми кнопку чтобы получить посты 👇",
        reply_markup=main_keyboard()
    )


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message):
    await message.answer("Выбери действие 👇", reply_markup=main_keyboard())


# ========== КНОПКИ ==========

@dp.callback_query(F.data == "get_all")
async def on_get_all(callback: types.CallbackQuery):
    await callback.answer()
    msg = await callback.message.answer("⏳ Получаю посты...")

    results = await fetch_all_posts()

    # Сохраняем актуальное состояние
    state = load_state()
    for item in results:
        if item["text"]:
            state[item["path"]] = item["text"]
    save_state(state)

    errors = 0
    for item in results:
        if item["text"] is None:
            errors += 1
            await callback.message.answer(
                f"❌ Пост #{item['post_id']} — не удалось получить\n🔗 {item['url']}",
                disable_web_page_preview=True
            )
        else:
            full = f"📌 Пост #{item['post_id']}:\n\n{item['text']}\n\n🔗 {item['url']}"
            await send_long(callback.message.answer, full, disable_web_page_preview=True)

    summary = f"✅ Готово! Получено: {len(results) - errors}/{len(results)} постов"
    if errors:
        summary += f"\n❌ Ошибок: {errors}"

    await msg.edit_text(summary, reply_markup=main_keyboard())


@dp.callback_query(F.data == "check_changes")
async def on_check_changes(callback: types.CallbackQuery):
    await callback.answer()
    msg = await callback.message.answer("🔄 Проверяю изменения...")

    changed, errors = await check_posts(notify=True)

    lines = []
    if changed == 0:
        lines.append("✅ Изменений нет")
    else:
        lines.append(f"⚠️ Изменилось постов: {changed} — уведомления отправлены")
    if errors:
        lines.append(f"❌ Ошибок: {errors}")

    await msg.edit_text("\n".join(lines), reply_markup=main_keyboard())


# ========== ЗАПУСК ==========

async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан в .env!")

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        check_posts,
        "interval",
        seconds=CHECK_INTERVAL,
        kwargs={"notify": True},
        id="check_posts"
    )
    scheduler.start()
    logger.info(f"Планировщик запущен. Интервал: {CHECK_INTERVAL} сек.")

    logger.info("Первичное сохранение состояния...")
    await check_posts(notify=False)
    logger.info("Готово! Мониторинг активен.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())