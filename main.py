import os
import asyncio
import logging
import aiosqlite
from datetime import datetime, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.bot import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
print("=== БОТ ЗАПУЩЕН ===")

DB_PATH = "friends.db"

REMINDER_INTERVALS = {1: 5, 2: 14, 3: 30, 4: 60}
LEVEL_NAMES = {
    1: "самые близкие",
    2: "узкий круг",
    3: "друзья",
    4: "знакомые"
}

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS friends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                friend_name TEXT NOT NULL,
                closeness_level INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS birthdays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                friend_name TEXT NOT NULL,
                birth_date TEXT NOT NULL,
                last_reminded_year INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor = await db.execute("PRAGMA table_info(birthdays)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "last_reminded_year" not in columns:
            await db.execute("ALTER TABLE birthdays ADD COLUMN last_reminded_year INTEGER DEFAULT 0")
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS last_reminders (
                friend_id INTEGER PRIMARY KEY,
                last_reminded_at TEXT NOT NULL
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                friend_name TEXT NOT NULL,
                event_date TEXT NOT NULL,
                description TEXT NOT NULL,
                reminded_1day_before INTEGER DEFAULT 0,
                reminded_on_day INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()
    print(">>> База данных готова")

async def fill_missing_reminders():
    async with aiosqlite.connect(DB_PATH) as db:
        today = datetime.now().strftime("%Y-%m-%d")
        await db.execute("""
            INSERT OR IGNORE INTO last_reminders (friend_id, last_reminded_at)
            SELECT id, ? FROM friends
        """, (today,))
        await db.commit()

async def find_friend_name(db, user_id: int, name: str):
    cursor = await db.execute(
        "SELECT friend_name FROM friends WHERE user_id = ?", (user_id,)
    )
    rows = await cursor.fetchall()
    name_lower = name.lower()
    for row in rows:
        if row[0].lower() == name_lower:
            return row[0]
    return None

async def check_reminders(bot: Bot):
    print(f">>> [{datetime.now()}] Запуск проверки напоминаний...")
    today = datetime.now()
    today_md = f"{today.month:02d}-{today.day:02d}"
    tomorrow = today + timedelta(days=1)
    tomorrow_md = f"{tomorrow.month:02d}-{tomorrow.day:02d}"
    today_full = today.strftime("%Y-%m-%d")
    current_year = today.year

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, user_id, friend_name, last_reminded_year FROM birthdays WHERE strftime('%m-%d', birth_date) = ? AND (last_reminded_year IS NULL OR last_reminded_year != ?)",
            (today_md, current_year)
        )
        bdays_today = await cursor.fetchall()
        
        for bday_id, user_id, name, _ in bdays_today:
            try:
                await bot.send_message(
                    user_id,
                    f"🎂 Сегодня день рождения у <b>{name}</b>! Самое время отправить тёплые пожелания 💌",
                    parse_mode="HTML"
                )
                await db.execute(
                    "UPDATE birthdays SET last_reminded_year = ? WHERE id = ?",
                    (current_year, bday_id)
                )
            except Exception as e:
                logging.error(f"Error sending bday reminder to {user_id}: {e}")
        
        cursor = await db.execute(
            "SELECT id, user_id, friend_name, last_reminded_year FROM birthdays WHERE strftime('%m-%d', birth_date) = ? AND (last_reminded_year IS NULL OR last_reminded_year != ?)",
            (tomorrow_md, current_year)
        )
        bdays_tomorrow = await cursor.fetchall()
        
        for bday_id, user_id, name, _ in bdays_tomorrow:
            try:
                await bot.send_message(
                    user_id,
                    f"🎈 Завтра день рождения у <b>{name}</b>. Есть время подумать о поздравлении 🎁",
                    parse_mode="HTML"
                )
                await db.execute(
                    "UPDATE birthdays SET last_reminded_year = ? WHERE id = ?",
                    (current_year, bday_id)
                )
            except Exception as e:
                logging.error(f"Error sending bday reminder to {user_id}: {e}")
        
        cursor = await db.execute("""
            SELECT f.id, f.user_id, f.friend_name, f.closeness_level, lr.last_reminded_at
            FROM friends f
            JOIN last_reminders lr ON f.id = lr.friend_id
        """)
        rows = await cursor.fetchall()
        
        for friend_id, user_id, name, level, last_date in rows:
            interval = REMINDER_INTERVALS.get(level, 30)
            try:
                last_dt = datetime.strptime(last_date, "%Y-%m-%d")
                days_passed = (today - last_dt).days
                
                if days_passed >= interval:
                    level_name = LEVEL_NAMES.get(level, "друг")
                    await bot.send_message(
                        user_id,
                        f"💛 Давно не общались с <b>{name}</b> ({level_name}). Может, написать?",
                        parse_mode="HTML"
                    )
                    await db.execute(
                        "UPDATE last_reminders SET last_reminded_at = ? WHERE friend_id = ?",
                        (today_full, friend_id)
                    )
            except Exception as e:
                logging.error(f"Error processing friend reminder {friend_id}: {e}")
        
        cursor = await db.execute(
            "SELECT id, user_id, friend_name, event_date, description, reminded_1day_before, reminded_on_day FROM events"
        )
        events = await cursor.fetchall()
        events_to_delete = []
        
        for event_id, user_id, name, event_date, description, reminded_1day, reminded_on_day in events:
            try:
                event_dt = datetime.strptime(event_date, "%Y-%m-%d")
                
                if event_dt.date() == tomorrow.date() and not reminded_1day:
                    await bot.send_message(
                        user_id,
                        f"✨ Завтра важное событие: у <b>{name}</b> — {description} ({event_dt.strftime('%d.%m.%Y')})",
                        parse_mode="HTML"
                    )
                    await db.execute(
                        "UPDATE events SET reminded_1day_before = 1 WHERE id = ?",
                        (event_id,)
                    )
                
                if event_dt.date() == today.date() and not reminded_on_day:
                    await bot.send_message(
                        user_id,
                        f"🌟 Сегодня важный день: у <b>{name}</b> — {description}",
                        parse_mode="HTML"
                    )
                    await db.execute(
                        "UPDATE events SET reminded_on_day = 1 WHERE id = ?",
                        (event_id,)
                    )
                
                if event_dt.date() < today.date():
                    events_to_delete.append(event_id)
            except Exception as e:
                logging.error(f"Error processing event {event_id}: {e}")
        
        for event_id in events_to_delete:
            await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        
        await db.commit()

    print(">>> Проверка завершена")

async def setup_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="👋 Приветствие и список команд"),
        BotCommand(command="add", description="➕ Добавить друга (Имя Уровень)"),
        BotCommand(command="update", description="🔄 Изменить уровень друга"),
        BotCommand(command="reset", description="🔁 Сбросить счётчик напоминаний"),
        BotCommand(command="delete", description="🗑️ Удалить друга"),
        BotCommand(command="bday", description="🎂 Добавить день рождения"),
        BotCommand(command="event", description="📅 Добавить разовое событие"),
        BotCommand(command="list", description="📋 Показать всех друзей"),
        BotCommand(command="birthdays", description="🎈 Показать дни рождения"),
        BotCommand(command="events", description="📆 Показать разовые события"),
        BotCommand(command="test_remind", description="🔍 Проверить напоминания сейчас")
    ]
    await bot.set_my_commands(commands)
    print(">>> Меню команд установлено")

dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>Привет! Я твой бот-напоминалка.</b> 🗓️\n\n"
        "Нажми кнопку <b>/</b> слева от поля ввода, чтобы увидеть все команды!\n\n"
        "<b>Основные команды:</b>\n"
        "• <code>/add Имя Уровень</code> — добавить друга\n"
        "• <code>/bday Имя ДД.ММ</code> — день рождения\n"
        "• <code>/event Имя ДД.ММ.ГГГГ Описание</code> — разовое событие\n"
        "• <code>/list</code> — список друзей\n"
        "• <code>/birthdays</code> — список дней рождения\n"
        "• <code>/events</code> — список событий\n\n"
        "<b>Уровни близости:</b>\n"
        "1 🔥 Самые близкие (каждые 5 дней)\n"
        "2 💙 Узкий круг (каждые 14 дней)\n"
        "3 😊 Друзья (каждые 30 дней)\n"
        "4 👋 Знакомые (каждые 60 дней)",
        parse_mode="HTML"
    )

@dp.message(Command("add"))
async def cmd_add(message: types.Message):
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("⚠️ Формат неверный. Напиши так: <code>/add Имя Уровень</code>\nПример: <code>/add Анна Петрова 1</code>", parse_mode="HTML")
        return
    level = parts[-1]
    friend_name = " ".join(parts[1:-1])
    if level not in ["1", "2", "3", "4"]:
        await message.answer("⚠️ Уровень должен быть 1, 2, 3 или 4.")
        return
    user_id = message.from_user.id
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO friends (user_id, friend_name, closeness_level) VALUES (?, ?, ?)",
            (user_id, friend_name, int(level))
        )
        friend_id = cursor.lastrowid
        await db.execute(
            "INSERT INTO last_reminders (friend_id, last_reminded_at) VALUES (?, ?)",
            (friend_id, today)
        )
        await db.commit()
    level_name = LEVEL_NAMES.get(int(level), "друг")
    await message.answer(f"✅ Друг <b>{friend_name}</b> добавлен в категорию «{level_name}».", parse_mode="HTML")
    print(f">>> Добавлен друг {friend_name}, уровень {level}")

@dp.message(Command("update"))
async def cmd_update(message: types.Message):
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("⚠️ Формат неверный. Напиши так: <code>/update Имя Уровень</code>\nПример: <code>/update Анна Петрова 2</code>", parse_mode="HTML")
        return
    new_level = parts[-1]
    friend_name = " ".join(parts[1:-1])
    if new_level not in ["1", "2", "3", "4"]:
        await message.answer("⚠️ Уровень должен быть 1, 2, 3 или 4.")
        return
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        canonical_name = await find_friend_name(db, user_id, friend_name)
        if not canonical_name:
            await message.answer(f"⚠️ Друг с именем <b>{friend_name}</b> не найден.", parse_mode="HTML")
            return
        await db.execute(
            "UPDATE friends SET closeness_level = ? WHERE user_id = ? AND friend_name = ?",
            (int(new_level), user_id, canonical_name)
        )
        await db.commit()
    level_name = LEVEL_NAMES.get(int(new_level), "друг")
    await message.answer(f"✅ Уровень для <b>{canonical_name}</b> изменён на «{level_name}».", parse_mode="HTML")
    print(f">>> Обновлён уровень {canonical_name} на {new_level}")

@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("⚠️ Формат неверный. Напиши так: <code>/reset Имя</code>\nПример: <code>/reset Анна Петрова</code>", parse_mode="HTML")
        return
    friend_name = " ".join(parts[1:])
    user_id = message.from_user.id
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        canonical_name = await find_friend_name(db, user_id, friend_name)
        if not canonical_name:
            await message.answer(f"⚠️ Друг с именем <b>{friend_name}</b> не найден.", parse_mode="HTML")
            return
        cursor = await db.execute(
            "SELECT id, closeness_level FROM friends WHERE user_id = ? AND friend_name = ?",
            (user_id, canonical_name)
        )
        friend = await cursor.fetchone()
        friend_id = friend[0]
        friend_level = friend[1]
        await db.execute(
            "UPDATE last_reminders SET last_reminded_at = ? WHERE friend_id = ?",
            (today, friend_id)
        )
        await db.commit()
    interval = REMINDER_INTERVALS.get(friend_level, 30)
    await message.answer(
        f"✅ Счётчик для <b>{canonical_name}</b> сброшен. "
        f"Следующее напоминание через {interval} дней.",
        parse_mode="HTML"
    )
    print(f">>> Сброшен счётчик для {canonical_name}")

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("⚠️ Формат неверный. Напиши так: <code>/delete Имя</code>\nПример: <code>/delete Анна Петрова</code>", parse_mode="HTML")
        return
    friend_name = " ".join(parts[1:])
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        canonical_name = await find_friend_name(db, user_id, friend_name)
        if not canonical_name:
            await message.answer(f"⚠️ Друг с именем <b>{friend_name}</b> не найден.", parse_mode="HTML")
            return
        cursor = await db.execute(
            "SELECT id FROM friends WHERE user_id = ? AND friend_name = ?",
            (user_id, canonical_name)
        )
        friend = await cursor.fetchone()
        friend_id = friend[0]
        await db.execute("DELETE FROM last_reminders WHERE friend_id = ?", (friend_id,))
        await db.execute("DELETE FROM friends WHERE id = ?", (friend_id,))
        await db.execute(
            "DELETE FROM birthdays WHERE user_id = ? AND friend_name = ?",
            (user_id, canonical_name)
        )
        await db.commit()
    await message.answer(f"🗑️ <b>{canonical_name}</b> удалён(а) из списка.", parse_mode="HTML")
    print(f">>> Удалён друг {canonical_name}")

@dp.message(Command("bday"))
async def cmd_bday(message: types.Message):
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("⚠️ Формат неверный. Напиши так: <code>/bday Имя ДД.ММ</code>\nПример: <code>/bday Анна Петрова 15.03</code>", parse_mode="HTML")
        return
    date_str = parts[-1]
    input_name = " ".join(parts[1:-1])
    try:
        date_obj = datetime.strptime(date_str, "%d.%m")
        formatted_date = f"2000-{date_obj.month:02d}-{date_obj.day:02d}"
    except ValueError:
        await message.answer("⚠️ Неверный формат даты. Используй ДД.ММ\nПример: 15.03")
        return
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        canonical_name = await find_friend_name(db, user_id, input_name)
        if canonical_name:
            save_name = canonical_name
            note = f"\n\n💡 Нашёл(ла) в твоём списке — использую имя «<b>{canonical_name}</b>»."
        else:
            save_name = input_name
            note = ""
        await db.execute(
            "INSERT INTO birthdays (user_id, friend_name, birth_date) VALUES (?, ?, ?)",
            (user_id, save_name, formatted_date)
        )
        await db.commit()
    await message.answer(
        f"✅ День рождения <b>{save_name}</b> сохранён: {date_str}.{note}",
        parse_mode="HTML"
    )

@dp.message(Command("event"))
async def cmd_event(message: types.Message):
    parts = message.text.split()
    if len(parts) < 4:
        await message.answer(
            "⚠️ Формат неверный. Напиши так: <code>/event Имя ДД.ММ.ГГГГ Описание</code>\n"
            "Пример: <code>/event Вася Иванов 15.10.2026 Свадьба</code>",
            parse_mode="HTML"
        )
        return
    date_index = None
    for i, part in enumerate(parts[1:], 1):
        try:
            datetime.strptime(part, "%d.%m.%Y")
            date_index = i
            break
        except ValueError:
            continue
    if date_index is None:
        await message.answer("⚠️ Не нашёл дату в формате ДД.ММ.ГГГГ\nПример: 15.10.2026")
        return
    input_name = " ".join(parts[1:date_index])
    date_str = parts[date_index]
    description = " ".join(parts[date_index+1:]) if date_index + 1 < len(parts) else "Событие"
    try:
        date_obj = datetime.strptime(date_str, "%d.%m.%Y")
        formatted_date = date_obj.strftime("%Y-%m-%d")
    except ValueError:
        await message.answer("⚠️ Неверный формат даты. Используй ДД.ММ.ГГГГ\nПример: 15.10.2026")
        return
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        canonical_name = await find_friend_name(db, user_id, input_name)
        if canonical_name:
            save_name = canonical_name
            note = f"\n\n💡 Нашёл(ла) в твоём списке — использую имя «<b>{canonical_name}</b>»."
        else:
            save_name = input_name
            note = ""
        await db.execute(
            "INSERT INTO events (user_id, friend_name, event_date, description) VALUES (?, ?, ?, ?)",
            (user_id, save_name, formatted_date, description)
        )
        await db.commit()
    await message.answer(
        f"✅ Событие добавлено!\n"
        f"<b>{save_name}</b> — {description}\n"
        f"Дата: {date_str}\n\n"
        f"Я напомню за день и в сам день события.{note}",
        parse_mode="HTML"
    )
    print(f">>> Добавлено событие: {save_name} - {description} на {date_str}")

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT friend_name, closeness_level FROM friends WHERE user_id = ? ORDER BY closeness_level",
            (user_id,)
        )
        friends = await cursor.fetchall()
    if not friends:
        await message.answer("📭 У тебя пока нет друзей в списке.\nДобавь первого: <code>/add Имя Уровень</code>", parse_mode="HTML")
        return
    response = "📋 <b>Твои друзья:</b>\n\n"
    for name, level in friends:
        level_name = LEVEL_NAMES.get(level, "—")
        response += f"• <b>{name}</b> — {level_name}\n"
    await message.answer(response, parse_mode="HTML")

@dp.message(Command("birthdays"))
async def cmd_birthdays(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT friend_name, birth_date FROM birthdays WHERE user_id = ? ORDER BY birth_date",
            (user_id,)
        )
        birthdays = await cursor.fetchall()
    if not birthdays:
        await message.answer("📭 У тебя пока нет дней рождения в списке.\nДобавь первый: <code>/bday Имя ДД.ММ</code>", parse_mode="HTML")
        return
    response = "🎂 <b>Дни рождения:</b>\n\n"
    for name, date_str in birthdays:
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            formatted = f"{date_obj.day:02d}.{date_obj.month:02d}"
        except Exception:
            formatted = date_str
        response += f"• <b>{name}</b> — {formatted}\n"
    await message.answer(response, parse_mode="HTML")

@dp.message(Command("events"))
async def cmd_events(message: types.Message):
    user_id = message.from_user.id
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT friend_name, event_date, description FROM events WHERE user_id = ? AND event_date >= ? ORDER BY event_date",
            (user_id, today)
        )
        events = await cursor.fetchall()
    if not events:
        await message.answer("📭 У тебя пока нет разовых событий.\nДобавь первое: <code>/event Имя ДД.ММ.ГГГГ Описание</code>", parse_mode="HTML")
        return
    response = "📅 <b>Ближайшие события:</b>\n\n"
    for name, date_str, description in events:
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            formatted = date_obj.strftime("%d.%m.%Y")
        except Exception:
            formatted = date_str
        response += f"• <b>{name}</b> — {description}\n  {formatted}\n\n"
    await message.answer(response, parse_mode="HTML")

@dp.message(Command("test_remind"))
async def cmd_test(message: types.Message, bot: Bot):
    await message.answer("🔍 Запускаю проверку напоминаний...", parse_mode="HTML")
    await check_reminders(bot)
    await message.answer("✅ Проверка завершена!", parse_mode="HTML")

async def main():
    load_dotenv()
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN environment variable is missing!")
    
    await init_db()
    await fill_missing_reminders()
    
    PROXY_URL = os.getenv("PROXY_URL")
    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else AiohttpSession()
    
    bot = Bot(token=TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    
    await setup_bot_commands(bot)
    
    # Сбрасываем старые вебхуки перед включением Long Polling
    await bot.delete_webhook(drop_pending_updates=True)
    
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(check_reminders, "cron", hour=9, minute=0, args=[bot])
    scheduler.start()
    print(">>> Планировщик запущен (проверка каждый день в 9:00 МСК)")
    print("=== ОЖИДАЮ КОМАНД В TELEGRAM ===")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"!!! ОШИБКА: {e}")
