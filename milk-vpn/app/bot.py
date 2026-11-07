"""Telegram-бот на aiogram 3."""
import asyncio
import html
import logging
from datetime import timedelta
from urllib.parse import quote

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from app import db, fmt, services
from app import runtime as rt
from app.config import get_settings
from app.models import Key, Order, Plan, User, utcnow
from app.security import make_login_token
from app.state import state

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")

esc = html.escape

HELP_TEXT = (
    "📖 <b>Как подключить</b>\n\n"
    "1. Установите приложение:\n"
    "• Android — Hiddify или v2rayNG\n"
    "• iOS / macOS — Streisand, V2Box или Hiddify\n"
    "• Windows — Hiddify или Nekoray\n\n"
    "2. Откройте «🔑 Мои ключи», скопируйте <b>ссылку-подписку</b> (или отсканируйте QR-код).\n"
    "3. В приложении выберите «Добавить из буфера обмена» — появятся все локации сразу. Включите VPN."
)


class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        tg_user = data.get("event_from_user")
        if tg_user:
            async with db.Session() as s:
                u = await s.scalar(select(User).where(User.tg_id == tg_user.id))
            if u and u.is_banned:
                if isinstance(event, CallbackQuery):
                    await event.answer("Доступ ограничен", show_alert=True)
                return None
        return await handler(event, data)


router.message.middleware(BanMiddleware())
router.callback_query.middleware(BanMiddleware())


# ---------- клавиатуры и утилиты ----------
def main_menu(trial: bool, ref: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛒 Купить / продлить", callback_data="buy")
    b.button(text="🔑 Мои ключи", callback_data="keys")
    if trial:
        b.button(text="🎁 Пробный период", callback_data="trial")
    if ref:
        b.button(text="👥 Пригласить друга", callback_data="ref")
    b.button(text="👤 Личный кабинет", callback_data="cabinet")
    b.button(text="📖 Как подключить", callback_data="help")
    b.button(text="🛟 Поддержка", callback_data="support")
    b.adjust(1)
    return b.as_markup()


def menu_for(user: User) -> InlineKeyboardMarkup:
    return main_menu(services.trial_available(user), rt.get_bool("ref_enabled"))


def back_kb(extra: list[InlineKeyboardButton] | None = None) -> InlineKeyboardMarkup:
    rows = [[b] for b in (extra or [])]
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb)


async def tg_send(tg_id: int, text: str, kb: InlineKeyboardMarkup | None = None) -> bool:
    if state.bot is None:
        return False
    try:
        await state.bot.send_message(tg_id, text, reply_markup=kb)
        return True
    except TelegramAPIError as e:
        log.info("cannot send to %s: %s", tg_id, e)
        return False


def ref_link(user: User) -> str:
    return f"https://t.me/{state.bot_username}?start=r_{user.ref_code}"


async def key_card(key: Key) -> str:
    async with db.Session() as s:
        servers = await services.key_servers(s, key)
        usage = await services.safe_traffic(s, key)
    status = "⛔ истёк" if key.expired else "✅ активен"
    lines = [
        f"🔑 <b>Ключ #{key.id}</b> — {status}",
        f"Тариф: {esc(key.plan.name) if key.plan else ('пробный' if key.is_trial else '—')}",
        f"Действует до: <b>{fmt.dt_local(key.expires_at)}</b>",
    ]
    if servers:
        lines.append("Локации: " + " ".join(f"{sv.flag} {esc(sv.name)}" for sv in servers))
    if usage:
        used, total = usage
        limit = fmt.human_bytes(total) if total else "∞"
        lines.append(f"Трафик: {fmt.human_bytes(used)} / {limit}")
    lines += ["", "<b>Ссылка-подписка</b> (все локации сразу):", f"<code>{esc(services.sub_url(key))}</code>"]
    return "\n".join(lines)


async def notify_paid(res: services.FulfillResult) -> None:
    """Сообщает пользователю об оплате, а пригласившему — о начисленной награде."""
    key = res.key
    if key is not None:
        head = "✅ Оплата получена! Ключ продлён." if res.renewed else "✅ Оплата получена! Ваш ключ готов."
        if res.invitee_bonus:
            head += f"\n🎁 Бонус за приглашение: +{fmt.plural_days(res.invitee_bonus)}."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔑 Открыть ключ", callback_data=f"key:{key.id}")]]
        )
        await tg_send(res.user.tg_id, f"{head}\n\n{await key_card(key)}", kb)
    if res.inviter and res.reward_days:
        tail = (
            "Они уже добавлены к вашему ключу."
            if res.reward_applied
            else "Они добавятся к вашему ключу при ближайшей покупке или выдаче."
        )
        await tg_send(
            res.inviter.tg_id,
            f"🎉 Ваш друг оплатил подписку! Вам начислено <b>+{fmt.plural_days(res.reward_days)}</b>. {tail}",
        )


# ---------- команды ----------
@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    payload = (command.args or "").strip()
    inviter = None
    async with db.Session() as s:
        user, created = await services.register_user(
            s, m.from_user.id, m.from_user.username, m.from_user.first_name
        )
        if created and payload.startswith("r_"):
            inviter = await services.attach_referrer(s, user, payload[2:][:16])
        await s.commit()
    cfg = get_settings()
    text = f"👋 Привет! Это <b>{esc(cfg.brand_name)}</b> — быстрый и стабильный VPN.\n"
    if user.referrer_id and rt.get_int("ref_invitee_days") > 0:
        text += f"\n🎁 Вы пришли по приглашению: к первой покупке добавим <b>+{fmt.plural_days(rt.get_int('ref_invitee_days'))}</b>.\n"
    await m.answer(text + "\nВыберите действие:", reply_markup=menu_for(user))
    if inviter is not None:
        await tg_send(inviter.tg_id, "👥 По вашей ссылке пришёл новый пользователь. Награда — после его первой оплаты.")


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    cfg = get_settings()
    if m.from_user.id in cfg.admin_ids:
        await m.answer(f"Панель управления: {esc(cfg.public_url)}/admin")


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    if m.from_user.id not in get_settings().admin_ids:
        return
    async with db.Session() as s:
        users = await s.scalar(select(func.count(User.id)))
        keys = await s.scalar(
            select(func.count(Key.id)).where(Key.active.is_(True), Key.expires_at > utcnow())
        )
        paid = await s.scalar(select(func.count(Order.id)).where(Order.status == "paid"))
        revenue = await s.scalar(
            select(func.coalesce(func.sum(Order.amount_kop), 0)).where(Order.status == "paid")
        )
    await m.answer(
        f"👥 Пользователей: {users}\n🔑 Активных ключей: {keys}\n"
        f"💳 Оплаченных заказов: {paid}\n💰 Выручка: {fmt.rub(revenue)} ₽"
    )


# ---------- меню ----------
@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    async with db.Session() as s:
        user = await services.upsert_user(s, cb.from_user.id, cb.from_user.username, cb.from_user.first_name)
        await s.commit()
    await show(cb, "Выберите действие:", menu_for(user))
    await cb.answer()


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await show(cb, HELP_TEXT, back_kb())
    await cb.answer()


@router.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery):
    name = get_settings().support_username.lstrip("@")
    if name:
        kb = back_kb([InlineKeyboardButton(text="Написать в поддержку", url=f"https://t.me/{name}")])
        await show(cb, "🛟 Возникли вопросы? Напишите нам — поможем с подключением и оплатой.", kb)
    else:
        await show(cb, "🛟 Напишите администратору сервиса.", back_kb())
    await cb.answer()


@router.callback_query(F.data == "cabinet")
async def cb_cabinet(cb: CallbackQuery):
    url = f"{get_settings().public_url}/auth?token={make_login_token(cb.from_user.id)}"
    text = "👤 Личный кабинет: ключи, трафик, QR-коды и история платежей. Ссылка действует 10 минут."
    try:
        await cb.message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=url)]]
            ),
        )
    except TelegramBadRequest:  # Telegram не принимает localhost в кнопках
        await cb.message.answer(f"{text}\n\n<code>{esc(url)}</code>")
    await cb.answer()


# ---------- рефералка ----------
@router.callback_query(F.data == "ref")
async def cb_ref(cb: CallbackQuery):
    if not rt.get_bool("ref_enabled"):
        await cb.answer("Реферальная программа сейчас отключена", show_alert=True)
        return
    async with db.Session() as s:
        user = await services.upsert_user(s, cb.from_user.id, cb.from_user.username, cb.from_user.first_name)
        stats = await services.referral_stats(s, user)
        await s.commit()
    link = ref_link(user)
    inviter_days, invitee_days = rt.get_int("ref_inviter_days"), rt.get_int("ref_invitee_days")
    rule = rt.get_bool("ref_first_only")
    lines = ["👥 <b>Приведи друга</b>", ""]
    if inviter_days:
        lines.append(
            f"Вы получаете <b>+{fmt.plural_days(inviter_days)}</b> "
            + ("за первую оплату каждого друга." if rule else "за каждую оплату друга.")
        )
    if invitee_days:
        lines.append(f"Друг получает <b>+{fmt.plural_days(invitee_days)}</b> к первой покупке.")
    limit = rt.get_int("ref_max_rewards")
    if limit:
        lines.append(f"Максимум наград на одного пользователя: {limit}.")
    lines += [
        "",
        "<b>Ваша ссылка:</b>",
        f"<code>{esc(link)}</code>",
        "",
        f"Приглашено: {stats['invited']} · оплатили: {stats['paid']}",
        f"Начислено дней: {stats['days']}",
    ]
    if stats["pending"]:
        lines.append(f"Ждут применения: {stats['pending']} (добавятся к вашему ключу)")
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('Попробуй быстрый VPN — по моей ссылке бонус к первой покупке')}"
    kb = back_kb([InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share)])
    await show(cb, "\n".join(lines), kb)
    await cb.answer()


# ---------- покупка ----------
@router.callback_query(F.data.regexp(r"^buy(:\d+)?$"))
async def cb_buy(cb: CallbackQuery):
    parts = cb.data.split(":")
    key_id = int(parts[1]) if len(parts) > 1 else 0
    async with db.Session() as s:
        plans = await services.active_plans(s)
    if not plans:
        await cb.answer("Тарифы пока не настроены", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for p in plans:
        traffic = f"{p.traffic_gb} ГБ" if p.traffic_gb else "безлимит"
        b.button(text=f"{p.name} · {traffic} · {fmt.rub(p.price_kop)} ₽", callback_data=f"plan:{p.id}:{key_id}")
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(1)
    title = f"🔄 Продление ключа #{key_id}" if key_id else "🛒 Выберите тариф"
    await show(cb, f"{title}\nОплата картой или СБП через Робокассу.", b.as_markup())
    await cb.answer()


@router.callback_query(F.data.regexp(r"^plan:\d+:\d+$"))
async def cb_plan(cb: CallbackQuery):
    _, plan_id, key_id = cb.data.split(":")
    async with db.Session() as s:
        user = await services.upsert_user(s, cb.from_user.id, cb.from_user.username, cb.from_user.first_name)
        plan = await s.get(Plan, int(plan_id))
        if plan is None:
            await cb.answer("Тариф не найден", show_alert=True)
            return
        try:
            order, url = await services.create_order(s, user, plan, int(key_id) or None)
        except services.ServiceError as e:
            await cb.answer(str(e), show_alert=True)
            return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {fmt.rub(order.amount_kop)} ₽", url=url)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="buy")],
        ]
    )
    await show(
        cb,
        f"Заказ <b>№{order.id}</b>\nТариф: {esc(plan.name)}\nК оплате: <b>{fmt.rub(order.amount_kop)} ₽</b>\n\n"
        "После оплаты ключ придёт сюда автоматически.",
        kb,
    )
    await cb.answer()


# ---------- ключи ----------
@router.callback_query(F.data == "keys")
async def cb_keys(cb: CallbackQuery):
    async with db.Session() as s:
        user = await services.upsert_user(s, cb.from_user.id, cb.from_user.username, cb.from_user.first_name)
        keys = list(
            await s.scalars(
                select(Key).where(Key.user_id == user.id, Key.active.is_(True)).order_by(Key.expires_at.desc())
            )
        )
        await s.commit()
    if not keys:
        kb = back_kb([InlineKeyboardButton(text="🛒 Купить ключ", callback_data="buy")])
        await show(cb, "У вас пока нет ключей.", kb)
    else:
        b = InlineKeyboardBuilder()
        for k in keys:
            mark = "⛔" if k.expired else "🔑"
            b.button(text=f"{mark} #{k.id} · до {fmt.dt_local(k.expires_at)[:10]}", callback_data=f"key:{k.id}")
        b.button(text="⬅️ В меню", callback_data="menu")
        b.adjust(1)
        await show(cb, "🔑 <b>Ваши ключи</b>", b.as_markup())
    await cb.answer()


async def _own_key(s, tg_id: int, key_id: int) -> Key | None:
    key = await s.get(Key, key_id)
    if key and key.active and key.user.tg_id == tg_id:
        return key
    return None


@router.callback_query(F.data.regexp(r"^key:\d+$"))
async def cb_key(cb: CallbackQuery):
    async with db.Session() as s:
        key = await _own_key(s, cb.from_user.id, int(cb.data.split(":")[1]))
    if key is None:
        await cb.answer("Ключ не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Продлить", callback_data=f"buy:{key.id}"),
             InlineKeyboardButton(text="📷 QR-код", callback_data=f"qr:{key.id}")],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="keys")],
        ]
    )
    await show(cb, await key_card(key), kb)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^qr:\d+$"))
async def cb_qr(cb: CallbackQuery):
    async with db.Session() as s:
        key = await _own_key(s, cb.from_user.id, int(cb.data.split(":")[1]))
    if key is None:
        await cb.answer("Ключ не найден", show_alert=True)
        return
    await cb.message.answer_photo(
        BufferedInputFile(services.qr_png(services.sub_url(key)), filename=f"key{key.id}.png"),
        caption="Отсканируйте QR-код в приложении — добавятся все локации",
    )
    await cb.answer()


@router.callback_query(F.data == "trial")
async def cb_trial(cb: CallbackQuery):
    async with db.Session() as s:
        user = await services.upsert_user(s, cb.from_user.id, cb.from_user.username, cb.from_user.first_name)
        try:
            key = await services.start_trial(s, user)
        except services.ServiceError as e:
            await cb.answer(str(e), show_alert=True)
            return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить полный доступ", callback_data="buy")]]
    )
    await cb.message.answer(f"🎁 Пробный ключ выдан!\n\n{await key_card(key)}", reply_markup=kb)
    await cb.answer()


# ---------- фоновые задачи ----------
async def broadcast(text: str) -> tuple[int, int]:
    async with db.Session() as s:
        ids = list(await s.scalars(select(User.tg_id).where(User.is_banned.is_(False))))
    ok = fail = 0
    for tg_id in ids:
        if await tg_send(tg_id, text):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)  # лимиты Telegram: ~30 сообщений/сек
    log.info("broadcast finished: ok=%s fail=%s", ok, fail)
    return ok, fail


async def _check_expiry() -> None:
    now = utcnow()
    async with db.Session() as s:
        keys = list(
            await s.scalars(
                select(Key).where(
                    Key.active.is_(True), Key.expires_at <= now + timedelta(days=3), Key.notice_stage < 2
                )
            )
        )
        for k in keys:
            stage = 2 if k.expires_at <= now else 1
            if k.notice_stage >= stage:
                continue
            if stage == 2:
                text = f"⛔ Срок действия ключа #{k.id} истёк. Продлите, чтобы продолжить пользоваться VPN."
            else:
                text = f"⏳ Ключ #{k.id} истекает {fmt.dt_local(k.expires_at)}. Продлите заранее."
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔄 Продлить", callback_data=f"buy:{k.id}")]]
            )
            await tg_send(k.user.tg_id, text, kb)
            k.notice_stage = stage
        await s.commit()


async def expiry_loop(interval: int = 600) -> None:
    while True:
        try:
            await _check_expiry()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("expiry loop failed")
        await asyncio.sleep(interval)


def create_bot() -> Bot:
    return Bot(
        get_settings().bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
