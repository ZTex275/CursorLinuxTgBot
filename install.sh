#!/usr/bin/env bash
# Установка из текущего репозитория: venv + systemd, без /opt и /etc.
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Ошибка: этот проект работает только на Linux." >&2
  exit 1
fi

if [[ "${EUID:-0}" -ne 0 ]]; then
  echo "Запустите установку с sudo: sudo ./install.sh" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="cursor-linux-tg-bot"

if [[ -n "${SERVICE_USER:-}" ]]; then
  :
elif [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  SERVICE_USER="$SUDO_USER"
else
  SERVICE_USER="$(logname 2>/dev/null || echo root)"
fi

if [[ "$SERVICE_USER" == "root" && "${ALLOW_ROOT_SERVICE:-}" != "1" ]]; then
  echo "Укажите пользователя: SERVICE_USER=myuser sudo ./install.sh" >&2
  echo "Или для запуска от root: ALLOW_ROOT_SERVICE=1 SERVICE_USER=root sudo ./install.sh" >&2
  exit 1
fi

SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
DEFAULT_WORKSPACE="${SERVICE_HOME}"

echo "==> Репозиторий: ${REPO_DIR}"
echo "    Пользователь сервиса: ${SERVICE_USER}"

# --- Python 3.11+ ---
PYTHON=""
for candidate in python3.12 python3.11 "${SERVICE_HOME}/.local/bin/python3.11" python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "Нужен Python 3.11+. Установите: curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.11" >&2
  exit 1
fi
echo "==> Python: $($PYTHON --version)"

need_pkg=()
"$PYTHON" -c "import venv" 2>/dev/null || need_pkg+=(python3-venv)
if ((${#need_pkg[@]})); then
  if command -v apt-get >/dev/null; then
    apt-get update -qq
    apt-get install -y "${need_pkg[@]}"
  fi
fi

# --- Cursor CLI ---
if ! command -v agent >/dev/null && [[ ! -x "${SERVICE_HOME}/.local/bin/agent" ]]; then
  echo "==> Cursor Agent CLI не найден. Устанавливаю..."
  sudo -u "$SERVICE_USER" bash -c "curl -fsSL https://cursor.com/install | bash" || {
    echo "Не удалось установить Cursor CLI. Задайте CURSOR_API_KEY в ${REPO_DIR}/.env" >&2
  }
fi

# --- venv в репозитории ---
if [[ ! -d "${REPO_DIR}/.venv" ]]; then
  "$PYTHON" -m venv "${REPO_DIR}/.venv"
fi
"${REPO_DIR}/.venv/bin/pip" install --upgrade pip -q
"${REPO_DIR}/.venv/bin/pip" install -e "${REPO_DIR}" -q

mkdir -p "${REPO_DIR}/data/sessions"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${REPO_DIR}/.venv" "${REPO_DIR}/data"

# --- .env и config.yaml в репозитории ---
if [[ ! -f "${REPO_DIR}/.env" ]]; then
  if [[ -f "${REPO_DIR}/.env.example" ]]; then
    cp "${REPO_DIR}/.env.example" "${REPO_DIR}/.env"
  fi
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    read -r -p "TELEGRAM_BOT_TOKEN: " TELEGRAM_BOT_TOKEN
  fi
  if [[ -z "${CURSOR_API_KEY:-}" ]]; then
    read -r -p "CURSOR_API_KEY: " CURSOR_API_KEY
  fi
  umask 077
  TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN//$'\r'/}"
  CURSOR_API_KEY="${CURSOR_API_KEY//$'\r'/}"
  VK_BOT_TOKEN="${VK_BOT_TOKEN:-}"
  VK_BOT_TOKEN="${VK_BOT_TOKEN//$'\r'/}"
  cat >"${REPO_DIR}/.env" <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
CURSOR_API_KEY=${CURSOR_API_KEY}
VK_BOT_TOKEN=${VK_BOT_TOKEN}
EOF
  chmod 600 "${REPO_DIR}/.env"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${REPO_DIR}/.env"
fi

if [[ ! -f "${REPO_DIR}/config.yaml" ]]; then
  if [[ -z "${ALLOWED_USER_ID:-}" ]]; then
    read -r -p "Telegram user id (allowed_user_ids): " ALLOWED_USER_ID
  fi
  if [[ -z "${WORKSPACE:-}" ]]; then
    read -r -p "Workspace [${DEFAULT_WORKSPACE}]: " WORKSPACE
  fi
  WORKSPACE="${WORKSPACE:-$DEFAULT_WORKSPACE}"
  sed \
    -e "s|workspace: /home/YOUR_USER|workspace: ${WORKSPACE}|" \
    -e "s|- 123456789|- ${ALLOWED_USER_ID}|" \
    "${REPO_DIR}/config.example.yaml" >"${REPO_DIR}/config.yaml"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${REPO_DIR}/config.yaml"
fi

# --- systemd (запуск из репозитория) ---
AGENT_BIN="${SERVICE_HOME}/.local/bin"
cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Telegram/VK bridge to Cursor local agent (Linux)
Documentation=file://${REPO_DIR}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${REPO_DIR}/.env
Environment=PATH=${AGENT_BIN}:/usr/local/bin:/usr/bin:/bin
ExecStart=${REPO_DIR}/.venv/bin/python ${REPO_DIR}/run.py -c ${REPO_DIR}/config.yaml
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo ""
echo "Готово. Бот запускается из ${REPO_DIR}"
echo ""
echo "  Конфиг:   ${REPO_DIR}/config.yaml"
echo "  Секреты:  ${REPO_DIR}/.env"
echo "  Сессии:   ${REPO_DIR}/data/sessions"
echo "  Статус:   sudo systemctl status ${SERVICE_NAME}"
echo "  Логи:     sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Обновить: sudo ./update.sh"
echo ""
