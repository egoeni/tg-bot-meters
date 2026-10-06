"""
Telegram-бот учёта показаний счётчиков.

Распознаёт фото через GigaChat, ведёт историю в SQLite,
считает расход и стоимость, напоминает о сдаче показаний.
"""

import asyncio
import os
import re
import sqlite3
import json
import glob


from datetime import datetime

from PIL import Image, ImageEnhance, ImageOps
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

#DEBUG_DIR = "debug"
#os.makedirs(DEBUG_DIR, exist_ok=True)


# Конфигурация
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0")) # ограничения на /list_targets
_raw_allowed = os.getenv("ALLOWED_USERS", "") # ограничения на /set, временная переменная
ALLOWED_USERS = {
    int(x.strip())
    for x in _raw_allowed.split(",")
    if x.strip().isdigit()
}

if not ADMIN_ID:# Проверка, что настройки заданы
    raise RuntimeError("ADMIN_ID не задан в .env")
if not ALLOWED_USERS:
    raise RuntimeError("ALLOWED_USERS не задан в .env (через запятую)")

DB_PATH = "bot.db"
DEFAULT_METERS = [
    ("hvs",      "ХВС",         "м³",    5,   0),
    ("gvs",      "ГВС",         "м³",    5,   0),
    ("el_day",   "Эл-во день",  "кВт·ч", 5,   0),
    ("el_night", "Эл-во ночь",  "кВт·ч", 5,   0),
    ("gaz",      "Газ",         "м³",    6,   0),
    ("heat",     "Тепло",       "Гкал",  6,   3),
]


# ---------- База данных ----------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS meters (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    TEXT NOT NULL,
            code       TEXT NOT NULL,
            name       TEXT NOT NULL,
            serial     TEXT,
            unit       TEXT DEFAULT 'м³',
            features   TEXT,
            int_digits INTEGER DEFAULT 5,
            decimal_digits INTEGER DEFAULT 0,
            active     INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, code)
        );
        CREATE TABLE IF NOT EXISTS readings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id   INTEGER NOT NULL,
            value      REAL NOT NULL,
            raw_text   TEXT,
            photo_id   TEXT,
            created_by INTEGER,
            period     TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (meter_id) REFERENCES meters(id)
        );
        CREATE TABLE IF NOT EXISTS tariffs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id   INTEGER NOT NULL,
            rate       REAL NOT NULL,
            valid_from TEXT NOT NULL,          -- YYYY-MM-DD
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (meter_id) REFERENCES meters(id)
        );
        CREATE INDEX IF NOT EXISTS idx_tariffs_meter
            ON tariffs(meter_id, valid_from);
        """)
init_db()


# ---------- chats.json ----------
CHATS_FILE = "chats.json"
def get_chats() -> dict:
    if os.path.exists(CHATS_FILE):
        try:
            with open(CHATS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except (json.JSONDecodeError, ValueError):
            print(f"⚠️ {CHATS_FILE} повреждён — сбрасываю")
            return {}
    return {}

def save_chats(chats: dict):
    tmp = CHATS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(chats, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CHATS_FILE)


# ---------- targets.json ----------
TARGETS_FILE = "targets.json"
def get_targets() -> dict:
    if os.path.exists(TARGETS_FILE):
        try:
            with open(TARGETS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except (json.JSONDecodeError, ValueError):
            print(f"⚠️ {TARGETS_FILE} повреждён — сбрасываю")
            return {}
    return {}

def save_targets(targets):
    tmp = TARGETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(targets, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TARGETS_FILE)


# ---------- muted.json ----------
MUTED_FILE = "muted.json"
def get_muted() -> set:
    if os.path.exists(MUTED_FILE):
        try:
            with open(MUTED_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return set()
                return set(json.loads(content))
        except (json.JSONDecodeError, ValueError):
            print(f"⚠️ {MUTED_FILE} повреждён — сбрасываю")
            return set()
    return set()

def save_muted(muted: set):# атомарная запись с защитой
    tmp = MUTED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(muted), f, ensure_ascii=False, indent=2)
    os.replace(tmp, MUTED_FILE)


# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")


# ---------- Служебные команды ----------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот-помощник.\n"
        "• /set — активировать меня в этом чате\n"
        "• /stop — деактивировать меня в этом чате\n"
        "• /meters — список счётчиков\n"
        "• /last — последние показания\n"
        "• /report — отчет за текущий месяц\n"
        "• /mute — приостановить отправку напоминаний\n"        
        "• /unmute — возобновить отправку напоминаний\n"
        "• /add — ручной ввод показаний без фото\n"  
        "• или просто отправьте фото с показаниями счётчика и я их сохраню"
    )

@dp.my_chat_member()
async def on_my_chat_member(update):
    """
    Ловит изменения статуса бота в чате.
    Записывает все чаты, куда добавили бота, в chats.json.
    """
    chat = update.chat
    cid = str(chat.id)
    title = chat.title or chat.full_name or "Личный чат"
    status = update.new_chat_member.status

    chats = get_chats()

    if status in ("member", "administrator", "creator"):
        # Бота добавили или повысили
        if cid not in chats:
            print(f"[chats] Бот добавлен в чат: {title} ({cid})")
        chats[cid] = {
            "title": title,
            "type": chat.type,
            "status": status,
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_chats(chats)

    elif status in ("left", "kicked"):
        # Бота удалили
        if cid in chats:
            print(f"[chats] Бот удалён из чата: {title} ({cid})")
            chats[cid]["status"] = "left"
            chats[cid]["left_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_chats(chats)

@dp.message(Command("chats"))
async def cmd_chats(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Команда только для администратора.")
        return

    chats = get_chats()
    if not chats:
        await message.answer("Бот пока нигде не зафиксирован.")
        return

    # Активные чаты (status = member/admin/creator)
    active = []
    left = []

    for cid, info in chats.items():
        status = info.get("status", "unknown")
        title = info.get("title", "—")
        ctype = info.get("type", "—")
        line = f"• <b>{title}</b>\n  <code>{cid}</code> ({ctype}, {status})"

        if status in ("member", "administrator", "creator"):
            active.append(line)
        else:
            left.append(line)

    text = ""
    if active:
        text += "✅ <b>Активные чаты:</b>\n" + "\n".join(active) + "\n\n"
    if left:
        text += "❌ <b>Бот удалён:</b>\n" + "\n".join(left)

    await message.answer(text, parse_mode="HTML")

@dp.message(Command("mute"))
async def cmd_mute(message: Message):
    cid = str(message.chat.id)
    muted = get_muted()
    muted.add(cid)
    save_muted(muted)
    await message.answer("🔕 Напоминания приостановлены.")

@dp.message(Command("unmute"))
async def cmd_unmute(message: Message):
    cid = str(message.chat.id)
    muted = get_muted()
    muted.discard(cid)
    save_muted(muted)
    await message.answer("🔔 Напоминания включены.")

@dp.message(Command("list_targets"))
async def cmd_list_targets(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Этот бот приватный, обратитесь к администратору.")
        return
    targets = get_targets()
    if not targets:
        await message.answer("Активных чатов для напоминаний нет.")
        return
    lines = [f"• {title} — `{cid}`" for cid, title in targets.items()]
    await message.answer("Чаты с активированным помощником:\n" + "\n".join(lines), parse_mode="Markdown")

# ---------- Управление чатом ----------
@dp.message(Command("set"))
async def cmd_set(message: Message):
    if message.from_user.id not in ALLOWED_USERS:
        await message.answer("Этот бот приватный, обратитесь к администратору")
        return
    cid = str(message.chat.id)
    title = message.chat.title or message.chat.full_name or "Личный чат"
    #активация чата в JSON
    targets = get_targets()
    targets[cid] = title
    save_targets(targets)
    #создание стандартного набора счетчиков
    added = []
    with sqlite3.connect(DB_PATH) as conn:
        for code, name, unit, int_digits, decimal_digits in DEFAULT_METERS:
            try:
                conn.execute(
                    "INSERT INTO meters (chat_id, code, name, unit, int_digits, decimal_digits) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (cid, code, name, unit, int_digits, decimal_digits),
                )
                added.append(name)
            except sqlite3.IntegrityError:
                pass  # уже существует
    msg = f"✅ Помощник активирован для чата: {title}"
    if added:
        msg += "\n\nДобавлены счётчики:\n• " + "\n• ".join(added)
    await message.answer(msg)

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    cid = str(message.chat.id)
    targets = get_targets()          # ← снова читаем с диска
    if cid in targets:
        title = targets.pop(cid)
        save_targets(targets)
        await message.answer(f"❌ Помощник отключен для чата: {title}")
    else:
        await message.answer("Помощник и так не был включен.")

@dp.message(Command("meters"))
async def cmd_meters(message: Message):
    cid = str(message.chat.id)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT code, name, unit, serial FROM meters "
            "WHERE chat_id = ? AND active = 1 ORDER BY id",
            (cid,),
        )
        rows = cur.fetchall()
    if not rows:
        await message.answer("Счётчиков не зарегистрировано")
        return
    lines = []
    for code, name, unit, serial in rows:
        s = f" (зав. №{serial})" if serial else ""
        lines.append(f"• <b>{name}</b> — код <code>{code}</code>, {unit}{s}")
    await message.answer("Счётчики:\n" + "\n".join(lines), parse_mode="HTML")

@dp.message(Command("add"))
async def cmd_add(message: Message):
    cid = str(message.chat.id)
    targets = get_targets()
    if cid not in targets:
        await message.answer("Сначала активируйте помощника: /set")
        return

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, name, unit FROM meters "
            "WHERE chat_id = ? AND active = 1 ORDER BY id",
            (cid,),
        )
        meters = cur.fetchall()

    if not meters:
        await message.answer("Нет активных счётчиков.")
        return

    buttons = [
        [InlineKeyboardButton(
            text=f"{name} ({unit})",
            callback_data=f"man:{cid}:{mid}",
        )]
        for mid, name, unit in meters
    ]
    buttons.append([InlineKeyboardButton(
        text="❌ Отменить",
        callback_data=f"man:cancel:{cid}",
    )])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Выберите счётчик для ручного ввода:", reply_markup=kb)

@dp.message(Command("last"))
async def cmd_last(message: Message):
    cid = str(message.chat.id)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            SELECT m.name, m.unit, m.decimal_digits, r.value, r.created_at
            FROM meters m
            LEFT JOIN (
                SELECT meter_id, value, created_at,
                       ROW_NUMBER() OVER (PARTITION BY meter_id
                                          ORDER BY period DESC, created_at DESC) AS rn
                FROM readings
            ) r ON r.meter_id = m.id AND r.rn = 1
            WHERE m.chat_id = ? AND m.active = 1
            ORDER BY m.id
        """, (cid,))
        rows = cur.fetchall()
    if not rows:
        await message.answer("Нет счётчиков.")
        return
    lines = []
    for name, unit, decimal_digits, value, dt in rows:
        if value is None:
            lines.append(f"• <b>{name}</b>: нет данных")
        else:
            d = datetime.fromisoformat(dt).strftime("%d.%m.%Y")
            display = format_value(value, decimal_digits)
            lines.append(f"• <b>{name}</b>: {display} {unit} (от {d})")
    await message.answer("Последние показания:\n" + "\n".join(lines),
                         parse_mode="HTML")

@dp.message(Command("report"))
async def cmd_report(message: Message):
    cid = str(message.chat.id)
    parts = message.text.split(maxsplit=1)
    period = parts[1].strip() if len(parts) > 1 else datetime.now().strftime("%Y-%m")

    if not re.match(r"^\d{4}-\d{2}$", period):
        await message.answer(
            "Формат: <code>/report [YYYY-MM]</code>\nНапример: /report 2026-09",
            parse_mode="HTML",
        )
        return

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, name, unit, decimal_digits FROM meters "
            "WHERE chat_id = ? AND active = 1 ORDER BY id",
            (cid,),
        )
        meters = cur.fetchall()

    if not meters:
        await message.answer("Счётчиков нет.")
        return

    lines = [f"📊 <b>Отчёт за {period}</b>\n"]
    total = 0.0
    has_consumption = False

    for mid, name, unit, dec_d in meters:
        current = get_current_reading(mid, period)
        previous = get_previous_reading(mid, period)

        if not current:
            lines.append(f"🔹 <b>{name}</b>\n   нет показаний за этот месяц\n")
            continue

        if not previous:
            lines.append(
                f"🔹 <b>{name}</b>\n"
                f"   текущее: {format_value(current[0], dec_d)} {unit}\n"
                f"   ⚠️ нет предыдущих показаний для расчёта\n"
            )
            continue

        consumption = current[0] - previous[0]

        if consumption < 0:
            lines.append(
                f"🔹 <b>{name}</b>\n"
                f"   {format_value(previous[0], dec_d)} → {format_value(current[0], dec_d)}\n"
                f"   ⚠️ показания уменьшились, проверьте\n"
            )
            continue

        has_consumption = True
        tariff = get_tariff(mid, period)
        if tariff:
            rate, mult_voda, extra_coef, podogrev_gvs = tariff
            cost = consumption * rate * mult_voda + consumption * podogrev_gvs
            total += cost
            cons_str = format_value(consumption, dec_d)
            header = (
                f"🔹 <b>{name}</b>\n"
                f"   {format_value(previous[0], dec_d)} → {format_value(current[0], dec_d)} "
                f"= {cons_str} {unit}\n"
            )
            # Формируем формулу одной строкой
            if mult_voda == 1: # Обычный счётчик: расход × тариф
                formula = f"{cons_str} × {rate:.2f} ₽"
            elif mult_voda == 2 and extra_coef == 0: # ХВС: расход × тариф × множитель
                formula = f"{cons_str} × {rate:.2f} ₽ × {mult_voda:.0f}"
            else: # ГВС: вода + водоотведение + подогрев
                formula = (
                    f"{cons_str} × ({rate:.2f} ₽ × {mult_voda:.0f} "
                    f"+ {podogrev_gvs:.2f} ₽)"
                )

            lines.append(header + f"   {formula} = <b>{cost:.2f} ₽</b>\n")

        else:
            lines.append(
                f"🔹 <b>{name}</b>\n"
                f"   {format_value(previous[0], dec_d)} → {format_value(current[0], dec_d)} "
                f"= {format_value(consumption, dec_d)} {unit}\n"
                f"   ⚠️ тариф не задан (см. таблицу tariffs)\n"
            )

    if has_consumption and total > 0:
        lines.append(f"💰 <b>Итого: {total:.2f} ₽</b>")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------- работа с фото и ручным вводом ----------
# ---------- обработка изрбражения  ----------
def preprocess_photo(path: str): # оптимизация изображения
    img = Image.open(path)
    img.thumbnail((1280, 1280))
    img = img.convert("L") # Конвертация в градации серого
    img = ImageEnhance.Contrast(img).enhance(1.5) # Повышение контраста
    img = ImageEnhance.Sharpness(img).enhance(1.5) # Лёгкая резкость
    img = ImageOps.autocontrast(img, cutoff=1) # Автоконтраст — растягивает гистограмму
    img.save(path, quality=95)

def normalize_serial(s: str) -> str:# нормализация серийника счетчика
    if not s:
        return ""
    return re.sub(r"[^a-zа-я0-9]", "", s.lower())

def parse_gigachat_response(raw: str):# парсер ответа гигачата
    serial = None
    value_str = None
    matched_name = None

    m = re.search(r"SERIAL\s*[:\-]?\s*(.+)", raw, re.IGNORECASE)
    if m:
        s = m.group(1).strip()
        if s and s.upper() != "UNKNOWN":
            serial = normalize_serial(s)

    m = re.search(r"VALUE\s*[:\-]?\s*([\d.,]+)", raw, re.IGNORECASE)
    if m:
        value_str = m.group(1).strip().replace(" ", "")
        value_str = re.sub(r"[^\d.,]", "", value_str)

    m = re.search(r"MATCH\s*[:\-]?\s*(.+)", raw, re.IGNORECASE)
    if m:
        s = m.group(1).strip()
        if s and s.upper() != "UNKNOWN":
            matched_name = s
    return serial, value_str, matched_name

def parse_value(value_str: str, decimal_digits: int): # Превращает строку VALUE в число. При decimal_digits=0 отбрасывает дробь
    if not value_str:
        return None
    normalized = value_str.replace(",", ".").strip()
    normalized = re.sub(r"[^\d.]", "", normalized)
    if normalized.count(".") > 1:
        parts = normalized.split(".")
        normalized = parts[0] + "." + "".join(parts[1:])
    if not normalized:
        return None
    try:
        value = float(normalized)
    except ValueError:
        return None
    if decimal_digits == 0:
        return float(int(value))
    return value

def format_value(value, decimal_digits: int) -> str: # Форматирует число для отображения
    if value is None:
        return "—"
    if decimal_digits == 0:
        return str(int(value))
    return f"{value:.{decimal_digits}f}"


# ---------- расчетные функции  ----------
def get_current_reading(meter_id: int, period: str):# возвращает показаняи счетчика в периоде
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT value, created_at FROM readings "
            "WHERE meter_id = ? AND period = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (meter_id, period),
        )
        return cur.fetchone()

def get_previous_reading(meter_id: int, period: str):# возвращает последние показаняи счетчика
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT value, created_at FROM readings "
            "WHERE meter_id = ? AND period < ? "
            "ORDER BY created_at DESC LIMIT 1",
            (meter_id, period),
        )
        return cur.fetchone()

def get_tariff(meter_id: int, period: str):# возвращает тариф с учетом месяца
    end_date = f"{period}-99"   # строка заведомо больше любого реального дня
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT rate, mult_voda, extra_coef, podogrev_gvs FROM tariffs "
            "WHERE meter_id = ? AND valid_from <= ? "
            "ORDER BY valid_from DESC LIMIT 1",
            (meter_id, end_date),
        )
        row = cur.fetchone()
        return row if row else None


# ---------- Обработчики  ----------
pending_photos: dict[str, dict] = {}
pending_confirmations: dict[str, dict] = {}
pending_manual: dict[str, dict] = {}

async def cleanup_pending():# Удаляет устаревшие pending-записи (старше 4 часов)
    now = datetime.now()
    removed = 0
    for store in (pending_photos, pending_confirmations, pending_manual):
        for key in list(store.keys()):
            ts = store[key].get("ts")
            if ts and (now - ts).total_seconds() > 3600: #
                del store[key]
                removed += 1
     # Уборка сиротских temp-файлов старше 1 часа
    for f in glob.glob("temp_*.jpg"):
        try:
            if os.path.getmtime(f) < now.timestamp() - 3600:
                os.remove(f)
                print(f"[cleanup] Удалён устаревший файл {f}")
        except Exception as e:
            print(f"[cleanup] Не удалось удалить {f}: {e}")
    if removed:
        print(f"[cleanup] Удалено устаревших записей: {removed}")

async def recognize_photo_gigachat(file_id: str, cid: str, message_id: int, meters: list):# Jтправляет в GigaChat. Возвращает (serial_norm, value, raw)
    file_info = await bot.get_file(file_id)
    temp_file = f"temp_{cid}_{message_id}.jpg"
    await bot.download_file(file_info.file_path, temp_file)
    preprocess_photo(temp_file)

    # --- ОТЛАДКА: сохраняем копию перед отправкой в GigaChat ---
    #debug_file = os.path.join(DEBUG_DIR, f"debug_{cid}_{message_id}.jpg")
    #try:
    #    import shutil
    #    shutil.copy(temp_file, debug_file)
    #    print(f"[debug] Фото сохранено: {debug_file}")
    #except Exception as e:
    #    print(f"[debug] Не удалось сохранить копию: {e}")
    # --- конец отладки ---

    serial_norm = None
    value_str = None
    matched_name = None
    raw = ""

    try:
        with GigaChat(credentials=GIGACHAT_CREDENTIALS, verify_ssl_certs=False) as client:
            with open(temp_file, "rb") as f:
                uploaded = client.upload_file(f)
           
            # Формируем список описаний для модели
            meters_desc = "\n".join(
                f"- {name}: {features or 'описание отсутствует'}"
                for _, name, _, _, features, _ in meters
            )
            prompt = (
                "На фото счётчик учёта. Прочитай показания и определи, "
                "какой это счётчик из списка.\n\n"
                "Известные счётчики:\n"
                f"{meters_desc}\n\n"
                "Ответ строго в трёх строках:\n"
                "VALUE: <цифры показаний, как на дисплее. Если есть точка или "
                "запятая — включи её. Без пробелов и букв.>\n"
                "SERIAL: <номер или UNKNOWN>\n"
                "MATCH: <точное название из списка или UNKNOWN>"

            )
            chat = Chat(
                model="GigaChat-2-Pro",
                messages=[
                    Messages(
                        role=MessagesRole.USER,
                        content=prompt,
                        attachments=[uploaded.id_],
                    )
                ],
            )
            response = client.chat(chat)
            content = response.choices[0].message.content
            client.delete_file(uploaded.id_)

        if content:
            raw = content.strip()
            serial_norm, value_str, matched_name = parse_gigachat_response(raw)
            #Отладка
            #print(f"[gigachat] SERIAL={serial_norm}, VALUE={value_str}, MATCH={matched_name}")
            
    except Exception as e:
        print(f"Ошибка автоопределения: {e}")
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)
    return serial_norm, value_str, matched_name, raw

def find_meter_by_serial(serial_norm: str, meters):# Ищет счётчик по норм. серийному номеру
    if not serial_norm:
        return None
    for row in meters:
        serial_db = row[3]
        if serial_db and normalize_serial(serial_db) == serial_norm:
            return row
    return None

def find_meter_by_name(matched_name: str, meters):# Ищет по визуальному совпадению
    if not matched_name:
        return None
    matched_lower = matched_name.lower()
    for row in meters:
        name = row[1].lower()
        if name == matched_lower or name in matched_lower or matched_lower in name:
            return row
    return None

def build_meters_keyboard(cid: str, meters) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=f"{row[1]} ({row[2]})",
            callback_data=f"mtr:{cid}:{row[0]}",
        )]
        for row in meters
    ]
    buttons.append([InlineKeyboardButton(
        text="❌ Отменить",
        callback_data=f"mtr:cancel:{cid}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def delete_old_confirmation(cid: str):# Удаляет старое сообщение подтверждения, если оно висит в чате
    old = pending_confirmations.pop(cid, None)
    if old and old.get("message_id"):
        try:
            await bot.delete_message(int(cid), old["message_id"])
        except Exception:
            pass

async def ask_confirmation_photo(cid, mid, name, unit, value, raw, file_id, user_id, status_msg, decimal_digits=0):# Показывает подтверждение
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Верно", callback_data=f"cfm:yes:{cid}"),
        InlineKeyboardButton(text="❌ Неверно", callback_data=f"cfm:no:{cid}"),
    ]])
    display = format_value(value, decimal_digits)
    sent = await status_msg.edit_text(
        f"📊 <b>{name}</b>: {display} {unit}\n\nСохранить?",
        parse_mode="HTML",
        reply_markup=kb,
    )
    pending_confirmations[cid] = {
        "meter_id": mid, "meter_name": name, "unit": unit,
        "value": value, "raw": raw, "file_id": file_id, "user_id": user_id,
        "decimal_digits": decimal_digits,
        "ts": datetime.now(),
        "message_id": sent.message_id,
    }

async def ask_confirmation(message_or_call, cid, meter_id, meter_name, unit, value, raw, file_id, user_id, decimal_digits=0):
    """
    Универсальное подтверждение:
      - CallbackQuery → редактирует сообщение с кнопками
      - Message → создаёт новое сообщение (для ручного ввода)
    """
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Верно", callback_data=f"cfm:yes:{cid}"),
        InlineKeyboardButton(text="❌ Неверно", callback_data=f"cfm:no:{cid}"),
    ]])
    display = format_value(value, decimal_digits)   # ← добавили
    text = f"📊 <b>{meter_name}</b>: {display} {unit}\n\nСохранить?"

    if isinstance(message_or_call, CallbackQuery):
        sent = await message_or_call.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb
        )
    else:
        sent = await message_or_call.answer(
            text, parse_mode="HTML", reply_markup=kb
        )

    pending_confirmations[cid] = {
        "meter_id": meter_id, "meter_name": meter_name, "unit": unit,
        "value": value, "raw": raw, "file_id": file_id, "user_id": user_id,
        "decimal_digits": decimal_digits,
        "ts": datetime.now(),
        "message_id": sent.message_id,
    }

@dp.message(F.photo)
async def handle_photo(message: Message):
    cid = str(message.chat.id)
    # Проверяем, что чат активирован (JSON)
    if cid not in get_targets():
        await message.answer("Сначала активируйте помощника: /set")
        return
    # Счётчики — из БД
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, name, unit, serial, features, decimal_digits "
            "FROM meters WHERE chat_id = ? AND active = 1 ORDER BY id",
            (cid,),
        )
        meters = cur.fetchall()
    if not meters:
        await message.answer("Нет зарегистированных счётчиков")
        return
    # Удаляем старое сообщение подтверждения, если висит
    await delete_old_confirmation(cid)
    # Скачиваем и распознаём
    file_id = message.photo[-1].file_id
    status_msg = await message.answer("🔍 Определяю счётчик…")
    serial_norm, value_str, matched_name, raw = await recognize_photo_gigachat(
        file_id, cid, message.message_id, meters
    )
    
    # Определяем счётчик: серийник → визуальные признаки
    matched = find_meter_by_serial(serial_norm, meters)
    if not matched:
        matched = find_meter_by_name(matched_name, meters)

    # Если нашли и цифры есть — подтверждение
    if matched and value_str:
        mid, name, unit, serial, features, decimal_digits = matched
        value = parse_value(value_str, decimal_digits)

        if value is not None:
            await ask_confirmation_photo(
                cid, mid, name, unit, value, raw, file_id,
                message.from_user.id, status_msg, decimal_digits,
            )
            return

    # Fallback — выбор счётчика кнопкой
    await status_msg.delete()

    pending_photos[cid] = {
        "file_id": file_id,
        "chat_id": cid,
        "user_id": message.from_user.id,
        "ts": datetime.now(),
        "raw": raw,
        "value_str": value_str,
    }

    await message.answer(
        "К какому счётчику относится фото?",
        reply_markup=build_meters_keyboard(cid, meters),
    )
    
@dp.callback_query(F.data.startswith("mtr:"))
async def cb_meter_chosen(call: CallbackQuery):
    parts = call.data.split(":")
    if parts[1] == "cancel":
        cid = parts[2]
        pending_photos.pop(cid, None)
        await call.message.edit_text("❌ Отменено.")
        return

    cid = parts[1]
    meter_id = int(parts[2])

    # Забираем данные из pending
    data = pending_photos.pop(cid, None)
    if not data:
        await call.answer("Фото устарело — отправьте заново", show_alert=True)
        await call.message.edit_text("❌ Фото устарело, отправьте новое.")
        return

    file_id = data["file_id"]
    # ← СНАЧАЛА читаем счётчик из БД
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT name, unit, decimal_digits FROM meters WHERE id = ?",
            (meter_id,),
        )
        row = cur.fetchone()

    if not row:
        await call.answer("Счётчик не найден", show_alert=True)
        return
    #определяем переменные
    meter_name, unit, decimal_digits  = row

    # Парсим значение с учётом decimal_digits
    value_str = data.get("value_str")
    if value_str:
        value = parse_value(value_str, decimal_digits)
        if value is not None:
            raw = data.get("raw", "")
            await ask_confirmation(
                call, cid, meter_id, meter_name, unit,
                value, raw, file_id, call.from_user.id, decimal_digits,
            )
            return

    # Если сюда дошли — показаний нет
    await call.message.edit_text(
        "⚠️ Не удалось распознать показания на фото.\n"
        "Сфотографируйте счётчик крупнее, чётче и при хорошем свете — "
        "и отправьте фото заново."
    )

@dp.callback_query(F.data.startswith("man:"))# Функция ручного заполнения
async def cb_manual_meter(call: CallbackQuery):
    parts = call.data.split(":")
    if parts[1] == "cancel":
        cid = parts[2]
        pending_manual.pop(cid, None)
        await call.message.edit_text("❌ Отменено.")
        return

    cid = parts[1]
    meter_id = int(parts[2])

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT name, unit FROM meters WHERE id = ?", (meter_id,)
        )
        row = cur.fetchone()
    if not row:
        await call.answer("Счётчик не найден", show_alert=True)
        return
    meter_name, unit = row

    pending_manual[cid] = {
        "meter_id": meter_id,
        "meter_name": meter_name,
        "unit": unit,
        "ts": datetime.now(),
    }

    await call.message.edit_text(
        f"✍️ Введите показания для <b>{meter_name}</b> ({unit}).\n",
        parse_mode="HTML",
    )

@dp.callback_query(F.data.startswith("cfm:"))
async def cb_confirm(call: CallbackQuery):
    _, action, cid = call.data.split(":")

    data = pending_confirmations.pop(cid, None)
    if not data:
        await call.answer("Подтверждение устарело", show_alert=True)
        await call.message.edit_text("❌ Подтверждение устарело, отправьте фото заново.")
        return

    if action == "no":
        # Предлагаем ввести показания вручную для того же счётчика
        mid = data["meter_id"]
        name = data["meter_name"]
        unit = data["unit"]

        pending_manual[cid] = {
            "meter_id": mid,
            "meter_name": name,
            "unit": unit,
            "ts": datetime.now(),
        }

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"man:cancel:{cid}"),
        ]])
        await call.message.edit_text(
            f"✍️ Введите показания для "
            f"<b>{name}</b> ({unit}) вручную.\n",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    # action == "yes" — сохраняем
    period = datetime.now().strftime("%Y-%m")
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO readings "
            "(meter_id, value, raw_text, photo_id, created_by, period, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                data["meter_id"], data["value"], data["raw"],
                data["file_id"], data["user_id"], period, created_at,
            ),
        )

    display = format_value(data["value"], data.get("decimal_digits", 0))
    await call.message.edit_text(
        f"✅ Сохранено: <b>{data['meter_name']}: {display} {data['unit']}</b>",
        parse_mode="HTML",
    )

@dp.message(F.text & ~F.text.startswith("/"))# Текстовый обработчик ручного воода
async def handle_manual_input(message: Message):
    cid = str(message.chat.id)
    data = pending_manual.get(cid)
    if not data:
        return

    text = message.text.strip()
    cleaned = text.replace(" ", "").replace(",", ".")

    try:
        value = float(cleaned)
    except ValueError:
        await message.answer("⚠️ Введите число, например: 1234.56 или 1234")
        return

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT decimal_digits FROM meters WHERE id = ?",
            (data["meter_id"],),
        )
        row = cur.fetchone()
    decimal_digits = row[0] if row else 0

    if decimal_digits == 0:
        value = float(int(value))

    pending_manual.pop(cid, None)
    # Переиспользуем ask_confirmation
    await ask_confirmation(
        message, cid,
        data["meter_id"], data["meter_name"], data["unit"],
        value, "manual", "",
        message.from_user.id, decimal_digits,
    )


# ---------- Прочие команды ----------

# ---------- Планировщик ----------
async def send_daily_reminder():#Типовая функция рассылки
    targets = get_targets()
    if not targets:
        print("Нет активных чатов — рассылка пропущена")
        return
    for cid in list(targets.keys()):
        try:
            await bot.send_message(
                int(cid),
                text="Добрый день, сфотографируйте показания счетчиков"
            )
        except Exception as e:
            print(f"Не удалось отправить в {cid}: {e}")

async def check_and_remind():# С 25-го числа напоминает о несданных показаниях
    now = datetime.now()
    if now.day < 25:
        return   # до 25-го ничего не делаем

    period = now.strftime("%Y-%m")
    targets = get_targets()
    if not targets:
        print("Нет активных чатов — рассылка пропущена")
        return

    muted = get_muted()

    for cid in list(targets.keys()):
        if cid in muted:
            print(f"[mute] {cid} — напоминание пропущено")
            continue

        with sqlite3.connect(DB_PATH) as conn:
            # Ищем активные счётчики чата без показаний за текущий период
            cur = conn.execute("""
                SELECT m.name, m.unit
                FROM meters m
                WHERE m.chat_id = ? AND m.active = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM readings r
                      WHERE r.meter_id = m.id AND r.period = ?
                  )
            """, (cid, period))
            missing = cur.fetchall()

        if not missing:
            continue   # все показания сданы — не напоминаем
        if now.day == 25:
            text = "Добрый день, сфотографируйте показания счетчиков"
        else:
            names = "\n".join(f"• {n} ({u})" for n, u in missing)
            text = (
                f"⚠️ За {period} не получены показания:\n{names}\n\n"
                f"Пожалуйста, сфотографируйте и отправьте фото."
            )
        try:
            await bot.send_message(int(cid), text)
        except Exception as e:
            print(f"Не удалось отправить в {cid}: {e}")

# Планируем задачу
#scheduler.add_job(send_daily_reminder, 'cron',day=25, hour=18, minute=00, id='daily_reminder', replace_existing=True)
scheduler.add_job(check_and_remind, 'cron', hour=18, minute=0, id='daily_reminder', replace_existing=True)
scheduler.add_job(cleanup_pending, "interval", minutes=30, id="cleanup_pending", replace_existing=True)#очистка устаревших фото


# ---------- Запуск ----------
async def main():
    scheduler.start()
    await dp.start_polling(bot)
if __name__ == "__main__":
    asyncio.run(main())
