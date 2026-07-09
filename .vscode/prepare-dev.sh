#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "${ROOT}/.env" ]]; then
  echo "Создайте .env с TELEGRAM_BOT_TOKEN и CURSOR_API_KEY (см. README.md)" >&2
  exit 1
fi

if [[ ! -f "${ROOT}/config.yaml" ]]; then
  cp "${ROOT}/config.example.yaml" "${ROOT}/config.yaml"
  echo "==> Создан config.yaml из config.example.yaml — заполните allowed_user_ids"
fi

PYTHON="/opt/cursor-linux-tg-bot/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Сначала установите проект: sudo ./install.sh" >&2
  exit 1
fi

SERVICE_NAME="cursor-linux-tg-bot"
if command -v systemctl >/dev/null 2>&1; then
  SERVICE_STATE="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)"
  if [[ "$SERVICE_STATE" == "active" || "$SERVICE_STATE" == "activating" || "$SERVICE_STATE" == "reloading" ]]; then
    echo "==> Останавливаю systemd-сервис ${SERVICE_NAME}, чтобы F5 не конфликтовал с getUpdates"
    systemctl stop "$SERVICE_NAME"
  fi
fi

SITE_PACKAGES="$("$PYTHON" -c 'import site; print(site.getsitepackages()[0])')"

if ! "$PYTHON" -c "import debugpy" 2>/dev/null; then
  BUNDLED=""
  for candidate in \
    "${HOME}/.cursor-server/extensions/ms-python.debugpy-"*/bundled/libs/debugpy \
    "${HOME}/.vscode-server/extensions/ms-python.debugpy-"*/bundled/libs/debugpy; do
    if [[ -d "$candidate" ]]; then
      BUNDLED="$candidate"
      break
    fi
  done
  if [[ -z "$BUNDLED" ]]; then
    echo "Установите расширение Python Debugger (ms-python.debugpy) в Cursor" >&2
    exit 1
  fi
  cp -r "$BUNDLED" "${SITE_PACKAGES}/"
  echo "==> debugpy установлен из расширения Cursor"
fi
