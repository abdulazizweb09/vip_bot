#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import os
import random
import string
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is required in .env")

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
if CHANNEL_ID == 0:
    raise ValueError("CHANNEL_ID is required in .env")

SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "0"))
if SUPER_ADMIN_ID == 0:
    raise ValueError("SUPER_ADMIN_ID is required in .env")

CARD_NUMBER = os.getenv("CARD_NUMBER", "8600123456789012")
CARD_HOLDER = os.getenv("CARD_HOLDER", "YOUR NAME")
PAYMENT_TIMEOUT_MINUTES = int(os.getenv("PAYMENT_TIMEOUT_MINUTES", "30"))
TIMEZONE_STR = os.getenv("TIMEZONE", "Asia/Tashkent")

# timezone offset fixed to +5:00 (Asia/Tashkent)
TZ = timezone(timedelta(hours=5))

DATA_FILE = "data.json"
TEMP_FILE = DATA_FILE + ".tmp"

# ---------- DATA HELPERS ----------
data_lock = asyncio.Lock()
_data: Optional[Dict[str, Any]] = None


def default_data() -> Dict[str, Any]:
    return {
        "users": [],
        "payments": [],
        "subscriptions": [],
        "admins": [],
        "join_requests": [],
        "notifications": [],
        "admin_logs": [],
        "tariffs": [
            {"id": 1, "name": "15 kun", "days": 15, "price": 25000, "is_active": True},
            {"id": 2, "name": "30 kun", "days": 30, "price": 50000, "is_active": True},
            {"id": 3, "name": "60 kun", "days": 60, "price": 90000, "is_active": True},
            {"id": 4, "name": "90 kun", "days": 90, "price": 120000, "is_active": True},
        ],
    }


def load_data() -> Dict[str, Any]:
    global _data
    if _data is not None:
        return _data
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            _data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _data = default_data()
        save_data(force=True)
    return _data


def save_data(force: bool = False) -> None:
    global _data
    if _data is None and not force:
        return
    data_to_save = _data if _data is not None else default_data()
    # Write to temp file atomically
    with open(TEMP_FILE, "w", encoding="utf-8") as f:
        json.dump(data_to_save, f, ensure_ascii=False, indent=2)
    os.replace(TEMP_FILE, DATA_FILE)


async def atomic_save() -> None:
    """Save data with lock."""
    async with data_lock:
        save_data()


# ---------- HELPER FUNCTIONS ----------
def now() -> datetime:
    """Current datetime with fixed timezone."""
    return datetime.now(TZ)


def format_dt(dt: datetime) -> str:
    """ISO 8601 with timezone."""
    return dt.isoformat()


def parse_dt(s: str) -> datetime:
    """Parse ISO 8601 string."""
    return datetime.fromisoformat(s)


def generate_id(prefix: str) -> str:
    """Generate unique ID like PAY-123ABC."""
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{prefix}-{suffix}"


def get_user(telegram_id: int, create: bool = True) -> Optional[Dict[str, Any]]:
    data = load_data()
    for u in data["users"]:
        if u["telegram_id"] == telegram_id:
            return u
    if not create:
        return None
    # Create new user
    user_dict = {
        "id": len(data["users"]) + 1,
        "telegram_id": telegram_id,
        "username": None,
        "first_name": "",
        "last_name": "",
        "joined_at": format_dt(now()),
        "is_blocked": False,
    }
    data["users"].append(user_dict)
    save_data()
    return user_dict


def update_user_from_message(message: Message) -> Dict[str, Any]:
    user = get_user(message.from_user.id, create=True)
    # Update username and names
    if message.from_user.username:
        user["username"] = message.from_user.username
    if message.from_user.first_name:
        user["first_name"] = message.from_user.first_name
    if message.from_user.last_name:
        user["last_name"] = message.from_user.last_name
    save_data()
    return user


def get_tariff(tariff_id: int) -> Optional[Dict[str, Any]]:
    data = load_data()
    for t in data["tariffs"]:
        if t["id"] == tariff_id and t["is_active"]:
            return t
    return None


def find_payment(payment_id: str) -> Optional[Dict[str, Any]]:
    data = load_data()
    for p in data["payments"]:
        if p["id"] == payment_id:
            return p
    return None


def find_pending_payment(user_id: int) -> Optional[Dict[str, Any]]:
    data = load_data()
    now_ts = now()
    for p in data["payments"]:
        if p["user_id"] == user_id and p["status"] == "PENDING":
            expires_at = parse_dt(p["expires_at"])
            if expires_at > now_ts:
                return p
    return None


def get_active_subscription(user_id: int) -> Optional[Dict[str, Any]]:
    data = load_data()
    for sub in data["subscriptions"]:
        if sub["user_id"] == user_id and sub["status"] == "ACTIVE":
            return sub
    return None


def get_all_active_subscriptions() -> List[Dict[str, Any]]:
    data = load_data()
    return [s for s in data["subscriptions"] if s["status"] == "ACTIVE"]


def create_payment(user_id: int, tariff_id: int) -> Dict[str, Any]:
    tariff = get_tariff(tariff_id)
    if not tariff:
        raise ValueError("Invalid tariff")
    data = load_data()
    payment = {
        "id": generate_id("PAY"),
        "user_id": user_id,
        "tariff_id": tariff_id,
        "amount": tariff["price"],
        "status": "PENDING",
        "receipt_file_id": None,
        "rejection_reason": None,
        "admin_id": None,
        "created_at": format_dt(now()),
        "expires_at": format_dt(now() + timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)),
        "approved_at": None,
    }
    data["payments"].append(payment)
    save_data()
    return payment


def create_subscription(
    user_id: int,
    payment_id: str,
    days: int,
    start_date: datetime,
    end_date: datetime,
) -> Dict[str, Any]:
    data = load_data()
    sub = {
        "id": generate_id("SUB"),
        "user_id": user_id,
        "payment_id": payment_id,
        "start_date": format_dt(start_date),
        "end_date": format_dt(end_date),
        "status": "ACTIVE",
        "is_active": True,
        "created_at": format_dt(now()),
        "updated_at": format_dt(now()),
    }
    data["subscriptions"].append(sub)
    save_data()
    return sub


def get_join_request(user_id: int) -> Optional[Dict[str, Any]]:
    data = load_data()
    for jr in data["join_requests"]:
        if jr["user_id"] == user_id and jr["status"] == "PENDING":
            return jr
    return None


def create_join_request(user_id: int) -> Dict[str, Any]:
    data = load_data()
    jr = {
        "id": generate_id("JR"),
        "user_id": user_id,
        "chat_id": CHANNEL_ID,
        "status": "PENDING",
        "created_at": format_dt(now()),
        "approved_at": None,
    }
    data["join_requests"].append(jr)
    save_data()
    return jr


def approve_join_request(user_id: int) -> None:
    data = load_data()
    for jr in data["join_requests"]:
        if jr["user_id"] == user_id and jr["status"] == "PENDING":
            jr["status"] = "APPROVED"
            jr["approved_at"] = format_dt(now())
            save_data()
            return


def cancel_subscription(user_id: int) -> Optional[Dict[str, Any]]:
    data = load_data()
    for sub in data["subscriptions"]:
        if sub["user_id"] == user_id and sub["status"] == "ACTIVE":
            sub["status"] = "CANCELLED"
            sub["is_active"] = False
            sub["updated_at"] = format_dt(now())
            save_data()
            return sub
    return None


def add_notification(user_id: int, notif_type: str) -> None:
    """Prevent duplicate notifications of same type."""
    data = load_data()
    # Check if already exists for this user and type within last day to avoid duplicates
    for n in data["notifications"]:
        if n["user_id"] == user_id and n["type"] == notif_type:
            created = parse_dt(n["created_at"])
            if now() - created < timedelta(days=1):
                return
    data["notifications"].append(
        {
            "user_id": user_id,
            "type": notif_type,
            "created_at": format_dt(now()),
        }
    )
    save_data()


def is_admin(telegram_id: int) -> bool:
    if telegram_id == SUPER_ADMIN_ID:
        return True
    data = load_data()
    for a in data["admins"]:
        if a["telegram_id"] == telegram_id:
            return True
    return False


def is_super_admin(telegram_id: int) -> bool:
    return telegram_id == SUPER_ADMIN_ID


def log_admin_action(admin_id: int, action: str, details: Dict[str, Any]) -> None:
    data = load_data()
    data["admin_logs"].append(
        {
            "admin_id": admin_id,
            "action": action,
            "details": details,
            "created_at": format_dt(now()),
        }
    )
    save_data()


# ---------- FSM STATES ----------
class AdminStates(StatesGroup):
    REJECT_REASON = State()
    USER_SEARCH = State()
    MANUAL_SUB_USER = State()
    MANUAL_SUB_TARIFF = State()
    CANCEL_SUB_USER = State()
    CANCEL_SUB_CONFIRM = State()
    ADD_ADMIN = State()
    REMOVE_ADMIN = State()
    BROADCAST = State()
    BROADCAST_CONFIRM = State()


# ---------- KEYBOARDS ----------
def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    tariffs = load_data()["tariffs"]
    for t in tariffs:
        if t["is_active"]:
            builder.button(
                text=f"💎 {t['name']}",
                callback_data=f"tariff_{t['id']}"
            )
    builder.button(text="👤 PROFILIM", callback_data="profile")
    builder.adjust(2)
    return builder.as_markup()


def tariff_detail_keyboard(tariff_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ TO'LOV QILDIM", callback_data=f"pay_{tariff_id}")
    builder.button(text="🔙 ORQAGA", callback_data="back_main")
    builder.adjust(1)
    return builder.as_markup()


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Statistika", callback_data="admin_stats")
    builder.button(text="👥 Foydalanuvchilar", callback_data="admin_users")
    builder.button(text="💳 To'lovlar", callback_data="admin_payments")
    builder.button(text="💎 Obunalar", callback_data="admin_subscriptions")
    builder.button(text="🔎 User qidirish", callback_data="admin_search")
    builder.button(text="➕ Obuna berish", callback_data="admin_manual_sub")
    builder.button(text="➖ Obunani bekor qilish", callback_data="admin_cancel_sub")
    builder.button(text="📢 Broadcast", callback_data="admin_broadcast")
    builder.button(text="👮 Adminlar", callback_data="admin_admins")
    builder.button(text="📋 Loglar", callback_data="admin_logs")
    builder.adjust(2)
    return builder.as_markup()


def admin_stats_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="24 soat", callback_data="admin_stats_24h")
    builder.button(text="7 kun", callback_data="admin_stats_7d")
    builder.button(text="30 kun", callback_data="admin_stats_30d")
    builder.button(text="Umumiy", callback_data="admin_stats_overall")
    builder.button(text="🔙 ORQAGA", callback_data="admin_back")
    builder.adjust(2)
    return builder.as_markup()


def user_detail_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ OBUNA BERISH", callback_data=f"admin_manual_sub_{user_id}")
    builder.button(text="➖ OBUNANI BEKOR QILISH", callback_data=f"admin_cancel_sub_{user_id}")
    builder.button(text="🚫 KANALDAN CHIQARISH", callback_data=f"admin_kick_{user_id}")
    builder.button(text="📜 OBUNA TARIXI", callback_data=f"admin_sub_history_{user_id}")
    builder.button(text="💳 TO'LOVLAR", callback_data=f"admin_pay_history_{user_id}")
    builder.button(text="🔙 ORQAGA", callback_data="admin_back")
    builder.adjust(2)
    return builder.as_markup()


def admin_management_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Admin qo'shish", callback_data="admin_add_admin")
    builder.button(text="➖ Admin o'chirish", callback_data="admin_remove_admin")
    builder.button(text="📋 Adminlar ro'yxati", callback_data="admin_list_admins")
    builder.button(text="🔙 ORQAGA", callback_data="admin_back")
    builder.adjust(2)
    return builder.as_markup()


def yes_no_keyboard(callback_prefix: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ HA", callback_data=f"{callback_prefix}_yes")
    builder.button(text="❌ YO'Q", callback_data=f"{callback_prefix}_no")
    builder.adjust(2)
    return builder.as_markup()


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ YUBORISH", callback_data="broadcast_send")
    builder.button(text="❌ BEKOR", callback_data="broadcast_cancel")
    builder.adjust(2)
    return builder.as_markup()


# ---------- MESSAGE HELPERS ----------
async def send_main_menu(message: Message, user_id: int) -> None:
    text = (
        "🎬 VIP ANIME KANAL\n\n"
        "🔐 Yopiq VIP Anime kanaliga xush kelibsiz!\n\n"
        "💎 VIP obuna orqali:\n\n"
        "• Eksklyuziv anime\n"
        "• Yangi qismlar\n"
        "• Yuqori sifat\n"
        "• Yopiq kanalga kirish\n\n"
        "Tarifni tanlang:"
    )
    await message.answer(text, reply_markup=main_menu_keyboard(user_id))


async def send_profile(message: Message, user_id: int) -> None:
    user = get_user(user_id, create=False)
    if not user:
        await message.answer("Iltimos, /start buyrug'ini bosing.")
        return
    sub = get_active_subscription(user_id)
    if sub:
        start_date = parse_dt(sub["start_date"])
        end_date = parse_dt(sub["end_date"])
        remaining = (end_date - now()).days
        status = "ACTIVE"
        status_emoji = "✅"
    else:
        start_date = None
        end_date = None
        remaining = None
        status = "NO ACTIVE SUBSCRIPTION"
        status_emoji = "❌"
    text = (
        f"👤 PROFIL\n\n"
        f"🆔 ID: {user['telegram_id']}\n"
        f"👤 Username: @{user['username'] or 'mavjud emas'}\n\n"
        f"💎 Status: {status_emoji} {status}\n"
    )
    if sub:
        text += (
            f"📅 Boshlangan: {start_date.strftime('%d.%m.%Y')}\n"
            f"📅 Tugaydi: {end_date.strftime('%d.%m.%Y')}\n"
            f"⏳ Qolgan: {remaining} kun\n"
        )
    await message.answer(text, reply_markup=main_menu_keyboard(user_id))


# ---------- PAYMENT APPROVAL/REJECT ----------
async def send_payment_to_admins(bot: Bot, payment: Dict[str, Any]) -> None:
    """Send payment details to all admins for approval."""
    user = get_user(payment["user_id"], create=False)
    if not user:
        return
    tariff = get_tariff(payment["tariff_id"])
    tariff_name = tariff["name"] if tariff else "Noma'lum"
    amount = payment["amount"]
    created = parse_dt(payment["created_at"])
    text = (
        f"💳 YANGI TO'LOV\n\n"
        f"👤 Ism: {user['first_name']} {user['last_name'] or ''}\n"
        f"🔗 Username: @{user['username'] or 'mavjud emas'}\n"
        f"🆔 Telegram ID: {user['telegram_id']}\n\n"
        f"💎 Tarif: {tariff_name}\n"
        f"💰 Summa: {amount:,} so'm\n"
        f"🕐 Vaqt: {created.strftime('%d.%m.%Y %H:%M')}\n"
    )
    keyboard = InlineKeyboardBuilder()
    # Tariff buttons for approval: we allow admin to choose which tariff to grant (could be different)
    # But we will use the tariff from payment.
    keyboard.button(
        text=f"✅ {tariff_name}ni tasdiqlash",
        callback_data=f"approve_{payment['id']}"
    )
    keyboard.button(
        text="❌ RAD ETISH",
        callback_data=f"reject_{payment['id']}"
    )
    keyboard.adjust(1)

    # Send to all admins
    admin_ids = [SUPER_ADMIN_ID] + [a["telegram_id"] for a in load_data()["admins"]]
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                admin_id,
                text,
                reply_markup=keyboard.as_markup(),
            )
        except Exception:
            continue


# ---------- EXPIRATION WORKER ----------
async def expiration_worker(bot: Bot) -> None:
    """Check active subscriptions every 60 seconds and expire if needed."""
    while True:
        try:
            now_ts = now()
            data = load_data()
            expired_subs = []
            for sub in data["subscriptions"]:
                if sub["status"] != "ACTIVE":
                    continue
                end_date = parse_dt(sub["end_date"])
                if end_date <= now_ts:
                    expired_subs.append(sub)

            for sub in expired_subs:
                user_id = sub["user_id"]
                # Kick user from channel
                try:
                    await bot.ban_chat_member(CHANNEL_ID, user_id)
                    await bot.unban_chat_member(CHANNEL_ID, user_id)
                except Exception:
                    pass
                # Update subscription
                sub["status"] = "EXPIRED"
                sub["is_active"] = False
                sub["updated_at"] = format_dt(now_ts)
                # Notify user
                try:
                    await bot.send_message(
                        user_id,
                        "❌ VIP obunangiz tugadi.\n\n"
                        "Kanalga kirishni davom ettirish uchun yangi obuna sotib oling."
                    )
                except Exception:
                    pass
                # Add notification to log to avoid duplicate reminders? Not needed.
                # Save data after all modifications
            if expired_subs:
                save_data()
        except Exception:
            pass
        await asyncio.sleep(60)


async def reminder_worker(bot: Bot) -> None:
    """Check subscriptions and send reminders at 3 days, 1 day, 1 hour before expiry."""
    while True:
        try:
            now_ts = now()
            data = load_data()
            for sub in data["subscriptions"]:
                if sub["status"] != "ACTIVE":
                    continue
                end_date = parse_dt(sub["end_date"])
                time_left = end_date - now_ts
                user_id = sub["user_id"]
                # 3 days
                if timedelta(days=3) - timedelta(minutes=1) < time_left <= timedelta(days=3) + timedelta(minutes=1):
                    if not any(n["user_id"] == user_id and n["type"] == "3days" for n in data["notifications"]):
                        await bot.send_message(
                            user_id,
                            "⚠️ VIP obunangiz tugashiga 3 kun qoldi."
                        )
                        add_notification(user_id, "3days")
                # 1 day
                if timedelta(days=1) - timedelta(minutes=1) < time_left <= timedelta(days=1) + timedelta(minutes=1):
                    if not any(n["user_id"] == user_id and n["type"] == "1day" for n in data["notifications"]):
                        await bot.send_message(
                            user_id,
                            "⚠️ VIP obunangiz ertaga tugaydi."
                        )
                        add_notification(user_id, "1day")
                # 1 hour
                if timedelta(hours=1) - timedelta(minutes=1) < time_left <= timedelta(hours=1) + timedelta(minutes=1):
                    if not any(n["user_id"] == user_id and n["type"] == "1hour" for n in data["notifications"]):
                        await bot.send_message(
                            user_id,
                            "⚠️ VIP obunangiz tugashiga 1 soat qoldi."
                        )
                        add_notification(user_id, "1hour")
        except Exception:
            pass
        await asyncio.sleep(60)


# ---------- BOT HANDLERS ----------
router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    update_user_from_message(message)
    await send_main_menu(message, message.from_user.id)


@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_profile(message, message.from_user.id)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not is_admin(message.from_user.id):
        await message.answer("Siz admin emassiz.")
        return
    await message.answer(
        "👑 ADMIN PANEL\n\nQuyidagi amallardan birini tanlang:",
        reply_markup=admin_panel_keyboard()
    )


# ---------- TARIFF SELECTION ----------
@router.callback_query(F.data.startswith("tariff_"))
async def tariff_callback(callback: CallbackQuery) -> None:
    tariff_id = int(callback.data.split("_")[1])
    tariff = get_tariff(tariff_id)
    if not tariff:
        await callback.answer("Tarif topilmadi.", show_alert=True)
        return
    text = (
        f"💎 {tariff['name']}LIK VIP\n\n"
        f"💰 Narx: {tariff['price']:,} so'm\n\n"
        f"💳 Karta:\n{CARD_NUMBER}\n\n"
        f"👤 Karta egasi:\n{CARD_HOLDER}\n\n"
        f"⚠️ To'lovni amalga oshirgandan keyin\nchekni botga yuboring.\n\n"
        f"⏱ Chekni {PAYMENT_TIMEOUT_MINUTES} daqiqa ichida yuborish kerak."
    )
    await callback.message.edit_text(
        text,
        reply_markup=tariff_detail_keyboard(tariff_id)
    )
    await callback.answer()


@router.callback_query(F.data == "back_main")
async def back_main_callback(callback: CallbackQuery) -> None:
    await send_main_menu(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery) -> None:
    await send_profile(callback.message, callback.from_user.id)
    await callback.answer()


# ---------- PAYMENT CREATION ----------
@router.callback_query(F.data.startswith("pay_"))
async def pay_callback(callback: CallbackQuery) -> None:
    tariff_id = int(callback.data.split("_")[1])
    tariff = get_tariff(tariff_id)
    if not tariff:
        await callback.answer("Tarif topilmadi.", show_alert=True)
        return
    user_id = callback.from_user.id
    # Check if there is already a pending payment
    existing = find_pending_payment(user_id)
    if existing:
        # If still pending, inform user
        await callback.answer("Sizda kutilayotgan to'lov mavjud. Iltimos, uni yakunlang yoki kuting.", show_alert=True)
        return
    # Create payment
    payment = create_payment(user_id, tariff_id)
    # Notify user
    text = (
        "💳 To'lovni amalga oshiring.\n\n"
        "To'lovdan keyin chekni shu botga\n"
        "rasm sifatida yuboring.\n\n"
        f"⏱ Sizda {PAYMENT_TIMEOUT_MINUTES} daqiqa vaqt bor."
    )
    await callback.message.edit_text(text, reply_markup=None)
    await callback.answer("To'lov yaratildi. Iltimos, chekni yuboring.")

    # Notify admins
    await send_payment_to_admins(callback.bot, payment)


# ---------- RECEIPT HANDLING ----------
@router.message(F.photo)
async def handle_receipt(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    payment = find_pending_payment(user_id)
    if not payment:
        await message.answer("Sizda faol to'lov topilmadi. Iltimos, tarifni qaytadan tanlang.")
        return
    # Check expiry
    expires_at = parse_dt(payment["expires_at"])
    if now() > expires_at:
        payment["status"] = "EXPIRED"
        save_data()
        await message.answer("⏰ To'lov vaqti tugagan. Iltimos, tarifni qaytadan tanlang.")
        return
    # Store receipt
    file_id = message.photo[-1].file_id
    payment["receipt_file_id"] = file_id
    # We don't change status yet; still pending, but we store receipt
    save_data()
    await message.answer(
        "✅ Chekingiz qabul qilindi.\n\n"
        "⏳ Admin to'lovni tekshirmoqda..."
    )
    # Notify admins that receipt arrived (maybe send photo again)
    # But we already sent payment notification; we can optionally update.
    # However, we can send the photo to admins as well.
    admin_ids = [SUPER_ADMIN_ID] + [a["telegram_id"] for a in load_data()["admins"]]
    for admin_id in admin_ids:
        try:
            await message.bot.send_photo(
                admin_id,
                file_id,
                caption=f"🧾 Chek qabul qilindi.\nID: {payment['id']}"
            )
        except Exception:
            pass


# ---------- ADMIN APPROVE/REJECT ----------
@router.callback_query(F.data.startswith("approve_"))
async def approve_payment_callback(callback: CallbackQuery) -> None:
    admin_id = callback.from_user.id
    if not is_admin(admin_id):
        await callback.answer("Siz admin emassiz.", show_alert=True)
        return
    payment_id = callback.data.split("_")[1]
    payment = find_payment(payment_id)
    if not payment:
        await callback.answer("To'lov topilmadi.", show_alert=True)
        return
    if payment["status"] != "PENDING":
        await callback.answer("Bu to'lov allaqachon tasdiqlangan yoki rad etilgan.", show_alert=True)
        return
    expires_at = parse_dt(payment["expires_at"])
    if now() > expires_at:
        payment["status"] = "EXPIRED"
        save_data()
        await callback.answer("To'lov vaqti tugagan.", show_alert=True)
        return
    # Approve
    user_id = payment["user_id"]
    tariff = get_tariff(payment["tariff_id"])
    if not tariff:
        await callback.answer("Tarif topilmadi.", show_alert=True)
        return
    days = tariff["days"]
    # Determine subscription start
    existing_sub = get_active_subscription(user_id)
    if existing_sub:
        start_date = parse_dt(existing_sub["end_date"])
    else:
        start_date = now()
    end_date = start_date + timedelta(days=days)
    # Create subscription
    sub = create_subscription(user_id, payment_id, days, start_date, end_date)
    # Update payment
    payment["status"] = "APPROVED"
    payment["admin_id"] = admin_id
    payment["approved_at"] = format_dt(now())
    save_data()

    # Approve join request
    approve_join_request(user_id)
    # Also try to approve chat join request
    try:
        await callback.bot.approve_chat_join_request(CHANNEL_ID, user_id)
    except Exception:
        pass

    # Notify user
    try:
        await callback.bot.send_message(
            user_id,
            f"✅ Obunangiz tasdiqlandi!\n\n"
            f"💎 Tarif: {tariff['name']}\n"
            f"📅 Boshlanish: {start_date.strftime('%d.%m.%Y')}\n"
            f"📅 Tugash: {end_date.strftime('%d.%m.%Y')}\n\n"
            "Endi kanalga kira olasiz."
        )
    except Exception:
        pass

    # Log
    log_admin_action(admin_id, "approve_payment", {"payment_id": payment_id, "user_id": user_id})

    await callback.message.edit_text(
        f"✅ To'lov tasdiqlandi.\n\n"
        f"Foydalanuvchi: {user_id}\n"
        f"Tarif: {tariff['name']}"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reject_"))
async def reject_payment_callback(callback: CallbackQuery, state: FSMContext) -> None:
    admin_id = callback.from_user.id
    if not is_admin(admin_id):
        await callback.answer("Siz admin emassiz.", show_alert=True)
        return
    payment_id = callback.data.split("_")[1]
    payment = find_payment(payment_id)
    if not payment:
        await callback.answer("To'lov topilmadi.", show_alert=True)
        return
    if payment["status"] != "PENDING":
        await callback.answer("Bu to'lov allaqachon tasdiqlangan yoki rad etilgan.", show_alert=True)
        return
    # Ask for reason
    await state.set_state(AdminStates.REJECT_REASON)
    await state.update_data(payment_id=payment_id)
    await callback.message.edit_text("Rad etish sababini kiriting:")
    await callback.answer()


@router.message(AdminStates.REJECT_REASON, F.text)
async def reject_reason_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    payment_id = data.get("payment_id")
    if not payment_id:
        await state.clear()
        await message.answer("Xatolik yuz berdi. Qaytadan urinib ko'ring.")
        return
    payment = find_payment(payment_id)
    if not payment:
        await state.clear()
        await message.answer("To'lov topilmadi.")
        return
    reason = message.text
    payment["status"] = "REJECTED"
    payment["admin_id"] = message.from_user.id
    payment["rejection_reason"] = reason
    save_data()
    # Notify user
    user_id = payment["user_id"]
    try:
        await message.bot.send_message(
            user_id,
            f"❌ To'lov rad etildi.\n\n"
            f"Sabab: {reason}\n\n"
            f"Iltimos, qaytadan urinib ko'ring yoki admin bilan bog'laning."
        )
    except Exception:
        pass
    # Log
    log_admin_action(message.from_user.id, "reject_payment", {"payment_id": payment_id, "user_id": user_id})
    await state.clear()
    await message.answer(f"✅ To'lov rad etildi. Sabab: {reason}")
    await message.answer("Admin panel:", reply_markup=admin_panel_keyboard())


# ---------- ADMIN PANEL CALLBACKS ----------
@router.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "👑 ADMIN PANEL\n\nQuyidagi amallardan birini tanlang:",
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()


# Statistics
@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "📊 Statistika\n\nDavrni tanlang:",
        reply_markup=admin_stats_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_stats_"))
async def admin_stats_period(callback: CallbackQuery) -> None:
    period = callback.data.split("_")[2]  # 24h, 7d, 30d, overall
    data = load_data()
    now_ts = now()
    if period == "24h":
        start = now_ts - timedelta(days=1)
    elif period == "7d":
        start = now_ts - timedelta(days=7)
    elif period == "30d":
        start = now_ts - timedelta(days=30)
    else:  # overall
        start = None

    # Filter users, payments, subscriptions by date
    users = data["users"]
    payments = data["payments"]
    subscriptions = data["subscriptions"]

    def in_period(items, date_key):
        if start is None:
            return len(items)
        return sum(1 for i in items if parse_dt(i[date_key]) >= start)

    new_users = in_period(users, "joined_at")
    total_payments = len(payments)
    pending_payments = sum(1 for p in payments if p["status"] == "PENDING")
    approved_payments = sum(1 for p in payments if p["status"] == "APPROVED")
    rejected_payments = sum(1 for p in payments if p["status"] == "REJECTED")
    active_subs = sum(1 for s in subscriptions if s["status"] == "ACTIVE")
    expired_subs = sum(1 for s in subscriptions if s["status"] == "EXPIRED")
    cancelled_subs = sum(1 for s in subscriptions if s["status"] == "CANCELLED")
    total_revenue = sum(p["amount"] for p in payments if p["status"] == "APPROVED")

    text = (
        f"📊 Statistika ({period})\n\n"
        f"👥 Yangi foydalanuvchilar: {new_users}\n"
        f"💳 To'lovlar: {total_payments}\n"
        f"   - Pending: {pending_payments}\n"
        f"   - Tasdiqlangan: {approved_payments}\n"
        f"   - Rad etilgan: {rejected_payments}\n"
        f"💎 Aktiv obunalar: {active_subs}\n"
        f"   - Tugagan: {expired_subs}\n"
        f"   - Bekor qilingan: {cancelled_subs}\n"
        f"💰 Jami tushum: {total_revenue:,} so'm\n"
    )
    await callback.message.edit_text(text, reply_markup=admin_stats_keyboard())
    await callback.answer()


# Users list (just a placeholder, we can show count)
@router.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery) -> None:
    data = load_data()
    count = len(data["users"])
    await callback.message.edit_text(
        f"👥 Foydalanuvchilar\n\nJami: {count} ta foydalanuvchi.\n\n"
        "Batafsil ma'lumot uchun 'User qidirish' dan foydalaning.",
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()


# Payments list (show recent)
@router.callback_query(F.data == "admin_payments")
async def admin_payments(callback: CallbackQuery) -> None:
    data = load_data()
    payments = data["payments"][-10:][::-1]  # last 10
    if not payments:
        text = "💳 To'lovlar\n\nHozircha to'lovlar mavjud emas."
    else:
        lines = []
        for p in payments:
            user = get_user(p["user_id"], create=False)
            username = f"@{user['username']}" if user and user['username'] else str(p["user_id"])
            lines.append(f"{p['id']} | {username} | {p['status']} | {p['amount']:,}")
        text = "💳 So'nggi to'lovlar:\n\n" + "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())
    await callback.answer()


# Subscriptions list
@router.callback_query(F.data == "admin_subscriptions")
async def admin_subscriptions(callback: CallbackQuery) -> None:
    data = load_data()
    subs = data["subscriptions"][-10:][::-1]
    if not subs:
        text = "💎 Obunalar\n\nHozircha obunalar mavjud emas."
    else:
        lines = []
        for s in subs:
            user = get_user(s["user_id"], create=False)
            username = f"@{user['username']}" if user and user['username'] else str(s["user_id"])
            lines.append(f"{s['id']} | {username} | {s['status']}")
        text = "💎 So'nggi obunalar:\n\n" + "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())
    await callback.answer()


# User search
@router.callback_query(F.data == "admin_search")
async def admin_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.USER_SEARCH)
    await callback.message.edit_text("🔎 Foydalanuvchi Telegram ID sini kiriting:")
    await callback.answer()


@router.message(AdminStates.USER_SEARCH, F.text)
async def user_search_handler(message: Message, state: FSMContext) -> None:
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("Iltimos, faqat son (ID) kiriting.")
        return
    user = get_user(user_id, create=False)
    if not user:
        await message.answer("Bunday ID li foydalanuvchi topilmadi.")
        await state.clear()
        return
    # Show user info
    sub = get_active_subscription(user_id)
    text = (
        f"👤 USER\n\n"
        f"ID: {user['telegram_id']}\n"
        f"Username: @{user['username'] or 'mavjud emas'}\n"
        f"Ism: {user['first_name']} {user['last_name'] or ''}\n"
        f"Qo'shilgan: {parse_dt(user['joined_at']).strftime('%d.%m.%Y')}\n"
    )
    if sub:
        start = parse_dt(sub["start_date"])
        end = parse_dt(sub["end_date"])
        remaining = (end - now()).days
        text += (
            f"\n💎 Status: ACTIVE\n"
            f"📅 Start: {start.strftime('%d.%m.%Y')}\n"
            f"📅 End: {end.strftime('%d.%m.%Y')}\n"
            f"⏳ Qolgan: {remaining} kun\n"
        )
    else:
        text += "\n💎 Status: INACTIVE\n"

    await state.clear()
    await message.answer(
        text,
        reply_markup=user_detail_keyboard(user_id)
    )


# Manual subscription
@router.callback_query(F.data.startswith("admin_manual_sub"))
async def admin_manual_sub(callback: CallbackQuery, state: FSMContext) -> None:
    # If callback includes user_id, we can pre-fill
    parts = callback.data.split("_")
    if len(parts) == 4:
        user_id = int(parts[3])
        await state.update_data(manual_user_id=user_id)
        # Ask tariff
        await state.set_state(AdminStates.MANUAL_SUB_TARIFF)
        # Show tariff selection
        keyboard = InlineKeyboardBuilder()
        tariffs = load_data()["tariffs"]
        for t in tariffs:
            if t["is_active"]:
                keyboard.button(text=f"{t['name']} ({t['price']:,} so'm)", callback_data=f"manual_tariff_{t['id']}")
        keyboard.button(text="🔙 BEKOR", callback_data="manual_cancel")
        keyboard.adjust(1)
        await callback.message.edit_text(
            "➕ Obuna berish\n\nFoydalanuvchi uchun tarif tanlang:",
            reply_markup=keyboard.as_markup()
        )
    else:
        await state.set_state(AdminStates.MANUAL_SUB_USER)
        await callback.message.edit_text("Foydalanuvchi Telegram ID sini kiriting:")
    await callback.answer()


@router.message(AdminStates.MANUAL_SUB_USER, F.text)
async def manual_sub_user_handler(message: Message, state: FSMContext) -> None:
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("Iltimos, faqat son (ID) kiriting.")
        return
    user = get_user(user_id, create=False)
    if not user:
        await message.answer("Bunday ID li foydalanuvchi topilmadi. Iltimos, qaytadan kiriting.")
        return
    await state.update_data(manual_user_id=user_id)
    await state.set_state(AdminStates.MANUAL_SUB_TARIFF)
    # Show tariff selection
    keyboard = InlineKeyboardBuilder()
    tariffs = load_data()["tariffs"]
    for t in tariffs:
        if t["is_active"]:
            keyboard.button(text=f"{t['name']} ({t['price']:,} so'm)", callback_data=f"manual_tariff_{t['id']}")
    keyboard.button(text="🔙 BEKOR", callback_data="manual_cancel")
    keyboard.adjust(1)
    await message.answer(
        "Tarifni tanlang:",
        reply_markup=keyboard.as_markup()
    )


@router.callback_query(F.data.startswith("manual_tariff_"))
async def manual_tariff_callback(callback: CallbackQuery, state: FSMContext) -> None:
    tariff_id = int(callback.data.split("_")[2])
    tariff = get_tariff(tariff_id)
    if not tariff:
        await callback.answer("Tarif topilmadi.", show_alert=True)
        return
    data = await state.get_data()
    user_id = data.get("manual_user_id")
    if not user_id:
        await callback.message.edit_text("Xatolik yuz berdi. Qaytadan urinib ko'ring.")
        await state.clear()
        return
    # Check existing active subscription to extend
    existing = get_active_subscription(user_id)
    if existing:
        start_date = parse_dt(existing["end_date"])
    else:
        start_date = now()
    end_date = start_date + timedelta(days=tariff["days"])
    # Create subscription without payment
    sub = create_subscription(user_id, f"MANUAL-{generate_id('SUB')}", tariff["days"], start_date, end_date)
    # Also approve join request
    approve_join_request(user_id)
    try:
        await callback.bot.approve_chat_join_request(CHANNEL_ID, user_id)
    except Exception:
        pass
    # Notify user
    try:
        await callback.bot.send_message(
            user_id,
            f"✅ Admin tomonidan obuna berildi!\n\n"
            f"💎 Tarif: {tariff['name']}\n"
            f"📅 Boshlanish: {start_date.strftime('%d.%m.%Y')}\n"
            f"📅 Tugash: {end_date.strftime('%d.%m.%Y')}\n\n"
            "Endi kanalga kira olasiz."
        )
    except Exception:
        pass
    log_admin_action(callback.from_user.id, "manual_sub", {"user_id": user_id, "tariff_id": tariff_id})
    await callback.message.edit_text(
        f"✅ Obuna muvaffaqiyatli berildi.\n\n"
        f"Foydalanuvchi: {user_id}\n"
        f"Tarif: {tariff['name']}\n"
        f"Tugash: {end_date.strftime('%d.%m.%Y')}"
    )
    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "manual_cancel")
async def manual_cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Obuna berish bekor qilindi.", reply_markup=admin_panel_keyboard())
    await callback.answer()


# Cancel subscription
@router.callback_query(F.data.startswith("admin_cancel_sub"))
async def admin_cancel_sub(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split("_")
    if len(parts) == 4:
        user_id = int(parts[3])
        # Check if user has active subscription
        sub = get_active_subscription(user_id)
        if not sub:
            await callback.answer("Bu foydalanuvchining faol obunasi yo'q.", show_alert=True)
            return
        await state.update_data(cancel_user_id=user_id)
        await state.set_state(AdminStates.CANCEL_SUB_CONFIRM)
        await callback.message.edit_text(
            f"⚠️ Ushbu foydalanuvchining obunasini bekor qilaymi?\n\nID: {user_id}",
            reply_markup=yes_no_keyboard("cancel_confirm")
        )
    else:
        await state.set_state(AdminStates.CANCEL_SUB_USER)
        await callback.message.edit_text("Foydalanuvchi Telegram ID sini kiriting:")
    await callback.answer()


@router.message(AdminStates.CANCEL_SUB_USER, F.text)
async def cancel_sub_user_handler(message: Message, state: FSMContext) -> None:
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("Iltimos, faqat son (ID) kiriting.")
        return
    user = get_user(user_id, create=False)
    if not user:
        await message.answer("Bunday ID li foydalanuvchi topilmadi.")
        return
    sub = get_active_subscription(user_id)
    if not sub:
        await message.answer("Bu foydalanuvchining faol obunasi yo'q.")
        await state.clear()
        return
    await state.update_data(cancel_user_id=user_id)
    await state.set_state(AdminStates.CANCEL_SUB_CONFIRM)
    await message.answer(
        f"⚠️ Ushbu foydalanuvchining obunasini bekor qilaymi?\n\nID: {user_id}",
        reply_markup=yes_no_keyboard("cancel_confirm")
    )


@router.callback_query(F.data.startswith("cancel_confirm_"))
async def cancel_confirm_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = data.get("cancel_user_id")
    if not user_id:
        await callback.answer("Xatolik yuz berdi.")
        return
    choice = callback.data.split("_")[2]
    if choice == "yes":
        sub = cancel_subscription(user_id)
        if sub:
            # Kick user from channel
            try:
                await callback.bot.ban_chat_member(CHANNEL_ID, user_id)
                await callback.bot.unban_chat_member(CHANNEL_ID, user_id)
            except Exception:
                pass
            # Notify user
            try:
                await callback.bot.send_message(
                    user_id,
                    "❌ Obunangiz admin tomonidan bekor qilindi.\n\n"
                    "Qayta obuna bo'lish uchun /start bosing."
                )
            except Exception:
                pass
            log_admin_action(callback.from_user.id, "cancel_sub", {"user_id": user_id})
            await callback.message.edit_text(f"✅ Obuna bekor qilindi.\n\nFoydalanuvchi: {user_id}")
        else:
            await callback.message.edit_text("Obuna topilmadi.")
    else:
        await callback.message.edit_text("Bekor qilish bekor qilindi.")
    await state.clear()
    await callback.answer()


# Admin management
@router.callback_query(F.data == "admin_admins")
async def admin_admins(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "👮 Adminlar\n\n",
        reply_markup=admin_management_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "admin_add_admin")
async def admin_add_admin(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_super_admin(callback.from_user.id):
        await callback.answer("Faqat super admin qo'sha oladi.", show_alert=True)
        return
    await state.set_state(AdminStates.ADD_ADMIN)
    await callback.message.edit_text("Qo'shiladigan adminning Telegram ID sini kiriting:")
    await callback.answer()


@router.message(AdminStates.ADD_ADMIN, F.text)
async def add_admin_handler(message: Message, state: FSMContext) -> None:
    try:
        admin_id = int(message.text.strip())
    except ValueError:
        await message.answer("Iltimos, faqat son (ID) kiriting.")
        return
    if is_admin(admin_id):
        await message.answer("Bu foydalanuvchi allaqachon admin.")
        await state.clear()
        return
    data = load_data()
    data["admins"].append({"telegram_id": admin_id, "is_super_admin": False, "created_at": format_dt(now())})
    save_data()
    log_admin_action(message.from_user.id, "add_admin", {"admin_id": admin_id})
    await message.answer(f"✅ Admin qo'shildi: {admin_id}")
    await state.clear()


@router.callback_query(F.data == "admin_remove_admin")
async def admin_remove_admin(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_super_admin(callback.from_user.id):
        await callback.answer("Faqat super admin o'chira oladi.", show_alert=True)
        return
    await state.set_state(AdminStates.REMOVE_ADMIN)
    await callback.message.edit_text("O'chiriladigan adminning Telegram ID sini kiriting:")
    await callback.answer()


@router.message(AdminStates.REMOVE_ADMIN, F.text)
async def remove_admin_handler(message: Message, state: FSMContext) -> None:
    try:
        admin_id = int(message.text.strip())
    except ValueError:
        await message.answer("Iltimos, faqat son (ID) kiriting.")
        return
    if admin_id == SUPER_ADMIN_ID:
        await message.answer("Super adminni o'chirib bo'lmaydi.")
        await state.clear()
        return
    data = load_data()
    for idx, a in enumerate(data["admins"]):
        if a["telegram_id"] == admin_id:
            del data["admins"][idx]
            save_data()
            log_admin_action(message.from_user.id, "remove_admin", {"admin_id": admin_id})
            await message.answer(f"✅ Admin o'chirildi: {admin_id}")
            await state.clear()
            return
    await message.answer("Bunday admin topilmadi.")
    await state.clear()


@router.callback_query(F.data == "admin_list_admins")
async def admin_list_admins(callback: CallbackQuery) -> None:
    data = load_data()
    admins = [SUPER_ADMIN_ID] + [a["telegram_id"] for a in data["admins"]]
    text = "📋 Adminlar ro'yxati:\n\n" + "\n".join(f"- {a}" for a in admins)
    await callback.message.edit_text(text, reply_markup=admin_management_keyboard())
    await callback.answer()


# Logs
@router.callback_query(F.data == "admin_logs")
async def admin_logs(callback: CallbackQuery) -> None:
    data = load_data()
    logs = data["admin_logs"][-20:][::-1]
    if not logs:
        text = "📋 Loglar\n\nHozircha loglar mavjud emas."
    else:
        lines = []
        for l in logs:
            lines.append(f"{l['created_at'][:16]} | Admin {l['admin_id']} | {l['action']}")
        text = "📋 So'nggi loglar:\n\n" + "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())
    await callback.answer()


# Kick user from channel
@router.callback_query(F.data.startswith("admin_kick_"))
async def admin_kick_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Siz admin emassiz.", show_alert=True)
        return
    user_id = int(callback.data.split("_")[2])
    try:
        await callback.bot.ban_chat_member(CHANNEL_ID, user_id)
        await callback.bot.unban_chat_member(CHANNEL_ID, user_id)
        await callback.answer("Foydalanuvchi kanaldan chiqarildi.")
    except Exception as e:
        await callback.answer(f"Xatolik: {str(e)}", show_alert=True)


# Subscription history and payment history (placeholder)
@router.callback_query(F.data.startswith("admin_sub_history_"))
async def admin_sub_history(callback: CallbackQuery) -> None:
    user_id = int(callback.data.split("_")[3])
    data = load_data()
    subs = [s for s in data["subscriptions"] if s["user_id"] == user_id]
    if not subs:
        text = "📜 Obuna tarixi:\n\nHech qanday obuna topilmadi."
    else:
        lines = []
        for s in subs:
            lines.append(f"{s['id']} | {s['status']} | {parse_dt(s['start_date']).strftime('%d.%m.%Y')} -> {parse_dt(s['end_date']).strftime('%d.%m.%Y')}")
        text = "📜 Obuna tarixi:\n\n" + "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=user_detail_keyboard(user_id))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_pay_history_"))
async def admin_pay_history(callback: CallbackQuery) -> None:
    user_id = int(callback.data.split("_")[3])
    data = load_data()
    payments = [p for p in data["payments"] if p["user_id"] == user_id]
    if not payments:
        text = "💳 To'lovlar tarixi:\n\nHech qanday to'lov topilmadi."
    else:
        lines = []
        for p in payments:
            lines.append(f"{p['id']} | {p['status']} | {p['amount']:,} so'm")
        text = "💳 To'lovlar tarixi:\n\n" + "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=user_detail_keyboard(user_id))
    await callback.answer()


# ---------- BROADCAST ----------
@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Siz admin emassiz.", show_alert=True)
        return
    await state.set_state(AdminStates.BROADCAST)
    await callback.message.edit_text(
        "📢 Broadcast\n\n"
        "Xabar matnini yoki rasm (caption bilan) yuboring.\n"
        "Agar rasm yuborsangiz, caption matn sifatida ishlatiladi."
    )
    await callback.answer()


@router.message(AdminStates.BROADCAST, F.text)
async def broadcast_text(message: Message, state: FSMContext) -> None:
    await state.update_data(broadcast_text=message.text, broadcast_photo=None)
    await confirm_broadcast(message, state)


@router.message(AdminStates.BROADCAST, F.photo)
async def broadcast_photo(message: Message, state: FSMContext) -> None:
    file_id = message.photo[-1].file_id
    caption = message.caption or ""
    await state.update_data(broadcast_photo=file_id, broadcast_text=caption)
    await confirm_broadcast(message, state)


async def confirm_broadcast(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    photo = data.get("broadcast_photo")
    users = load_data()["users"]
    count = len(users)
    await state.set_state(AdminStates.BROADCAST_CONFIRM)
    preview = f"📢 BROADCAST\n\n👥 Qabul qiluvchilar: {count}\n\nXabar:\n{text[:200]}{'...' if len(text)>200 else ''}"
    if photo:
        await message.answer_photo(photo, caption=preview, reply_markup=broadcast_confirm_keyboard())
    else:
        await message.answer(preview, reply_markup=broadcast_confirm_keyboard())


@router.callback_query(F.data == "broadcast_send", StateFilter(AdminStates.BROADCAST_CONFIRM))
async def broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    photo = data.get("broadcast_photo")
    users = load_data()["users"]
    count = 0
    for user in users:
        try:
            if photo:
                await callback.bot.send_photo(user["telegram_id"], photo, caption=text)
            else:
                await callback.bot.send_message(user["telegram_id"], text)
            count += 1
        except Exception:
            # Mark as blocked
            user["is_blocked"] = True
            save_data()
    log_admin_action(callback.from_user.id, "broadcast", {"recipients": count})
    await state.clear()
    await callback.message.edit_text(f"✅ Broadcast yuborildi. {count} ta foydalanuvchiga yetkazildi.")
    await callback.answer()


@router.callback_query(F.data == "broadcast_cancel", StateFilter(AdminStates.BROADCAST_CONFIRM))
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Broadcast bekor qilindi.", reply_markup=admin_panel_keyboard())
    await callback.answer()


# ---------- JOIN REQUEST HANDLER ----------
@router.chat_join_request()
async def chat_join_request_handler(update: types.ChatJoinRequest) -> None:
    user_id = update.from_user.id
    # Create join request if not exists
    jr = get_join_request(user_id)
    if not jr:
        create_join_request(user_id)
    # If user has active subscription, auto approve
    sub = get_active_subscription(user_id)
    if sub:
        try:
            await update.bot.approve_chat_join_request(CHANNEL_ID, user_id)
            approve_join_request(user_id)
        except Exception:
            pass


# ---------- ERROR HANDLER ----------
@router.errors()
async def global_error_handler(event: Any, exception: Exception) -> None:
    # Ignore specific exceptions, just log
    if isinstance(exception, (TelegramBadRequest, TelegramForbiddenError,
                              TelegramRetryAfter, TelegramNetworkError)):
        # Log but don't crash
        print(f"Telegram error: {exception}")
    else:
        print(f"Unexpected error: {exception}")
    # Return True to indicate handled


# ---------- MAIN ----------
async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Start background workers
    asyncio.create_task(expiration_worker(bot))
    asyncio.create_task(reminder_worker(bot))

    # Start polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped.")