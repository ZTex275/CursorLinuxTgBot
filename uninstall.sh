#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Только Linux." >&2
  exit 1
fi

if [[ "${EUID:-0}" -ne 0 ]]; then
  echo "Запустите с sudo: sudo ./uninstall.sh" >&2
  exit 1
fi

SERVICE_NAME="cursor-linux-tg-bot"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

echo "Сервис ${SERVICE_NAME} удалён."
echo "Репозиторий ${REPO_DIR} не тронут (.env, config.yaml, data/ остаются)."

read -r -p "Удалить старую установку /opt/cursor-linux-tg-bot? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
  rm -rf /opt/cursor-linux-tg-bot
fi

read -r -p "Удалить старый конфиг /etc/cursor-linux-tg-bot? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
  rm -rf /etc/cursor-linux-tg-bot
fi

read -r -p "Удалить старые сессии /var/lib/cursor-linux-tg-bot? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
  rm -rf /var/lib/cursor-linux-tg-bot
fi

echo "Удаление завершено."
