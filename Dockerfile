FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=UTC
WORKDIR /app
COPY requirements.txt .
# шрифт с кириллицей для картинки переписки в алертах (shared/chatshot)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
COPY shared shared
COPY services services
COPY knowledge knowledge
COPY scripts scripts
