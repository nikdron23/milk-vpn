"""Интеграция с Робокассой: формирование ссылки на оплату и проверка подписей."""
import hashlib
import hmac
import json
from urllib.parse import quote, urlencode

from app.config import get_settings

PAY_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"


class SignatureError(Exception):
    pass


def _hash(text: str) -> str:
    return hashlib.new(get_settings().robokassa_algo.lower(), text.encode()).hexdigest()


def _shp_tail(shp: dict[str, str]) -> list[str]:
    # Shp_* параметры входят в подпись, отсортированные по имени
    return [f"{k}={v}" for k, v in sorted(shp.items())]


def fmt_sum(amount_kop: int) -> str:
    return f"{amount_kop // 100}.{amount_kop % 100:02d}"


def build_payment_url(
    *,
    inv_id: int,
    amount_kop: int,
    description: str,
    shp: dict[str, str] | None = None,
    email: str | None = None,
) -> str:
    s = get_settings()
    shp = shp or {}
    out_sum = fmt_sum(amount_kop)
    description = description[:100]

    parts = [s.robokassa_login, out_sum, str(inv_id)]
    receipt_enc = None
    if s.robokassa_receipt:
        receipt = {
            "sno": s.robokassa_sno,
            "items": [
                {
                    "name": description[:128],
                    "quantity": 1,
                    "sum": float(out_sum),
                    "payment_method": "full_payment",
                    "payment_object": "service",
                    "tax": s.robokassa_tax,
                }
            ],
        }
        # В подписи участвует Receipt, закодированный один раз; в URL он должен
        # оказаться ровно в таком же виде — поэтому добавляем его вручную.
        receipt_enc = quote(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")), safe="")
        parts.append(receipt_enc)
    parts.append(s.robokassa_pass1)
    parts += _shp_tail(shp)

    query = {
        "MerchantLogin": s.robokassa_login,
        "OutSum": out_sum,
        "InvId": inv_id,
        "Description": description,
        "SignatureValue": _hash(":".join(parts)),
        "Culture": "ru",
        **shp,
    }
    if email:
        query["Email"] = email
    if s.robokassa_test:
        query["IsTest"] = 1
    url = f"{PAY_URL}?{urlencode(query)}"
    if receipt_enc:
        url += f"&Receipt={receipt_enc}"
    return url


def _verify(params: dict[str, str], password: str) -> tuple[int, str]:
    try:
        out_sum = params["OutSum"]
        inv_raw = params["InvId"]
        given = params["SignatureValue"]
        inv_id = int(inv_raw)
    except (KeyError, ValueError) as e:
        raise SignatureError("missing or invalid parameters") from e
    shp = {k: v for k, v in params.items() if k.lower().startswith("shp_")}
    expected = _hash(":".join([out_sum, inv_raw, password, *_shp_tail(shp)]))
    if not hmac.compare_digest(expected.lower(), given.strip().lower()):
        raise SignatureError("bad signature")
    return inv_id, out_sum


def verify_result(params: dict[str, str]) -> tuple[int, str]:
    """ResultURL (сервер-сервер): подпись на Пароле #2."""
    return _verify(params, get_settings().robokassa_pass2)


def verify_success(params: dict[str, str]) -> tuple[int, str]:
    """SuccessURL (редирект пользователя): подпись на Пароле #1."""
    return _verify(params, get_settings().robokassa_pass1)
