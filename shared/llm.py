"""OpenAI-совместимый клиент LLM (DeepSeek, YandexGPT через совместимый шлюз и т.п.)."""
import asyncio
import json
import logging
import ssl
import time

import certifi
import httpx

from shared.config import env

log = logging.getLogger("llm")
ALARM_EVERY = 3600   # сбой LLM — админу не чаще раза в час на процесс (13–14.10: 15 ч без ответов, никто не знал)
_alarm_at = 0.0


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
        self._http = httpx.AsyncClient(
            base_url=(base_url or env("LLM_BASE_URL")).rstrip("/"), timeout=timeout,
            headers={"Authorization": f"Bearer {api_key or env('LLM_API_KEY')}"},
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
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(self.tries):
            try:
                r = await self._http.post("/chat/completions", json=body)
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

    async def chat_json(self, system: str, user: str, **kw) -> dict:
        return json.loads(await self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True, **kw))
