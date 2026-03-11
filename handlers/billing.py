# handlers/billing.py
"""
Система тарифов WOW Bilet.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ТАРИФЫ (меняй только словарь PLANS):
  free    — бесплатно: 5 горячих + 3 дайджест + 3 слежения, задержка 45 мин
  plus    — 149 руб/мес: 20 горячих + 10 дайджест + 15 слежений, мгновенно
  premium — 349 руб/мес: безлимит + 20 токенов FlyStack, мгновенно
  vip     — служебный, не отображается обычным пользователям

Redis-ключи:
  flight_bot:plan:{user_id}           — JSON с данными активного тарифа
  flight_bot:payment_pending:{pay_id} — TTL 24ч, ожидающий платёж
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import json
import os
import time
from datetime import datetime
from typing import Optional

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
)

from utils.redis_client import redis_client
from utils.logger import logger

router = Router()

# Токен провайдера ЮКасса из BotFather (Payments → ЮКасса Live)
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip()

# ══════════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ ТАРИФОВ  ← меняй только здесь
# ══════════════════════════════════════════════════════════════════

PLANS: dict[str, dict] = {
    "free": {
        "label":           "Бесплатный",
        "emoji":           "🆓",
        "price_rub":       0,
        "hot_limit":       5,    # горячих подписок
        "digest_limit":    3,    # дайджест-подписок
        "watch_limit":     3,    # слежений за ценой
        "flystack_tokens": 0,
        "priority":        False,
        "priority_delay":  45 * 60,  # 45 минут задержки
        "multi_origin":    False,
        "multi_month":     False,
    },
    "plus": {
        "label":           "Плюс",
        "emoji":           "⚡️",
        "price_rub":       149,
        "hot_limit":       20,
        "digest_limit":    10,
        "watch_limit":     15,
        "flystack_tokens": 0,
        "priority":        True,
        "priority_delay":  0,
        "multi_origin":    True,
        "multi_month":     True,
    },
    "premium": {
        "label":           "Премиум",
        "emoji":           "💎",
        "price_rub":       349,
        "hot_limit":       0,    # 0 = безлимит
        "digest_limit":    0,
        "watch_limit":     0,
        "flystack_tokens": 20,
        "priority":        True,
        "priority_delay":  0,
        "multi_origin":    True,
        "multi_month":     True,
    },
    # Служебный — не отображается в меню
    "vip": {
        "label":           "VIP",
        "emoji":           "👑",
        "price_rub":       0,
        "hot_limit":       0,
        "digest_limit":    0,
        "watch_limit":     0,
        "flystack_tokens": 0,
        "priority":        True,
        "priority_delay":  0,
        "multi_origin":    True,
        "multi_month":     True,
    },
}

PLAN_DURATION_DAYS = 30
_PLAN_TTL = (PLAN_DURATION_DAYS + 5) * 86400


# ══════════════════════════════════════════════════════════════════
#  VIP — безлимит из env, без оплаты
#  VIP_USERNAMES=virmayer,meyer_ira   (через запятую, без @)
# ══════════════════════════════════════════════════════════════════

def _load_vip_usernames() -> frozenset:
    raw = os.getenv("VIP_USERNAMES", "")
    names = {n.strip().lstrip("@").lower() for n in raw.split(",") if n.strip()}
    if names:
        logger.info(f"[Billing] VIP загружены: {len(names)} чел.")
    return frozenset(names)


_VIP_USERNAMES: frozenset = _load_vip_usernames()


def is_vip(username: Optional[str]) -> bool:
    if not username:
        return False
    return username.lower().lstrip("@") in _VIP_USERNAMES


# ══════════════════════════════════════════════════════════════════
#  Работа с планами
# ══════════════════════════════════════════════════════════════════

def _empty_plan() -> dict:
    return {"plan": "free", "expires_at": 0, "paid_at": 0, "payment_id": ""}


async def get_user_plan(user_id: int, username: Optional[str] = None) -> dict:
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
            logger.info(f"[Billing] user={user_id}: план истёк → free")
            plan = _empty_plan()
            await _persist_plan(user_id, plan)

    return plan


async def _persist_plan(user_id: int, plan: dict):
    if redis_client.client:
        await redis_client.client.set(
            f"{redis_client.prefix}plan:{user_id}",
            json.dumps(plan),
            ex=_PLAN_TTL,
        )


async def get_flystack_balance(user_id: int) -> int:
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


# ══════════════════════════════════════════════════════════════════
#  Проверка лимитов  (вызывается из hot_deals.py и search_results.py)
# ══════════════════════════════════════════════════════════════════

async def can_add_sub(
    user_id: int,
    sub_type: str,           # "hot" | "digest" | "watch"
    username: Optional[str] = None,
) -> tuple:
    """
    Возвращает (True, "") — можно добавить,
    или (False, reason_html) — лимит исчерпан.
    """
    if is_vip(username):
        return True, ""

    plan_data = await get_user_plan(user_id)
    plan_key  = plan_data.get("plan", "free")
    cfg       = PLANS.get(plan_key) or PLANS["free"]

    if sub_type == "hot":
        limit = cfg["hot_limit"]
    elif sub_type == "digest":
        limit = cfg["digest_limit"]
    else:  # watch
        limit = cfg["watch_limit"]

    if limit == 0:
        return True, ""   # безлимит

    # Считаем текущее количество
    if sub_type == "watch":
        watches = await redis_client.get_user_watches(user_id)
        current = len(watches)
    else:
        subs    = await redis_client.get_hot_subs(user_id)
        current = sum(1 for s in subs.values() if s.get("sub_type") == sub_type)

    if current < limit:
        return True, ""

    type_labels = {
        "hot":    "горячих предложений",
        "digest": "дайджест-подписок",
        "watch":  "слежений за ценой",
    }
    tl = type_labels.get(sub_type, sub_type)

    if plan_key == "free":
        reason = (
            f"Использовано <b>{current} из {limit}</b> {tl} на бесплатном тарифе.\n\n"
            f"Хочешь больше?\n"
            f"⚡️ <b>Плюс</b> — до 10 каждого типа · 149\u202f\u20bd/мес\n"
            f"💎 <b>Премиум</b> — безлимит + FlyStack · 349\u202f\u20bd/мес"
        )
    elif plan_key == "plus":
        reason = (
            f"Использовано <b>{current} из {limit}</b> {tl} на тарифе Плюс.\n\n"
            f"💎 <b>Премиум</b> снимает все ограничения — безлимит · 349\u202f\u20bd/мес"
        )
    else:
        reason = f"Достигнут лимит: {limit} шт."

    return False, reason


# ══════════════════════════════════════════════════════════════════
#  UI — текст и клавиатура меню тарифов
# ══════════════════════════════════════════════════════════════════

def _lim(n: int) -> str:
    """0 → ∞, иначе число."""
    return "\u221e" if n == 0 else str(n)


async def _plans_text(user_id: int, username: Optional[str] = None) -> str:
    plan_data = await get_user_plan(user_id, username)
    current   = plan_data.get("plan", "free")
    nb        = "\u202f"   # узкий неразрывный пробел

    # ── VIP видит только свой тариф ─────────────────────────────
    if current == "vip":
        fs_bal = await get_flystack_balance(user_id)
        lines  = ["<b>Твой тариф: VIP 👑</b>\n"]
        lines += [
            "  · Горячие предложения: <b>\u221e</b>",
            "  · Дайджест: <b>\u221e</b>",
            "  · Слежение за ценой: <b>\u221e</b>",
        ]
        if fs_bal > 0:
            lines.append(f"\n\U0001f3af Баланс FlyStack: <b>{fs_bal} токенов</b>")
        return "\n".join(lines)

    # ── Обычный экран тарифов ────────────────────────────────────
    lines = ["<b>Тарифы WOW Bilet</b>\n"]

    for key, cfg in PLANS.items():
        if key == "vip":          # VIP скрыт от всех остальных
            continue

        is_active = key == current
        emoji     = cfg["emoji"]
        label     = cfg["label"]

        if cfg["price_rub"] == 0:
            price_str = "бесплатно"
        else:
            price_str = f"{cfg['price_rub']}{nb}\u20bd/мес"

        # Заголовок: активный выделен иначе
        if is_active:
            header = f"{emoji} <b>{label}</b>  <i>{price_str}</i>  <b>\u2713 ваш тариф</b>"
        else:
            header = f"{emoji} <b>{label}</b>  <i>{price_str}</i>"

        block  = f"{header}\n"
        block += f"  · Горячие предложения: <b>{_lim(cfg['hot_limit'])}</b>\n"
        block += f"  · Дайджест: <b>{_lim(cfg['digest_limit'])}</b>\n"
        block += f"  · Слежение за ценой: <b>{_lim(cfg['watch_limit'])}</b>\n"
        delay = cfg.get("priority_delay", 0)
        if cfg.get("priority") or delay == 0:
            prio = "⚡️ мгновенно"
        else:
            prio = f"⏱ +{delay // 60} мин"
        block += f"  · Приоритет уведомлений: <b>{prio}</b>\n"
        multi = "✅" if cfg.get("multi_origin") else "—"
        block += f"  · Мульти-поиск городов и дат <b>{multi}</b>\n"

        # FlyStack — только в Премиуме
        if key == "premium":
            block += f"  · FlyStack токены: <b>{cfg['flystack_tokens']} шт.</b>\n"

        lines.append(block)

    # Срок действия платного тарифа
    if current not in ("free", "vip"):
        expires = plan_data.get("expires_at", 0)
        if expires:
            exp_str = datetime.fromtimestamp(expires).strftime("%d.%m.%Y")
            lines.append(f"📅 Тариф активен до: <b>{exp_str}</b>")

    # Баланс FlyStack (если есть)
    fs_bal = await get_flystack_balance(user_id)
    if fs_bal > 0:
        lines.append(f"\U0001f3af Баланс FlyStack: <b>{fs_bal} токенов</b>")

    return "\n".join(lines)


def _plans_kb(current_plan: str) -> InlineKeyboardMarkup:
    # VIP — только кнопка «назад»
    if current_plan == "vip":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
        ])

    rows = []
    for key, cfg in PLANS.items():
        if key in ("free", "vip"):
            continue
        prefix = "✅ " if key == current_plan else ""
        rows.append([InlineKeyboardButton(
            text=f"{prefix}{cfg['emoji']} {cfg['label']} — {cfg['price_rub']}\u202f\u20bd/мес",
            callback_data=f"billing_buy:{key}",
        )])
    rows.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ══════════════════════════════════════════════════════════════════
#  Хендлеры
# ══════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "billing_menu")
async def billing_menu(callback: CallbackQuery):
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
    """Отправляем инвойс ЮКасса через Telegram Payments."""
    user_id  = callback.from_user.id
    username = callback.from_user.username
    plan_key = callback.data.split(":", 1)[1]
    cfg      = PLANS.get(plan_key)

    if not cfg or plan_key in ("free", "vip"):
        await callback.answer("Тариф не найден", show_alert=True)
        return

    if is_vip(username):
        await callback.answer("У тебя уже безлимитный доступ 👑", show_alert=True)
        return

    current = await get_user_plan(user_id, username)
    if current.get("plan") == plan_key:
        await callback.answer("У тебя уже активен этот тариф!", show_alert=True)
        return

    if not PAYMENT_PROVIDER_TOKEN:
        await callback.answer("Оплата временно недоступна", show_alert=True)
        logger.error("[Billing] PAYMENT_PROVIDER_TOKEN не задан")
        return

    await callback.answer()

    price_kopecks = cfg["price_rub"] * 100

    benefits = {
        "plus": (
            "✅ До 20 горячих предложений\n"
            "✅ До 10 дайджест-подписок\n"
            "✅ До 15 слежений за ценой\n"
            "✅ Мгновенные уведомления\n"
            "✅ Мульти-поиск по городам и датам"
        ),
        "premium": (
            "✅ Безлимитные подписки\n"
            "✅ 20 токенов FlyStack\n"
            "✅ Мгновенные уведомления\n"
            "✅ Мульти-поиск по городам и датам\n"
            "✅ Все возможности без ограничений"
        ),
    }
    description = benefits.get(plan_key, f"Тариф {cfg['label']} на 30 дней")

    try:
        await callback.message.answer_invoice(
            title=f"{cfg['emoji']} WOW Bilet {cfg['label']}",
            description=description,
            payload=f"{plan_key}:{user_id}",
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(label=f"Тариф {cfg['label']} (30 дней)", amount=price_kopecks)],
            need_email=False,
            need_phone_number=False,
            need_shipping_address=False,
            is_flexible=False,
        )
    except Exception as e:
        logger.error(f"[Billing] Ошибка отправки инвойса: {e}")
        await callback.message.answer(
            "❌ Не удалось создать платёж. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Назад к тарифам", callback_data="billing_menu")],
            ]),
        )


# ══════════════════════════════════════════════════════════════════
#  Telegram Payments: pre_checkout и successful_payment
# ══════════════════════════════════════════════════════════════════

@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout: PreCheckoutQuery):
    """Отвечаем ok в течение 10 секунд — иначе платёж отменится."""
    try:
        parts = pre_checkout.invoice_payload.split(":", 1)
        if len(parts) != 2 or parts[0] not in PLANS:
            await pre_checkout.answer(ok=False, error_message="Неверный платёж. Попробуй снова.")
            return
        pay_id  = str(pre_checkout.id)
        already = await redis_client.client.get(f"{redis_client.prefix}payment_done:{pay_id}")
        if already:
            await pre_checkout.answer(ok=False, error_message="Этот платёж уже обработан.")
            return
        await pre_checkout.answer(ok=True)
        logger.info(f"[Billing] pre_checkout OK payload={pre_checkout.invoice_payload}")
    except Exception as e:
        logger.error(f"[Billing] pre_checkout error: {e}")
        await pre_checkout.answer(ok=False, error_message="Внутренняя ошибка. Попробуй позже.")


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    """После успешного списания — активируем тариф."""
    payment  = message.successful_payment
    payload  = payment.invoice_payload
    pay_id   = payment.telegram_payment_charge_id
    amount   = payment.total_amount // 100
    user_id  = message.from_user.id

    try:
        plan_key = payload.split(":", 1)[0]
        cfg      = PLANS.get(plan_key)
        if not cfg:
            logger.error(f"[Billing] unknown plan in payload: {payload}")
            await message.answer("⚠️ Платёж получен, но план не распознан. Напиши в поддержку.")
            return

        # Idempotency — защита от дублей
        idem_key = f"{redis_client.prefix}payment_done:{pay_id}"
        already  = await redis_client.client.get(idem_key)
        if already:
            logger.warning(f"[Billing] дубликат платежа {pay_id}")
            return
        await redis_client.client.set(idem_key, "1", ex=90 * 86400)

        # Активируем тариф
        now     = int(time.time())
        expires = now + PLAN_DURATION_DAYS * 86400
        await _persist_plan(user_id, {
            "plan":       plan_key,
            "expires_at": expires,
            "paid_at":    now,
            "payment_id": pay_id,
            "amount_rub": amount,
        })

        # Начисляем FlyStack токены
        fs_tokens = cfg.get("flystack_tokens", 0)
        if fs_tokens > 0:
            await _credit_flystack(user_id, fs_tokens)

        exp_str = datetime.fromtimestamp(expires).strftime("%d.%m.%Y")
        fs_line = f"\n🎯 Начислено <b>{fs_tokens} токенов FlyStack</b>" if fs_tokens else ""
        nb = "\u202f"

        logger.info(f"[Billing] ✅ user={user_id} план={plan_key} pay_id={pay_id} сумма={amount}₽")

        await message.answer(
            f"✅ <b>Оплата прошла успешно!</b>\n\n"
            f"{cfg['emoji']} Тариф <b>{cfg['label']}</b> активирован.\n"
            f"📅 Действует до: <b>{exp_str}</b>\n"
            f"💰 Списано: <b>{amount}\u202f\u20bd</b>"
            f"{fs_line}\n\n"
            "Спасибо, что выбрал WOW Bilet! ✈️",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои подписки", callback_data="subs_menu")],
                [InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")],
            ]),
        )
    except Exception as e:
        logger.error(f"[Billing] successful_payment error: {e}")
        await message.answer("✅ Платёж получен, но возникла ошибка активации. Напиши нам — разберёмся.")


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
            "<b>Твой тариф: VIP 👑</b>\n",
            "  · Горячие предложения: <b>\u221e</b>",
            "  · Дайджест: <b>\u221e</b>",
            "  · Слежение за ценой: <b>\u221e</b>",
        ]
        if fs_bal > 0:
            lines.append(f"\n\U0001f3af Баланс FlyStack: <b>{fs_bal} токенов</b>")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
        ])
    else:
        cfg   = PLANS.get(plan_key) or PLANS["free"]
        _prio = "⚡️ мгновенно" if cfg.get("priority") else "⏰ +30 мин"
        lines = [
            f"{cfg['emoji']} <b>Твой тариф: {cfg['label']}</b>\n",
            f"  · Горячие предложения: <b>{_lim(cfg['hot_limit'])}</b>",
            f"  · Дайджест: <b>{_lim(cfg['digest_limit'])}</b>",
            f"  · Слежение за ценой: <b>{_lim(cfg['watch_limit'])}</b>",
            f"  · Приоритет уведомлений: <b>{_prio}</b>",
        ]
        if plan_key == "premium" and fs_bal > 0:
            lines.append(f"\U0001f3af Баланс FlyStack: <b>{fs_bal} токенов</b>")
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


# ══════════════════════════════════════════════════════════════════
#  Paywall — показывается когда лимит исчерпан
# ══════════════════════════════════════════════════════════════════

async def show_paywall(callback: CallbackQuery, reason_html: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Посмотреть тарифы", callback_data="billing_menu")],
        [InlineKeyboardButton(text="↩️ Назад",             callback_data="subs_menu")],
    ])
    text = f"🔒 <b>Лимит исчерпан</b>\n\n{reason_html}"
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()