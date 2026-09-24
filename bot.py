import os
import asyncio
import logging
import aiosqlite
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

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
    """Ищет друга по имени без учёта регистра.
    Делает это в Python, чтобы корректно работало с кириллицей."""
    cursor = await db.execute(
        "SELECT friend_name FROM friends WHERE user_id = ?",
        (user_id,)
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
        # Дни рождения СЕГОДНЯ (только если в этом году ещё не напоминали)
        cursor = await db.execute(
            "SELECT id, user_id, friend_name, last_reminded_year FROM birthdays WHERE strftime('%m-%d', birth_date) = ? AND (last_reminded_year IS NULL OR last_reminded_year != ?)",
            (today_md, current_year)
        )
        bdays_today = await cursor.fetchall()
        
        for bday_id, user_id, name, _ in bdays_today:
            await bot.send_message(
                user_id,
                f"🎂 Сегодня день рождения у {name}! Самое время отправить тёплые пожелания 💌"
            )
            await db.execute(
                "UPDATE birthdays SET last_reminded_year = ? WHERE id = ?",
                (current_year, bday_id)
            )
        
        # Дни рождения ЗАВТРА (только если в этом году ещё не напоминали)
        cursor = await db.execute(
            "SELECT id, user_id, friend_name, last_reminded_year FROM birthdays WHERE strftime('%m-%d', birth_date) = ? AND (last_reminded_year IS NULL OR last_reminded_year != ?)",
            (tomorrow_md, current_year)
        )
        bdays_tomorrow = await cursor.fetchall()
        
        for bday_id, user_id, name, _ in bdays_tomorrow:
            await bot.send_message(
                user_id,
                f"🎈 Завтра день рождения у {name}. Есть время подумать о поздравлении 🎁"
            )
            await db.execute(
                "UPDATE birthdays SET last_reminded_year = ? WHERE id = ?",
                (current_year, bday_id)
            )
        
        # Напоминания по уровням близости
        cursor = await db.execute("""
            SELECT f.id, f.user_id, f.friend_name, f.closeness_level, lr.last_reminded_at
            FROM friends f
            JOIN last_reminders lr ON f.id = lr.friend_id
        """)
        rows = await cursor.fetchall()
        
        for friend_id, user_id, name, level, last_date in rows:
            interval = REMINDER_INTERVALS.get(level, 30)
            last_dt = datetime.strptime(last_date, "%Y-%m-%d")
            days_passed = (today - last_dt).days
            
            if days_passed >= interval:
                level_name = LEVEL_NAMES.get(level, "друг")
                await bot.send_message(
                    user_id,
                    f"💛 Давно не общались с {name} ({level_name}). Может, написать?"
                )
                
                await db.execute(
                    "UPDATE last_reminders SET last_reminded_at = ? WHERE friend_id = ?",
                    (today_full, friend_id)
                )
        
        # Разовые события
        cursor = await db.execute(
            "SELECT id, user_id, friend_name, event_date, description, reminded_1day_before, reminded_on_day FROM events"
        )
        events = await cursor.fetchall()
        
        events_to_delete = []
        
        for event_id, user_id, name, event_date, description, reminded_1day, reminded_on_day in events:
            event_dt = datetime.strptime(event_date, "%Y-%m-%d")
            
            if event_dt.date() == tomorrow.date() and not reminded_1day:
                await bot.send_message(
                    user_id,
                    f"✨ Завтра важное событие: у {name} — {description} ({event_dt.strftime('%d.%m.%Y')})"
                )
                await db.execute(
                    "UPDATE events SET reminded_1day_before = 1 WHERE id = ?",
                    (event_id,)
                )
            
            if event_dt.date() == today.date() and not reminded_on_day:
                await bot.send_message(
                    user_id,
                    f"🌟 Сегодня важный день: у {name} — {description}"
                )
                await db.execute(
                    "UPDATE events SET reminded_on_day = 1 WHERE id = ?",
                    (event_id,)
                )
            
            if event_dt.date() < today.date():
                events_to_delete.append(event_id)
        
        for event_id in events_to_delete:
            await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        
        await db.commit()
    
    print(">>> Проверка завершена")

async def main():
    load_dotenv()
    TOKEN = os.getenv("BOT_TOKEN")
    
    await init_db()
    await fill_missing_reminders()
    
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    
    await setup_bot_commands(bot)
    
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(check_reminders, "cron", hour=9, minute=0, args=[bot])
    scheduler.start()
    print(">>> Планировщик запущен (проверка каждый день в 9:00 МСК)")

    @dp.message(Command("start"))
    async def cmd_start(message: types.Message):
        await message.answer(
            "Привет! Я твой бот-напоминалка. 🗓️\n\n"
            "Нажми кнопку / слева от поля ввода, чтобы увидеть все команды!\n\n"
            "Основные команды:\n"
            "• /add Имя Уровень — добавить друга\n"
            "• /bday Имя ДД.ММ — день рождения\n"
            "• /event Имя ДД.ММ.ГГГГ Описание — разовое событие\n"
            "• /list — список друзей\n"
            "• /events — список событий\n\n"
            "Уровни близости:\n"
            "1 🔥 Самые близкие (каждые 5 дней)\n"
            "2 💙 Узкий круг (каждые 14 дней)\n"
            "3 😊 Друзья (каждые 30 дней)\n"
            "4 👋 Знакомые (каждые 60 дней)"
        )

    @dp.message(Command("add"))
    async def cmd_add(message: types.Message):
        parts = message.text.split()
        
        if len(parts) < 3:
            await message.answer("⚠️ Формат неверный. Напиши так: /add Имя Уровень\nПример: /add Анна Петрова 1")
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
        await message.answer(f"✅ Друг {friend_name} добавлен в категорию «{level_name}».")
        print(f">>> Добавлен друг {friend_name}, уровень {level}")

    @dp.message(Command("update"))
    async def cmd_update(message: types.Message):
        parts = message.text.split()
        
        if len(parts) < 3:
            await message.answer("⚠️ Формат неверный. Напиши так: /update Имя Уровень\nПример: /update Анна Петрова 2")
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
                await message.answer(f"⚠️ Друг с именем {friend_name} не найден.")
                return
            
            await db.execute(
                "UPDATE friends SET closeness_level = ? WHERE user_id = ? AND friend_name = ?",
                (int(new_level), user_id, canonical_name)
            )
            await db.commit()
        
        level_name = LEVEL_NAMES.get(int(new_level), "друг")
        await message.answer(f"✅ Уровень для {canonical_name} изменён на «{level_name}».")
        print(f">>> Обновлён уровень {canonical_name} на {new_level}")

    @dp.message(Command("reset"))
    async def cmd_reset(message: types.Message):
        parts = message.text.split()
        
        if len(parts) < 2:
            await message.answer("⚠️ Формат неверный. Напиши так: /reset Имя\nПример: /reset Анна Петрова")
            return
        
        friend_name = " ".join(parts[1:])
        user_id = message.from_user.id
        today = datetime.now().strftime("%Y-%m-%d")
        
        async with aiosqlite.connect(DB_PATH) as db:
            canonical_name = await find_friend_name(db, user_id, friend_name)
            
            if not canonical_name:
                await message.answer(f"⚠️ Друг с именем {friend_name} не найден.")
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
            f"✅ Счётчик для {canonical_name} сброшен. "
            f"Следующее напоминание через {interval} дней."
        )
        print(f">>> Сброшен счётчик для {canonical_name}")

    @dp.message(Command("delete"))
    async def cmd_delete(message: types.Message):
        parts = message.text.split()
        
        if len(parts) < 2:
            await message.answer("⚠️ Формат неверный. Напиши так: /delete Имя\nПример: /delete Анна Петрова")
            return
        
        friend_name = " ".join(parts[1:])
        user_id = message.from_user.id
        
        async with aiosqlite.connect(DB_PATH) as db:
            canonical_name = await find_friend_name(db, user_id, friend_name)
            
            if not canonical_name:
                await message.answer(f"⚠️ Друг с именем {friend_name} не найден.")
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
        
        await message.answer(f"🗑️ {canonical_name} удалён(а) из списка.")
        print(f">>> Удалён друг {canonical_name}")

    @dp.message(Command("bday"))
    async def cmd_bday(message: types.Message):
        parts = message.text.split()
        
        if len(parts) < 3:
            await message.answer("⚠️ Формат неверный. Напиши так: /bday Имя ДД.ММ\nПример: /bday Анна Петрова 15.03")
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
                note = f"\n\n💡 Нашёл(ла) в твоём списке — использую имя «{canonical_name}»."
            else:
                save_name = input_name
                note = ""
            
            await db.execute(
                "INSERT INTO birthdays (user_id, friend_name, birth_date) VALUES (?, ?, ?)",
                (user_id, save_name, formatted_date)
            )
            await db.commit()
        
        await message.answer(
            f"✅ День рождения {save_name} сохранён: {date_str}.{note}"
        )

    @dp.message(Command("event"))
    async def cmd_event(message: types.Message):
        parts = message.text.split()
        
        if len(parts) < 4:
            await message.answer(
                "⚠️ Формат неверный. Напиши так: /event Имя ДД.ММ.ГГГГ Описание\n"
                "Пример: /event Вася Иванов 15.10.2026 Свадьба"
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
                note = f"\n\n💡 Нашёл(ла) в твоём списке — использую имя «{canonical_name}»."
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
            f"{save_name} — {description}\n"
            f"Дата: {date_str}\n\n"
            f"Я напомню за день и в сам день события.{note}"
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
            await message.answer("📭 У тебя пока нет друзей в списке.\nДобавь первого: /add Имя Уровень")
            return
        
        response = "📋 Твои друзья:\n\n"
        
        for name, level in friends:
            level_name = LEVEL_NAMES.get(level, "—")
            response += f"• {name} — {level_name}\n"
        
        await message.answer(response)

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
            await message.answer("📭 У тебя пока нет дней рождения в списке.\nДобавь первый: /bday Имя ДД.ММ")
            return
        
        response = "🎂 Дни рождения:\n\n"
        
        for name, date_str in birthdays:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            formatted = f"{date_obj.day:02d}.{date_obj.month:02d}"
            response += f"• {name} — {formatted}\n"
        
        await message.answer(response)

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
            await message.answer("📭 У тебя пока нет разовых событий.\nДобавь первое: /event Имя ДД.ММ.ГГГГ Описание")
            return
        
        response = "📅 Ближайшие события:\n\n"
        
        for name, date_str, description in events:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            formatted = date_obj.strftime("%d.%m.%Y")
            response += f"• {name} — {description}\n  {formatted}\n\n"
        
        await message.answer(response)

    @dp.message(Command("test_remind"))
    async def cmd_test(message: types.Message):
        await message.answer("🔍 Запускаю проверку напоминаний...")
        await check_reminders(bot)
        await message.answer("✅ Проверка завершена!")

    print("=== ОЖИДАЮ КОМАНД В TELEGRAM ===")
    await dp.start_polling(bot)

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

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"!!! ОШИБКА: {e}")