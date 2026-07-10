#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${ROOT}/.venv"
PYTHON="${VENV}/bin/python"

if [[ ! -f "${ROOT}/.env" ]]; then
  echo "Создайте .env (см. .env.example)" >&2
  exit 1
fi

if [[ ! -f "${ROOT}/config.yaml" ]]; then
  cp "${ROOT}/config.example.yaml" "${ROOT}/config.yaml"
  echo "==> Создан config.yaml из config.example.yaml — заполните allowed_user_ids"
fi

mkdir -p "${ROOT}/data/sessions"

if [[ ! -x "$PYTHON" ]]; then
  echo "==> Создаю .venv в репозитории (sudo ./install.sh для полной установки)"
  PY=""
  for candidate in python3.12 python3.11 "${HOME}/.local/bin/python3.11" python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PY="$candidate"
        break
      fi
    fi
  done
  if [[ -z "$PY" ]]; then
    echo "Нужен Python 3.11+: sudo ./install.sh" >&2
    exit 1
  fi
  "$PY" -m venv "$VENV"
  "$PYTHON" -m pip install -q -e "${ROOT}"
fi

SERVICE_NAME="cursor-linux-tg-bot"
if command -v systemctl >/dev/null 2>&1; then
  SERVICE_STATE="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)"
  if [[ "$SERVICE_STATE" == "active" || "$SERVICE_STATE" == "activating" || "$SERVICE_STATE" == "reloading" ]]; then
    echo "==> Останавливаю systemd-сервис ${SERVICE_NAME}, чтобы F5 не конфликтовал с getUpdates"
    systemctl stop "$SERVICE_NAME"
  fi
fi

stop_pids=()
while IFS= read -r line; do
  pid="${line%% *}"
  cmd="${line#* }"
  [[ "$pid" =~ ^[0-9]+$ ]] || continue
  [[ "$pid" -eq $$ || "$pid" -eq "$PPID" ]] && continue
  if [[ "$cmd" == */python\ run.py* ]] \
    || [[ "$cmd" == *cursor-linux-tg-bot\ -c* ]] \
    || [[ "$cmd" == *cursor_linux_tg_bot.bot* ]]; then
    stop_pids+=("$pid")
  fi
done < <(pgrep -af 'run\.py|cursor-linux-tg-bot|cursor_linux_tg_bot\.bot' 2>/dev/null || true)

if ((${#stop_pids[@]})); then
  echo "==> Останавливаю другие экземпляры бота: ${stop_pids[*]}"
  kill "${stop_pids[@]}" 2>/dev/null || true
  sleep 1
  for pid in "${stop_pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
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
