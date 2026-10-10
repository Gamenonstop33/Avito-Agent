"""Токен пользователя VK (руководитель сообщества) для загрузки фото на стену.

Сейчас — бессрочный токен Kate Mobile (VK_APP_ID=2685278, scope с offline). Вход через Kate в окне, которым
управляет программа, VK отклоняет (Access denied [15]), поэтому: в обычном браузере под целевым аккаунтом открыть
https://oauth.vk.com/authorize?client_id=2685278&display=page&redirect_uri=https://oauth.vk.com/blank.html&scope=wall,photos,groups,offline&response_type=token&v=5.199
→ «Разрешить» → скопировать адрес страницы → python -m services.content.vk_token --paste (токен из буфера в .env).

Ниже — прежний способ для мини-приложений:

Токен мини-приложения живёт 24 ч, бессрочный (offline) этому типу приложений недоступен. Раз в сутки:
  python -m services.content.vk_token --login
откроется окно Edge (отдельный профиль data/vk_browser, вход в VK в нём сохраняется) — нажать «Разрешить»;
токен запишется в .env (VK_USER_TOKEN). Полностью без человека нельзя: на программное нажатие «Разрешить»
и на повторный заход без согласия VK отвечает Security Error. Секреты не выводит.
"""
import asyncio
import re
import ssl
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode

import certifi
import httpx
from dotenv import set_key
from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

from shared.config import DATA_DIR, ROOT, env
from shared.vk import VKError

BROWSER = env("VK_BROWSER") or "chrome"  # chrome | msedge — установленный браузер, у каждого свой профиль
PROFILE = DATA_DIR / ("vk_browser" if BROWSER == "msedge" else f"vk_browser_{BROWSER}")
AUTH = "https://oauth.vk.com/authorize?" + urlencode({
    "client_id": env("VK_APP_ID") or "54798652", "display": "page", "redirect_uri": "https://oauth.vk.com/blank.html",
    "scope": env("VK_SCOPE") or "wall,photos", "response_type": "token", "v": "5.199",
    "revoke": 1})  # всегда показывать «Разрешить»: без него VK при уже выданном доступе отвечает Security Error
# Для сервера: токен мини-приложения привязан к IP, где нажали «Разрешить» (с другого IP — «access_token was given
# to another ip address»). Поэтому человек присылает адрес с одноразовым кодом, а токен по коду получает сам сервер
# (защищённый ключ приложения VK_APP_SECRET) — так токен выдан на IP сервера.
CODE_AUTH = AUTH.replace("response_type=token", "response_type=code")
_cache = {"token": "", "exp": 0.0}  # .env читается один раз при старте — свежий токен держим в памяти


class NeedLogin(Exception):
    pass


async def _logged_in(ctx) -> bool:
    """Без входа VK отвечает на ссылку «application is disabled» (приложение видно только своему администратору)."""
    r = await ctx.request.get(AUTH, max_redirects=0, fail_on_status_code=False)
    return not (r.status == 401 and "disabled" in await r.text())


def _step(text: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)


async def fetch_token(login: bool = False) -> str:
    """Открывает ссылку авторизации в сохранённом профиле и забирает токен из адреса страницы."""
    stage = "запуск браузера"
    async with async_playwright() as pw:
        try:
            ctx = await pw.chromium.launch_persistent_context(str(PROFILE), channel=BROWSER, headless=not login,
                                                              chromium_sandbox=True)
        except PWError:  # на сервере браузера нет: токен получают на ПК и переносят в .env сервера
            raise NeedLogin("токен VK недействителен, а браузера здесь нет — получите токен на ПК "
                            "(python -m services.content.vk_token --paste) и перенесите VK_USER_TOKEN на сервер")
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if not await _logged_in(ctx):
                if not login:
                    raise NeedLogin("нужен вход в VK — запустите python -m services.content.vk_token --login")
                stage = "вход в VK"
                await page.goto("https://vk.com/")
                _step("Окно открыто. Войдите в VK; окно НЕ закрывайте — оно само перейдёт к выдаче токена. Жду 10 минут…")
                for _ in range(120):
                    await asyncio.sleep(5)
                    if await _logged_in(ctx):
                        _step("Вход в VK выполнен.")
                        break
                else:
                    raise NeedLogin("вход в VK не выполнен за 10 минут")
            if not login:  # «Разрешить» должен нажать человек: на программное нажатие VK отвечает Security Error
                raise NeedLogin("доступ к VK истёк — запустите python -m services.content.vk_token --login "
                                "и нажмите «Разрешить» (раз в сутки)")
            stage = "страница разрешения"
            await page.goto(AUTH)
            _step("Открыта страница разрешения. Нажмите «Разрешить». Жду 5 минут…")
            try:
                await page.wait_for_url(lambda u: "access_token=" in u, timeout=300_000)
            except PWTimeout:
                raise NeedLogin("«Разрешить» не нажато за 5 минут")
            q = parse_qs(page.url.split("#", 1)[1])
            token, ttl = q["access_token"][0], int(q.get("expires_in", ["0"])[0])
            _step("Токен получен.")
        except PWError as e:
            raise NeedLogin(f"окно браузера закрылось на шаге «{stage}» ({str(e).splitlines()[0][:100]})")
        finally:
            try:
                await ctx.close()
            except PWError:
                pass
    return _save(token, ttl)


def _save(token: str, ttl: int) -> str:
    exp = int(time.time()) + ttl if ttl else 0
    set_key(ROOT / ".env", "VK_USER_TOKEN", token, quote_mode="never")
    set_key(ROOT / ".env", "VK_USER_TOKEN_EXPIRES", str(exp), quote_mode="never")
    _cache.update(token=token, exp=exp)
    return token


def parse_url(text: str) -> tuple[str, int, int] | None:
    """Адрес страницы после «Разрешить» (…blank.html#access_token=…) → (токен, срок в секундах, id пользователя)."""
    q = parse_qs((text or "").strip().split("#", 1)[-1])
    if "access_token" not in q:
        return None
    return q["access_token"][0], int(q.get("expires_in", ["0"])[0]), int(q.get("user_id", ["0"])[0])


def parse_code(text: str) -> str | None:
    """Одноразовый код из адреса страницы после «Разрешить» (…blank.html?code=… или #code=…)."""
    m = re.search(r"[?#&]code=([^&#\s]+)", text or "")
    return m.group(1) if m else None


async def exchange(code: str) -> tuple[str, int, int]:
    """Код → (токен, срок в секундах, id пользователя); токен выдаётся на IP того, кто меняет код."""
    async with httpx.AsyncClient(timeout=30, verify=ssl.create_default_context(cafile=certifi.where())) as c:
        r = (await c.get("https://oauth.vk.com/access_token", params={
            "client_id": env("VK_APP_ID") or "54798652", "client_secret": env("VK_APP_SECRET"),
            "redirect_uri": "https://oauth.vk.com/blank.html", "code": code})).json()
    if "access_token" not in r:
        raise VKError(f"VK не выдал токен по коду: {r.get('error_description') or r.get('error')}")
    return r["access_token"], int(r.get("expires_in") or 0), int(r.get("user_id") or 0)


def from_clipboard() -> str:
    """Токен из адреса страницы, скопированного в обычном браузере после «Разрешить» (вход в окне, которым
    управляет программа, VK для Kate отклоняет: Access denied [15]). Адрес в чат/консоль не попадает."""
    got = parse_url(subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                                   capture_output=True, text=True).stdout)
    if not got:
        raise NeedLogin("в буфере обмена нет адреса с access_token — скопируйте адрес страницы после «Разрешить»")
    return _save(got[0], got[1])


async def check(token: str) -> VKError | None:
    """None — токен рабочий, иначе ошибка VK (code 5 — токен недействителен, остальные — отказ VK)."""
    if not token:
        return VKError("токена нет", 5)
    async with httpx.AsyncClient(timeout=30, verify=ssl.create_default_context(cafile=certifi.where())) as c:
        d = (await c.post("https://api.vk.com/method/users.get", data={"access_token": token, "v": "5.199"})).json()
    e = d.get("error")
    return VKError(f"VK: {e.get('error_msg')} (код {e.get('error_code')})", e.get("error_code") or 0) if e else None


async def ensure_token() -> str:
    """Действующий токен: из памяти/.env, а если истёк или истекает в ближайший час — обновляет.

    Если VK отказывает не из-за токена (flood control и т. п.) — VKError с настоящей причиной, токен не трогаем."""
    token = _cache["token"] or env("VK_USER_TOKEN")
    exp = _cache["exp"] or int(env("VK_USER_TOKEN_EXPIRES") or 0)
    if not exp or exp - time.time() > 3600:
        err = await check(token)
        if not err:
            return token
        if err.code != 5:
            raise err
    return await fetch_token()


async def main() -> None:
    try:
        token = from_clipboard() if "--paste" in sys.argv else await fetch_token(login="--login" in sys.argv)
    except NeedLogin as e:
        sys.exit(f"Токен не получен: {e}")
    err = await check(token)
    print(f"VK выдал токен, но он не проходит проверку: {err}" if err else "Токен получен и записан в .env")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
