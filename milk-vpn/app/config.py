from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

HASH_ALGOS = {"md5", "sha256", "sha384", "sha512"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- общее ---
    brand_name: str = "Milk VPN"
    base_url: str = "http://localhost:8000"  # публичный https-адрес сервиса
    secret_key: str = ""
    timezone: str = "Europe/Moscow"
    database_url: str = "sqlite+aiosqlite:///./data/vpnshop.db"
    contact_email: str = ""  # показывается на страницах «Условия» и «Конфиденциальность»

    # --- Telegram ---
    bot_token: str = ""
    run_bot: bool = True
    admin_tg_ids: str = ""  # через запятую
    support_username: str = ""  # без @

    # --- админ-панель ---
    admin_login: str = "admin"
    admin_password: str = ""

    # --- первый VPN-сервер (необязательно) ---
    # Если в админке ещё нет серверов, а эти поля заполнены — сервер создастся автоматически.
    # Остальные серверы добавляйте в админке: раздел «Серверы».
    xui_url: str = ""
    xui_username: str = ""
    xui_password: str = ""
    xui_inbound_id: int = 1
    xui_flow: str = ""
    xui_verify_tls: bool = True
    xui_public_host: str = ""
    xui_country: str = "NL"
    xui_name: str = "Основной"

    # --- Робокасса ---
    robokassa_login: str = ""
    robokassa_password1: str = ""
    robokassa_password2: str = ""
    robokassa_test_password1: str = ""
    robokassa_test_password2: str = ""
    robokassa_test: bool = True
    robokassa_algo: str = "md5"
    robokassa_receipt: bool = False  # чек 54-ФЗ
    robokassa_sno: str = "usn_income"
    robokassa_tax: str = "none"

    # --- значения по умолчанию для пробного периода (дальше меняются в админке) ---
    trial_days: int = 1
    trial_gb: int = 1

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.admin_tg_ids.replace(" ", "").split(",") if x.strip().isdigit()}

    @property
    def robokassa_pass1(self) -> str:
        return self.robokassa_test_password1 if self.robokassa_test else self.robokassa_password1

    @property
    def robokassa_pass2(self) -> str:
        return self.robokassa_test_password2 if self.robokassa_test else self.robokassa_password2

    @property
    def public_url(self) -> str:
        return self.base_url.rstrip("/")

    def problems(self) -> list[str]:
        """Критичные ошибки конфигурации — с ними запускаться нельзя."""
        out = []
        if len(self.secret_key) < 16 or self.secret_key == "change-me":
            out.append("SECRET_KEY должен быть случайной строкой от 16 символов")
        if len(self.admin_password) < 8:
            out.append("ADMIN_PASSWORD должен быть не короче 8 символов")
        if self.robokassa_algo.lower() not in HASH_ALGOS:
            out.append(f"ROBOKASSA_ALGO должен быть одним из: {', '.join(sorted(HASH_ALGOS))}")
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
