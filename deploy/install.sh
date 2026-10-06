#!/usr/bin/env bash
# Первичная установка sayr-gorets на VPS одной командой (от root):
#
#   git clone git@github.com:abdulaziztag/sayr-gorec.git /root/Projects/sayr-gorets
#   /root/Projects/sayr-gorets/deploy/install.sh
#
# Делает всё, что не требует ваших секретов и телефона: роль и база в Postgres,
# venv, .env из шаблона с готовыми секретами HMAC и API, миграции, unit-файлы.
# После него остаётся вписать в .env четыре значения и выполнить `gorets login`.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/Projects/sayr-gorets}"
DB_NAME="${DB_NAME:-sayr_gorets}"
DB_ROLE="${DB_ROLE:-sayr_gorets}"
cd "$PROJECT_DIR"

command -v uv >/dev/null || { echo "Нужен uv: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
command -v psql >/dev/null || { echo "Нужен Postgres (psql не найден)"; exit 1; }

echo "==> Роль и база $DB_NAME"
DB_PASSWORD="$(openssl rand -hex 16)"
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_ROLE'" | grep -q 1; then
    echo "    роль $DB_ROLE уже есть — пароль не меняю"
    DB_PASSWORD=""
else
    sudo -u postgres psql -qc "CREATE ROLE $DB_ROLE LOGIN PASSWORD '$DB_PASSWORD';"
fi
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    echo "    база $DB_NAME уже есть"
else
    sudo -u postgres psql -qc "CREATE DATABASE $DB_NAME OWNER $DB_ROLE ENCODING 'UTF8';"
fi

echo "==> Зависимости (uv sync --frozen --no-dev --extra mcp)"
uv sync --frozen --no-dev --extra mcp

echo "==> Настройки"
if [[ -f .env ]]; then
    echo "    .env уже есть — не трогаю"
else
    cp .env.example .env && chmod 600 .env
    sed -i "s|^GORETS_AUTHOR_HMAC_SECRET=.*|GORETS_AUTHOR_HMAC_SECRET=$(openssl rand -hex 32)|" .env
    sed -i "s|^GORETS_API_TOKEN=.*|GORETS_API_TOKEN=$(openssl rand -hex 32)|" .env
    if [[ -n "$DB_PASSWORD" ]]; then
        sed -i "s|^GORETS_DATABASE_URL=.*|GORETS_DATABASE_URL=postgresql+psycopg://$DB_ROLE:$DB_PASSWORD@localhost:5432/$DB_NAME|" .env
    fi
    echo "    создан .env с секретами HMAC и API"
fi
mkdir -p reports

echo "==> Схема базы"
if grep -q "^GORETS_DATABASE_URL=.*ПАРОЛЬ" .env; then
    echo "    в .env ещё пароль-заглушка: впишите пароль роли и запустите .venv/bin/alembic upgrade head"
else
    .venv/bin/alembic upgrade head
fi

echo "==> Unit-файлы systemd (ставлю, не включаю)"
install -m 644 deploy/sayr-gorets-*.service deploy/sayr-gorets-*.timer /etc/systemd/system/
systemctl daemon-reload

cat <<MSG

Готово. Осталось вам:
  1. В $PROJECT_DIR/.env вписать GORETS_TG_API_ID, GORETS_TG_API_HASH (my.telegram.org),
     GORETS_OWNER (ваш @username) и ANTHROPIC_API_KEY. Для истории с января:
     GORETS_RETENTION_DAYS=400.
  2. $PROJECT_DIR/.venv/bin/gorets login      (телефон, код, пароль 2FA)
  3. Написать аккаунту-сборщику любое сообщение со своего аккаунта.
  4. $PROJECT_DIR/.venv/bin/gorets collect && .venv/bin/gorets extract --dry-run
  5. systemctl enable --now sayr-gorets-collect.timer sayr-gorets-digest.timer \\
         sayr-gorets-api.service            # и sayr-gorets-watch.service, если нужны вебхуки
  6. curl -s http://127.0.0.1:8765/health ; systemctl list-timers 'sayr-gorets-*'
MSG
