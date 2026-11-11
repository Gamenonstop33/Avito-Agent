"""OpenAI-совместимый клиент LLM (DeepSeek, YandexGPT через совместимый шлюз и т.п.)."""
import asyncio
import json
import logging
import re
import ssl
import time
from datetime import datetime, timedelta, timezone

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
# Сбой у провайдера (03.11, Леонид: «если дипсик падает, пингуй каждую минуту, ожил — алерт»; 03.11 12:04–12:50 МСК
# DeepSeek отдавал 503): после алерта процесс раз в PING_EVERY шлёт пробный запрос в 1 токен; ответил — в группу своего
# аккаунта «нейросеть снова отвечает» со временем начала сбоя. Процессов с LLM несколько — начало сбоя в sales.settings
# llm_down, сообщение шлёт тот, кто снял отметку. Ошибки нашего запроса (400 и т.п.) — не сбой провайдера, не пингуем
PING_EVERY = 60
PROVIDER_DOWN = {401, 402, 429}
_watch: asyncio.Task | None = None
TZ = timezone(timedelta(hours=int(env("OWNER_UTC_OFFSET", "3"))))
# Резервная нейросеть (03.11, Леонид: «резервный — YandexGPT», модель — DeepSeek V4.1 Flash в Yandex AI Studio:
# у YandexGPT Pro окно 32 тыс. токенов, наш промпт больше; из РФ работает, оплата в рублях). Только ответы клиентам
# (LLM(backup=True) в dialog): сбой провайдера у DeepSeek — тот же запрос резервной; пока идёт сбой (watch_up пингует
# DeepSeek) — сразу резервной, без ожидания таймаутов. Переход и возврат — в группу своего аккаунта (решение 03.11).
BACKUP_URL = env("LLM_BACKUP_URL", "https://llm.api.cloud.yandex.net/v1")
BACKUP_FOLDER = env("LLM_BACKUP_FOLDER", "")
BACKUP_MODEL = env("LLM_BACKUP_MODEL", "deepseek-v4.1-flash")
BACKUP_NAME = "Yandex Cloud"
# Основная (deepseek-chat) у DeepSeek — deepseek-flash без рассуждений; в Yandex та же Flash по умолчанию рассуждает:
# ответы медленнее в 3–5 раз и расходятся с основной (стенд 03.11) — выключаем
BACKUP_EXTRA = {"reasoning_effort": "none"}
BACKUP_MARK = "|backup"   # в отметке llm_down: о переходе на резерв в группу уже написали
_down_err: BaseException | None = None   # с чего начался текущий сбой (для сообщений)
_backup_said = False                     # процесс уже сообщал о переходе на резерв за этот сбой


def on_reserve() -> bool:
    return bool(_reserve_at)


def down() -> bool:
    """В этом процессе идёт сбой провайдера: watch_up ещё не дождался ответа DeepSeek."""
    return _watch is not None and not _watch.done()


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


def describe(e: BaseException, who: str = "DeepSeek") -> str:
    """Причина сбоя LLM по-человечески — для алертов владельцу и админу."""
    if isinstance(e, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        return f"{who} не ответил вовремя"
    code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
    return {402: f"закончился баланс {who}", 401: f"ключ {who} не подходит",
            429: f"{who} ограничил частоту запросов"}.get(code) or (
        f"{who} отвечает ошибкой {code}" if code else
        f"{who} недоступен (нет связи)" if isinstance(e, httpx.TransportError) else f"ошибка программы ({type(e).__name__})")


def provider_down(e: BaseException) -> bool:
    """Сбой на стороне провайдера (не отвечает, 5xx, лимит, ключ/деньги), а не ошибка нашего запроса."""
    if isinstance(e, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException, httpx.TransportError)):
        return True
    code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
    return bool(code) and (code >= 500 or code in PROVIDER_DOWN)


async def watch(e: BaseException) -> None:
    """Сбой провайдера — отметить начало и пинговать, пока не оживёт."""
    global _watch, _down_err, _backup_said
    if provider_down(e) and not down():
        _down_err, _backup_said = e, False
        try:
            await _mark_down()
        except Exception:
            log.exception("сбой нейросети: отметка в БД не записана")
        _watch = asyncio.create_task(watch_up())


async def alarm(e: BaseException, backup: BaseException | None = None) -> None:
    """Админу в VK: нейросеть не отвечает — бот не может отвечать клиентам. Сбой провайдера — следим, когда оживёт.
    backup — резервная тоже не ответила."""
    global _alarm_at
    await watch(e)
    if time.monotonic() - _alarm_at < ALARM_EVERY:
        return
    _alarm_at = time.monotonic()
    from shared import alerts   # alerts тянет VK — импорт только при сбое
    await alerts.notify_admins(f"🚨 Нейросеть не отвечает: {describe(e)}"
                               + (f", резервная — тоже: {describe(backup, BACKUP_NAME)}" if backup else "")
                               + ". Бот не может отвечать клиентам "
                               "(кроме приветствия с прайсом), случаи уходят владельцу. Повтор — не раньше чем через час."
                               + (" Проверяю раз в минуту — как оживёт, напишу в группу." if provider_down(e) else ""))


async def _say_backup(e: BaseException) -> None:
    """Первый ответ резервной за сбой — в группу своего аккаунта (пишет процесс, первым дописавший отметку)."""
    from shared import alerts
    db = await connect_one()
    try:
        since = await db.fetchval(
            "UPDATE sales.settings SET value=value || $1, updated_at=now() WHERE key='llm_down' AND value <> '' "
            "AND value NOT LIKE '%' || $1 RETURNING value", BACKUP_MARK)
        if since:
            at = datetime.fromisoformat(since.removesuffix(BACKUP_MARK)).astimezone(TZ)
            await alerts.send_alert(db, f"{alerts.TAG_INFO} · ⚠️ Нейросеть не отвечает с {at:%H:%M} МСК: {describe(e)}. "
                                        "Бот отвечает клиентам через резервную (DeepSeek в Yandex Cloud) — без ответа "
                                        "никто не остаётся. Как основная оживёт, бот вернётся на неё сам и напишет сюда.")
    finally:
        await db.close()


async def _mark_down() -> None:
    db = await connect_one()
    try:   # первый заметивший пишет начало сбоя, остальные его не сдвигают
        await db.execute("INSERT INTO sales.settings (key, value, updated_by) VALUES ('llm_down', $1, 'llm') "
                         "ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=now() WHERE sales.settings.value = ''",
                         datetime.now(timezone.utc).isoformat())
    finally:
        await db.close()


async def _mark_up() -> tuple[datetime, bool] | None:
    """Снимает отметку сбоя. Вернёт начало сбоя и был ли переход на резерв, если отметку снял этот процесс
    (сообщение шлёт только он)."""
    db = await connect_one()
    try:
        since = await db.fetchval(
            "WITH o AS (SELECT value FROM sales.settings WHERE key='llm_down' AND value <> '' FOR UPDATE) "
            "UPDATE sales.settings s SET value='', updated_at=now() FROM o WHERE s.key='llm_down' RETURNING o.value")
    finally:
        await db.close()
    return (datetime.fromisoformat(since.removesuffix(BACKUP_MARK)), since.endswith(BACKUP_MARK)) if since else None


async def _say_up(since: datetime, backup: bool = False) -> None:
    from shared import alerts
    mins = max(1, round((datetime.now(timezone.utc) - since).total_seconds() / 60))
    db = await connect_one()
    try:
        await alerts.send_alert(db, f"{alerts.TAG_INFO} · ✅ Нейросеть снова отвечает: DeepSeek не отвечал с "
                                    f"{since.astimezone(TZ):%H:%M} МСК (около {mins} мин). "
                                    + ("Бот вернулся на основную нейросеть." if backup
                                       else "Бот снова отвечает клиентам сам."))
    finally:
        await db.close()


async def watch_up() -> None:
    """Раз в PING_EVERY — пробный запрос; ответил — снять отметку сбоя и сообщить в группу."""
    llm = LLM(timeout=30, tries=1)
    try:
        while True:
            await asyncio.sleep(PING_EVERY)
            try:
                await llm._chat([{"role": "user", "content": "ping"}], False, 0, 1)
            except Exception as e:
                log.info("нейросеть всё ещё не отвечает: %s", describe(e))
                continue
            log.info("нейросеть снова отвечает")
            try:
                if up := await _mark_up():
                    await _say_up(*up)
            except Exception:
                log.exception("нейросеть ожила, но сообщение не отправлено")
            return
    finally:
        await llm.close()


class LLM:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 timeout: float = 180, tries: int = 4, backup: bool = False):
        """timeout — на один запрос, tries — попыток при обрыве/429/5xx. Ответам клиентам — коротко (dialog),
        ночным разборам — с запасом. backup — при сбое DeepSeek отвечать резервной (если она задана в .env)."""
        self.model, self.tries, self.timeout = model or env("LLM_MODEL", "deepseek-chat"), tries, timeout
        self._main = api_key or env("LLM_API_KEY")
        self._reserve = None if api_key else env("LLM_API_KEY_RESERVE") or None
        self._http = httpx.AsyncClient(
            base_url=(base_url or env("LLM_BASE_URL")).rstrip("/"), timeout=timeout,
            verify=ssl.create_default_context(cafile=certifi.where()))
        self._backup = backup and BACKUP_FOLDER and env("LLM_BACKUP_KEY") and httpx.AsyncClient(
            base_url=BACKUP_URL.rstrip("/"), timeout=timeout, verify=ssl.create_default_context(cafile=certifi.where()),
            headers={"Authorization": f"Bearer {env('LLM_BACKUP_KEY')}", "OpenAI-Project": BACKUP_FOLDER})
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    async def close(self) -> None:
        await self._http.aclose()
        if self._backup:
            await self._backup.aclose()

    async def chat(self, messages: list[dict], *, json_mode: bool = False, temperature: float = 0.3,
                   max_tokens: int = 4000) -> str:
        if self._backup and down():   # сбой уже идёт — DeepSeek проверяет watch_up, клиенту отвечает резервная
            return await self._via_backup(None, messages, json_mode, temperature, max_tokens)
        try:
            if not self._backup:
                return await self._chat(messages, json_mode, temperature, max_tokens)
            # повторы DeepSeek не дольше одного таймаута — иначе резервной не останется времени до REPLY_DEADLINE
            return await asyncio.wait_for(self._chat(messages, json_mode, temperature, max_tokens), self.timeout)
        except (httpx.HTTPError, RuntimeError, TimeoutError) as e:
            log.error("LLM: %s", describe(e))
            if self._backup and provider_down(e):
                return await self._via_backup(e, messages, json_mode, temperature, max_tokens)
            await alarm(e)
            raise

    async def _via_backup(self, e: BaseException | None, messages: list[dict], json_mode: bool, temperature: float,
                          max_tokens: int) -> str:
        """Тот же запрос резервной нейросети. e — сбой DeepSeek только что (None — сбой шёл и раньше)."""
        global _backup_said
        if e:
            await watch(e)
        body = {"model": BACKUP_MODEL if "://" in BACKUP_MODEL else f"gpt://{BACKUP_FOLDER}/{BACKUP_MODEL}",
                "messages": messages, "temperature": temperature, "max_tokens": max_tokens, **BACKUP_EXTRA}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            text = await self._ask(body, None, self._backup)
        except (httpx.HTTPError, RuntimeError) as b:
            log.error("резервная нейросеть: %s", describe(b, BACKUP_NAME))
            await alarm(e or _down_err or b, b)
            raise
        if not _backup_said:
            _backup_said = True
            try:
                await _say_backup(e or _down_err or TimeoutError())
            except Exception:
                log.exception("резервная нейросеть ответила, но сообщение в группу не отправлено")
        return text

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

    async def _ask(self, body: dict, key: str | None, http: httpx.AsyncClient | None = None) -> str:
        """key None — ключ уже в заголовках клиента http (резервная)."""
        for attempt in range(self.tries):
            try:
                r = await (http or self._http).post("/chat/completions", json=body,
                                                    headers=self._auth(key) if key else None)
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
