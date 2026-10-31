"""OpenAI-совместимый клиент LLM (DeepSeek, YandexGPT через совместимый шлюз и т.п.)."""
import asyncio
import json
import logging
import re
import ssl
import time

import certifi
import httpx

from shared.config import NS, env
from shared.db import connect_one

log = logging.getLogger("llm")
ALARM_EVERY = 3600   # сбой LLM — админу не чаще раза в час на процесс (13–14.10: 15 ч без ответов, никто не знал)
_alarm_at = 0.0
# Резервный ключ (28.10): LLM_API_KEY_RESERVE — только когда на основном кончились деньги (402); владельцу и админу
# в VK — при переходе и при возврате, без напоминаний. На резервном раз в MAIN_CHECK смотрим баланс основного:
# MAIN_BACK и больше — снова основной (порог — чтобы у нуля не прыгать туда-обратно).
MAIN_CHECK = 600
MAIN_BACK = 1.0
_reserve_at = 0.0    # процесс на резервном ключе: когда последний раз смотрели основной (monotonic); 0 — на основном
_main_ok = False     # основной в этом процессе сработал и отмечен в БД


def on_reserve() -> bool:
    return bool(_reserve_at)


def mask(key: str) -> str:
    return f"{key[:8]}…{key[-4:]}"


async def _key_state(value: str, text: str) -> None:
    """Ключ сменился — отметка в sales.settings и сообщение владельцу и админу. Процессов с LLM несколько, каждый
    переключается сам; пишет только тот, кто первым сменил отметку (а 'main' — только после 'reserve')."""
    if NS:
        return   # ключ на оба аккаунта Avito общий — пишет основная копия бота, иначе два одинаковых сообщения (29.10)
    from shared import alerts   # alerts тянет VK — импорт только при смене ключа
    try:
        db = await connect_one()
    except Exception:
        log.exception("ключ нейросети: БД недоступна, уведомление не отправлено")
        return
    try:
        changed = await db.fetchval(
            "INSERT INTO sales.settings (key, value, updated_by) VALUES ('llm_key', $1, 'llm') "
            "ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=now() WHERE sales.settings.value <> $1 "
            "RETURNING 1", value) if value == "reserve" else await db.fetchval(
            "UPDATE sales.settings SET value=$1, updated_at=now() WHERE key='llm_key' AND value <> $1 "
            "RETURNING 1", value)
        if changed:
            await alerts.send_alert(db, f"{alerts.TAG_INFO} · {text}")
    except Exception:
        log.exception("ключ нейросети: уведомление не отправлено")
    finally:
        await db.close()


def describe(e: BaseException) -> str:
    """Причина сбоя LLM по-человечески — для алертов владельцу и админу."""
    if isinstance(e, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        return "DeepSeek не ответил вовремя"
    code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
    return {402: "закончился баланс DeepSeek", 401: "ключ DeepSeek не подходит",
            429: "DeepSeek ограничил частоту запросов"}.get(code) or (
        f"DeepSeek отвечает ошибкой {code}" if code else
        "DeepSeek недоступен (нет связи)" if isinstance(e, httpx.TransportError) else f"ошибка программы ({type(e).__name__})")


async def alarm(e: BaseException) -> None:
    """Админу в VK: нейросеть не отвечает — бот не может отвечать клиентам."""
    global _alarm_at
    if time.monotonic() - _alarm_at < ALARM_EVERY:
        return
    _alarm_at = time.monotonic()
    from shared import alerts   # alerts тянет VK — импорт только при сбое
    await alerts.notify_admins(f"🚨 Нейросеть не отвечает: {describe(e)}. Бот не может отвечать клиентам "
                               "(кроме приветствия с прайсом), случаи уходят владельцу. Повтор — не раньше чем через час.")


class LLM:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 timeout: float = 180, tries: int = 4):
        """timeout — на один запрос, tries — попыток при обрыве/429/5xx. Ответам клиентам — коротко (dialog),
        ночным разборам — с запасом."""
        self.model, self.tries = model or env("LLM_MODEL", "deepseek-chat"), tries
        self._main = api_key or env("LLM_API_KEY")
        self._reserve = None if api_key else env("LLM_API_KEY_RESERVE") or None
        self._http = httpx.AsyncClient(
            base_url=(base_url or env("LLM_BASE_URL")).rstrip("/"), timeout=timeout,
            verify=ssl.create_default_context(cafile=certifi.where()))
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    async def close(self) -> None:
        await self._http.aclose()

    async def chat(self, messages: list[dict], *, json_mode: bool = False, temperature: float = 0.3,
                   max_tokens: int = 4000) -> str:
        try:
            return await self._chat(messages, json_mode, temperature, max_tokens)
        except (httpx.HTTPError, RuntimeError) as e:
            log.error("LLM: %s", describe(e))
            await alarm(e)
            raise

    async def _chat(self, messages: list[dict], json_mode: bool, temperature: float, max_tokens: int) -> str:
        global _main_ok
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        key = await self._key()
        try:
            text = await self._ask(body, key)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 402 or key != self._main or not self._reserve:
                raise
            await self._to_reserve()
            return await self._ask(body, self._reserve)   # на основном кончились деньги — тот же запрос резервным
        if key == self._main and self._reserve and not _main_ok:
            _main_ok = True
            await _key_state("main", f"✅ Основной ключ нейросети (DeepSeek, {mask(self._main)}) снова работает — "
                                     "бот вернулся на него, резервный не тратится.")
        return text

    async def _key(self) -> str:
        """Основной ключ; после 402 — резервный, пока на основном не появятся деньги."""
        global _reserve_at
        if not _reserve_at or not self._reserve:
            return self._main
        if time.monotonic() - _reserve_at < MAIN_CHECK:
            return self._reserve
        _reserve_at = time.monotonic()
        info = await self.balance()
        if info and info[0] >= MAIN_BACK:
            _reserve_at = 0.0
            log.warning("на основном ключе нейросети %.2f %s — возвращаемся на него", *info)
            return self._main
        return self._reserve

    async def _to_reserve(self) -> None:
        global _reserve_at, _main_ok
        if _reserve_at:
            return   # параллельный запрос уже переключил
        _reserve_at, _main_ok = time.monotonic(), False
        log.warning("на основном ключе нейросети кончились деньги — работаем на резервном")
        await _key_state("reserve", f"💸 На основном ключе нейросети (DeepSeek, {mask(self._main)}) кончились "
                                    f"деньги — бот перешёл на резервный ({mask(self._reserve)}) и отвечает как обычно. "
                                    "Пополните основной — бот вернётся на него сам.")

    def _auth(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}"}

    async def _ask(self, body: dict, key: str) -> str:
        for attempt in range(self.tries):
            try:
                r = await self._http.post("/chat/completions", json=body, headers=self._auth(key))
            except httpx.TransportError:  # обрыв соединения — повторяем
                if attempt == self.tries - 1:
                    raise
                await asyncio.sleep(2 ** attempt * 3)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(2 ** attempt * 3)
                continue
            r.raise_for_status()
            d = r.json()
            for k in self.usage:
                self.usage[k] += d.get("usage", {}).get(k, 0)
            return d["choices"][0]["message"]["content"]
        r.raise_for_status()
        raise RuntimeError("LLM недоступна")

    async def balance(self) -> tuple[float, str] | None:
        """Остаток на основном ключе (DeepSeek: /user/balance) — (сумма, валюта); провайдер так не умеет или ошибка —
        None."""
        url = re.sub(r"/v1$", "", str(self._http.base_url).rstrip("/")) + "/user/balance"
        try:
            r = await self._http.get(url, headers=self._auth(self._main))
            r.raise_for_status()
            info = (r.json().get("balance_infos") or [None])[0]
            return (float(info["total_balance"]), info["currency"]) if info else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("баланс нейросети не получен: %s", e)
            return None

    async def chat_json(self, system: str, user: str, **kw) -> dict:
        return json.loads(await self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True, **kw))
