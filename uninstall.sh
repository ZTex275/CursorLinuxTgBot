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
INSTALL_DIR="${INSTALL_DIR:-/opt/cursor-linux-tg-bot}"
CONFIG_DIR="${CONFIG_DIR:-/etc/cursor-linux-tg-bot}"
DATA_DIR="${DATA_DIR:-/var/lib/cursor-linux-tg-bot}"

systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

read -r -p "Удалить файлы приложения ${INSTALL_DIR}? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
  rm -rf "$INSTALL_DIR"
fi

read -r -p "Удалить конфиг ${CONFIG_DIR}? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
  rm -rf "$CONFIG_DIR"
fi

read -r -p "Удалить сессии ${DATA_DIR}? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
  rm -rf "$DATA_DIR"
fi

echo "Удаление завершено."
