"""Общее для веб-слоя: шаблоны, flash-сообщения, CSRF."""
import hmac
import secrets
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup

from app import fmt
from app import runtime as rt
from app.config import get_settings
from app.state import state

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@pass_context
def csrf_input(ctx) -> Markup:
    session = ctx["request"].session
    token = session.get("csrf")
    if not token:
        token = session["csrf"] = secrets.token_urlsafe(24)
    return Markup(f'<input type="hidden" name="csrf" value="{token}">')


templates.env.globals["csrf_input"] = csrf_input
templates.env.filters["rub"] = fmt.rub
templates.env.filters["dt"] = fmt.dt_local
templates.env.filters["hbytes"] = fmt.human_bytes
templates.env.filters["days"] = fmt.plural_days


def bot_link() -> str:
    return f"https://t.me/{state.bot_username}" if state.bot_username else ""


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200) -> HTMLResponse:
    ctx = dict(ctx or {})
    ctx["flash"] = request.session.pop("flash", None)
    ctx["cfg"] = get_settings()
    ctx["bot_username"] = state.bot_username
    ctx["bot_link"] = bot_link()
    ctx["ref_enabled"] = rt.get_bool("ref_enabled")
    ctx["ref_inviter_days"] = rt.get_int("ref_inviter_days")
    ctx["ref_invitee_days"] = rt.get_int("ref_invitee_days")
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def flash(request: Request, text: str) -> None:
    request.session["flash"] = text


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


async def csrf_check(request: Request) -> None:
    form = await request.form()
    expected = request.session.get("csrf", "")
    given = str(form.get("csrf", ""))
    if not expected or not hmac.compare_digest(expected.encode(), given.encode()):
        raise HTTPException(403, "Неверный CSRF-токен. Обновите страницу и повторите.")


def message_page(request: Request, title: str, text: str, status: int = 200, link: tuple[str, str] | None = None):
    return render(request, "message.html", {"title": title, "text": text, "link": link}, status)
