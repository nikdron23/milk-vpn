from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import get_settings


def _ser() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="cabinet-login")


def make_login_token(tg_id: int) -> str:
    """Одноразовая (по времени) ссылка для входа в кабинет из бота."""
    return _ser().dumps(tg_id)


def read_login_token(token: str, max_age: int = 600) -> int | None:
    try:
        return int(_ser().loads(token, max_age=max_age))
    except (BadSignature, ValueError):
        return None
