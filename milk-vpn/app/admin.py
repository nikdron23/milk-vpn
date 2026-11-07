"""Админ-панель: серверы, тарифы, пользователи, ключи, заказы, рефералка, рассылка."""
import asyncio
import hmac
import logging
import time
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select

from app import bot as tgbot
from app import db, fmt, services
from app import runtime as rt
from app.common import csrf_check, flash, message_page, redirect, render
from app.config import get_settings
from app.models import Key, KeyServer, Order, Plan, ReferralReward, Server, User, utcnow
from app.state import state
from app.xui import XUIError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

COUNTRIES = {
    "NL": "Нидерланды", "DE": "Германия", "FI": "Финляндия", "SE": "Швеция", "FR": "Франция",
    "GB": "Великобритания", "US": "США", "CA": "Канада", "PL": "Польша", "CH": "Швейцария",
    "AT": "Австрия", "LV": "Латвия", "LT": "Литва", "EE": "Эстония", "CZ": "Чехия", "RO": "Румыния",
    "BG": "Болгария", "TR": "Турция", "KZ": "Казахстан", "AM": "Армения", "GE": "Грузия",
    "RS": "Сербия", "ES": "Испания", "IT": "Италия", "NO": "Норвегия", "IE": "Ирландия",
    "AE": "ОАЭ", "IL": "Израиль", "IN": "Индия", "JP": "Япония", "KR": "Южная Корея",
    "SG": "Сингапур", "HK": "Гонконг", "AU": "Австралия", "BR": "Бразилия",
}

_attempts: dict[str, list[float]] = defaultdict(list)


async def admin_required(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(303, headers={"Location": "/admin/login"})


_guard = Depends(admin_required)
_csrf = Depends(csrf_check)


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---------- вход ----------
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return render(request, "admin/login.html")


@router.post("/login", dependencies=[_csrf])
async def login(request: Request, login: str = Form(""), password: str = Form("")):
    cfg = get_settings()
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _attempts[ip] if now - t < 300]
    _attempts[ip] = recent
    if len(recent) >= 5:
        return message_page(request, "Слишком много попыток", "Подождите 5 минут и повторите.", 429)
    if _eq(login, cfg.admin_login) and _eq(password, cfg.admin_password):
        _attempts.pop(ip, None)
        request.session.clear()
        request.session["admin"] = True
        return redirect("/admin")
    _attempts[ip].append(now)
    flash(request, "Неверный логин или пароль")
    return redirect("/admin/login")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return redirect("/admin/login")


# ---------- обзор ----------
@router.get("", response_class=HTMLResponse, dependencies=[_guard])
async def dashboard(request: Request):
    now = utcnow()
    async with db.Session() as s:
        stats = {
            "users": await s.scalar(select(func.count(User.id))),
            "active_keys": await s.scalar(
                select(func.count(Key.id)).where(Key.active.is_(True), Key.expires_at > now)
            ),
            "paid": await s.scalar(select(func.count(Order.id)).where(Order.status == "paid")),
            "pending": await s.scalar(select(func.count(Order.id)).where(Order.status == "pending")),
            "revenue": await s.scalar(
                select(func.coalesce(func.sum(Order.amount_kop), 0)).where(Order.status == "paid")
            ),
            "revenue30": await s.scalar(
                select(func.coalesce(func.sum(Order.amount_kop), 0)).where(
                    Order.status == "paid", Order.paid_at >= now - timedelta(days=30)
                )
            ),
        }
        last_orders = list(await s.scalars(select(Order).order_by(Order.id.desc()).limit(8)))
        servers = list(await s.scalars(select(Server).order_by(Server.sort, Server.id)))
    pings = await services.ping_all([sv for sv in servers if sv.active])
    return render(
        request, "admin/dashboard.html",
        {"stats": stats, "orders": last_orders, "servers": servers, "pings": pings},
    )


# ---------- серверы ----------
@router.get("/servers", response_class=HTMLResponse, dependencies=[_guard])
async def servers_page(request: Request):
    async with db.Session() as s:
        servers = list(await s.scalars(select(Server).order_by(Server.sort, Server.id)))
        counts = dict((await s.execute(select(KeyServer.server_id, func.count()).group_by(KeyServer.server_id))).all())
        active_keys = await s.scalar(
            select(func.count(Key.id)).where(Key.active.is_(True), Key.expires_at > utcnow())
        )
    pings = await services.ping_all([sv for sv in servers if sv.active])
    return render(
        request, "admin/servers.html",
        {"servers": servers, "pings": pings, "counts": counts, "active_keys": active_keys},
    )


def _server_form_ctx(server: Server | None) -> dict:
    return {"server": server, "countries": COUNTRIES}


@router.get("/servers/new", response_class=HTMLResponse, dependencies=[_guard])
async def server_new(request: Request):
    return render(request, "admin/server_form.html", _server_form_ctx(None))


@router.get("/servers/{server_id}/edit", response_class=HTMLResponse, dependencies=[_guard])
async def server_edit(request: Request, server_id: int):
    async with db.Session() as s:
        server = await s.get(Server, server_id)
    if server is None:
        raise HTTPException(404)
    return render(request, "admin/server_form.html", _server_form_ctx(server))


@router.post("/servers/save", dependencies=[_guard, _csrf])
async def server_save(
    request: Request,
    server_id: int = Form(0),
    name: str = Form(...),
    country: str = Form(""),
    url: str = Form(...),
    username: str = Form(...),
    password: str = Form(""),
    inbound_id: int = Form(1),
    flow: str = Form(""),
    public_host: str = Form(""),
    sort: int = Form(100),
    verify_tls: str = Form(""),
    active: str = Form(""),
):
    name, url, username = name.strip(), url.strip().rstrip("/"), username.strip()
    back = f"/admin/servers/{server_id}/edit" if server_id else "/admin/servers/new"
    if not name or len(name) > 64 or not url.lower().startswith(("http://", "https://")) or not username:
        flash(request, "Проверьте поля: название, адрес панели (http:// или https://) и логин обязательны")
        return redirect(back)
    host = public_host.strip().replace("https://", "").replace("http://", "").strip("/")
    async with db.Session() as s:
        server = await s.get(Server, server_id) if server_id else Server()
        if server is None:
            raise HTTPException(404)
        if not server_id and not password:
            flash(request, "Укажите пароль от панели 3x-ui")
            return redirect(back)
        server.name, server.country, server.url = name, country.strip().upper()[:2], url
        server.username, server.inbound_id, server.flow = username, inbound_id, flow.strip()
        server.public_host, server.sort = host, sort
        server.verify_tls, server.active = bool(verify_tls), bool(active)
        if password:
            services.set_server_password(server, password)
        s.add(server)
        await s.commit()
        await s.refresh(server)
    ok, info = await services.ping_server(server)
    if server.active:
        services.spawn_sync(server.id)  # ключи действующих клиентов добавятся на сервер в фоне
    msg = f"Сервер «{server.name}» сохранён. "
    msg += f"Подключение успешно: {info}" if ok else f"⚠ Панель не ответила: {info}"
    if ok and not info.startswith("vless"):
        msg += " — внимание: inbound не VLESS, такой сервер в подписку не попадёт."
    flash(request, msg)
    return redirect("/admin/servers")


@router.post("/servers/{server_id}/toggle", dependencies=[_guard, _csrf])
async def server_toggle(request: Request, server_id: int):
    async with db.Session() as s:
        server = await s.get(Server, server_id)
        if server is None:
            raise HTTPException(404)
        server.active = not server.active
        await s.commit()
    if server.active:
        services.spawn_sync(server.id)
    flash(request, f"Сервер «{server.name}» {'включён' if server.active else 'отключён (в подписки не попадает)'}")
    return redirect("/admin/servers")


@router.post("/servers/{server_id}/sync", dependencies=[_guard, _csrf])
async def server_sync(request: Request, server_id: int):
    services.spawn_sync(server_id)
    flash(request, "Синхронизация запущена: недостающие ключи добавятся на сервер в фоне")
    return redirect("/admin/servers")


@router.post("/servers/{server_id}/delete", dependencies=[_guard, _csrf])
async def server_delete(request: Request, server_id: int):
    async with db.Session() as s:
        server = await s.get(Server, server_id)
        if server is None:
            raise HTTPException(404)
        await s.execute(KeyServer.__table__.delete().where(KeyServer.server_id == server_id))
        await s.delete(server)
        await s.commit()
    flash(request, f"Сервер «{server.name}» удалён из списка. Клиенты на самой панели 3x-ui не тронуты.")
    return redirect("/admin/servers")


# ---------- тарифы ----------
@router.get("/plans", response_class=HTMLResponse, dependencies=[_guard])
async def plans_page(request: Request):
    async with db.Session() as s:
        plans = list(await s.scalars(select(Plan).order_by(Plan.sort, Plan.id)))
    return render(request, "admin/plans.html", {"plans": plans})


@router.post("/plans/save", dependencies=[_guard, _csrf])
async def plans_save(
    request: Request,
    plan_id: int = Form(0),
    name: str = Form(...),
    days: int = Form(...),
    traffic_gb: int = Form(0),
    limit_ip: int = Form(0),
    price: str = Form(...),
    sort: int = Form(100),
    active: str = Form(""),
):
    try:
        price_kop = fmt.parse_price(price)
    except ValueError as e:
        flash(request, str(e))
        return redirect("/admin/plans")
    name = name.strip()
    if not name or len(name) > 100 or days < 1 or traffic_gb < 0 or limit_ip < 0:
        flash(request, "Проверьте поля: название, срок ≥ 1 дня, трафик и устройства ≥ 0")
        return redirect("/admin/plans")
    async with db.Session() as s:
        plan = await s.get(Plan, plan_id) if plan_id else Plan()
        if plan is None:
            raise HTTPException(404)
        plan.name, plan.days, plan.traffic_gb, plan.limit_ip = name, days, traffic_gb, limit_ip
        plan.price_kop, plan.sort, plan.active = price_kop, sort, bool(active)
        s.add(plan)
        await s.commit()
    flash(request, "Тариф сохранён")
    return redirect("/admin/plans")


# ---------- рефералка и пробный период ----------
@router.get("/settings", response_class=HTMLResponse, dependencies=[_guard])
async def settings_page(request: Request):
    async with db.Session() as s:
        top = (
            await s.execute(
                select(
                    User, func.count(ReferralReward.id), func.coalesce(func.sum(ReferralReward.days), 0)
                )
                .join(ReferralReward, ReferralReward.inviter_id == User.id)
                .group_by(User.id)
                .order_by(func.count(ReferralReward.id).desc())
                .limit(10)
            )
        ).all()
        invited_total = await s.scalar(select(func.count(User.id)).where(User.referrer_id.is_not(None)))
        rewarded_total = await s.scalar(select(func.count(ReferralReward.id)))
        days_total = await s.scalar(select(func.coalesce(func.sum(ReferralReward.days), 0)))
    vals = {k: rt.get(k) for k in rt.defaults()}
    return render(
        request, "admin/settings.html",
        {"v": vals, "top": top, "invited_total": invited_total, "rewarded_total": rewarded_total,
         "days_total": days_total},
    )


@router.post("/settings", dependencies=[_guard, _csrf])
async def settings_save(
    request: Request,
    ref_enabled: str = Form(""),
    ref_first_only: str = Form(""),
    ref_inviter_days: int = Form(0),
    ref_invitee_days: int = Form(0),
    ref_max_rewards: int = Form(0),
    trial_days: int = Form(0),
    trial_gb: int = Form(0),
):
    nums = [ref_inviter_days, ref_invitee_days, ref_max_rewards, trial_days, trial_gb]
    if any(n < 0 or n > 3650 for n in nums):
        flash(request, "Числа должны быть от 0 до 3650")
        return redirect("/admin/settings")
    await rt.save(
        {
            "ref_enabled": "1" if ref_enabled else "0",
            "ref_first_only": "1" if ref_first_only else "0",
            "ref_inviter_days": str(ref_inviter_days),
            "ref_invitee_days": str(ref_invitee_days),
            "ref_max_rewards": str(ref_max_rewards),
            "trial_days": str(trial_days),
            "trial_gb": str(trial_gb),
        }
    )
    flash(request, "Настройки сохранены")
    return redirect("/admin/settings")


# ---------- пользователи ----------
@router.get("/users", response_class=HTMLResponse, dependencies=[_guard])
async def users_page(request: Request, q: str = ""):
    stmt = select(User).order_by(User.id.desc()).limit(200)
    q = q.strip()
    if q:
        conds = [User.username.ilike(f"%{q.lstrip('@')}%"), User.first_name.ilike(f"%{q}%")]
        if q.isdigit():
            conds.append(User.tg_id == int(q))
        stmt = stmt.where(or_(*conds))
    async with db.Session() as s:
        users = list(await s.scalars(stmt))
        counts = dict(
            (await s.execute(
                select(Key.user_id, func.count(Key.id)).where(Key.active.is_(True)).group_by(Key.user_id)
            )).all()
        )
    return render(request, "admin/users.html", {"users": users, "counts": counts, "q": q})


@router.get("/users/{user_id}", response_class=HTMLResponse, dependencies=[_guard])
async def user_page(request: Request, user_id: int):
    async with db.Session() as s:
        user = await s.get(User, user_id)
        if user is None:
            raise HTTPException(404)
        keys = list(await s.scalars(select(Key).where(Key.user_id == user_id).order_by(Key.id.desc())))
        orders = list(await s.scalars(select(Order).where(Order.user_id == user_id).order_by(Order.id.desc())))
        plans = await services.active_plans(s)
        referrer = await s.get(User, user.referrer_id) if user.referrer_id else None
        stats = await services.referral_stats(s, user)
    return render(
        request, "admin/user.html",
        {"u": user, "keys": keys, "orders": orders, "plans": plans, "referrer": referrer, "stats": stats},
    )


@router.post("/users/{user_id}/ban", dependencies=[_guard, _csrf])
async def user_ban(request: Request, user_id: int):
    async with db.Session() as s:
        user = await s.get(User, user_id)
        if user is None:
            raise HTTPException(404)
        user.is_banned = not user.is_banned
        await s.commit()
        flash(request, "Пользователь заблокирован" if user.is_banned else "Пользователь разблокирован")
    return redirect(f"/admin/users/{user_id}")


@router.post("/users/{user_id}/grant", dependencies=[_guard, _csrf])
async def user_grant(request: Request, user_id: int, plan_id: int = Form(...)):
    async with db.Session() as s:
        user, plan = await s.get(User, user_id), await s.get(Plan, plan_id)
        if user is None or plan is None:
            raise HTTPException(404)
        try:
            key = await services.grant_key(s, user, plan)
            await s.commit()
        except services.ServiceError as e:
            flash(request, str(e))
            return redirect(f"/admin/users/{user_id}")
    await tgbot.tg_send(user.tg_id, f"🎁 Вам выдан ключ #{key.id}. Откройте «🔑 Мои ключи» в боте.")
    flash(request, f"Ключ #{key.id} выдан")
    return redirect(f"/admin/users/{user_id}")


# ---------- ключи ----------
@router.get("/keys", response_class=HTMLResponse, dependencies=[_guard])
async def keys_page(request: Request, status: str = "active"):
    now = utcnow()
    stmt = select(Key).order_by(Key.expires_at.asc()).limit(300)
    if status == "active":
        stmt = stmt.where(Key.active.is_(True), Key.expires_at > now)
    elif status == "expired":
        stmt = stmt.where(Key.active.is_(True), Key.expires_at <= now)
    elif status == "revoked":
        stmt = stmt.where(Key.active.is_(False))
    async with db.Session() as s:
        keys = list(await s.scalars(stmt))
        counts = dict((await s.execute(select(KeyServer.key_id, func.count()).group_by(KeyServer.key_id))).all())
        total_servers = await s.scalar(select(func.count(Server.id)).where(Server.active.is_(True)))
    return render(
        request, "admin/keys.html",
        {"keys": keys, "status": status, "counts": counts, "total_servers": total_servers},
    )


@router.post("/keys/{key_id}/extend", dependencies=[_guard, _csrf])
async def key_extend(request: Request, key_id: int, days: int = Form(...)):
    if not 1 <= days <= 3650:
        flash(request, "Количество дней: от 1 до 3650")
        return redirect("/admin/keys")
    async with db.Session() as s:
        key = await s.get(Key, key_id)
        if key is None:
            raise HTTPException(404)
        try:
            await services.extend_key(s, key, days)
            await s.commit()
        except XUIError as e:
            flash(request, f"3x-ui: {e}")
            return redirect("/admin/keys")
    flash(request, f"Ключ #{key_id} продлён на {fmt.plural_days(days)}")
    return redirect(request.headers.get("referer") or "/admin/keys")


@router.post("/keys/{key_id}/revoke", dependencies=[_guard, _csrf])
async def key_revoke(request: Request, key_id: int):
    async with db.Session() as s:
        key = await s.get(Key, key_id)
        if key is None:
            raise HTTPException(404)
        try:
            await services.revoke_key(s, key)
            await s.commit()
        except services.ServiceError as e:
            await s.commit()  # сохраняем частичный прогресс (на каких серверах уже удалён)
            flash(request, str(e))
            return redirect("/admin/keys")
    flash(request, f"Ключ #{key_id} отозван")
    return redirect(request.headers.get("referer") or "/admin/keys")


# ---------- заказы ----------
@router.get("/orders", response_class=HTMLResponse, dependencies=[_guard])
async def orders_page(request: Request, status: str = "all"):
    stmt = select(Order).order_by(Order.id.desc()).limit(300)
    if status in ("paid", "pending"):
        stmt = stmt.where(Order.status == status)
    async with db.Session() as s:
        orders = list(await s.scalars(stmt))
    return render(request, "admin/orders.html", {"orders": orders, "status": status})


@router.post("/orders/{order_id}/mark-paid", dependencies=[_guard, _csrf])
async def order_mark_paid(request: Request, order_id: int):
    async with db.Session() as s:
        try:
            res = await services.fulfill_order(s, order_id)
        except (XUIError, services.ServiceError) as e:
            flash(request, f"Не удалось выдать ключ: {e}")
            return redirect("/admin/orders")
    if res.newly_paid:
        await tgbot.notify_paid(res)
    flash(request, f"Заказ №{order_id} оплачен, ключ выдан")
    return redirect("/admin/orders")


# ---------- рассылка ----------
@router.get("/broadcast", response_class=HTMLResponse, dependencies=[_guard])
async def broadcast_page(request: Request):
    return render(request, "admin/broadcast.html")


@router.post("/broadcast", dependencies=[_guard, _csrf])
async def broadcast(request: Request, text: str = Form(...)):
    text = text.strip()
    if state.bot is None:
        flash(request, "Бот не запущен (RUN_BOT=false или не задан BOT_TOKEN)")
        return redirect("/admin/broadcast")
    if not text or len(text) > 4000:
        flash(request, "Текст: от 1 до 4000 символов")
        return redirect("/admin/broadcast")
    async with db.Session() as s:
        total = await s.scalar(select(func.count(User.id)).where(User.is_banned.is_(False)))
    services._spawn(tgbot.broadcast(text))
    flash(request, f"Рассылка запущена, получателей: {total}")
    return redirect("/admin/broadcast")
