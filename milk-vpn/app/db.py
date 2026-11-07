from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base

engine = None
Session: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str) -> None:
    global engine, Session
    kwargs = {}
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"timeout": 30}
    engine = create_async_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Session = async_sessionmaker(engine, expire_on_commit=False)


def _add_missing_columns(conn) -> None:
    """Лёгкая миграция: добавляет в уже существующие таблицы новые колонки (после обновления версии)."""
    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(conn.dialect)}"
            if col.server_default is not None:
                ddl += f" DEFAULT {col.server_default.arg}"
            conn.execute(text(ddl))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        # у старых таблиц индекса на ref_code может не быть
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_ref_code ON users (ref_code)"))


async def dispose() -> None:
    if engine is not None:
        await engine.dispose()
