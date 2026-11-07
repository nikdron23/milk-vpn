import base64
import hashlib
import json
import os
import re
import tempfile
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlparse

import pytest

_tmp = tempfile.mkdtemp()
os.environ.update(
    DATABASE_URL=f"sqlite+aiosqlite:///{_tmp}/test.db",
    SECRET_KEY="test-secret-key-0123456789",
    ADMIN_PASSWORD="correct-horse-battery",
    ADMIN_LOGIN="admin",
    RUN_BOT="false",
    BASE_URL="http://testserver",
    ROBOKASSA_LOGIN="shop",
    ROBOKASSA_PASSWORD1="pass1",
    ROBOKASSA_PASSWORD2="pass2",
    ROBOKASSA_TEST="false",
    TRIAL_DAYS="1",
    TRIAL_GB="1",
)
for _k in ("XUI_URL",):
    os.environ.pop(_k, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app import bot as tgbot  # noqa: E402
from app import crypto, db, robokassa, services  # noqa: E402
from app import runtime as rt  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Key, KeyServer, ReferralReward, Server, User, utcnow  # noqa: E402
from app.security import make_login_token  # noqa: E402
from app.state import state  # noqa: E402
from app.xui import XUIClient, XUIError, build_vless_link  # noqa: E402

REALITY = json.dumps(
    {
        "network": "tcp", "security": "reality",
        "realitySettings": {"serverNames": ["example.com"], "shortIds": ["ab12"],
                            "settings": {"publicKey": "PBK", "fingerprint": "chrome"}},
    }
)


class FakeXUI:
    """Поддельная панель 3x-ui."""

    def __init__(self):
        self.clients: dict[str, dict] = {}
        self.fail = False

    async def add_client(self, **kw):
        if self.fail:
            raise XUIError("down")
        if kw["client_id"] in self.clients:
            raise XUIError("duplicate")
        self.clients[kw["client_id"]] = kw

    async def update_client(self, **kw):
        if self.fail:
            raise XUIError("down")
        if kw["client_id"] not in self.clients:
            raise XUIError("not found")
        self.clients[kw["client_id"]] = kw

    async def delete_client(self, client_id):
        if self.fail:
            raise XUIError("down")
        self.clients.pop(client_id, None)

    async def reset_traffic(self, email):
        pass

    async def get_traffic(self, email):
        if self.fail:
            raise XUIError("down")
        return {"up": 1024**2, "down": 2 * 1024**2, "total": 0}

    async def get_inbound(self, force=False):
        if self.fail:
            raise XUIError("down")
        return {"protocol": "vless", "port": 443, "remark": "main", "streamSettings": REALITY}

    async def ping(self):
        if self.fail:
            raise XUIError("down")
        return "vless:443 «main»"

    async def aclose(self):
        pass


PANELS: dict[str, FakeXUI] = {}


def factory(server):
    return PANELS.setdefault(server.name, FakeXUI())


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


def sign(out_sum, inv_id, password="pass2", tail=""):
    return hashlib.md5(f"{out_sum}:{inv_id}:{password}{tail}".encode()).hexdigest()


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture(scope="module")
def client():
    get_settings.cache_clear()
    services.set_factory(factory)
    with TestClient(app) as c:
        yield c


def run(c, coro_fn):
    return c.portal.call(coro_fn)


def make_user(c, tg_id, ref_code=None):
    async def _mk():
        async with db.Session() as s:
            u, created = await services.register_user(s, tg_id, f"user{tg_id}", "Test")
            if created and ref_code:
                await services.attach_referrer(s, u, ref_code)
            await s.commit()
            return u.id, u.ref_code

    return run(c, _mk)


def db_get(c, fn):
    async def _q():
        async with db.Session() as s:
            return await fn(s)

    return run(c, _q)


def user_login(c, tg_id):
    c.get("/logout")
    r = c.get(f"/auth?token={make_login_token(tg_id)}", follow_redirects=True)
    assert r.status_code == 200 and "Мои ключи" in r.text
    return r


def admin_login(c):
    c.get("/logout")
    token = csrf_from(c.get("/admin/login").text)
    r = c.post("/admin/login", data={"login": "admin", "password": "correct-horse-battery", "csrf": token},
               follow_redirects=True)
    assert r.status_code == 200 and "Обзор" in r.text
    return r


def buy(c, tg_id, plan_id=1, key_id=0):
    user_login(c, tg_id)
    token = csrf_from(c.get("/cabinet").text)
    r = c.post("/cabinet/buy", data={"plan_id": plan_id, "key_id": key_id, "csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and urlparse(r.headers["location"]).netloc == "auth.robokassa.ru"
    q = parse_qs(urlparse(r.headers["location"]).query)
    inv_id, out_sum = q["InvId"][0], q["OutSum"][0]
    res = c.post("/payment/result", data={"OutSum": out_sum, "InvId": inv_id, "SignatureValue": sign(out_sum, inv_id)})
    return inv_id, res


def latest_expiry(c, tg_id):
    async def _q(s):
        u = await s.scalar(select(User).where(User.tg_id == tg_id))
        return await s.scalar(
            select(func.max(Key.expires_at)).where(Key.user_id == u.id, Key.active.is_(True))
        )

    return db_get(c, _q)


# ====================== чистые функции ======================
def test_robokassa_signatures():
    url = robokassa.build_payment_url(inv_id=7, amount_kop=19900, description="Milk VPN: test")
    q = parse_qs(urlparse(url).query)
    assert q["OutSum"] == ["199.00"] and q["InvId"] == ["7"]
    assert q["SignatureValue"] == [hashlib.md5(b"shop:199.00:7:pass1").hexdigest()]
    assert "IsTest" not in q


def test_robokassa_shp_and_verify():
    params = {"OutSum": "10.00", "InvId": "5", "Shp_b": "2", "Shp_a": "1"}
    params["SignatureValue"] = sign("10.00", 5, "pass2", ":Shp_a=1:Shp_b=2")
    assert robokassa.verify_result(params) == (5, "10.00")
    params["Shp_a"] = "tampered"
    with pytest.raises(robokassa.SignatureError):
        robokassa.verify_result(params)


def test_vless_link_reality_and_remark():
    inbound = {"port": 443, "remark": "main", "streamSettings": REALITY}
    link = build_vless_link(inbound, "uuid-1", "tg1-abc", "vpn.test", "xtls-rprx-vision", remark="🇳🇱 Нидерланды")
    assert link.startswith("vless://uuid-1@vpn.test:443?")
    for part in ("security=reality", "pbk=PBK", "sni=example.com", "sid=ab12", "flow=xtls-rprx-vision"):
        assert part in link
    assert unquote(link.split("#", 1)[1]) == "🇳🇱 Нидерланды"


def test_crypto_roundtrip():
    enc = crypto.encrypt("панель-пароль")
    assert enc != "панель-пароль" and crypto.decrypt(enc) == "панель-пароль"
    with pytest.raises(ValueError):
        crypto.decrypt("garbage")


def test_plural_days():
    from app.fmt import plural_days

    assert [plural_days(n) for n in (1, 2, 5, 11, 21, 22, 100)] == [
        "1 день", "2 дня", "5 дней", "11 дней", "21 день", "22 дня", "100 дней"]


def test_add_missing_columns_migration(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, tg_id BIGINT NOT NULL, username VARCHAR(64), "
                          "first_name VARCHAR(128), is_banned BOOLEAN, trial_used BOOLEAN, created_at DATETIME)"))
        conn.execute(text("INSERT INTO users (id, tg_id) VALUES (1, 5)"))
        db._add_missing_columns(conn)
        cols = {c["name"] for c in inspect(conn).get_columns("users")}
        assert {"ref_code", "referrer_id", "bonus_days"} <= cols
        assert conn.execute(text("SELECT bonus_days FROM users")).scalar() == 0


# ====================== публичный сайт ======================
def test_public_pages(client):
    assert client.get("/healthz").text == "ok"
    land = client.get("/")
    assert land.status_code == 200 and "Milk VPN" in land.text and "1 месяц" in land.text
    for path in ("/setup", "/terms", "/privacy", "/login"):
        assert client.get(path).status_code == 200, path
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/favicon.svg").status_code == 200


def test_cabinet_requires_auth_and_bad_token(client):
    r = client.get("/cabinet", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/auth?token=garbage").status_code == 400


# ====================== админка: вход и серверы ======================
def test_admin_requires_login(client):
    client.get("/logout")
    for path in ("/admin", "/admin/servers", "/admin/servers/new", "/admin/plans", "/admin/users",
                 "/admin/keys", "/admin/orders", "/admin/settings", "/admin/broadcast"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login", path


def test_admin_wrong_password(client):
    token = csrf_from(client.get("/admin/login").text)
    r = client.post("/admin/login", data={"login": "admin", "password": "nope", "csrf": token}, follow_redirects=True)
    assert "Неверный логин или пароль" in r.text


def test_admin_adds_servers(client):
    admin_login(client)
    for name, cc, host in (("Нидерланды", "NL", "nl.test"), ("Германия", "DE", "de.test")):
        token = csrf_from(client.get("/admin/servers/new").text)
        r = client.post(
            "/admin/servers/save",
            data={"server_id": 0, "name": name, "country": cc, "url": f"https://{cc.lower()}.panel.test:2053/x/",
                  "username": "admin", "password": "pw-" + cc, "inbound_id": 1, "flow": "xtls-rprx-vision",
                  "public_host": host, "sort": 10 if cc == "NL" else 20, "verify_tls": "1", "active": "1",
                  "csrf": token},
            follow_redirects=True,
        )
        assert "сохранён" in r.text and "Подключение успешно" in r.text, r.text[:400]
    page = client.get("/admin/servers")
    assert page.text.count("онлайн") >= 2 and "🇳🇱" in page.text and "🇩🇪" in page.text

    async def _q(s):
        return list(await s.scalars(select(Server).order_by(Server.id)))

    servers = db_get(client, _q)
    assert len(servers) == 2
    assert servers[0].password_enc != "pw-NL" and crypto.decrypt(servers[0].password_enc) == "pw-NL"
    assert servers[0].url == f"https://nl.panel.test:2053/x"  # слэш в конце убран


def test_admin_server_form_validation(client):
    token = csrf_from(client.get("/admin/servers/new").text)
    r = client.post("/admin/servers/save", data={"server_id": 0, "name": "X", "url": "ftp://bad", "username": "a",
                                                 "password": "p", "csrf": token}, follow_redirects=True)
    assert "Проверьте поля" in r.text
    r = client.post("/admin/servers/save", data={"server_id": 0, "name": "X", "url": "https://ok.test/p",
                                                 "username": "a", "password": "", "csrf": token}, follow_redirects=True)
    assert "Укажите пароль" in r.text


def test_landing_shows_locations(client):
    land = client.get("/")
    assert "Нидерланды" in land.text and "Германия" in land.text and "🇳🇱" in land.text


# ====================== покупка, подписка ======================
def test_purchase_flow_two_servers(client, monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(state, "bot", bot)
    make_user(client, 111)
    user_login(client, 111)
    token = csrf_from(client.get("/cabinet").text)

    # без CSRF — отказ
    assert client.post("/cabinet/buy", data={"plan_id": 1}, follow_redirects=False).status_code == 403

    r = client.post("/cabinet/buy", data={"plan_id": 1, "csrf": token}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    inv_id, out_sum = q["InvId"][0], q["OutSum"][0]

    # подделанная подпись и неверная сумма — отклоняются, ключ не выдаётся
    assert client.get("/payment/result", params={"OutSum": out_sum, "InvId": inv_id,
                                                 "SignatureValue": "0" * 32}).status_code == 400
    assert client.get("/payment/result", params={"OutSum": "1.00", "InvId": inv_id,
                                                 "SignatureValue": sign("1.00", inv_id)}).status_code == 400
    assert all(len(p.clients) == 0 for p in PANELS.values())

    good = {"OutSum": out_sum + "000", "InvId": inv_id, "SignatureValue": sign(out_sum + "000", inv_id)}
    assert client.post("/payment/result", data=good).text == f"OK{inv_id}"
    assert client.post("/payment/result", data=good).text == f"OK{inv_id}"  # идемпотентно

    # клиент создан на ОБОИХ серверах с одним и тем же UUID
    assert len(PANELS["Нидерланды"].clients) == 1 and len(PANELS["Германия"].clients) == 1
    assert set(PANELS["Нидерланды"].clients) == set(PANELS["Германия"].clients)
    assert len(bot.sent) == 1 and "Оплата получена" in bot.sent[0][1]
    assert "http://testserver/sub/" in bot.sent[0][1]

    cab = client.get("/cabinet")
    assert "Ключ #1" in cab.text and "оплачен" in cab.text
    page = client.get("/cabinet/key/1")
    assert page.status_code == 200 and "/sub/" in page.text and "<svg" in page.text
    assert "Нидерланды" in page.text and "Германия" in page.text

    ok = client.get("/payment/success", params={"OutSum": out_sum, "InvId": inv_id,
                                                "SignatureValue": sign(out_sum, inv_id, "pass1")})
    assert ok.status_code == 200 and "Ключ уже отправлен" in ok.text


def test_subscription_endpoint(client):
    key = db_get(client, lambda s: s.get(Key, 1))
    r = client.get(f"/sub/{key.sub_id}")
    assert r.status_code == 200
    assert r.headers["profile-title"].startswith("base64:")
    info = r.headers["subscription-userinfo"]
    assert "download=6291456" in info and "expire=" in info  # (1+2 МБ) × 2 сервера
    links = base64.b64decode(r.text).decode().splitlines()
    assert len(links) == 2 and all(l.startswith(f"vless://{key.client_uuid}@") for l in links)
    tags = [unquote(l.split("#", 1)[1]) for l in links]
    assert tags[0].startswith("🇳🇱") and tags[1].startswith("🇩🇪")
    assert "@nl.test:443" in links[0] and "@de.test:443" in links[1]
    assert client.get("/sub/deadbeefdeadbeef").status_code == 404
    assert client.get("/sub/not-hex!").status_code == 404


def test_sub_skips_dead_server(client):
    key = db_get(client, lambda s: s.get(Key, 1))
    PANELS["Германия"].fail = True
    try:
        links = base64.b64decode(client.get(f"/sub/{key.sub_id}").text).decode().splitlines()
        assert len(links) == 1 and "nl.test" in links[0]
    finally:
        PANELS["Германия"].fail = False


def test_one_server_down_then_sync(client):
    PANELS["Нидерланды"].fail = True
    inv_id, res = buy(client, 111, plan_id=2)  # новый ключ, пока NL лежит
    assert res.text == f"OK{inv_id}"
    PANELS["Нидерланды"].fail = False

    async def _count(s):
        return await s.scalar(select(func.count()).select_from(KeyServer).where(KeyServer.key_id == 2))

    assert db_get(client, _count) == 1
    done, failed = run(client, services.sync_missing_keys)
    assert (done, failed) == (1, 0)
    assert db_get(client, _count) == 2
    key2 = db_get(client, lambda s: s.get(Key, 2))
    assert key2.client_uuid in PANELS["Нидерланды"].clients


def test_all_servers_down_does_not_confirm_payment(client):
    user_login(client, 111)
    token = csrf_from(client.get("/cabinet").text)
    r = client.post("/cabinet/buy", data={"plan_id": 3, "csrf": token}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    inv_id, out_sum = q["InvId"][0], q["OutSum"][0]
    params = {"OutSum": out_sum, "InvId": inv_id, "SignatureValue": sign(out_sum, inv_id)}
    for p in PANELS.values():
        p.fail = True
    assert client.post("/payment/result", data=params).status_code == 500  # Робокасса повторит запрос
    for p in PANELS.values():
        p.fail = False
    assert client.post("/payment/result", data=params).text == f"OK{inv_id}"


def test_renewal_extends_existing_key(client):
    before = latest_expiry(client, 111)
    uuid_ = db_get(client, lambda s: s.get(Key, 1)).client_uuid
    key1_before = db_get(client, lambda s: s.get(Key, 1)).expires_at
    buy(client, 111, plan_id=1, key_id=1)
    key1_after = db_get(client, lambda s: s.get(Key, 1)).expires_at
    assert timedelta(days=29, hours=23) < key1_after - key1_before < timedelta(days=30, hours=1)
    assert PANELS["Нидерланды"].clients[uuid_]["expiry_ms"] == PANELS["Германия"].clients[uuid_]["expiry_ms"]
    assert latest_expiry(client, 111) >= before


def test_other_users_key_is_hidden(client):
    make_user(client, 222)
    user_login(client, 222)
    assert client.get("/cabinet/key/1").status_code == 404


def test_trial_only_once(client):
    make_user(client, 444)
    user_login(client, 444)
    token = csrf_from(client.get("/cabinet").text)
    r = client.post("/cabinet/trial", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and "/cabinet/key/" in r.headers["location"]
    n = sum(len(p.clients) for p in PANELS.values())
    page = client.get("/cabinet")
    assert "Получить ключ" not in page.text
    r = client.post("/cabinet/trial", data={"csrf": csrf_from(page.text)}, follow_redirects=True)
    assert "Пробный период недоступен" in r.text
    assert sum(len(p.clients) for p in PANELS.values()) == n


# ====================== рефералка ======================
def test_referral_link_in_cabinet_and_redirect(client, monkeypatch):
    monkeypatch.setattr(state, "bot_username", "milk_test_bot")
    _, code = make_user(client, 111)
    user_login(client, 111)
    cab = client.get("/cabinet")
    assert f"https://t.me/milk_test_bot?start=r_{code}" in cab.text and "Приведи друга" in cab.text
    r = client.get(f"/r/{code}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"https://t.me/milk_test_bot?start=r_{code}"
    assert client.get("/r/bad code!", follow_redirects=False).headers["location"] == "/"


def test_referral_rewards(client, monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(state, "bot", bot)
    _, code = make_user(client, 111)
    invitee_id, _ = make_user(client, 333, ref_code=code)
    inviter_exp = latest_expiry(client, 111)

    inv, res = buy(client, 333, plan_id=1)
    assert res.text == f"OK{inv}"

    # приглашённый: 30 дней тарифа + 3 бонусных
    invitee_exp = latest_expiry(client, 333)
    assert timedelta(days=32, hours=23) < invitee_exp - utcnow() < timedelta(days=33, hours=1)
    # пригласивший: +7 дней к самому свежему ключу
    assert timedelta(days=6, hours=23) < latest_expiry(client, 111) - inviter_exp < timedelta(days=7, hours=1)

    async def _stats(s):
        n = await s.scalar(select(func.count(ReferralReward.id)))
        d = await s.scalar(select(func.coalesce(func.sum(ReferralReward.days), 0)))
        u = await s.scalar(select(User).where(User.tg_id == 111))
        return n, d, u.bonus_days

    assert db_get(client, _stats) == (1, 7, 0)
    assert any(chat == 111 and "Вам начислено" in text for chat, text in bot.sent)

    # повторная покупка друга при «только за первую оплату» — без наград
    buy(client, 333, plan_id=1)
    assert db_get(client, _stats)[0] == 1


def test_referral_self_and_repeat_are_ignored(client):
    uid, code = make_user(client, 111)

    async def _try(s):
        user = await s.scalar(select(User).where(User.tg_id == 111))
        return await services.attach_referrer(s, user, code)

    assert db_get(client, _try) is None  # нельзя пригласить самого себя
    _, other = make_user(client, 222)
    # уже зарегистрированного пользователя «приглашение» не привязывает (create=False в register_user)
    make_user(client, 333, ref_code=other)

    async def _ref(s):
        return (await s.scalar(select(User).where(User.tg_id == 333))).referrer_id

    assert db_get(client, _ref) == db_get(client, lambda s: s.scalar(select(User.id).where(User.tg_id == 111)))


def test_admin_changes_referral_settings(client, monkeypatch):
    monkeypatch.setattr(state, "bot", FakeBot())
    admin_login(client)
    assert client.get("/admin/settings").status_code == 200
    token = csrf_from(client.get("/admin/settings").text)
    r = client.post("/admin/settings", data={"ref_enabled": "1", "ref_inviter_days": 10, "ref_invitee_days": 0,
                                             "ref_max_rewards": 0, "trial_days": 1, "trial_gb": 1, "csrf": token},
                    follow_redirects=True)  # ref_first_only не отмечен -> награда за каждую оплату
    assert "Настройки сохранены" in r.text
    assert rt.get_int("ref_inviter_days") == 10 and not rt.get_bool("ref_first_only")

    before = latest_expiry(client, 111)
    buy(client, 333, plan_id=1)
    assert timedelta(days=9, hours=23) < latest_expiry(client, 111) - before < timedelta(days=10, hours=1)

    async def _rewards(s):
        return await s.scalar(select(func.count(ReferralReward.id)))

    assert db_get(client, _rewards) == 2

    # лимит наград
    admin_login(client)
    token = csrf_from(client.get("/admin/settings").text)
    client.post("/admin/settings", data={"ref_enabled": "1", "ref_inviter_days": 10, "ref_invitee_days": 0,
                                         "ref_max_rewards": 2, "trial_days": 1, "trial_gb": 1, "csrf": token})
    buy(client, 333, plan_id=1)
    assert db_get(client, _rewards) == 2

    # программа выключена
    admin_login(client)
    token = csrf_from(client.get("/admin/settings").text)
    client.post("/admin/settings", data={"ref_inviter_days": 10, "ref_invitee_days": 5, "ref_max_rewards": 0,
                                         "trial_days": 1, "trial_gb": 1, "csrf": token})
    assert not rt.get_bool("ref_enabled")
    buy(client, 333, plan_id=1)
    assert db_get(client, _rewards) == 2
    user_login(client, 111)
    assert "Приведи друга" not in client.get("/cabinet").text  # блок скрыт

    # возвращаем значения по умолчанию
    admin_login(client)
    token = csrf_from(client.get("/admin/settings").text)
    client.post("/admin/settings", data={"ref_enabled": "1", "ref_first_only": "1", "ref_inviter_days": 7,
                                         "ref_invitee_days": 3, "ref_max_rewards": 0, "trial_days": 1,
                                         "trial_gb": 1, "csrf": token})


def test_pending_bonus_applies_to_first_key(client):
    """Бонус пригласившему без ключа ждёт первой покупки и прибавляется к ней."""
    make_user(client, 555)

    async def _give(s):
        u = await s.scalar(select(User).where(User.tg_id == 555))
        u.bonus_days = 5
        await s.commit()

    db_get(client, _give)
    assert run(client, services.apply_pending_bonuses) is None  # ключа нет — ничего не ломается
    buy(client, 555, plan_id=1)
    exp = latest_expiry(client, 555)
    assert timedelta(days=34, hours=23) < exp - utcnow() < timedelta(days=35, hours=1)

    async def _bonus(s):
        return (await s.scalar(select(User).where(User.tg_id == 555))).bonus_days

    assert db_get(client, _bonus) == 0


# ====================== админка: остальное ======================
def test_admin_pages_render(client):
    admin_login(client)
    for path in ("/admin", "/admin/servers", "/admin/servers/new", "/admin/servers/1/edit", "/admin/plans",
                 "/admin/users", "/admin/users/1", "/admin/keys?status=all", "/admin/orders", "/admin/settings",
                 "/admin/broadcast"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "VPN-серверы" in client.get("/admin").text


def test_admin_plan_edit(client):
    token = csrf_from(client.get("/admin/plans").text)
    r = client.post(
        "/admin/plans/save",
        data={"plan_id": 0, "name": "Тест", "days": 7, "traffic_gb": 50, "limit_ip": 2, "price": "99,50",
              "sort": 5, "active": "1", "csrf": token},
        follow_redirects=True,
    )
    assert "Тариф сохранён" in r.text and "Тест" in r.text


def test_admin_server_toggle_edit_delete(client):
    token = csrf_from(client.get("/admin/servers").text)
    r = client.post("/admin/servers/2/toggle", data={"csrf": token}, follow_redirects=True)
    assert "отключён" in r.text
    key = db_get(client, lambda s: s.get(Key, 1))
    links = base64.b64decode(client.get(f"/sub/{key.sub_id}").text).decode().splitlines()
    assert len(links) == 1  # отключённый сервер пропал из подписки

    # правка без смены пароля: пароль остаётся прежним
    token = csrf_from(client.get("/admin/servers/2/edit").text)
    client.post("/admin/servers/save",
                data={"server_id": 2, "name": "Германия", "country": "DE", "url": "https://de.panel.test:2053/x",
                      "username": "admin", "password": "", "inbound_id": 1, "flow": "xtls-rprx-vision",
                      "public_host": "de.test", "sort": 20, "verify_tls": "1", "active": "1", "csrf": token})
    srv = db_get(client, lambda s: s.get(Server, 2))
    assert srv.active and crypto.decrypt(srv.password_enc) == "pw-DE"

    token = csrf_from(client.get("/admin/servers").text)
    r = client.post("/admin/servers/2/delete", data={"csrf": token}, follow_redirects=True)
    assert "удалён из списка" in r.text
    assert db_get(client, lambda s: s.get(Server, 2)) is None
    assert len(PANELS["Германия"].clients) > 0  # на самой панели ничего не удалено


def test_admin_key_extend_and_revoke(client):
    key = db_get(client, lambda s: s.get(Key, 1))
    token = csrf_from(client.get("/admin/keys").text)
    client.post("/admin/keys/1/extend", data={"days": 10, "csrf": token}, follow_redirects=True)
    assert db_get(client, lambda s: s.get(Key, 1)).expires_at - key.expires_at > timedelta(days=9)

    token = csrf_from(client.get("/admin/keys").text)
    n = len(PANELS["Нидерланды"].clients)
    r = client.post("/admin/keys/1/revoke", data={"csrf": token}, follow_redirects=True)
    assert "отозван" in r.text
    assert len(PANELS["Нидерланды"].clients) == n - 1
    assert key.client_uuid not in PANELS["Нидерланды"].clients
    assert client.get(f"/sub/{key.sub_id}").status_code == 404  # отозванный ключ не отдаётся


def test_admin_csrf_enforced(client):
    assert client.post("/admin/keys/2/revoke", data={}).status_code == 403
    assert client.post("/admin/settings", data={}).status_code == 403
    assert client.post("/admin/servers/save", data={}).status_code == 403


# ====================== клиент 3x-ui и бот ======================
def test_xui_client_login_relogin_and_payload():
    import asyncio

    import httpx

    calls = []
    state_ = {"session_valid": True}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/base/login":
            assert b"username=admin" in request.content and b"password=secret" in request.content
            state_["session_valid"] = True
            return httpx.Response(200, json={"success": True, "msg": "ok"})
        if not state_["session_valid"]:
            return httpx.Response(404, text="404 page not found")
        if request.url.path == "/base/panel/api/inbounds/addClient":
            body = json.loads(request.content)
            assert body["id"] == 5
            client = json.loads(body["settings"])["clients"][0]
            assert client["id"] == "u-1" and client["email"] == "e1" and client["subId"] == "s1"
            assert client["totalGB"] == 1024**3 and client["expiryTime"] == 1700000000000
            assert client["flow"] == "xtls-rprx-vision" and client["enable"] is True
            return httpx.Response(200, json={"success": True, "obj": None})
        if request.url.path == "/base/panel/api/inbounds/getClientTraffics/e1":
            return httpx.Response(200, json={"success": True, "obj": {"up": 1, "down": 2}})
        if request.url.path == "/base/panel/api/inbounds/get/5":
            return httpx.Response(200, json={"success": True, "obj": {"protocol": "vless", "port": 443}})
        return httpx.Response(200, json={"success": False, "msg": "unexpected " + request.url.path})

    async def run_():
        x = XUIClient("http://panel/base/", "admin", "secret", 5, flow="xtls-rprx-vision")
        x._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await x.add_client(client_id="u-1", email="e1", total_bytes=1024**3, expiry_ms=1700000000000, sub_id="s1")
        state_["session_valid"] = False  # сессия «протухла» — клиент сам войдёт заново
        assert (await x.get_traffic("e1")) == {"up": 1, "down": 2}
        with pytest.raises(XUIError, match="unexpected"):
            await x.delete_client("zzz")
        # устаревший inbound отдаётся, если панель легла
        assert (await x.get_inbound())["port"] == 443
        x._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="x")))
        x._inbound_cache = (0.0, x._inbound_cache[1])  # кэш «просрочен»
        assert (await x.get_inbound())["port"] == 443
        await x.aclose()

    asyncio.run(run_())
    assert len([c for c in calls if c[1].endswith("/login")]) == 2


def test_bot_dispatcher_and_menu():
    dp = tgbot.create_dispatcher()
    assert dp is not None
    labels = [b.text for row in tgbot.main_menu(trial=True, ref=True).inline_keyboard for b in row]
    assert any("Пробный" in t for t in labels) and any("Пригласить" in t for t in labels)
    labels = [b.text for row in tgbot.main_menu(False).inline_keyboard for b in row]
    assert not any("Пробный" in t for t in labels) and not any("Пригласить" in t for t in labels)
