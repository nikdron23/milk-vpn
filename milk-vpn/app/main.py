import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from app import admin, db, services, web
from app import bot as tgbot
from app import runtime as rt
from app.config import get_settings
from app.models import Plan
from app.state import state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("milkvpn")

BASE = Path(__file__).parent
DEFAULT_PLANS = [
    # name, days, traffic_gb, price_kop
    ("1 месяц", 30, 0, 19900),
    ("3 месяца", 90, 0, 49900),
    ("12 месяцев", 365, 0, 159000),
]


async def seed_plans() -> None:
    async with db.Session() as s:
        if await s.scalar(select(func.count(Plan.id))):
            return
        for i, (name, days, gb, price) in enumerate(DEFAULT_PLANS):
            s.add(Plan(name=name, days=days, traffic_gb=gb, price_kop=price, sort=(i + 1) * 10))
        await s.commit()
        log.info("Созданы тарифы по умолчанию — отредактируйте их в админ-панели")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    problems = cfg.problems()
    if problems:
        raise RuntimeError("Ошибка конфигурации:\n - " + "\n - ".join(problems))
    if not cfg.robokassa_login:
        log.warning("ROBOKASSA_LOGIN не задан — оплата недоступна")
    elif cfg.robokassa_test:
        log.warning("Робокасса в ТЕСТОВОМ режиме (ROBOKASSA_TEST=true)")

    db.init_engine(cfg.database_url)
    await db.init_db()
    await rt.load()
    await seed_plans()
    async with db.Session() as s:
        await services.seed_server_from_env(s)

    tasks = [asyncio.create_task(services.sync_loop())]
    dp = None
    if cfg.run_bot and cfg.bot_token:
        state.bot = tgbot.create_bot()
        state.bot_username = (await state.bot.get_me()).username or ""
        dp = tgbot.create_dispatcher()
        tasks.append(asyncio.create_task(dp.start_polling(state.bot, handle_signals=False)))
        tasks.append(asyncio.create_task(tgbot.expiry_loop()))
        log.info("Бот @%s запущен", state.bot_username)
    else:
        log.warning("Бот не запущен (RUN_BOT=false или пустой BOT_TOKEN)")

    try:
        yield
    finally:
        if dp is not None:
            try:
                await dp.stop_polling()
            except RuntimeError:
                pass
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if state.bot is not None:
            await state.bot.session.close()
            state.bot = None
        await services.close_all()
        await db.dispose()


_cfg = get_settings()
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=_cfg.secret_key or "unset-secret-startup-will-fail",
    same_site="lax",
    https_only=_cfg.base_url.startswith("https"),
    max_age=60 * 60 * 24 * 14,
)


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.include_router(web.router)
app.include_router(admin.router)
