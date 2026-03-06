# handlers/billing.py
"""
Система тарифов и платных подписок FlightBot Scout.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ТАРИФЫ (меняй только словарь PLANS):
  free    — бесплатно: 3 горячих + 3 дайджест подписки
  plus    — 50 ₽/мес:  10 горячих + 10 дайджест
  premium — 150 ₽/мес: безлимит подписок + 20 токенов FlyStack

ПЛАТЁЖНАЯ СИСТЕМА: ЮКасса
  Сейчас — заглушка (ждём регистрацию).
  После получения YOOKASSA_SHOP_ID + YOOKASSA_SECRET_KEY:
    1. Раскомментировать блок в create_yookassa_payment()
    2. Добавить webhook endpoint (FastAPI/aiohttp) вызывающий handle_yookassa_webhook()
    3. Задать переменные окружения: YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY

Redis-ключи:
  flight_bot:plan:{user_id}             — JSON с данными активного тарифа
  flight_bot:payment_pending:{pay_id}   — TTL 24ч, данные ожидающего платежа
  flight_bot:billing_waitlist:{plan}    — set user_id, ждут включения оплаты
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import time
import uuid
import logging
import os
from datetime import datetime
from typing import Optional

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from utils.redis_client import redis_client
from utils.logger import logger

router = Router()


# ══════════════════════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ ТАРИФОВ
#  ↓ Меняй цены и лимиты только здесь ↓
# ══════════════════════════════════════════════════════════════════════════════

PLANS: dict[str, dict] = {
    "free": {
        "label":            "🆓 Бесплатный",
        "price_rub":        0,
        "hot_limit":        3,   # лимит «Горячих» подписок  (0 = безлимит)
        "digest_limit":     3,   # лимит «Дайджест» подписок (0 = безлимит)
        "flystack_tokens":  0,   # токены FlyStack, начисляются при активации
        "priority":         False,
        "badge":            "",
    },
    "plus": {
        "label":            "⚡️ Плюс",
        "price_rub":        50,
        "hot_limit":        10,
        "digest_limit":     10,
        "flystack_tokens":  0,
        "priority":         True,
        "badge":            "⚡️",
    },
    "premium": {
        "label":            "💎 Премиум",
        "price_rub":        150,
        "hot_limit":        0,   # безлимит
        "digest_limit":     0,
        "flystack_tokens":  20,
        "priority":         True,
        "badge":            "💎",
    },
}

# Сколько дней активен оплаченный тариф
PLAN_DURATION_DAYS = 30

# TTL ключа в Redis (с запасом +5 дней на случай просроченных планов)
_PLAN_TTL = (PLAN_DURATION_DAYS + 5) * 86400


# ══════════════════════════════════════════════════════════════════════════════
#  VIP — безлимитные пользователи (без оплаты)
#  Задаётся через переменную окружения VIP_USERNAMES
#  Формат: через запятую, без @  →  VIP_USERNAMES=virmayer,meyer_ira,другой_ник
# ══════════════════════════════════════════════════════════════════════════════

def _load_vip_usernames() -> frozenset[str]:
    """Читает VIP_USERNAMES из env, возвращает frozenset lowercase-юзернеймов."""
    raw = os.getenv("VIP_USERNAMES", "")
    names = {n.strip().lstrip("@").lower() for n in raw.split(",") if n.strip()}
    if names:
        logger.info(f"[Billing] VIP-пользователи загружены: {len(names)} чел.")
    return frozenset(names)

# Загружаем один раз при старте; для обновления без рестарта используй is_vip()
_VIP_USERNAMES: frozenset[str] = _load_vip_usernames()


def is_vip(username: Optional[str]) -> bool:
    """True если @username есть в VIP-списке."""
    if not username:
        return False
    return username.lower().lstrip("@") in _VIP_USERNAMES


# ══════════════════════════════════════════════════════════════════════════════
#  Работа с планами пользователей
# ══════════════════════════════════════════════════════════════════════════════

def _empty_plan() -> dict:
    return {"plan": "free", "expires_at": 0, "paid_at": 0, "payment_id": ""}


async def get_user_plan(user_id: int, username: Optional[str] = None) -> dict:
    """
    Возвращает актуальный план.
    VIP-пользователи всегда получают plan="vip" (безлимит, без срока).
    Если платный план истёк — тихо понижает до free и сохраняет.
    """
    # VIP — проверяем по username
    if is_vip(username):
        return {"plan": "vip", "expires_at": 0, "paid_at": 0, "payment_id": ""}

    if not redis_client.client:
        return _empty_plan()

    raw = await redis_client.client.get(f"{redis_client.prefix}plan:{user_id}")
    if not raw:
        return _empty_plan()

    try:
        plan = json.loads(raw)
    except Exception:
        return _empty_plan()

    # Автопонижение при истечении срока
    if plan.get("plan", "free") != "free":
        expires = plan.get("expires_at", 0)
        if expires and time.time() > expires:
            logger.info(f"[Billing] user={user_id}: план истёк, понижение до free")
            plan = _empty_plan()
            await _persist_plan(user_id, plan)

    return plan


async def activate_plan(user_id: int, plan_key: str, payment_id: str = "") -> dict:
    """
    Активирует тариф на PLAN_DURATION_DAYS дней.
    Начисляет токены FlyStack если предусмотрены.
    Возвращает сохранённый план.
    """
    cfg = PLANS.get(plan_key, PLANS["free"])
    now = int(time.time())
    plan = {
        "plan":       plan_key,
        "expires_at": 0 if plan_key == "free" else now + PLAN_DURATION_DAYS * 86400,
        "paid_at":    now if plan_key != "free" else 0,
        "payment_id": payment_id,
    }
    await _persist_plan(user_id, plan)

    if cfg["flystack_tokens"] > 0:
        await _credit_flystack(user_id, cfg["flystack_tokens"])
        logger.info(f"[Billing] user={user_id}: начислено {cfg['flystack_tokens']} FlyStack-токенов")

    logger.info(f"[Billing] user={user_id}: активирован план={plan_key} до {plan['expires_at']}")
    return plan


async def _persist_plan(user_id: int, plan: dict):
    if redis_client.client:
        await redis_client.client.set(
            f"{redis_client.prefix}plan:{user_id}",
            json.dumps(plan),
            ex=_PLAN_TTL,
        )


async def get_flystack_balance(user_id: int) -> int:
    """Текущий остаток FlyStack-токенов."""
    if not redis_client.client:
        return 0
    raw = await redis_client.client.get(f"{redis_client.prefix}fs_tokens:{user_id}")
    return int(raw) if raw else 0


async def _credit_flystack(user_id: int, amount: int):
    if not redis_client.client:
        return
    key = f"{redis_client.prefix}fs_tokens:{user_id}"
    await redis_client.client.incrby(key, amount)
    await redis_client.client.expire(key, 86400 * 60)


# ══════════════════════════════════════════════════════════════════════════════
#  Проверка лимитов (вызывается из hot_deals.py)
# ══════════════════════════════════════════════════════════════════════════════

async def can_add_sub(user_id: int, sub_type: str, username: Optional[str] = None) -> tuple[bool, str]:
    """
    Проверяет, может ли пользователь создать ещё одну подписку.

    sub_type: "hot" | "digest"

    Возвращает:
        (True,  "")           — можно создавать
        (False, reason_html)  — нельзя, reason содержит HTML для показа юзеру
    """
    # VIP — всегда безлимит
    if is_vip(username):
        return True, ""

    plan_data = await get_user_plan(user_id)
    plan_key  = plan_data.get("plan", "free")
    cfg       = PLANS[plan_key]

    limit = cfg["hot_limit"] if sub_type == "hot" else cfg["digest_limit"]
    if limit == 0:
        return True, ""   # безлимит

    subs    = await redis_client.get_hot_subs(user_id)
    current = sum(1 for s in subs.values() if s.get("sub_type") == sub_type)

    if current < limit:
        return True, ""

    # Формируем текст в зависимости от текущего плана
    type_label = "горячих" if sub_type == "hot" else "дайджест"
    if plan_key == "free":
        reason = (
            f"На <b>бесплатном тарифе</b> доступно <b>{limit} {type_label} подписки</b>.\n\n"
            f"Переходи на тариф <b>Плюс</b> (10 подписок, 50\u202f₽/мес) "
            f"или <b>Премиум</b> (безлимит, 150\u202f₽/мес)."
        )
    elif plan_key == "plus":
        reason = (
            f"На тарифе <b>Плюс</b> доступно <b>{limit} {type_label} подписок</b>.\n\n"
            f"Переходи на <b>Премиум</b> для безлимитных подписок (150\u202f₽/мес)."
        )
    else:
        reason = f"Достигнут лимит подписок: {limit} шт."

    return False, reason


# ══════════════════════════════════════════════════════════════════════════════
#  ЮКасса — готова к подключению
# ══════════════════════════════════════════════════════════════════════════════

async def create_payment(user_id: int, plan_key: str) -> Optional[str]:
    """
    Создаёт платёж в ЮКассе и возвращает URL для оплаты.
    Сейчас — заглушка, возвращает None.

    ── Активация после получения реквизитов ──────────────────────────────────
    Установить: pip install yookassa
    Задать env: YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, BOT_USERNAME

    Раскомментировать блок ниже:
    ──────────────────────────────────────────────────────────────────────────
    import yookassa
    cfg = PLANS.get(plan_key)
    if not cfg or cfg["price_rub"] == 0:
        return None
    yookassa.Configuration.configure(
        os.getenv("YOOKASSA_SHOP_ID"),
        os.getenv("YOOKASSA_SECRET_KEY"),
    )
    pay_id = str(uuid.uuid4())
    payment = yookassa.Payment.create({
        "amount":       {"value": f"{cfg['price_rub']}.00", "currency": "RUB"},
        "confirmation": {
            "type": "redirect",
            "return_url": f"https://t.me/{os.getenv('BOT_USERNAME', 'your_bot')}",
        },
        "capture":     True,
        "description": f"FlightBot {cfg['label']} — user {user_id}",
        "metadata":    {"user_id": str(user_id), "plan": plan_key},
    }, pay_id)
    if payment.status in ("pending", "waiting_for_capture"):
        await redis_client.client.set(
            f"{redis_client.prefix}payment_pending:{pay_id}",
            json.dumps({"user_id": user_id, "plan": plan_key}),
            ex=86400,
        )
        return payment.confirmation.confirmation_url
    ──────────────────────────────────────────────────────────────────────────
    """
    return None   # заглушка


async def handle_yookassa_webhook(payment_id: str, status: str) -> bool:
    """
    Обрабатывает webhook от ЮКассы (успешная оплата).
    Вызывается из webhook-роута (FastAPI/aiohttp) вашего сервера.

    Возвращает True если платёж успешно зачтён.
    """
    if not redis_client.client:
        return False

    raw = await redis_client.client.get(
        f"{redis_client.prefix}payment_pending:{payment_id}"
    )
    if not raw:
        logger.warning(f"[Billing] webhook: платёж {payment_id} не найден")
        return False

    try:
        data = json.loads(raw)
    except Exception:
        return False

    if status != "succeeded":
        return False

    user_id  = int(data["user_id"])
    plan_key = data["plan"]
    await activate_plan(user_id, plan_key, payment_id=payment_id)
    await redis_client.client.delete(f"{redis_client.prefix}payment_pending:{payment_id}")

    # Уведомляем пользователя об успешной оплате
    try:
        import utils.bot_instance as _bi
        if _bi.bot:
            cfg = PLANS[plan_key]
            tok_line = (
                f"\n🎯 Начислено <b>{cfg['flystack_tokens']} токенов FlyStack</b>"
                if cfg["flystack_tokens"] else ""
            )
            await _bi.bot.send_message(
                user_id,
                f"✅ <b>Оплата прошла!</b>\n\n"
                f"Активирован тариф <b>{cfg['label']}</b> на {PLAN_DURATION_DAYS} дней."
                + tok_line + "\n\nВсе возможности тарифа уже доступны 🚀",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Мой тариф", callback_data="billing_status")],
                    [InlineKeyboardButton(text="↩️ В начало",  callback_data="main_menu")],
                ]),
            )
    except Exception as e:
        logger.error(f"[Billing] не удалось уведомить user={user_id}: {e}")

    logger.info(f"[Billing] ✅ payment={payment_id} user={user_id} plan={plan_key}")
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  UI — вспомогательные функции
# ══════════════════════════════════════════════════════════════════════════════

async def _plans_text(user_id: int, username: Optional[str] = None) -> str:
    plan_data = await get_user_plan(user_id, username)
    current   = plan_data.get("plan", "free")
    fs_bal    = await get_flystack_balance(user_id)
    nb        = "\u202f"

    # VIP — особый экран
    if current == "vip":
        lines = [
            "👑 <b>Тариф: VIP</b>\n",
            "• Горячих подписок: <b>∞</b>",
            "• Дайджест: <b>∞</b>",
            "• Приоритет уведомлений: ✅",
            "• FlyStack: ✅",
        ]
        if fs_bal > 0:
            lines.append(f"🎯 Баланс FlyStack: <b>{fs_bal} токенов</b>")
        return "\n".join(lines)

    lines = ["💳 <b>Тарифы FlightBot</b>\n"]

    for key, cfg in PLANS.items():
        is_active = key == current
        mark      = " ◀ текущий" if is_active else ""
        price_str = "бесплатно" if cfg["price_rub"] == 0 else f"{cfg['price_rub']}{nb}₽/мес"
        hot_str   = "∞" if cfg["hot_limit"]    == 0 else str(cfg["hot_limit"])
        dig_str   = "∞" if cfg["digest_limit"] == 0 else str(cfg["digest_limit"])
        fs_str    = f"{cfg['flystack_tokens']} токенов" if cfg["flystack_tokens"] else "—"
        prio_str  = "✅" if cfg["priority"] else "—"

        lines.append(
            f"<b>{cfg['label']}</b>  <i>{price_str}</i>{mark}\n"
            f"  • Горячих подписок: <b>{hot_str}</b>\n"
            f"  • Дайджест: <b>{dig_str}</b>\n"
            f"  • FlyStack: <b>{fs_str}</b>\n"
            f"  • Приоритет уведомлений: {prio_str}\n"
        )

    if current != "free":
        expires = plan_data.get("expires_at", 0)
        if expires:
            exp_str = datetime.fromtimestamp(expires).strftime("%d.%m.%Y")
            lines.append(f"\n📅 Тариф активен до: <b>{exp_str}</b>")

    if fs_bal > 0:
        lines.append(f"🎯 Баланс FlyStack: <b>{fs_bal} токенов</b>")

    lines.append("\n<i>Оплата через ЮКассу — безопасно и быстро.</i>")
    return "\n".join(lines)


def _plans_kb(current_plan: str) -> InlineKeyboardMarkup:
    # VIP не видит кнопки покупки
    if current_plan == "vip":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
        ])
    rows = []
    for key, cfg in PLANS.items():
        if key == "free":
            continue
        mark = "✅ " if key == current_plan else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{cfg['label']} — {cfg['price_rub']}\u202f₽/мес",
            callback_data=f"billing_buy:{key}",
        )])
    rows.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ══════════════════════════════════════════════════════════════════════════════
#  Хендлеры
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "billing_menu")
async def billing_menu(callback: CallbackQuery):
    """Экран выбора тарифа."""
    user_id  = callback.from_user.id
    username = callback.from_user.username
    plan     = await get_user_plan(user_id, username)
    text     = await _plans_text(user_id, username)
    kb       = _plans_kb(plan.get("plan", "free"))
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("billing_buy:"))
async def billing_buy(callback: CallbackQuery):
    """Нажали 'купить тариф'."""
    user_id  = callback.from_user.id
    username = callback.from_user.username
    plan_key = callback.data.split(":", 1)[1]
    cfg      = PLANS.get(plan_key)

    if not cfg:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    # VIP не должен попадать сюда, но на всякий случай
    if is_vip(username):
        await callback.answer("У тебя уже безлимитный доступ 👑", show_alert=True)
        return

    current = await get_user_plan(user_id, username)
    if current.get("plan") == plan_key:
        await callback.answer("У тебя уже активен этот тариф!", show_alert=True)
        return

    pay_url = await create_payment(user_id, plan_key)

    if pay_url:
        # ЮКасса подключена — отдаём ссылку
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=pay_url)],
            [InlineKeyboardButton(text="↩️ Назад к тарифам", callback_data="billing_menu")],
        ])
        await callback.message.edit_text(
            f"💳 <b>Оплата тарифа {cfg['label']}</b>\n\n"
            f"Сумма: <b>{cfg['price_rub']}\u202f₽/мес</b>\n\n"
            "После оплаты тариф активируется автоматически.",
            parse_mode="HTML", reply_markup=kb,
        )
    else:
        # Заглушка: оплата ещё не подключена
        if redis_client.client:
            await redis_client.client.sadd(
                f"{redis_client.prefix}billing_waitlist:{plan_key}",
                str(user_id),
            )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Уведомить меня", callback_data=f"billing_notify:{plan_key}")],
            [InlineKeyboardButton(text="↩️ Назад к тарифам", callback_data="billing_menu")],
        ])
        await callback.message.edit_text(
            f"⏳ <b>Оплата пока недоступна</b>\n\n"
            f"Тариф <b>{cfg['label']}</b> — {cfg['price_rub']}\u202f₽/мес\n\n"
            "Подключаем платёжную систему ЮКасса.\n"
            "Как только оплата появится — сразу сообщим!\n\n"
            "<i>Нажми «Уведомить меня» и получишь сообщение в первый же день.</i>",
            parse_mode="HTML", reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("billing_notify:"))
async def billing_notify(callback: CallbackQuery):
    """Запись в лист ожидания."""
    plan_key = callback.data.split(":", 1)[1]
    user_id  = callback.from_user.id
    if redis_client.client:
        await redis_client.client.sadd(
            f"{redis_client.prefix}billing_waitlist:{plan_key}",
            str(user_id),
        )
    await callback.answer("✅ Запомнили! Уведомим, как только откроется оплата.", show_alert=True)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Вы в списке ожидания", callback_data="billing_menu")],
                [InlineKeyboardButton(text="↩️ Назад к тарифам",     callback_data="billing_menu")],
            ])
        )
    except Exception:
        pass


@router.callback_query(F.data == "billing_status")
async def billing_status(callback: CallbackQuery):
    """Карточка текущего тарифа."""
    user_id  = callback.from_user.id
    username = callback.from_user.username
    plan_d   = await get_user_plan(user_id, username)
    plan_key = plan_d.get("plan", "free")
    fs_bal   = await get_flystack_balance(user_id)

    if plan_key == "vip":
        lines = [
            "👑 <b>Твой тариф: VIP</b>\n",
            "• Горячих подписок: <b>∞</b>",
            "• Дайджест: <b>∞</b>",
            "• Приоритет уведомлений: ✅",
            f"• Баланс FlyStack: <b>{fs_bal} токенов</b>",
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
        ])
    else:
        cfg     = PLANS[plan_key]
        hot_str = "∞" if cfg["hot_limit"]    == 0 else str(cfg["hot_limit"])
        dig_str = "∞" if cfg["digest_limit"] == 0 else str(cfg["digest_limit"])
        lines = [
            f"👤 <b>Твой тариф: {cfg['label']}</b>\n",
            f"• Горячих подписок: <b>{hot_str}</b>",
            f"• Дайджест: <b>{dig_str}</b>",
            f"• Приоритет уведомлений: {'✅' if cfg['priority'] else '—'}",
            f"• Баланс FlyStack: <b>{fs_bal} токенов</b>",
        ]
        if plan_key != "free":
            expires = plan_d.get("expires_at", 0)
            if expires:
                exp_str = datetime.fromtimestamp(expires).strftime("%d.%m.%Y")
                lines.append(f"\n📅 Тариф активен до: <b>{exp_str}</b>")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Изменить тариф", callback_data="billing_menu")],
            [InlineKeyboardButton(text="↩️ В начало",       callback_data="main_menu")],
        ])

    try:
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  Paywall — показывается когда лимит исчерпан (вызывается из hot_deals.py)
# ══════════════════════════════════════════════════════════════════════════════

async def show_paywall(callback: CallbackQuery, reason_html: str):
    """
    Показывает экран «лимит подписок» с предложением сменить тариф.
    """
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Посмотреть тарифы", callback_data="billing_menu")],
        [InlineKeyboardButton(text="↩️ Назад",             callback_data="subs_menu")],
    ])
    try:
        await callback.message.edit_text(
            f"🔒 <b>Лимит подписок исчерпан</b>\n\n{reason_html}",
            parse_mode="HTML", reply_markup=kb,
        )
    except Exception:
        await callback.message.answer(
            f"🔒 <b>Лимит подписок исчерпан</b>\n\n{reason_html}",
            parse_mode="HTML", reply_markup=kb,
        )
    await callback.answer()