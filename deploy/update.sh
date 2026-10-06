#!/usr/bin/env bash
# Обновление sayr-gorets на VPS: код, зависимости, схема базы, unit-файлы.
#
#   /root/Projects/sayr-gorets/deploy/update.sh            # обновить до origin/main
#   /root/Projects/sayr-gorets/deploy/update.sh v0.2.0     # до тега или коммита
#
# Откат: укажите прежний коммит/тег тем же скриптом; миграции назад —
# `.venv/bin/alembic downgrade <ревизия>` вручную (см. README).
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/Projects/sayr-gorets}"
TARGET="${1:-}"

cd "$PROJECT_DIR"

echo "==> Код"
git fetch --tags origin
if [[ -n "$TARGET" ]]; then
    git checkout --detach "$TARGET"
else
    git checkout main
    git pull --ff-only origin main
fi
git log -1 --oneline

# Дополнение mcp сохраняем, если оно было поставлено при установке.
EXTRAS=()
if ls .venv/lib/python*/site-packages/mcp >/dev/null 2>&1; then
    EXTRAS+=(--extra mcp)
fi
echo "==> Зависимости (uv sync --frozen --no-dev ${EXTRAS[*]})"
uv sync --frozen --no-dev "${EXTRAS[@]}"

echo "==> Схема базы (alembic upgrade head)"
.venv/bin/alembic upgrade head

echo "==> Unit-файлы systemd"
changed=0
for unit in deploy/sayr-gorets-*.service deploy/sayr-gorets-*.timer; do
    name="$(basename "$unit")"
    if ! cmp -s "$unit" "/etc/systemd/system/$name"; then
        install -m 644 "$unit" "/etc/systemd/system/$name"
        echo "    обновлён $name"
        changed=1
    fi
done
if [[ "$changed" == 1 ]]; then
    systemctl daemon-reload
fi

echo "==> Готово: $(.venv/bin/gorets --version)"
systemctl list-timers 'sayr-gorets-*' --no-pager || true
