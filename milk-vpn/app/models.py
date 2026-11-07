from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Наивное UTC-время (везде в БД храним именно его)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    """Настройки, которые администратор меняет в панели (рефералка, пробный период)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ref_code: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # бонусные дни, которые ещё не добавлены к ключу (применяются при выдаче/продлении)
    bonus_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    @property
    def display(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.first_name or str(self.tg_id)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    days: Mapped[int] = mapped_column(Integer)
    traffic_gb: Mapped[int] = mapped_column(Integer, default=0)  # 0 = безлимит
    limit_ip: Mapped[int] = mapped_column(Integer, default=0)  # 0 = без ограничения устройств
    price_kop: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=100)

    @property
    def price(self) -> Decimal:
        return Decimal(self.price_kop) / 100


class Server(Base):
    """VPN-сервер: отдельная панель 3x-ui в какой-либо стране."""

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    country: Mapped[str] = mapped_column(String(2), default="")  # ISO-код: NL, DE, ...
    url: Mapped[str] = mapped_column(String(255))  # https://host:port/webBasePath
    username: Mapped[str] = mapped_column(String(128))
    password_enc: Mapped[str] = mapped_column(Text)  # зашифровано ключом из SECRET_KEY
    inbound_id: Mapped[int] = mapped_column(Integer, default=1)
    flow: Mapped[str] = mapped_column(String(64), default="")
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    public_host: Mapped[str] = mapped_column(String(255), default="")  # домен/IP для vless-ссылки
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def flag(self) -> str:
        c = (self.country or "").upper()
        if len(c) == 2 and c.isalpha() and c.isascii():
            return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in c)
        return "🌐"


class Key(Base):
    __tablename__ = "keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"))
    email: Mapped[str] = mapped_column(String(64), unique=True)  # идентификатор клиента на панелях
    client_uuid: Mapped[str] = mapped_column(String(64))  # один и тот же UUID на всех серверах
    sub_id: Mapped[str] = mapped_column(String(32), index=True)  # токен ссылки-подписки /sub/<sub_id>
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    traffic_gb: Mapped[int] = mapped_column(Integer, default=0)
    limit_ip: Mapped[int] = mapped_column(Integer, default=0)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # False = отозван
    notice_stage: Mapped[int] = mapped_column(Integer, default=0)  # 1 — «скоро истечёт», 2 — «истёк»
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(lazy="joined")
    plan: Mapped[Plan | None] = relationship(lazy="joined")

    @property
    def expired(self) -> bool:
        return self.expires_at <= utcnow()


class KeyServer(Base):
    """На каких серверах клиент этого ключа уже создан."""

    __tablename__ = "key_servers"

    key_id: Mapped[int] = mapped_column(ForeignKey("keys.id"), primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), primary_key=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)  # он же InvId в Робокассе
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    renew_key_id: Mapped[int | None] = mapped_column(ForeignKey("keys.id"))
    key_id: Mapped[int | None] = mapped_column(ForeignKey("keys.id"))
    amount_kop: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | paid
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship(lazy="joined")
    plan: Mapped[Plan] = relationship(lazy="joined")


class ReferralReward(Base):
    """Начисленная награда пригласившему (одна запись на оплаченный заказ друга)."""

    __tablename__ = "referral_rewards"

    id: Mapped[int] = mapped_column(primary_key=True)
    inviter_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    invitee_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    days: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
