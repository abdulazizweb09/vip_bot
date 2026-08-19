import os
import sys
import json
import uuid
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatJoinRequest,
    ErrorEvent,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramNetworkError,
    TelegramAPIError,
)

# --- LOGGING SOZLAMASI ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("VIPAnimeBot")

# --- CONFIG (.env) ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "0").strip()
DEFAULT_CHANNEL_ID = int(CHANNEL_ID_RAW) if CHANNEL_ID_RAW.lstrip("-").isdigit() else 0

SUPER_ADMIN_ID_RAW = os.getenv("SUPER_ADMIN_ID", "0").strip()
SUPER_ADMIN_ID = int(SUPER_ADMIN_ID_RAW) if SUPER_ADMIN_ID_RAW.isdigit() else 0

CARD_NUMBER = os.getenv("CARD_NUMBER", "8600123456789012").strip()
CARD_HOLDER = os.getenv("CARD_HOLDER", "YOUR NAME").strip()
PAYMENT_TIMEOUT_MINUTES = int(os.getenv("PAYMENT_TIMEOUT_MINUTES", "30").strip())
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tashkent").strip()

if not BOT_TOKEN:
    logger.error("BOT_TOKEN topilmadi! .env faylni tekshiring.")
    sys.exit(1)

# --- DATABASE VA ATOMIC SAVE SOZLAMALARI ---
DATA_FILE = "data.json"
TMP_DATA_FILE = "data.json.tmp"
data_lock = asyncio.Lock()

DEFAULT_DATA: Dict[str, Any] = {
    "settings": {
        "channel_id": DEFAULT_CHANNEL_ID,
        "invite_link": None
    },
    "users": [],
    "payments": [],
    "subscriptions": [],
    "admins": [],
    "join_requests": [],
    "notifications": [],
    "admin_logs": [],
    "tariffs": [
        {"id": 1, "name": "1 minut (Test)", "duration": "1m", "price": 1000, "is_active": True},
        {"id": 2, "name": "15 kun", "duration": "15d", "price": 25000, "is_active": True},
        {"id": 3, "name": "30 kun", "duration": "30d", "price": 50000, "is_active": True},
        {"id": 4, "name": "60 kun", "duration": "60d", "price": 90000, "is_active": True},
        {"id": 5, "name": "90 kun", "duration": "90d", "price": 120000, "is_active": True},
    ]
}


def get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Tashkent")


def now_dt() -> datetime:
    return datetime.now(get_tz())


def now_iso() -> str:
    return now_dt().isoformat()


def parse_iso(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str)


def format_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


def format_date(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")


def parse_duration(val: Any) -> Tuple[timedelta, str]:
    s = str(val).strip().lower()
    if s.endswith("m") or "min" in s:
        digits = "".join(c for c in s if c.isdigit())
        num = int(digits) if digits else 1
        return timedelta(minutes=num), f"{num} minut"
    elif s.endswith("d") or "kun" in s:
        digits = "".join(c for c in s if c.isdigit())
        num = int(digits) if digits else 1
        return timedelta(days=num), f"{num} kun"
    else:
        try:
            num = int(s)
            return timedelta(days=num), f"{num} kun"
        except ValueError:
            return timedelta(days=30), "30 kun"


def format_time_left(end_dt: datetime, start_from: Optional[datetime] = None) -> str:
    now = start_from or now_dt()
    remaining = end_dt - now
    if remaining.total_seconds() <= 0:
        return "Muddati tugagan"
    
    days = remaining.days
    hours = remaining.seconds // 3600
    minutes = (remaining.seconds % 3600) // 60
    seconds = remaining.seconds % 60
    
    parts = []
    if days > 0:
        parts.append(f"{days} kun")
    if hours > 0:
        parts.append(f"{hours} soat")
    if minutes > 0:
        parts.append(f"{minutes} daqiqa")
    if not parts or (days == 0 and hours == 0):
        parts.append(f"{seconds} soniya")
        
    return " ".join(parts[:2])


def load_data_sync() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_DATA, f, ensure_ascii=False, indent=2)
        return json.loads(json.dumps(DEFAULT_DATA))
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for key, val in DEFAULT_DATA.items():
                if key not in data:
                    data[key] = val
            if "settings" not in data:
                data["settings"] = DEFAULT_DATA["settings"]
            return data
    except Exception as e:
        logger.error(f"JSON yuklashda xatolik: {e}")
        return json.loads(json.dumps(DEFAULT_DATA))


def save_data_sync(data: Dict[str, Any]) -> None:
    try:
        with open(TMP_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(TMP_DATA_FILE, DATA_FILE)
    except Exception as e:
        logger.error(f"JSON saqlashda xatolik: {e}")


# --- KANAL VA LINKLAR BILAN ISHLASH ---
def get_channel_id(db: Dict[str, Any]) -> int:
    settings = db.get("settings", {})
    chid = settings.get("channel_id")
    if chid is not None and chid != 0:
        return int(chid)
    return DEFAULT_CHANNEL_ID


async def get_or_create_channel_invite_link(bot_instance: Bot, db: Dict[str, Any]) -> Optional[str]:
    channel_id = get_channel_id(db)
    if channel_id == 0:
        return None
    settings = db.setdefault("settings", {})
    existing_link = settings.get("invite_link")
    if existing_link:
        return existing_link
    try:
        invite = await bot_instance.create_chat_invite_link(
            chat_id=channel_id,
            name="VIP Auto Join Request",
            creates_join_request=True
        )
        settings["invite_link"] = invite.invite_link
        save_data_sync(db)
        return invite.invite_link
    except Exception as e:
        logger.error(f"Kanal uchun join request link yaratishda xatolik ({channel_id}): {e}")
        return None


# --- YORDAMCHI FUNKSIYALAR ---
def is_admin(user_id: int, db: Dict[str, Any]) -> bool:
    if user_id == SUPER_ADMIN_ID and SUPER_ADMIN_ID != 0:
        return True
    return any(a.get("telegram_id") == user_id for a in db.get("admins", []))


def is_super_admin(user_id: int) -> bool:
    return user_id == SUPER_ADMIN_ID and SUPER_ADMIN_ID != 0


def get_all_admin_ids(db: Dict[str, Any]) -> List[int]:
    admin_ids = set()
    if SUPER_ADMIN_ID != 0:
        admin_ids.add(SUPER_ADMIN_ID)
    for a in db.get("admins", []):
        tid = a.get("telegram_id")
        if tid:
            admin_ids.add(tid)
    return list(admin_ids)


def register_or_update_user(user: types.User, db: Dict[str, Any]) -> Dict[str, Any]:
    existing_user = next((u for u in db["users"] if u.get("telegram_id") == user.id), None)
    if existing_user:
        existing_user["username"] = user.username or ""
        existing_user["first_name"] = user.first_name or ""
        existing_user["last_name"] = user.last_name or ""
        existing_user["is_blocked"] = False
        return existing_user
    else:
        new_user = {
            "id": len(db["users"]) + 1,
            "telegram_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "joined_at": now_iso(),
            "is_blocked": False
        }
        db["users"].append(new_user)
        return new_user


def get_user_active_subscription(user_id: int, db: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    now = now_dt()
    for sub in db.get("subscriptions", []):
        if sub.get("user_id") == user_id and sub.get("status") == "ACTIVE" and sub.get("is_active", False):
            end_dt = parse_iso(sub["end_date"])
            if end_dt > now:
                return sub
    return None


# --- KEYBOARDS ---
def get_main_keyboard(tariffs: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for tariff in tariffs:
        if tariff.get("is_active", True):
            row.append(InlineKeyboardButton(
                text=f"💎 {tariff['name'].upper()}",
                callback_data=f"tariff_{tariff['id']}"
            ))
            if len(row) == 2:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="👤 PROFILIM", callback_data="user_profile")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_pay_approval_keyboard(pay_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚡ 1 MINUT", callback_data=f"adm_app_{pay_id}_1m"),
            InlineKeyboardButton(text="15 KUN", callback_data=f"adm_app_{pay_id}_15d"),
        ],
        [
            InlineKeyboardButton(text="30 KUN", callback_data=f"adm_app_{pay_id}_30d"),
            InlineKeyboardButton(text="60 KUN", callback_data=f"adm_app_{pay_id}_60d"),
        ],
        [
            InlineKeyboardButton(text="90 KUN", callback_data=f"adm_app_{pay_id}_90d"),
            InlineKeyboardButton(text="❌ RAD ETISH", callback_data=f"adm_rej_{pay_id}"),
        ]
    ])


def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Statistika", callback_data="adm_nav_stats"),
            InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="adm_nav_users")
        ],
        [
            InlineKeyboardButton(text="💳 To'lovlar", callback_data="adm_nav_payments"),
            InlineKeyboardButton(text="💎 Obunalar", callback_data="adm_nav_subs")
        ],
        [
            InlineKeyboardButton(text="📢 Kanal sozlamalari", callback_data="adm_nav_channel")
        ],
        [
            InlineKeyboardButton(text="🔎 User qidirish", callback_data="adm_nav_search")
        ],
        [
            InlineKeyboardButton(text="➕ Obuna berish", callback_data="adm_nav_givesub"),
            InlineKeyboardButton(text="➖ Obunani bekor qilish", callback_data="adm_nav_cancelsub")
        ],
        [
            InlineKeyboardButton(text="📢 Broadcast", callback_data="adm_nav_broadcast")
        ],
        [
            InlineKeyboardButton(text="👮 Adminlar", callback_data="adm_nav_admins"),
            InlineKeyboardButton(text="📋 Loglar", callback_data="adm_nav_logs")
        ],
        [
            InlineKeyboardButton(text="❌ Yopish", callback_data="adm_nav_close")
        ]
    ])


def get_manual_sub_duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚡ 1 MINUT", callback_data="man_sub_1m"),
            InlineKeyboardButton(text="15 KUN", callback_data="man_sub_15d"),
        ],
        [
            InlineKeyboardButton(text="30 KUN", callback_data="man_sub_30d"),
            InlineKeyboardButton(text="60 KUN", callback_data="man_sub_60d"),
        ],
        [
            InlineKeyboardButton(text="90 KUN", callback_data="man_sub_90d"),
            InlineKeyboardButton(text="◀️ BEKOR QILISH", callback_data="admin_panel_back"),
        ]
    ])


# --- FSM HOLATLARI ---
class RejectReasonState(StatesGroup):
    waiting_for_reason = State()


class UserSearchState(StatesGroup):
    waiting_for_user_id = State()


class ManualSubState(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_days = State()


class CancelSubState(StatesGroup):
    waiting_for_user_id = State()
    confirm_cancel = State()


class AddAdminState(StatesGroup):
    waiting_for_admin_id = State()


class RemoveAdminState(StatesGroup):
    waiting_for_admin_id = State()


class BroadcastState(StatesGroup):
    waiting_for_content = State()
    confirm_broadcast = State()


class ChannelSettingState(StatesGroup):
    waiting_for_channel_id = State()


# --- BOT VA ROUTER INIZIALIZATSIYASI ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# --- USER HANDLERS ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    async with data_lock:
        db = load_data_sync()
        register_or_update_user(message.from_user, db)
        save_data_sync(db)
        sub = get_user_active_subscription(user_id, db)
        tariffs = db.get("tariffs", [])
        has_pending_req = any(
            jr.get("user_id") == user_id and jr.get("status") == "PENDING"
            for jr in db.get("join_requests", [])
        )

    # 1. Foydalanuvchida faol obuna mavjud bo'lsa
    if sub:
        end_dt = parse_iso(sub["end_date"])
        time_left_str = format_time_left(end_dt)
        invite_link = await get_or_create_channel_invite_link(bot, db)
        text = (
            "🎉 <b>Sizda faol VIP obuna mavjud!</b>\n\n"
            f"📅 Tugash vaqti: {format_dt(end_dt)}\n"
            f"⏳ Qolgan vaqt: <b>{time_left_str}</b>\n\n"
            "Kanalga kirish yoki profilingizni ko'rish:"
        )
        buttons = []
        if invite_link:
            buttons.append([InlineKeyboardButton(text="🍿 VIP KANALGA KIRISH", url=invite_link)])
        buttons.append([InlineKeyboardButton(text="👤 PROFILIM", callback_data="user_profile")])
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        return

    # 2. Foydalanuvchi allaqachon kanalga qo'shilish so'rovi yuborgan bo'lsa
    if has_pending_req:
        text = (
            "🎬 <b>VIP ANIME KANAL</b>\n\n"
            "✅ Sizning kanalga qo'shilish so'rovingiz qabul qilingan!\n\n"
            "🔐 VIP kanalga kirish huquqini faollashtirish uchun quyidagi tariflardan birini tanlang:"
        )
        await message.answer(text, reply_markup=get_main_keyboard(tariffs), parse_mode="HTML")
        return

    # 3. Foydalanuvchi hali so'rov yubormagan bo'lsa: unga shaxsiy join-request link beriladi
    invite_link = await get_or_create_channel_invite_link(bot, db)
    if invite_link:
        text = (
            "🎬 <b>VIP ANIME KANAL</b>\n\n"
            "🔐 Yopiq VIP Anime kanalimizga xush kelibsiz!\n\n"
            "1️⃣ <b>1-Qadam:</b> Quyidagi tugma orqali kanalga <b>qo'shilish so'rovi</b> yuboring.\n"
            "2️⃣ <b>2-Qadam:</b> So'rov yuborganingizdan so'ng bot sizga tariflarni chiqaradi.\n"
            "3️⃣ <b>3-Qadam:</b> To'lov qilganingizdan keyin bot sizni avtomatik ravishda kanalga qabul qiladi! 🍿\n\n"
            "👇 Boshlash uchun kanalga so'rov yuboring:"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 KANALGA SO'ROV YUBORISH", url=invite_link)],
            [InlineKeyboardButton(text="👤 PROFILIM", callback_data="user_profile")]
        ])
    else:
        text = (
            "🎬 <b>VIP ANIME KANAL</b>\n\n"
            "⚠️ Kanal sozlamalari administrator tomonidan o'rnatilmagan.\n"
            "Iltimos, keyinroq qayta urinib ko'ring yoki administrator bilan bog'laning."
        )
        keyboard = None

    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with data_lock:
        db = load_data_sync()
        tariffs = db.get("tariffs", [])

    text = (
        "🎬 <b>VIP ANIME KANAL</b>\n\n"
        "🔐 Yopiq VIP Anime kanaliga xush kelibsiz!\n\n"
        "💎 VIP obuna orqali:\n"
        "• Eksklyuziv anime\n"
        "• Yangi qismlar\n"
        "• Yuqori sifat\n\n"
        "Tarifni tanlang:"
    )
    await callback.message.edit_text(text, reply_markup=get_main_keyboard(tariffs), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "user_profile")
async def cb_user_profile(callback: CallbackQuery):
    async with data_lock:
        db = load_data_sync()
        user_id = callback.from_user.id
        sub = get_user_active_subscription(user_id, db)

    if sub:
        start_dt = parse_iso(sub["start_date"])
        end_dt = parse_iso(sub["end_date"])
        time_left_str = format_time_left(end_dt)

        text = (
            "👤 <b>PROFIL</b>\n\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"👤 Username: @{callback.from_user.username or 'Mavjud emas'}\n\n"
            "💎 Status: <b>ACTIVE</b>\n\n"
            f"📅 Boshlangan:\n{format_dt(start_dt)}\n\n"
            f"📅 Tugaydi:\n{format_dt(end_dt)}\n\n"
            f"⏳ Qolgan: <b>{time_left_str}</b>"
        )
    else:
        text = (
            "👤 <b>PROFIL</b>\n\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"👤 Username: @{callback.from_user.username or 'Mavjud emas'}\n\n"
            "💎 Status: <b>NO ACTIVE SUBSCRIPTION</b>\n\n"
            "Sizda hozirda faol VIP obuna mavjud emas."
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ ORQAGA", callback_data="main_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("tariff_"))
async def cb_tariff_selected(callback: CallbackQuery):
    tariff_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    now = now_dt()
    expires_at = (now + timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)).isoformat()

    async with data_lock:
        db = load_data_sync()
        tariff = next((t for t in db.get("tariffs", []) if t["id"] == tariff_id), None)
        if not tariff:
            await callback.answer("Tarif topilmadi!", show_alert=True)
            return

        pay_id = f"PAY-{uuid.uuid4().hex[:8].upper()}"
        payment = {
            "id": pay_id,
            "user_id": user_id,
            "tariff_id": tariff_id,
            "amount": tariff["price"],
            "status": "PENDING",
            "receipt_file_id": None,
            "rejection_reason": None,
            "admin_id": None,
            "created_at": now_iso(),
            "expires_at": expires_at,
            "approved_at": None
        }
        db["payments"].append(payment)
        save_data_sync(db)

    text = (
        f"💎 <b>{tariff['name'].upper()} VIP</b>\n\n"
        f"💰 Narx: <b>{tariff['price']:,} so'm</b>\n\n"
        f"💳 Karta:\n<code>{CARD_NUMBER}</code>\n\n"
        f"👤 Karta egasi:\n<b>{CARD_HOLDER}</b>\n\n"
        "To'lovni amalga oshirib, chekni shu botga <b>rasm sifatida</b> yuboring.\n\n"
        f"⏱ Chekni <b>{PAYMENT_TIMEOUT_MINUTES} daqiqa</b> ichida yuborishingiz kerak."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ BEKOR QILISH", callback_data="main_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.message(F.photo)
async def handle_receipt_photo(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == BroadcastState.waiting_for_content:
        return

    user_id = message.from_user.id
    now = now_dt()
    file_id = message.photo[-1].file_id

    async with data_lock:
        db = load_data_sync()
        pending_payments = [
            p for p in db.get("payments", [])
            if p.get("user_id") == user_id and p.get("status") == "PENDING"
        ]
        if not pending_payments:
            await message.answer("⏰ To'lov vaqti tugagan yoki to'lov topilmadi.\n\nIltimos, tarifni qaytadan tanlang: /start")
            return

        latest_payment = pending_payments[-1]
        exp_dt = parse_iso(latest_payment["expires_at"])
        if now > exp_dt:
            latest_payment["status"] = "EXPIRED"
            save_data_sync(db)
            await message.answer("⏰ To'lov vaqti tugagan.\n\nIltimos, tarifni qaytadan tanlang: /start")
            return

        latest_payment["receipt_file_id"] = file_id
        save_data_sync(db)

        user_info = next((u for u in db.get("users", []) if u.get("telegram_id") == user_id), {})
        tariff_info = next((t for t in db.get("tariffs", []) if t["id"] == latest_payment["tariff_id"]), {})
        admin_ids = get_all_admin_ids(db)

    await message.answer("✅ Chekingiz qabul qilindi.\n\n⏳ Admin to'lovni tekshirmoqda...")

    admin_caption = (
        "💳 <b>YANGI TO'LOV</b>\n\n"
        f"👤 Ism: <b>{user_info.get('first_name', '')} {user_info.get('last_name', '')}</b>\n"
        f"🔗 Username: @{user_info.get('username', 'Mavjud emas')}\n"
        f"🆔 Telegram ID: <code>{user_id}</code>\n\n"
        f"💎 Tarif: <b>{tariff_info.get('name', 'Noma\'lum')}</b>\n"
        f"💰 Summa: <b>{latest_payment.get('amount', 0):,} so'm</b>\n\n"
        f"🕐 Vaqt: {format_dt(now)}\n"
        f"🆔 To'lov ID: <code>{latest_payment['id']}</code>"
    )
    keyboard = get_admin_pay_approval_keyboard(latest_payment["id"])

    for admin_id in admin_ids:
        try:
            await bot.send_photo(
                chat_id=admin_id,
                photo=file_id,
                caption=admin_caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Adminga ({admin_id}) xabar yuborishda xatolik: {e}")


# --- JOIN REQUEST HANDLER (KANALGA SO'ROV YUBORILGANDA) ---
@router.chat_join_request()
async def handle_chat_join_request(req: ChatJoinRequest):
    user_id = req.from_user.id
    chat_id = req.chat.id

    async with data_lock:
        db = load_data_sync()
        register_or_update_user(req.from_user, db)
        sub = get_user_active_subscription(user_id, db)
        tariffs = db.get("tariffs", [])

        # 1. Agar foydalanuvchida allaqachon faol VIP obuna bo'lsa, so'rov darhol tasdiqlanadi
        if sub:
            try:
                await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
                db["join_requests"].append({
                    "id": f"JR-{uuid.uuid4().hex[:8].upper()}",
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "status": "APPROVED",
                    "created_at": now_iso(),
                    "approved_at": now_iso()
                })
                save_data_sync(db)
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text="🎉 VIP obunangiz faol bo'lgani sababli kanalga kirish so'rovingiz avtomatik tasdiqlandi!\n\nKanalga xush kelibsiz! 🍿"
                    )
                except Exception:
                    pass
                return
            except Exception as e:
                logger.error(f"Avtomatik join request tasdiqlashda xatolik: {e}")

        # 2. Obunasi bo'lmasa, so'rovni PENDING holatida saqlaymiz
        existing = next((jr for jr in db.get("join_requests", []) if jr.get("user_id") == user_id and jr.get("status") == "PENDING"), None)
        if not existing:
            db["join_requests"].append({
                "id": f"JR-{uuid.uuid4().hex[:8].upper()}",
                "user_id": user_id,
                "chat_id": chat_id,
                "status": "PENDING",
                "created_at": now_iso(),
                "approved_at": None
            })
            save_data_sync(db)

    # Foydalanuvchining bot lichkasiga tariflar menyusini yuboramiz
    welcome_text = (
        "✅ <b>Kanalga qo'shilish so'rovingiz qabul qilindi!</b>\n\n"
        "🔐 VIP kanalga kirish huquqini faollashtirish uchun quyidagi tariflardan birini tanlang va to'lovni amalga oshiring:\n\n"
        "To'lovingiz tasdiqlangach, bot sizni avtomatik ravishda kanalga qabul qiladi! 🚀"
    )
    try:
        await bot.send_message(
            chat_id=user_id,
            text=welcome_text,
            reply_markup=get_main_keyboard(tariffs),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Foydalanuvchiga ({user_id}) tariflarni yuborishda xatolik (bot bloklangan bo'lishi mumkin): {e}")


# --- ADMIN TO'LOVNI TASDIQLASH VA KANALGA QABUL QILISH ---
@router.callback_query(F.data.startswith("adm_app_"))
async def cb_admin_approve_payment(callback: CallbackQuery):
    admin_id = callback.from_user.id
    parts = callback.data.split("_")
    pay_id = parts[2]
    duration_raw = parts[3]
    delta, duration_label = parse_duration(duration_raw)

    async with data_lock:
        db = load_data_sync()
        if not is_admin(admin_id, db):
            await callback.answer("Siz admin emassiz!", show_alert=True)
            return

        payment = next((p for p in db.get("payments", []) if p.get("id") == pay_id), None)
        if not payment:
            await callback.answer("To'lov topilmadi!", show_alert=True)
            return

        if payment.get("status") != "PENDING":
            await callback.answer(f"Bu to'lov allaqachon ko'rib chiqilgan (Status: {payment.get('status')})!", show_alert=True)
            return

        target_user_id = payment["user_id"]
        channel_id = get_channel_id(db)
        now = now_dt()
        active_sub = get_user_active_subscription(target_user_id, db)

        if active_sub:
            old_end = parse_iso(active_sub["end_date"])
            new_start = old_end
            new_end = old_end + delta
            active_sub["status"] = "EXPIRED"
            active_sub["is_active"] = False
            active_sub["updated_at"] = now_iso()
        else:
            new_start = now
            new_end = now + delta

        sub_id = f"SUB-{uuid.uuid4().hex[:8].upper()}"
        new_subscription = {
            "id": sub_id,
            "user_id": target_user_id,
            "payment_id": payment["id"],
            "start_date": new_start.isoformat(),
            "end_date": new_end.isoformat(),
            "status": "ACTIVE",
            "is_active": True,
            "created_at": now_iso(),
            "updated_at": now_iso()
        }
        db["subscriptions"].append(new_subscription)

        payment["status"] = "APPROVED"
        payment["admin_id"] = admin_id
        payment["approved_at"] = now_iso()

        db["admin_logs"].append({
            "id": f"LOG-{uuid.uuid4().hex[:8].upper()}",
            "admin_id": admin_id,
            "action": "APPROVE_PAYMENT",
            "target_user_id": target_user_id,
            "details": f"{pay_id} to'lovi {duration_label}ga tasdiqlandi. Sub ID: {sub_id}",
            "created_at": now_iso()
        })

        join_req = next((jr for jr in db.get("join_requests", []) if jr.get("user_id") == target_user_id and jr.get("status") == "PENDING"), None)
        if join_req:
            join_req["status"] = "APPROVED"
            join_req["approved_at"] = now_iso()

        save_data_sync(db)

    # BOT FOYDALANUVCHINING SO'ROVINI AVTOMATIK TASDIQLAYDI
    approved_via_req = False
    if channel_id != 0:
        try:
            await bot.approve_chat_join_request(chat_id=channel_id, user_id=target_user_id)
            approved_via_req = True
        except Exception as e:
            logger.info(f"Join request mavjud emas yoki tasdiqlanmadi ({e}), to'g'ridan-to'g'ri invite link yuboriladi.")

    if approved_via_req:
        user_msg = (
            "🎉 <b>To'lovingiz muvaffaqiyatli tasdiqlandi!</b>\n\n"
            f"💎 VIP obunangiz faollashtirildi (<b>{duration_label}</b>).\n"
            "✅ Kanalga yuborgan so'rovingiz avtomatik qabul qilindi.\n\n"
            "Kanalga xush kelibsiz! 🍿"
        )
        try:
            await bot.send_message(chat_id=target_user_id, text=user_msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Foydalanuvchiga xabar yetib bormadi: {e}")
    else:
        invite_url = None
        if channel_id != 0:
            try:
                exp_ts = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
                invite = await bot.create_chat_invite_link(
                    chat_id=channel_id,
                    member_limit=1,
                    expire_date=exp_ts
                )
                invite_url = invite.invite_link
            except Exception as e:
                logger.error(f"Invite link yaratishda xatolik: {e}")

        user_msg = (
            "🎉 <b>To'lovingiz muvaffaqiyatli tasdiqlandi!</b>\n\n"
            f"💎 VIP obunangiz faollashtirildi (<b>{duration_label}</b>).\n"
        )
        keyboard = None
        if invite_url:
            user_msg += "\n🔐 Quyidagi havola orqali kanalga kiring:"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 KANALGA KIRISH", url=invite_url)]
            ])
        else:
            user_msg += "\nKanal administratori bilan bog'laning."

        try:
            await bot.send_message(chat_id=target_user_id, text=user_msg, reply_markup=keyboard, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Foydalanuvchiga xabar yetib bormadi: {e}")

    try:
        new_caption = (callback.message.caption or "") + f"\n\n✅ <b>TASDIQLANDI ({duration_label})</b>\n👮 Admin: <code>{admin_id}</code>"
        await callback.message.edit_caption(caption=new_caption, reply_markup=None, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Admin xabarini tahrirlashda xatolik: {e}")

    await callback.answer(f"To'lov {duration_label}ga tasdiqlandi!")


@router.callback_query(F.data.startswith("adm_rej_"))
async def cb_admin_reject_start(callback: CallbackQuery, state: FSMContext):
    admin_id = callback.from_user.id
    pay_id = callback.data.replace("adm_rej_", "")

    async with data_lock:
        db = load_data_sync()
        if not is_admin(admin_id, db):
            await callback.answer("Siz admin emassiz!", show_alert=True)
            return

        payment = next((p for p in db.get("payments", []) if p.get("id") == pay_id), None)
        if not payment:
            await callback.answer("To'lov topilmadi!", show_alert=True)
            return

        if payment.get("status") != "PENDING":
            await callback.answer("Bu to'lov allaqachon ko'rib chiqilgan!", show_alert=True)
            return

    await state.set_state(RejectReasonState.waiting_for_reason)
    await state.update_data(pay_id=pay_id, admin_msg_id=callback.message.message_id, admin_chat_id=callback.message.chat.id)
    await callback.message.reply("❌ Iltimos, rad etish sababini yozing (yoki bekor qilish uchun /cancel):")
    await callback.answer()


@router.message(RejectReasonState.waiting_for_reason)
async def process_reject_reason(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("Bekor qilindi.")
        return

    reason = message.text or "To'lov chekida xatolik aniqlandi."
    data = await state.get_data()
    pay_id = data.get("pay_id")
    admin_id = message.from_user.id

    async with data_lock:
        db = load_data_sync()
        payment = next((p for p in db.get("payments", []) if p.get("id") == pay_id), None)
        if not payment:
            await message.answer("To'lov topilmadi!")
            await state.clear()
            return

        target_user_id = payment["user_id"]
        payment["status"] = "REJECTED"
        payment["rejection_reason"] = reason
        payment["admin_id"] = admin_id

        db["admin_logs"].append({
            "id": f"LOG-{uuid.uuid4().hex[:8].upper()}",
            "admin_id": admin_id,
            "action": "REJECT_PAYMENT",
            "target_user_id": target_user_id,
            "details": f"{pay_id} to'lovi rad etildi. Sabab: {reason}",
            "created_at": now_iso()
        })
        save_data_sync(db)

    user_msg = (
        "❌ <b>To'lovingiz rad etildi.</b>\n\n"
        f"Sabab: <i>{reason}</i>\n\n"
        "Qayta to'lov qilish uchun: /start"
    )
    try:
        await bot.send_message(chat_id=target_user_id, text=user_msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Foydalanuvchiga rad xabari yetib bormadi: {e}")

    await message.answer("✅ To'lov rad etildi va foydalanuvchiga xabar yuborildi.")
    await state.clear()


# --- ADMIN PANEL ASOSIY HANDLERS ---
@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    async with data_lock:
        db = load_data_sync()
        if not is_admin(message.from_user.id, db):
            return

    text = (
        "👑 <b>ADMIN PANEL</b>\n\n"
        "Quyidagi bo'limlardan birini tanlang:"
    )
    await message.answer(text, reply_markup=get_admin_panel_keyboard(), parse_mode="HTML")


@router.callback_query(F.data == "admin_panel_back")
async def cb_admin_panel_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

    text = (
        "👑 <b>ADMIN PANEL</b>\n\n"
        "Quyidagi bo'limlardan birini tanlang:"
    )
    await callback.message.edit_text(text, reply_markup=get_admin_panel_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_nav_close")
async def cb_admin_close(callback: CallbackQuery):
    await callback.message.delete()


# --- KANAL SOZLAMALARI (ADMIN PANEL) ---
@router.callback_query(F.data == "adm_nav_channel")
async def cb_admin_channel_menu(callback: CallbackQuery):
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return
        
        channel_id = get_channel_id(db)
        invite_link = db.get("settings", {}).get("invite_link") or "Mavjud emas (Link yaratilmagan)"

    text = (
        "📢 <b>VIP KANAL SOZLAMALARI</b>\n\n"
        f"🆔 Hozirgi Kanal ID: <code>{channel_id}</code>\n\n"
        f"🔗 Bot So'rov Linki:\n{invite_link}\n\n"
        "Kanal ID sini o'zgartirish yoki linkni qayta yangilash uchun quyidagi tugmalardan foydalaning:"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Kanal ID o'zgartirish", callback_data="adm_chan_edit")],
        [InlineKeyboardButton(text="🔄 Linkni yangilash", callback_data="adm_chan_relink")],
        [InlineKeyboardButton(text="◀️ ORQAGA", callback_data="admin_panel_back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_chan_edit")
async def cb_admin_chan_edit(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("Faqat Super Admin kanalni o'zgartira oladi!", show_alert=True)
        return
    
    await state.set_state(ChannelSettingState.waiting_for_channel_id)
    text = (
        "✏️ <b>Yangi VIP Kanal ID sini yuboring:</b>\n\n"
        "Masalan: <code>-1001234567890</code>\n\n"
        "<i>⚠️ Eslatma: Bot ushbu kanalda administrator bo'lishi va 'Add Members / Invite Users via Link' ruxsatlariga ega bo'lishi shart!</i>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ BEKOR QILISH", callback_data="adm_nav_channel")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.message(ChannelSettingState.waiting_for_channel_id)
async def process_new_channel_id(message: Message, state: FSMContext):
    raw_text = (message.text or "").strip()
    if not (raw_text.lstrip("-").isdigit() and len(raw_text) > 5):
        await message.answer("⚠️ Iltimos, to'g'ri formatdagi Kanal ID sini yuboring (masalan: <code>-1001234567890</code>).")
        return

    new_channel_id = int(raw_text)
    
    # Bot kanalga kira olishi va link yarata olishini tekshiramiz
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=new_channel_id,
            name="VIP Auto Join Link",
            creates_join_request=True
        )
        new_invite_link = invite.invite_link
    except Exception as e:
        await message.answer(
            f"❌ <b>Xatolik yuz berdi!</b>\n\n"
            f"Bot ushbu kanalda administrator emas yoki taklif havolasi yaratish huquqiga ega emas.\n"
            f"Xatolik: <code>{e}</code>\n\n"
            "Iltimos, botni kanalga admin qilib qo'shing va qayta urinib ko'ring."
        )
        return

    async with data_lock:
        db = load_data_sync()
        settings = db.setdefault("settings", {})
        settings["channel_id"] = new_channel_id
        settings["invite_link"] = new_invite_link
        db["admin_logs"].append({
            "id": f"LOG-{uuid.uuid4().hex[:8].upper()}",
            "admin_id": message.from_user.id,
            "action": "UPDATE_CHANNEL",
            "target_user_id": 0,
            "details": f"Kanal ID yangilandi: {new_channel_id}",
            "created_at": now_iso()
        })
        save_data_sync(db)

    await state.clear()
    text = (
        "✅ <b>Kanal muvaffaqiyatli saqlandi!</b>\n\n"
        f"🆔 Yangi Kanal ID: <code>{new_channel_id}</code>\n"
        f"🔗 Yangi So'rov Havolasi:\n{new_invite_link}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ KANAL SOZLAMALARIGA QAYTISH", callback_data="adm_nav_channel")]
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "adm_chan_relink")
async def cb_admin_chan_relink(callback: CallbackQuery):
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

        channel_id = get_channel_id(db)

    if channel_id == 0:
        await callback.answer("Avval kanal ID sini kiriting!", show_alert=True)
        return

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=channel_id,
            name="VIP Auto Join Link",
            creates_join_request=True
        )
        async with data_lock:
            db = load_data_sync()
            db.setdefault("settings", {})["invite_link"] = invite.invite_link
            save_data_sync(db)

        await callback.answer("Yangi havola yaratildi!", show_alert=True)
        await cb_admin_channel_menu(callback)
    except Exception as e:
        await callback.answer(f"Havola yaratishda xatolik: {e}", show_alert=True)


# --- BOSHQA ADMIN PANELLARI ---
@router.callback_query(F.data == "adm_nav_stats")
async def cb_admin_stats(callback: CallbackQuery):
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

    now = now_dt()
    users = db.get("users", [])
    payments = db.get("payments", [])
    subs = db.get("subscriptions", [])

    def get_stats_for_period(days_delta: Optional[int] = None):
        cutoff = now - timedelta(days=days_delta) if days_delta else None
        
        new_users = [u for u in users if not cutoff or parse_iso(u["joined_at"]) >= cutoff]
        period_payments = [p for p in payments if not cutoff or parse_iso(p["created_at"]) >= cutoff]
        approved_p = [p for p in period_payments if p.get("status") == "APPROVED"]
        rejected_p = [p for p in period_payments if p.get("status") == "REJECTED"]
        pending_p = [p for p in period_payments if p.get("status") == "PENDING"]
        revenue = sum(p.get("amount", 0) for p in approved_p)

        return {
            "users": len(new_users),
            "payments": len(period_payments),
            "approved": len(approved_p),
            "rejected": len(rejected_p),
            "pending": len(pending_p),
            "revenue": revenue
        }

    s24 = get_stats_for_period(1)
    s7d = get_stats_for_period(7)
    s30d = get_stats_for_period(30)
    s_all = get_stats_for_period(None)

    active_subs = len([s for s in subs if s.get("status") == "ACTIVE" and s.get("is_active", False) and parse_iso(s["end_date"]) > now])
    expired_subs = len([s for s in subs if s.get("status") == "EXPIRED" or parse_iso(s["end_date"]) <= now])
    cancelled_subs = len([s for s in subs if s.get("status") == "CANCELLED"])

    text = (
        "📊 <b>STATISTIKA</b>\n\n"
        "<b>⏱ 24 SOAT:</b>\n"
        f"• Yangi userlar: {s24['users']}\n"
        f"• To'lovlar: {s24['payments']}\n"
        f"• Tasdiqlangan: {s24['approved']}\n"
        f"• Rad etilgan: {s24['rejected']}\n"
        f"• Tushum: {s24['revenue']:,} so'm\n\n"
        "<b>📅 7 KUN:</b>\n"
        f"• Yangi userlar: {s7d['users']}\n"
        f"• To'lovlar: {s7d['payments']}\n"
        f"• Tasdiqlangan: {s7d['approved']}\n"
        f"• Rad etilgan: {s7d['rejected']}\n"
        f"• Tushum: {s7d['revenue']:,} so'm\n\n"
        "<b>🗓 30 KUN:</b>\n"
        f"• Yangi userlar: {s30d['users']}\n"
        f"• To'lovlar: {s30d['payments']}\n"
        f"• Tasdiqlangan: {s30d['approved']}\n"
        f"• Rad etilgan: {s30d['rejected']}\n"
        f"• Tushum: {s30d['revenue']:,} so'm\n\n"
        "<b>🌐 UMUMIY:</b>\n"
        f"• Jami userlar: {len(users)}\n"
        f"• Faol obunalar: {active_subs}\n"
        f"• Tugagan obunalar: {expired_subs}\n"
        f"• Bekor qilingan: {cancelled_subs}\n"
        f"• Kutilayotgan to'lovlar: {s_all['pending']}\n"
        f"• Jami tasdiqlangan: {s_all['approved']}\n"
        f"• Jami tushum: <b>{s_all['revenue']:,} so'm</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ ORQAGA", callback_data="admin_panel_back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_nav_users")
async def cb_admin_users(callback: CallbackQuery):
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

    users = db.get("users", [])
    blocked_count = len([u for u in users if u.get("is_blocked", False)])
    active_count = len(users) - blocked_count

    text = (
        "👥 <b>FOYDALANUVCHILAR</b>\n\n"
        f"• Jami foydalanuvchilar: <b>{len(users)}</b>\n"
        f"• Faol (botni bloklamagan): <b>{active_count}</b>\n"
        f"• Bloklaganlar: <b>{blocked_count}</b>\n\n"
        "Foydalanuvchini boshqarish yoki tekshirish uchun '🔎 User qidirish' bo'limidan foydalaning."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ ORQAGA", callback_data="admin_panel_back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_nav_payments")
async def cb_admin_payments(callback: CallbackQuery):
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

    payments = db.get("payments", [])
    pending = [p for p in payments if p.get("status") == "PENDING"]
    approved = [p for p in payments if p.get("status") == "APPROVED"]
    rejected = [p for p in payments if p.get("status") == "REJECTED"]
    expired = [p for p in payments if p.get("status") == "EXPIRED"]

    text = (
        "💳 <b>TO'LOVLAR BO'LIMI</b>\n\n"
        f"• Kutilayotgan (PENDING): <b>{len(pending)}</b>\n"
        f"• Tasdiqlangan (APPROVED): <b>{len(approved)}</b>\n"
        f"• Rad etilgan (REJECTED): <b>{len(rejected)}</b>\n"
        f"• Vaqti o'tgan (EXPIRED): <b>{len(expired)}</b>\n"
        f"• Jami to'lov urinishlari: <b>{len(payments)}</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ ORQAGA", callback_data="admin_panel_back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_nav_subs")
async def cb_admin_subs(callback: CallbackQuery):
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

    subs = db.get("subscriptions", [])
    now = now_dt()
    active = [s for s in subs if s.get("status") == "ACTIVE" and s.get("is_active", False) and parse_iso(s["end_date"]) > now]
    expired = [s for s in subs if s.get("status") == "EXPIRED" or (s.get("status") == "ACTIVE" and parse_iso(s["end_date"]) <= now)]
    cancelled = [s for s in subs if s.get("status") == "CANCELLED"]

    text = (
        "💎 <b>OBUNALAR BO'LIMI</b>\n\n"
        f"• Faol obunalar: <b>{len(active)}</b>\n"
        f"• Tugagan obunalar: <b>{len(expired)}</b>\n"
        f"• Bekor qilingan: <b>{len(cancelled)}</b>\n"
        f"• Jami berilgan obunalar: <b>{len(subs)}</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ ORQAGA", callback_data="admin_panel_back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_nav_logs")
async def cb_admin_logs(callback: CallbackQuery):
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

    logs = db.get("admin_logs", [])[-10:]
    if not logs:
        text = "📋 <b>ADMIN LOGLARI</b>\n\nLoglar mavjud emas."
    else:
        text = "📋 <b>SO'NGGI ADMIN LOGLARI:</b>\n\n"
        for l in reversed(logs):
            dt = format_dt(parse_iso(l["created_at"]))
            text += f"• <code>{dt}</code> | Admin: <code>{l['admin_id']}</code>\n  {l['action']}: {l['details']}\n\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ ORQAGA", callback_data="admin_panel_back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


# --- USER SEARCH HANDLERS ---
@router.callback_query(F.data == "adm_nav_search")
async def cb_admin_search_start(callback: CallbackQuery, state: FSMContext):
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

    await state.set_state(UserSearchState.waiting_for_user_id)
    text = "🔎 <b>Foydalanuvchi qidirish</b>\n\nFoydalanuvchining Telegram ID sini yuboring:"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ BEKOR QILISH", callback_data="admin_panel_back")]
    ]), parse_mode="HTML")
    await callback.answer()


@router.message(UserSearchState.waiting_for_user_id)
async def process_user_search(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("⚠️ Iltimos, faqat raqamlardan iborat Telegram ID yuboring.")
        return

    target_id = int(message.text.strip())
    await state.clear()

    async with data_lock:
        db = load_data_sync()
        user_info = next((u for u in db.get("users", []) if u.get("telegram_id") == target_id), None)
        sub = get_user_active_subscription(target_id, db)

    if not user_info:
        await message.answer(
            f"❌ Telegram ID <code>{target_id}</code> bo'lgan foydalanuvchi botda topilmadi.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ ADMIN PANEL", callback_data="admin_panel_back")]
            ]),
            parse_mode="HTML"
        )
        return

    if sub:
        start_dt = parse_iso(sub["start_date"])
        end_dt = parse_iso(sub["end_date"])
        time_left_str = format_time_left(end_dt)
        status_text = f"ACTIVE\n📅 Start: {format_dt(start_dt)}\n📅 End: {format_dt(end_dt)}\n⏳ Qolgan: {time_left_str}"
    else:
        status_text = "INACTIVE (Faol obuna yo'q)"

    text = (
        "👤 <b>USER MA'LUMOTI</b>\n\n"
        f"ID: <code>{target_id}</code>\n"
        f"Ism: <b>{user_info.get('first_name', '')} {user_info.get('last_name', '')}</b>\n"
        f"Username: @{user_info.get('username', 'Mavjud emas')}\n\n"
        f"💎 Status: <b>{status_text}</b>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ OBUNA BERISH", callback_data=f"adm_give_to_{target_id}"),
            InlineKeyboardButton(text="➖ OBUNANI BEKOR QILISH", callback_data=f"adm_cancel_for_{target_id}")
        ],
        [
            InlineKeyboardButton(text="🚫 KANALDAN CHIQARISH", callback_data=f"adm_kick_{target_id}")
        ],
        [
            InlineKeyboardButton(text="📜 OBUNA TARIXI", callback_data=f"adm_subhist_{target_id}"),
            InlineKeyboardButton(text="💳 TO'LOVLAR", callback_data=f"adm_payhist_{target_id}")
        ],
        [
            InlineKeyboardButton(text="◀️ ADMIN PANEL", callback_data="admin_panel_back")
        ]
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_subhist_"))
async def cb_sub_history(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[2])
    async with data_lock:
        db = load_data_sync()
        subs = [s for s in db.get("subscriptions", []) if s.get("user_id") == target_id]

    if not subs:
        text = f"ID: <code>{target_id}</code> uchun obuna tarixi topilmadi."
    else:
        text = f"📜 <b>ID: <code>{target_id}</code> ning Obuna Tarixi:</b>\n\n"
        for s in subs:
            st = format_dt(parse_iso(s["start_date"]))
            en = format_dt(parse_iso(s["end_date"]))
            text += f"• <code>{s['id']}</code> | {st} -> {en} | Status: <b>{s['status']}</b>\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ ORQAGA", callback_data="admin_panel_back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_payhist_"))
async def cb_pay_history(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[2])
    async with data_lock:
        db = load_data_sync()
        pays = [p for p in db.get("payments", []) if p.get("user_id") == target_id]

    if not pays:
        text = f"ID: <code>{target_id}</code> uchun to'lov tarixi topilmadi."
    else:
        text = f"💳 <b>ID: <code>{target_id}</code> ning To'lov Tarixi:</b>\n\n"
        for p in pays:
            dt = format_dt(parse_iso(p["created_at"]))
            text += f"• <code>{p['id']}</code> | {dt} | {p.get('amount', 0):,} so'm | Status: <b>{p['status']}</b>\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ ORQAGA", callback_data="admin_panel_back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


# --- MANUAL OBUNA BERISH HANDLERS ---
@router.callback_query(F.data == "adm_nav_givesub")
async def cb_admin_givesub_start(callback: CallbackQuery, state: FSMContext):
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

    await state.set_state(ManualSubState.waiting_for_user_id)
    text = "➕ <b>Obuna berish</b>\n\nFoydalanuvchining Telegram ID sini yuboring:"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ BEKOR QILISH", callback_data="admin_panel_back")]
    ]), parse_mode="HTML")
    await callback.answer()


@router.message(ManualSubState.waiting_for_user_id)
async def process_manual_sub_user_id(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("⚠️ Iltimos, to'g'ri Telegram ID yuboring.")
        return

    target_id = int(message.text.strip())
    await state.update_data(target_id=target_id)
    await state.set_state(ManualSubState.waiting_for_days)

    await message.answer(
        f"ID: <code>{target_id}</code> ga qancha muddatga obuna bermoqchisiz?",
        reply_markup=get_manual_sub_duration_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("adm_give_to_"))
async def cb_give_sub_direct(callback: CallbackQuery, state: FSMContext):
    target_id = int(callback.data.split("_")[3])
    await state.update_data(target_id=target_id)
    await state.set_state(ManualSubState.waiting_for_days)

    await callback.message.edit_text(
        f"ID: <code>{target_id}</code> ga muddatni tanlang:",
        reply_markup=get_manual_sub_duration_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(ManualSubState.waiting_for_days, F.data.startswith("man_sub_"))
async def process_manual_sub_days(callback: CallbackQuery, state: FSMContext):
    duration_raw = callback.data.replace("man_sub_", "")
    delta, duration_label = parse_duration(duration_raw)

    data = await state.get_data()
    target_id = data.get("target_id")
    admin_id = callback.from_user.id
    now = now_dt()

    async with data_lock:
        db = load_data_sync()
        channel_id = get_channel_id(db)
        active_sub = get_user_active_subscription(target_id, db)
        if active_sub:
            old_end = parse_iso(active_sub["end_date"])
            new_start = old_end
            new_end = old_end + delta
            active_sub["status"] = "EXPIRED"
            active_sub["is_active"] = False
            active_sub["updated_at"] = now_iso()
        else:
            new_start = now
            new_end = now + delta

        sub_id = f"SUB-{uuid.uuid4().hex[:8].upper()}"
        new_subscription = {
            "id": sub_id,
            "user_id": target_id,
            "payment_id": "MANUAL",
            "start_date": new_start.isoformat(),
            "end_date": new_end.isoformat(),
            "status": "ACTIVE",
            "is_active": True,
            "created_at": now_iso(),
            "updated_at": now_iso()
        }
        db["subscriptions"].append(new_subscription)

        db["admin_logs"].append({
            "id": f"LOG-{uuid.uuid4().hex[:8].upper()}",
            "admin_id": admin_id,
            "action": "MANUAL_SUBSCRIPTION",
            "target_user_id": target_id,
            "details": f"Admin qo'lda {duration_label}lik obuna berdi. Sub ID: {sub_id}",
            "created_at": now_iso()
        })
        save_data_sync(db)

    approved_via_req = False
    if channel_id != 0:
        try:
            await bot.approve_chat_join_request(chat_id=channel_id, user_id=target_id)
            approved_via_req = True
        except Exception:
            pass

    if approved_via_req:
        try:
            await bot.send_message(
                chat_id=target_id,
                text=f"🎉 <b>Admin tomonidan sizga {duration_label}lik VIP obuna berildi!</b>\n\nKanalga xush kelibsiz! 🍿",
                parse_mode="HTML"
            )
        except Exception:
            pass
    else:
        invite_url = None
        if channel_id != 0:
            try:
                exp_ts = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
                invite = await bot.create_chat_invite_link(chat_id=channel_id, member_limit=1, expire_date=exp_ts)
                invite_url = invite.invite_link
            except Exception:
                pass

        user_text = f"🎉 <b>Admin tomonidan sizga {duration_label}lik VIP obuna taqdim etildi!</b>\n"
        keyboard = None
        if invite_url:
            user_text += "\n🔐 Kanalga kirish havolasi:"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 KANALGA KIRISH", url=invite_url)]
            ])
        try:
            await bot.send_message(chat_id=target_id, text=user_text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            pass

    await state.clear()
    await callback.message.edit_text(
        f"✅ ID <code>{target_id}</code> ga {duration_label}lik obuna muvaffaqiyatli berildi!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ ADMIN PANEL", callback_data="admin_panel_back")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


# --- OBUNANI BEKOR QILISH VA KANALDAN CHIQARISH ---
@router.callback_query(F.data == "adm_nav_cancelsub")
async def cb_admin_cancelsub_start(callback: CallbackQuery, state: FSMContext):
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

    await state.set_state(CancelSubState.waiting_for_user_id)
    text = "➖ <b>Obunani bekor qilish</b>\n\nFoydalanuvchining Telegram ID sini yuboring:"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ BEKOR QILISH", callback_data="admin_panel_back")]
    ]), parse_mode="HTML")
    await callback.answer()


@router.message(CancelSubState.waiting_for_user_id)
async def process_cancel_user_id(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("⚠️ Iltimos, to'g'ri Telegram ID yuboring.")
        return

    target_id = int(message.text.strip())
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ HA", callback_data=f"adm_conf_cancel_{target_id}"),
            InlineKeyboardButton(text="❌ YO'Q", callback_data="admin_panel_back")
        ]
    ])
    await state.clear()
    await message.answer(f"⚠️ Ushbu foydalanuvchining (<code>{target_id}</code>) obunasini bekor qilaymi?", reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_cancel_for_"))
async def cb_cancel_sub_direct(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[3])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ HA", callback_data=f"adm_conf_cancel_{target_id}"),
            InlineKeyboardButton(text="❌ YO'Q", callback_data="admin_panel_back")
        ]
    ])
    await callback.message.edit_text(f"⚠️ Ushbu foydalanuvchining (<code>{target_id}</code>) obunasini bekor qilaymi?", reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_conf_cancel_"))
async def cb_confirm_cancel_sub(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[3])
    admin_id = callback.from_user.id

    async with data_lock:
        db = load_data_sync()
        if not is_admin(admin_id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

        channel_id = get_channel_id(db)
        active_subs = [s for s in db.get("subscriptions", []) if s.get("user_id") == target_id and s.get("status") == "ACTIVE"]
        for s in active_subs:
            s["status"] = "CANCELLED"
            s["is_active"] = False
            s["updated_at"] = now_iso()

        db["admin_logs"].append({
            "id": f"LOG-{uuid.uuid4().hex[:8].upper()}",
            "admin_id": admin_id,
            "action": "CANCEL_SUBSCRIPTION",
            "target_user_id": target_id,
            "details": "Obuna admin tomonidan bekor qilindi",
            "created_at": now_iso()
        })
        save_data_sync(db)

    if channel_id != 0:
        try:
            await bot.ban_chat_member(chat_id=channel_id, user_id=target_id)
            await bot.unban_chat_member(chat_id=channel_id, user_id=target_id)
        except Exception as e:
            logger.warning(f"Foydalanuvchini kanaldan chiqarishda xatolik: {e}")

    try:
        await bot.send_message(
            chat_id=target_id,
            text="❌ Sizning VIP obunangiz admin tomonidan bekor qilindi va kanaldan chiqarildingiz."
        )
    except Exception:
        pass

    await callback.message.edit_text(
        f"✅ ID <code>{target_id}</code> foydalanuvchining obunasi bekor qilindi va kanaldan chiqarildi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ ADMIN PANEL", callback_data="admin_panel_back")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_kick_"))
async def cb_admin_kick_user(callback: CallbackQuery):
    target_id = int(callback.data.split("_")[2])
    admin_id = callback.from_user.id

    async with data_lock:
        db = load_data_sync()
        if not is_admin(admin_id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

        channel_id = get_channel_id(db)
        db["admin_logs"].append({
            "id": f"LOG-{uuid.uuid4().hex[:8].upper()}",
            "admin_id": admin_id,
            "action": "KICK_USER",
            "target_user_id": target_id,
            "details": "Foydalanuvchi kanaldan chiqarildi",
            "created_at": now_iso()
        })
        save_data_sync(db)

    if channel_id != 0:
        try:
            await bot.ban_chat_member(chat_id=channel_id, user_id=target_id)
            await bot.unban_chat_member(chat_id=channel_id, user_id=target_id)
        except Exception as e:
            logger.warning(f"Foydalanuvchini chiqarishda xatolik: {e}")

    await callback.message.edit_text(
        f"🚫 ID <code>{target_id}</code> kanaldan chiqarildi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ ADMIN PANEL", callback_data="admin_panel_back")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


# --- ADMINLARNI BOSHQARISH ---
@router.callback_query(F.data == "adm_nav_admins")
async def cb_admin_list(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with data_lock:
        db = load_data_sync()
        if not is_admin(user_id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

    text = "👮 <b>ADMINLAR RO'YXATI:</b>\n\n"
    text += f"👑 <b>Super Admin:</b> <code>{SUPER_ADMIN_ID}</code>\n\n"
    other_admins = db.get("admins", [])
    if other_admins:
        text += "<b>Qo'shilgan Adminlar:</b>\n"
        for a in other_admins:
            text += f"• <code>{a['telegram_id']}</code> (Qo'shilgan: {format_date(parse_iso(a['created_at']))})\n"
    else:
        text += "Qo'shimcha adminlar yo'q.\n"

    buttons = []
    if is_super_admin(user_id):
        buttons.append([
            InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="adm_mgmt_add"),
            InlineKeyboardButton(text="➖ Admin o'chirish", callback_data="adm_mgmt_del")
        ])
    buttons.append([InlineKeyboardButton(text="◀️ ORQAGA", callback_data="admin_panel_back")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_mgmt_add")
async def cb_add_admin_start(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("Faqat Super Admin qo'sha oladi!", show_alert=True)
        return

    await state.set_state(AddAdminState.waiting_for_admin_id)
    text = "➕ <b>Yangi admin qo'shish</b>\n\nYangi adminning Telegram ID sini yuboring:"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ BEKOR QILISH", callback_data="adm_nav_admins")]
    ]), parse_mode="HTML")
    await callback.answer()


@router.message(AddAdminState.waiting_for_admin_id)
async def process_add_admin_id(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("⚠️ Iltimos, to'g'ri Telegram ID yuboring.")
        return

    new_admin_id = int(message.text.strip())
    async with data_lock:
        db = load_data_sync()
        if new_admin_id == SUPER_ADMIN_ID or any(a["telegram_id"] == new_admin_id for a in db.get("admins", [])):
            await message.answer("⚠️ Ushbu foydalanuvchi allaqachon admin!")
            await state.clear()
            return

        db["admins"].append({
            "telegram_id": new_admin_id,
            "is_super_admin": False,
            "created_at": now_iso()
        })
        db["admin_logs"].append({
            "id": f"LOG-{uuid.uuid4().hex[:8].upper()}",
            "admin_id": message.from_user.id,
            "action": "ADD_ADMIN",
            "target_user_id": new_admin_id,
            "details": "Yangi admin qo'shildi",
            "created_at": now_iso()
        })
        save_data_sync(db)

    await state.clear()
    await message.answer(
        f"✅ <code>{new_admin_id}</code> admin qilib tayinlandi!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ ADMINLARGA QAYTISH", callback_data="adm_nav_admins")]
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "adm_mgmt_del")
async def cb_del_admin_start(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("Faqat Super Admin o'chira oladi!", show_alert=True)
        return

    await state.set_state(RemoveAdminState.waiting_for_admin_id)
    text = "➖ <b>Adminni o'chirish</b>\n\nO'chiriladigan adminning Telegram ID sini yuboring:"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ BEKOR QILISH", callback_data="adm_nav_admins")]
    ]), parse_mode="HTML")
    await callback.answer()


@router.message(RemoveAdminState.waiting_for_admin_id)
async def process_del_admin_id(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("⚠️ Iltimos, to'g'ri Telegram ID yuboring.")
        return

    del_admin_id = int(message.text.strip())
    if del_admin_id == SUPER_ADMIN_ID:
        await message.answer("🚫 Super Adminni o'chirib bo'lmaydi!")
        await state.clear()
        return

    async with data_lock:
        db = load_data_sync()
        admins = db.get("admins", [])
        filtered = [a for a in admins if a["telegram_id"] != del_admin_id]
        if len(filtered) == len(admins):
            await message.answer("⚠️ Ushbu ID adminlar ro'yxatida topilmadi.")
            await state.clear()
            return

        db["admins"] = filtered
        db["admin_logs"].append({
            "id": f"LOG-{uuid.uuid4().hex[:8].upper()}",
            "admin_id": message.from_user.id,
            "action": "REMOVE_ADMIN",
            "target_user_id": del_admin_id,
            "details": "Admin o'chirildi",
            "created_at": now_iso()
        })
        save_data_sync(db)

    await state.clear()
    await message.answer(
        f"✅ <code>{del_admin_id}</code> adminlar ro'yxatidan olib tashlandi!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ ADMINLARGA QAYTISH", callback_data="adm_nav_admins")]
        ]),
        parse_mode="HTML"
    )


# --- BROADCAST HANDLERS ---
@router.callback_query(F.data == "adm_nav_broadcast")
async def cb_broadcast_start(callback: CallbackQuery, state: FSMContext):
    async with data_lock:
        db = load_data_sync()
        if not is_admin(callback.from_user.id, db):
            await callback.answer("Ruxsat yo'q!", show_alert=True)
            return

    await state.set_state(BroadcastState.waiting_for_content)
    text = (
        "📢 <b>Broadcast (Xabar tarqatish)</b>\n\n"
        "Barcha foydalanuvchilarga yuboriladigan xabarni (matn yoki rasm izohi bilan) yuboring:"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ BEKOR QILISH", callback_data="admin_panel_back")]
    ]), parse_mode="HTML")
    await callback.answer()


@router.message(BroadcastState.waiting_for_content)
async def process_broadcast_content(message: Message, state: FSMContext):
    is_photo = bool(message.photo)
    photo_id = message.photo[-1].file_id if is_photo else None
    text_content = message.caption if is_photo else message.text

    if not text_content and not is_photo:
        await message.answer("⚠️ Iltimos, xabar matni yoki rasmni yuboring.")
        return

    await state.update_data(
        is_photo=is_photo,
        photo_id=photo_id,
        text_content=text_content
    )
    await state.set_state(BroadcastState.confirm_broadcast)

    async with data_lock:
        db = load_data_sync()
        target_users = [u for u in db.get("users", []) if not u.get("is_blocked", False)]
        total_recipients = len(target_users)

    confirm_text = (
        "📢 <b>BROADCAST TASDIQLASH</b>\n\n"
        f"👥 Qabul qiluvchilar soni: <b>{total_recipients}</b>\n\n"
        "Xabarni yuborishni tasdiqlaysizmi?"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ YUBORISH", callback_data="adm_broadcast_confirm"),
            InlineKeyboardButton(text="❌ BEKOR QILISH", callback_data="admin_panel_back")
        ]
    ])
    await message.answer(confirm_text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(BroadcastState.confirm_broadcast, F.data == "adm_broadcast_confirm")
async def execute_broadcast(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    is_photo = data.get("is_photo", False)
    photo_id = data.get("photo_id")
    text_content = data.get("text_content") or ""

    await state.clear()
    await callback.message.edit_text("⏳ Xabar tarqatilmoqda, iltimos kuting...")

    async with data_lock:
        db = load_data_sync()
        users = [u for u in db.get("users", []) if not u.get("is_blocked", False)]

    success_count = 0
    fail_count = 0
    blocked_ids = []

    for u in users:
        uid = u.get("telegram_id")
        if not uid:
            continue
        try:
            if is_photo and photo_id:
                await bot.send_photo(chat_id=uid, photo=photo_id, caption=text_content, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=uid, text=text_content, parse_mode="HTML")
            success_count += 1
        except TelegramForbiddenError:
            fail_count += 1
            blocked_ids.append(uid)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                if is_photo and photo_id:
                    await bot.send_photo(chat_id=uid, photo=photo_id, caption=text_content, parse_mode="HTML")
                else:
                    await bot.send_message(chat_id=uid, text=text_content, parse_mode="HTML")
                success_count += 1
            except Exception:
                fail_count += 1
        except Exception as e:
            logger.warning(f"Broadcast xatosi ({uid}): {e}")
            fail_count += 1

        await asyncio.sleep(0.05)

    if blocked_ids:
        async with data_lock:
            db = load_data_sync()
            for u in db.get("users", []):
                if u.get("telegram_id") in blocked_ids:
                    u["is_blocked"] = True
            save_data_sync(db)

    report = (
        "✅ <b>Broadcast yakunlandi!</b>\n\n"
        f"📤 Muvaffaqiyatli yuborildi: <b>{success_count}</b>\n"
        f"❌ Yetib bormadi (bloklangan/xatolik): <b>{fail_count}</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ ADMIN PANEL", callback_data="admin_panel_back")]
    ])
    await callback.message.edit_text(report, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


# --- ASYNC EXPIRATION & REMINDER WORKER ---
async def expiration_worker():
    logger.info("Expiration va Reminder worker ishga tushdi.")
    while True:
        try:
            now = now_dt()
            async with data_lock:
                db = load_data_sync()
                channel_id = get_channel_id(db)
                changed = False

                # 1. Kutilayotgan to'lovlar muddatini tekshirish
                for p in db.get("payments", []):
                    if p.get("status") == "PENDING":
                        exp_dt = parse_iso(p["expires_at"])
                        if now >= exp_dt:
                            p["status"] = "EXPIRED"
                            changed = True

                # 2. Obunalar muddati va eslatmalarni tekshirish
                subscriptions = db.get("subscriptions", [])
                notifications = db.get("notifications", [])

                for sub in subscriptions:
                    if sub.get("status") == "ACTIVE" and sub.get("is_active", False):
                        end_dt = parse_iso(sub["end_date"])
                        time_left = end_dt - now
                        user_id = sub["user_id"]
                        sub_id = sub["id"]

                        # Obuna muddati tugagan bo'lsa: kanaldan chiqarish
                        if now >= end_dt:
                            sub["status"] = "EXPIRED"
                            sub["is_active"] = False
                            sub["updated_at"] = now_iso()
                            changed = True

                            if channel_id != 0:
                                try:
                                    await bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
                                    await bot.unban_chat_member(chat_id=channel_id, user_id=user_id)
                                except Exception as e:
                                    logger.warning(f"Muddat tugaganda kanaldan chiqarishda xatolik ({user_id}): {e}")

                            try:
                                await bot.send_message(
                                    chat_id=user_id,
                                    text="❌ <b>VIP obunangiz tugadi.</b>\n\nKanalga kirishni davom ettirish uchun yangi obuna sotib oling: /start",
                                    parse_mode="HTML"
                                )
                            except Exception:
                                pass

                            db["admin_logs"].append({
                                "id": f"LOG-{uuid.uuid4().hex[:8].upper()}",
                                "admin_id": 0,
                                "action": "SUB_EXPIRED",
                                "target_user_id": user_id,
                                "details": f"{sub_id} obuna muddati tugadi va foydalanuvchi kanaldan chiqarildi",
                                "created_at": now_iso()
                            })
                            continue

                        # Eslatma: 3 kun qolganda
                        if timedelta(days=2) < time_left <= timedelta(days=3):
                            notif_id = f"{sub_id}_3days"
                            if not any(n.get("id") == notif_id for n in notifications):
                                try:
                                    await bot.send_message(
                                        chat_id=user_id,
                                        text="⚠️ <b>VIP obunangiz tugashiga 3 kun qoldi.</b>\n\nObunani uzaytirish uchun: /start",
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    pass
                                notifications.append({
                                    "id": notif_id,
                                    "user_id": user_id,
                                    "sub_id": sub_id,
                                    "type": "3_DAYS",
                                    "sent_at": now_iso()
                                })
                                changed = True

                        # Eslatma: 1 kun qolganda
                        elif timedelta(hours=2) < time_left <= timedelta(days=1):
                            notif_id = f"{sub_id}_1day"
                            if not any(n.get("id") == notif_id for n in notifications):
                                try:
                                    await bot.send_message(
                                        chat_id=user_id,
                                        text="⚠️ <b>VIP obunangiz ertaga tugaydi.</b>\n\nObunani uzaytirish uchun: /start",
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    pass
                                notifications.append({
                                    "id": notif_id,
                                    "user_id": user_id,
                                    "sub_id": sub_id,
                                    "type": "1_DAY",
                                    "sent_at": now_iso()
                                })
                                changed = True

                        # Eslatma: 1 soat qolganda
                        elif timedelta(minutes=5) < time_left <= timedelta(hours=1):
                            notif_id = f"{sub_id}_1hour"
                            if not any(n.get("id") == notif_id for n in notifications):
                                try:
                                    await bot.send_message(
                                        chat_id=user_id,
                                        text="⚠️ <b>VIP obunangiz tugashiga 1 soat qoldi.</b>\n\nObunani uzaytirish uchun: /start",
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    pass
                                notifications.append({
                                    "id": notif_id,
                                    "user_id": user_id,
                                    "sub_id": sub_id,
                                    "type": "1_HOUR",
                                    "sent_at": now_iso()
                                })
                                changed = True

                if changed:
                    save_data_sync(db)

        except Exception as e:
            logger.exception(f"Expiration workerda kutilmagan xatolik: {e}")

        # 1 minutlik obunalar o'z vaqtida tugashi uchun tekshirish oralig'i: 10 soniya
        await asyncio.sleep(10)


# --- GLOBAL ERROR HANDLER ---
@dp.error()
async def global_error_handler(event: ErrorEvent):
    logger.exception(f"Global xatolik ushlandi: {event.exception}")
    return True


# --- ASOSIY ISHGA TUSHIRISH FUNKSIYASI ---
async def main():
    logger.info("Bot ishga tushirilmoqda...")
    async with data_lock:
        db = load_data_sync()
        # Dastlabki linkni generatsiya qilishga urinib ko'rish
        await get_or_create_channel_invite_link(bot, db)

    # Worker ni ishga tushirish
    asyncio.create_task(expiration_worker())

    # Update larni qabul qilishni boshlash
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")