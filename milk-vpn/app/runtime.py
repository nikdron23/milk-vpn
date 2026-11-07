"""Настройки, которые администратор меняет из панели (хранятся в БД, кэшируются в памяти)."""
from sqlalchemy import select

from app import db
from app.config import get_settings
from app.models import Setting

_cache: dict[str, str] = {}


def defaults() -> dict[str, str]:
    c = get_settings()
    return {
        "ref_enabled": "1",
        "ref_inviter_days": "7",  # сколько дней получает пригласивший
        "ref_invitee_days": "3",  # сколько дней получает приглашённый к первой покупке
        "ref_first_only": "1",  # награда только за первую оплату друга
        "ref_max_rewards": "0",  # максимум наград на одного пригласившего (0 — без лимита)
        "trial_days": str(c.trial_days),
        "trial_gb": str(c.trial_gb),
    }


async def load() -> None:
    async with db.Session() as s:
        rows = (await s.execute(select(Setting.key, Setting.value))).all()
    _cache.clear()
    _cache.update({k: v for k, v in rows})


def get(key: str) -> str:
    return _cache.get(key, defaults()[key])


def get_bool(key: str) -> bool:
    return get(key).strip().lower() in ("1", "true", "on", "yes")


def get_int(key: str) -> int:
    try:
        return int(get(key))
    except ValueError:
        return int(defaults()[key])


async def save(values: dict[str, str]) -> None:
    async with db.Session() as s:
        for k, v in values.items():
            row = await s.get(Setting, k)
            if row is None:
                s.add(Setting(key=k, value=v))
            else:
                row.value = v
        await s.commit()
    _cache.update(values)
