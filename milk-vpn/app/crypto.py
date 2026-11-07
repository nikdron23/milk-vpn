"""Шифрование паролей от панелей 3x-ui, которые хранятся в БД (ключ выводится из SECRET_KEY)."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(("milkvpn:" + get_settings().secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ValueError(
            "Не удалось расшифровать пароль сервера — изменился SECRET_KEY? "
            "Откройте сервер в админке и введите пароль заново."
        ) from e
