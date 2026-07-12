#!/bin/bash
set -e

# Адрес сервера берётся из окружения или .env: DEPLOY_SERVER=user@host
if [ -z "$DEPLOY_SERVER" ] && [ -f .env ]; then
    DEPLOY_SERVER=$(grep -E '^DEPLOY_SERVER=' .env | cut -d= -f2-)
fi
if [ -z "$DEPLOY_SERVER" ]; then
    echo "❌ DEPLOY_SERVER не задан (окружение или .env). Формат: user@host"
    exit 1
fi

SERVER="$DEPLOY_SERVER"
PROJECT_NAME="${DEPLOY_PROJECT_NAME:-moysklad-copilot}"
CONTAINER="moysklad-copilot"
REMOTE_DIR="/opt/$PROJECT_NAME"
SERVICE_TYPE="service"

EXCLUDES=(
    "venv/" ".venv/" "__pycache__/" "*.pyc"
    ".git/" ".vscode/" ".idea/" ".claude/" ".pytest_cache/" ".DS_Store"
    ".env" ".env.example"
    "data/"
    "docs/internal/"
)

echo "🚀 Deploy: $PROJECT_NAME → $SERVER"

ssh "$SERVER" "mkdir -p $REMOTE_DIR"

if ssh "$SERVER" "[ ! -f $REMOTE_DIR/.env ]"; then
    echo "❌ Нет $REMOTE_DIR/.env на сервере. Создайте по .env.example."
    exit 1
fi

EXCLUDE_ARGS=()
for e in "${EXCLUDES[@]}"; do EXCLUDE_ARGS+=(--exclude "$e"); done

rsync -avz "${EXCLUDE_ARGS[@]}" ./ "$SERVER:$REMOTE_DIR/"

if [ "$SERVICE_TYPE" = "service" ]; then
    ssh "$SERVER" "cd $REMOTE_DIR && mkdir -p data && docker compose up -d --build"
    ssh "$SERVER" "docker ps --filter name=^$CONTAINER$ --format 'table {{.Names}}\t{{.Status}}'"
    ssh "$SERVER" "docker logs --tail 20 $CONTAINER"
else
    ssh "$SERVER" "cd $REMOTE_DIR && docker compose build"
    echo "✅ Образ собран. Запуск по cron."
fi

echo "✅ Деплой $PROJECT_NAME завершён"
