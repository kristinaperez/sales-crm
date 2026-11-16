#!/bin/sh
# Запуск контейнера: миграции -> начальные данные -> веб-сервер.
set -e
alembic upgrade head
python -m app.cli init
if [ "${SEED_DEMO:-0}" = "1" ]; then
  python -m app.cli seed-demo
fi
# Один процесс: для SQLite и команды до 11 человек этого достаточно; запросы идут в пуле потоков.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
