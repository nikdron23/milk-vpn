"""Клиент API панели 3x-ui (MHSanaei). Работает с клиентами VLESS/VMess внутри одного inbound."""
import asyncio
import json
import logging
import time
from urllib.parse import quote, urlencode

import httpx

log = logging.getLogger(__name__)


class XUIError(Exception):
    pass


class XUIClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        inbound_id: int,
        flow: str = "",
        verify_tls: bool = True,
    ):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.inbound_id = inbound_id
        self.flow = flow
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(20.0), verify=verify_tls)
        self._lock = asyncio.Lock()
        self._logged_in = False
        self._inbound_cache: tuple[float, dict | None] = (0.0, None)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------- транспорт ----------
    async def _login(self) -> None:
        try:
            r = await self._http.post(
                f"{self.base}/login", data={"username": self.username, "password": self.password}
            )
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise XUIError(f"login failed: {e}") from e
        if not data.get("success"):
            raise XUIError(f"login rejected: {data.get('msg')}")
        self._logged_in = True

    async def _request(self, method: str, path: str, **kw):
        last = ""
        for _ in range(2):
            if not self._logged_in:
                async with self._lock:
                    if not self._logged_in:
                        await self._login()
            try:
                r = await self._http.request(method, f"{self.base}{path}", **kw)
            except httpx.HTTPError as e:
                raise XUIError(f"{method} {path}: {e}") from e
            # Без валидной сессии 3x-ui отвечает 404/не-JSON — логинимся заново
            if r.status_code in (401, 403, 404) or "json" not in r.headers.get("content-type", ""):
                self._logged_in = False
                last = f"HTTP {r.status_code}"
                continue
            data = r.json()
            if not data.get("success"):
                raise XUIError(data.get("msg") or "unknown error")
            return data.get("obj")
        raise XUIError(f"{method} {path}: not authorized ({last})")

    # ---------- клиенты ----------
    def _payload(self, *, client_id, email, total_bytes, expiry_ms, sub_id, limit_ip, enable) -> dict:
        client = {
            "id": client_id,
            "flow": self.flow,
            "email": email,
            "limitIp": limit_ip,
            "totalGB": total_bytes,
            "expiryTime": expiry_ms,
            "enable": enable,
            "subId": sub_id,
            "comment": "",
        }
        return {"id": self.inbound_id, "settings": json.dumps({"clients": [client]})}

    async def add_client(
        self, *, client_id, email, total_bytes, expiry_ms, sub_id, limit_ip=0, enable=True
    ) -> None:
        payload = self._payload(
            client_id=client_id, email=email, total_bytes=total_bytes, expiry_ms=expiry_ms,
            sub_id=sub_id, limit_ip=limit_ip, enable=enable,
        )
        await self._request("POST", "/panel/api/inbounds/addClient", json=payload)

    async def update_client(
        self, *, client_id, email, total_bytes, expiry_ms, sub_id, limit_ip=0, enable=True
    ) -> None:
        payload = self._payload(
            client_id=client_id, email=email, total_bytes=total_bytes, expiry_ms=expiry_ms,
            sub_id=sub_id, limit_ip=limit_ip, enable=enable,
        )
        await self._request("POST", f"/panel/api/inbounds/updateClient/{client_id}", json=payload)

    async def delete_client(self, client_id: str) -> None:
        await self._request("POST", f"/panel/api/inbounds/{self.inbound_id}/delClient/{client_id}")

    async def reset_traffic(self, email: str) -> None:
        await self._request(
            "POST", f"/panel/api/inbounds/{self.inbound_id}/resetClientTraffic/{quote(email)}"
        )

    async def get_traffic(self, email: str) -> dict | None:
        return await self._request("GET", f"/panel/api/inbounds/getClientTraffics/{quote(email)}")

    # ---------- inbound ----------
    async def get_inbound(self, force: bool = False) -> dict:
        ts, cached = self._inbound_cache
        if cached and not force and time.monotonic() - ts < 300:
            return cached
        try:
            obj = await self._request("GET", f"/panel/api/inbounds/get/{self.inbound_id}")
        except XUIError:
            if cached and not force:  # панель недоступна — отдаём устаревшие настройки, они меняются редко
                return cached
            raise
        if not obj:
            raise XUIError(f"inbound {self.inbound_id} not found")
        self._inbound_cache = (time.monotonic(), obj)
        return obj

    async def ping(self) -> str:
        ib = await self.get_inbound(force=True)
        return f"{ib.get('protocol', '?')}:{ib.get('port', '?')} «{ib.get('remark', '')}»"


def build_vless_link(
    inbound: dict, client_id: str, email: str, host: str, flow: str = "", remark: str | None = None
) -> str:
    """Собирает vless:// ссылку из настроек inbound (tcp/ws/grpc + none/tls/reality)."""
    stream = json.loads(inbound.get("streamSettings") or "{}")
    net = stream.get("network", "tcp")
    sec = stream.get("security", "none")
    params: dict[str, str] = {"type": net, "security": sec, "encryption": "none"}

    if net == "ws":
        ws = stream.get("wsSettings", {})
        params["path"] = ws.get("path", "/")
        h = (ws.get("headers") or {}).get("Host") or ws.get("host")
        if h:
            params["host"] = h
    elif net == "grpc":
        g = stream.get("grpcSettings", {})
        params["serviceName"] = g.get("serviceName", "")
        if g.get("multiMode"):
            params["mode"] = "multi"

    if sec == "reality":
        r = stream.get("realitySettings", {})
        rs = r.get("settings", {})
        params["pbk"] = rs.get("publicKey", "")
        params["fp"] = rs.get("fingerprint", "chrome")
        if r.get("serverNames"):
            params["sni"] = r["serverNames"][0]
        if r.get("shortIds"):
            params["sid"] = r["shortIds"][0]
        if rs.get("spiderX"):
            params["spx"] = rs["spiderX"]
    elif sec == "tls":
        t = stream.get("tlsSettings", {})
        params["sni"] = t.get("serverName") or host
        params["fp"] = (t.get("settings") or {}).get("fingerprint", "chrome")
        if t.get("alpn"):
            params["alpn"] = ",".join(t["alpn"])

    if flow and net == "tcp":
        params["flow"] = flow

    tag = quote(remark if remark else f"{inbound.get('remark') or 'VPN'}-{email}")
    return f"vless://{client_id}@{host}:{inbound['port']}?{urlencode(params, quote_via=quote)}#{tag}"
