"""OpenAI-совместимый клиент LLM (DeepSeek, YandexGPT через совместимый шлюз и т.п.)."""
import asyncio
import json
import ssl

import certifi
import httpx

from shared.config import env


class LLM:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        self.model = model or env("LLM_MODEL", "deepseek-chat")
        self._http = httpx.AsyncClient(
            base_url=(base_url or env("LLM_BASE_URL")).rstrip("/"), timeout=180,
            headers={"Authorization": f"Bearer {api_key or env('LLM_API_KEY')}"},
            verify=ssl.create_default_context(cafile=certifi.where()))
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    async def close(self) -> None:
        await self._http.aclose()

    async def chat(self, messages: list[dict], *, json_mode: bool = False, temperature: float = 0.3,
                   max_tokens: int = 4000) -> str:
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(4):
            try:
                r = await self._http.post("/chat/completions", json=body)
            except httpx.TransportError:  # обрыв соединения — повторяем
                if attempt == 3:
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
