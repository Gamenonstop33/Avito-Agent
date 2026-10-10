"""avito-gateway: uvicorn services.avito_gateway.main:app --port 8001"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from services.avito_gateway.worker import Gateway, ensure_account
from shared.avito import AvitoClient
from shared.config import env, env_list
from shared.db import connect, single_instance

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await connect(SCHEMA)
    await single_instance(pool, "avito_gateway")
    avito = AvitoClient(env("AVITO_CLIENT_ID"), env("AVITO_CLIENT_SECRET"))
    user_id = await ensure_account(pool, avito, env_list("AVITO_TEST_CHAT_IDS"))
    interval = 1.0 if env("REPLY_INSTANT") == "1" else float(env("GATEWAY_POLL_SECONDS", "3"))  # тесты — опрос раз в 1 с
    task = asyncio.create_task(Gateway(pool, avito, user_id).run(interval))
    yield
    task.cancel()
    await avito.close()
    pool.terminate()  # одно соединение держит блокировку «единственной копии»


app = FastAPI(title="avito-gateway", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
