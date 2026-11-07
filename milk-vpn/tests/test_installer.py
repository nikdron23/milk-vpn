"""Проверка deploy/install.sh в режиме --dry-run (Docker не нужен) и синтаксиса всех скриптов."""
import json
import os
import pathlib
import stat
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "deploy").glob("*.sh"))


def run_install(tmp_path, **extra):
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path), "NONINTERACTIVE": "1",
        "INSTALL_DIR": str(tmp_path / "inst"), "DOMAIN": "VPN.Example.com",
        "BOT_TOKEN": "123456789:" + "A" * 35, "ADMIN_TG_IDS": "42", "BRAND_NAME": "Milk VPN",
    }
    env.update(extra)
    return subprocess.run(
        ["bash", str(ROOT / "deploy" / "install.sh"), "--dry-run"],
        env=env, capture_output=True, text=True, timeout=60,
    )


def load_settings(env_file: pathlib.Path) -> dict:
    code = (
        f"import json,sys; sys.path.insert(0, {str(ROOT)!r}); from app.config import Settings; "
        f"s=Settings(_env_file={str(env_file)!r}); "
        "print(json.dumps({'problems': s.problems(), 'base_url': s.base_url, 'brand': s.brand_name, "
        "'admin_password': s.admin_password, 'secret_len': len(s.secret_key), 'robokassa_login': s.robokassa_login, "
        "'robokassa_test': s.robokassa_test, 'admin_ids': sorted(s.admin_ids), 'rk_pass2': s.robokassa_password2}))"
    )
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True,
                         cwd=str(env_file.parent), timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_have_valid_syntax(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert script.read_bytes().startswith(b"#!/usr/bin/env bash\n")
    assert b"\r\n" not in script.read_bytes()  # только LF, иначе на Linux не запустится


def test_dry_run_generates_valid_env(tmp_path):
    r = run_install(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    inst = tmp_path / "inst"
    env_file = inst / ".env"
    assert env_file.exists() and stat.S_IMODE(env_file.stat().st_mode) == 0o600
    text = env_file.read_text()
    assert "DOMAIN='vpn.example.com'" in text  # домен приведён к нижнему регистру
    assert "BOT_TOKEN='123456789:" in text
    s = load_settings(env_file)
    assert s["problems"] == []
    assert s["base_url"] == "https://vpn.example.com" and s["brand"] == "Milk VPN"
    assert s["secret_len"] == 48 and len(s["admin_password"]) == 18  # пароль сгенерирован
    assert s["admin_ids"] == [42]
    # проект скопирован, тесты и секреты — нет
    assert (inst / "app" / "main.py").exists() and (inst / "docker-compose.yml").exists()
    assert (inst / "deploy" / "backup.sh").exists()
    assert not (inst / "tests").exists()


def test_dry_run_robokassa_and_special_chars(tmp_path):
    r = run_install(tmp_path, ADMIN_PASSWORD="p@ss#w0rd $x1", ROBOKASSA_LOGIN="shop",
                    ROBOKASSA_PASSWORD1="a b#c", ROBOKASSA_PASSWORD2="d$e#f g")
    assert r.returncode == 0, r.stdout + r.stderr
    s = load_settings(tmp_path / "inst" / ".env")
    assert s["admin_password"] == "p@ss#w0rd $x1"  # спецсимволы переживают .env без искажений
    assert s["rk_pass2"] == "d$e#f g" and s["robokassa_login"] == "shop" and s["robokassa_test"] is True


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"ADMIN_PASSWORD": "it's-bad-pass"}, "нельзя"),
        ({"ADMIN_PASSWORD": "back\\slash-pass"}, "нельзя"),
        ({"ADMIN_PASSWORD": "short"}, "минимум 8"),
        ({"DOMAIN": "not a domain"}, "DOMAIN"),
        ({"BOT_TOKEN": "bad-token"}, "BOT_TOKEN"),
    ],
)
def test_dry_run_rejects_bad_input(tmp_path, extra, message):
    r = run_install(tmp_path, **extra)
    assert r.returncode != 0
    assert message in (r.stdout + r.stderr)
    assert not (tmp_path / "inst" / ".env").exists()


def test_rerun_keeps_existing_env(tmp_path):
    assert run_install(tmp_path).returncode == 0
    env_file = tmp_path / "inst" / ".env"
    first = env_file.read_text()
    # повторный запуск (неинтерактивно «сохранить настройки» = да) ничего не перезаписывает
    r = run_install(tmp_path, DOMAIN="", BOT_TOKEN="")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env_file.read_text() == first
