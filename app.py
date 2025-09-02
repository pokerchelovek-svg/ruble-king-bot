# -*- coding: utf-8 -*-
"""
Ruble King Wallet Bot — full build (aiogram when SSL available, CLI fallback removed here)

Fix:
- Restored ALL handlers (previous revision accidentally omitted them), which caused
  "Update is not handled" messages. Now /start, buttons, and all flows are wired.
- Only one operator button kept: "Открыть чат оператора".
- Duplicate protection for HASH + clickable Tronscan link.

Install:
    pip install aiogram qrcode pillow
Run:
    python Бот.py
"""
import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import html as ihtml

# --- aiogram imports (assumes normal Windows/Python with SSL) ---
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.client.default import DefaultBotProperties

import qrcode

# =================== CONFIG ===================
BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "8201069256:AAF7EMe_O-_YudJ614MN50qiz8yNDJvO-hY",
)
WALLET_ADDRESS = os.getenv("USDT_TRC20_ADDRESS", "TNXZrXV2SMChzkzMZUhTBC77CfAUvDfu7R")
OPERATOR_CHAT = os.getenv("OPERATOR_CHAT", "@ClubGG_Ruble_King")
DB_PATH = os.getenv("DB_PATH", "ruble_king_bot.db")

# key-value settings (e.g., bound operator chat id) will be stored in DB

# ==============================================
router = Router()

# -------------- DB helpers --------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                club_id TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER,
                club_id TEXT,
                amount TEXT,
                usdt_address TEXT,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER,
                club_id TEXT,
                tx_hash TEXT UNIQUE,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()

def get_user(tg_id: int):
    with db_connect() as conn:
        cur = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        return cur.fetchone()

def set_user(tg_id: int, username: str | None, club_id: str | None):
    now = datetime.utcnow().isoformat()
    with db_connect() as conn:
        if get_user(tg_id):
            conn.execute("UPDATE users SET username=?, club_id=?, updated_at=? WHERE tg_id=?", (username, club_id, now, tg_id))
        else:
            conn.execute("INSERT INTO users (tg_id, username, club_id, created_at, updated_at) VALUES (?,?,?,?,?)", (tg_id, username, club_id, now, now))
        conn.commit()

def update_club_id(tg_id: int, club_id: str):
    u = get_user(tg_id)
    username = None if not u else u["username"]
    set_user(tg_id, username, club_id)

def deposit_exists(tx_hash: str) -> bool:
    with db_connect() as conn:
        cur = conn.execute("SELECT 1 FROM deposits WHERE tx_hash=?", (tx_hash,))
        return cur.fetchone() is not None

def add_deposit(tg_id: int, club_id: str, tx_hash: str) -> bool:
    now = datetime.utcnow().isoformat()
    try:
        with db_connect() as conn:
            conn.execute("INSERT INTO deposits (tg_id, club_id, tx_hash, created_at) VALUES (?,?,?,?)", (tg_id, club_id, tx_hash, now))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def add_withdrawal(tg_id: int, club_id: str, amount_str: str, usdt_address: str):
    now = datetime.utcnow().isoformat()
    with db_connect() as conn:
        conn.execute("INSERT INTO withdrawals (tg_id, club_id, amount, usdt_address, created_at) VALUES (?,?,?,?,?)", (tg_id, club_id, amount_str, usdt_address, now))
        conn.commit()

# -------------- Settings helpers --------------

def set_setting(key: str, value: str):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()

def get_setting(key: str) -> str | None:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

# -------------- Validators --------------

def valid_club_id(s: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{4}", (s or "").strip()))

def valid_tx_hash(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Fa-f0-9]{64}", (s or "").strip()))

def tronscan_url(tx_hash: str) -> str:
    return f"https://tronscan.org/#/transaction/{tx_hash}"

# -------------- Keyboards --------------

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="deposit")],
        [InlineKeyboardButton(text="💸 Вывод средств", callback_data="withdraw")],
        [InlineKeyboardButton(text="👨‍💼 Связь с оператором", callback_data="contact")],
    ])

def contact_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть чат оператора", url="https://t.me/ClubGG_Ruble_King")]])

# -------------- QR helpers --------------

def make_qr_png_bytes(data: str) -> bytes:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def try_load_static_qr() -> bytes | None:
    for path in ["qr.png", "qr.jpg", "Кьюар.jpg", "/mnt/data/Кьюар.jpg"]:
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    return f.read()
            except Exception:
                pass
    return None

async def send_wallet_info(message: Message, ask_for_tx: bool = True):
    text = (
        "<b>USDT (TRC20) — адрес для пополнения</b>\n\n"
        f"<code>{WALLET_ADDRESS}</code>\n\n"
        "Отправьте только USDT в сети TRON (TRC20). Иные сети/монеты будут потеряны."
    )
    qr_bytes = try_load_static_qr() or make_qr_png_bytes(WALLET_ADDRESS)
    if qr_bytes:
        photo = BufferedInputFile(qr_bytes, filename="usdt_trc20_qr.png")
        caption = text + ("\n\nПосле оплаты пришлите <b>Hash транзакции</b>." if ask_for_tx else "")
        await message.answer_photo(photo=photo, caption=caption, parse_mode=ParseMode.HTML)
    else:
        await message.answer(text + ("\n\nПосле оплаты пришлите <b>Hash транзакции</b>." if ask_for_tx else ""), parse_mode=ParseMode.HTML)

async def notify_operator(bot: "Bot", text: str) -> tuple[bool, str | None]:
    try:
        await bot.send_message(chat_id=(get_setting("ops_chat_id") or OPERATOR_CHAT), text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

def format_user_link(message: Message) -> str:
    name = ihtml.escape(message.from_user.full_name)
    return f'<a href="tg://user?id={message.from_user.id}">{name}</a>'

# -------------- FSM States --------------
class DepositStates(StatesGroup):
    waiting_club_id = State()
    waiting_tx_hash = State()

class WithdrawStates(StatesGroup):
    waiting_club_id = State()
    waiting_amount = State()
    waiting_wallet = State()

class OperatorStates(StatesGroup):
    waiting_message = State()

# -------------- Handlers --------------
@router.message(Command("bind_ops"))
async def bind_ops(message: Message):
    set_setting("ops_chat_id", str(message.chat.id))
    await message.answer(
        f"✅ Привязано! Теперь уведомления будут отправляться сюда.\nchat_id: <code>{message.chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )

@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext):
    set_user(message.from_user.id, message.from_user.username, None)
    await state.clear()
    await message.answer("Привет! Я бот Ruble King. Чем могу помочь?", reply_markup=main_menu_kb())

@router.callback_query(F.data == "deposit")
async def on_deposit(cb: CallbackQuery, state: FSMContext):
    user = get_user(cb.from_user.id)
    if user and user["club_id"]:
        await cb.message.answer("Ваш ID сохранён. Ниже адрес для пополнения, затем пришлите Hash транзакции:")
        await send_wallet_info(cb.message, ask_for_tx=True)
        await state.set_state(DepositStates.waiting_tx_hash)
        await cb.answer()
        return
    await state.set_state(DepositStates.waiting_club_id)
    await cb.message.answer("Укажите ваш ID в клубе Ruble King (формат ####-####):")
    await cb.answer()

@router.message(DepositStates.waiting_club_id)
async def on_deposit_set_id(message: Message, state: FSMContext):
    club_id = (message.text or "").strip()
    if not valid_club_id(club_id):
        await message.answer("Неверный формат. Введите ID в виде 1234-5678.")
        return
    update_club_id(message.from_user.id, club_id)
    await message.answer("ID сохранён. Отправляю реквизиты:")
    await send_wallet_info(message, ask_for_tx=True)
    await state.set_state(DepositStates.waiting_tx_hash)

@router.message(DepositStates.waiting_tx_hash)
async def on_deposit_tx_hash(message: Message, state: FSMContext, bot: Bot):
    tx_hash = (message.text or "").strip()
    if not valid_tx_hash(tx_hash):
        await message.answer("Некорректный Hash. Должен быть 64 символа (0-9, A-F).")
        return
    if deposit_exists(tx_hash):
        await message.answer("Такой Hash уже зарегистрирован. Дубликаты запрещены.")
        return

    user = get_user(message.from_user.id)
    club_id = user["club_id"] if user else "-"
    if not add_deposit(message.from_user.id, club_id or "-", tx_hash):
        await message.answer("Такой Hash уже есть в системе. Дубликаты запрещены.")
        return

    url = tronscan_url(tx_hash)
    summary = (
        "<b>💰 Новая оплата (депозит)</b>\n\n"
        f"Пользователь: {format_user_link(message)}\n"
        f"TG ID: <code>{message.from_user.id}</code>\n"
        f"Club ID: <code>{ihtml.escape(club_id or '-')}</code>\n"
        f"Hash: <code>{ihtml.escape(tx_hash)}</code>\n"
        f"Tronscan: <a href='{ihtml.escape(url)}'>{ihtml.escape(url)}</a>\n"
        f"Дата/время (UTC): {datetime.utcnow().isoformat()}"
    )
    delivered, err = await notify_operator(bot, summary)
    if not delivered:
        logging.error("Operator notification failed: %s", err)
        await message.answer("⚠️ Не удалось доставить сообщение оператору. Нажмите кнопку ниже и напишите напрямую.", reply_markup=contact_kb())
    else:
        await message.answer("Спасибо! Hash получен. Оператор проверит транзакцию в ближайшее время.", reply_markup=main_menu_kb())
    await state.clear()

@router.callback_query(F.data == "withdraw")
async def on_withdraw(cb: CallbackQuery, state: FSMContext):
    await state.set_state(WithdrawStates.waiting_club_id)
    await cb.message.answer("1/3: Укажите ваш ID в клубе Ruble King (формат ####-####):")
    await cb.answer()

@router.message(WithdrawStates.waiting_club_id)
async def on_withdraw_id(message: Message, state: FSMContext):
    club_id = (message.text or "").strip()
    if not valid_club_id(club_id):
        await message.answer("Неверный формат. Введите ID в виде 1234-5678.")
        return
    await state.update_data(club_id=club_id)
    await state.set_state(WithdrawStates.waiting_amount)
    await message.answer("2/3: Введите сумму вывода (например, 150.25):")

@router.message(WithdrawStates.waiting_amount)
async def on_withdraw_amount(message: Message, state: FSMContext):
    amt = (message.text or "").strip().replace(",", ".")
    try:
        dec = Decimal(amt)
        if dec <= 0:
            raise InvalidOperation
    except Exception:
        await message.answer("Некорректная сумма. Попробуйте ещё раз.")
        return
    await state.update_data(amount=str(dec))
    await state.set_state(WithdrawStates.waiting_wallet)
    await message.answer("3/3: Укажите адрес кошелька USDT (TRC20):")

@router.message(WithdrawStates.waiting_wallet)
async def on_withdraw_wallet(message: Message, state: FSMContext, bot: Bot):
    usdt_addr = (message.text or "").strip()
    data = await state.get_data()
    club_id = data.get("club_id", "-")
    amount = data.get("amount", "-")
    add_withdrawal(message.from_user.id, club_id, amount, usdt_addr)

    summary = (
        "<b>🧾 Заявка на вывод средств</b>\n\n"
        f"Пользователь: {format_user_link(message)}\n"
        f"TG ID: <code>{message.from_user.id}</code>\n"
        f"Club ID: <code>{ihtml.escape(club_id)}</code>\n"
        f"Сумма: <b>{ihtml.escape(amount)}</b> USDT\n"
        f"Кошелёк (TRC20): <code>{ihtml.escape(usdt_addr)}</code>\n"
        f"Дата/время (UTC): {datetime.utcnow().isoformat()}"
    )
    delivered, err = await notify_operator(bot, summary)
    if not delivered:
        logging.error("Operator notification failed: %s", err)
        await message.answer("⚠️ Не удалось доставить сообщение оператору. Нажмите кнопку ниже и напишите напрямую.", reply_markup=contact_kb())
    else:
        await message.answer("Заявка принята! Оператор свяжется с вами в ближайшее время.", reply_markup=main_menu_kb())
    await state.clear()

@router.callback_query(F.data == "contact")
async def on_contact(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Связь с оператором:\n\n— Откройте чат по кнопке ниже", reply_markup=contact_kb())
    await cb.answer()

# -------------- Entrypoint --------------
async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    print("Bot is up. Press Ctrl+C to stop.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")
