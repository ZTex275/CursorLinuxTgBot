#!/usr/bin/env bash
# Обновление зависимостей в .venv и перезапуск systemd (без /opt и /etc).
set -euo pipefail

if [[ "${EUID:-0}" -ne 0 ]]; then
  echo "Запустите с sudo: sudo ./update.sh" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="cursor-linux-tg-bot"
VENV="${REPO_DIR}/.venv"

if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "Сначала: sudo ./install.sh" >&2
  exit 1
fi

echo "==> Обновление ${REPO_DIR}"
"${VENV}/bin/pip" install --upgrade pip -q
"${VENV}/bin/pip" install -e "${REPO_DIR}" -q
mkdir -p "${REPO_DIR}/data/sessions"

if systemctl is-enabled "$SERVICE_NAME" >/dev/null 2>&1; then
  systemctl restart "$SERVICE_NAME"
  echo "==> Сервис перезапущен"
  systemctl --no-pager --full status "$SERVICE_NAME" | head -12
else
  echo "==> Сервис не включён. Запуск: sudo ./install.sh"
fi

echo "Готово."
