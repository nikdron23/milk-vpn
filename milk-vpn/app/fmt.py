from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.config import get_settings


def rub(kop: int) -> str:
    r, k = divmod(int(kop), 100)
    return str(r) if k == 0 else f"{r}.{k:02d}"


def plural_days(n: int) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        word = "дней"
    elif n % 10 == 1:
        word = "день"
    elif 2 <= n % 10 <= 4:
        word = "дня"
    else:
        word = "дней"
    return f"{n} {word}"


def human_bytes(n: int | float) -> str:
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} ТБ"


def dt_local(dt: datetime | None) -> str:
    if not dt:
        return "—"
    tz = ZoneInfo(get_settings().timezone)
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%d.%m.%Y %H:%M")


def parse_price(text: str) -> int:
    """'199' / '149,50' -> копейки. ValueError при некорректном вводе."""
    try:
        d = Decimal(text.strip().replace(",", "."))
    except InvalidOperation as e:
        raise ValueError("Некорректная цена") from e
    if d <= 0 or d > 1_000_000:
        raise ValueError("Цена должна быть больше 0")
    return int((d * 100).to_integral_value())
