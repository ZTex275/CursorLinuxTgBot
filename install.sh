#!/usr/bin/env bash
# Установка cursor-linux-tg-bot на Linux + автозапуск через systemd.
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Ошибка: этот проект работает только на Linux." >&2
  exit 1
fi

if [[ "${EUID:-0}" -ne 0 ]]; then
  echo "Запустите установку с sudo: sudo ./install.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/cursor-linux-tg-bot}"
CONFIG_DIR="${CONFIG_DIR:-/etc/cursor-linux-tg-bot}"
DATA_DIR="${DATA_DIR:-/var/lib/cursor-linux-tg-bot}"
SERVICE_NAME="cursor-linux-tg-bot"

# Пользователь, от которого крутится сервис (не root).
if [[ -n "${SERVICE_USER:-}" ]]; then
  :
elif [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  SERVICE_USER="$SUDO_USER"
else
  SERVICE_USER="$(logname 2>/dev/null || echo root)"
fi

if [[ "$SERVICE_USER" == "root" ]]; then
  echo "Укажите пользователя: SERVICE_USER=myuser sudo ./install.sh" >&2
  exit 1
fi

SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
DEFAULT_WORKSPACE="${SERVICE_HOME}"

echo "==> Установка в ${INSTALL_DIR}"
echo "    Пользователь сервиса: ${SERVICE_USER}"
echo "    Конфиг: ${CONFIG_DIR}"
echo "    Данные: ${DATA_DIR}"

# --- зависимости ---
need_pkg=()
command -v python3 >/dev/null || need_pkg+=(python3)
python3 -c "import venv" 2>/dev/null || need_pkg+=(python3-venv)
if ((${#need_pkg[@]})); then
  if command -v apt-get >/dev/null; then
    apt-get update -qq
    apt-get install -y "${need_pkg[@]}"
  elif command -v dnf >/dev/null; then
    dnf install -y python3 python3-pip
  elif command -v yum >/dev/null; then
    yum install -y python3 python3-pip
  else
    echo "Установите вручную: python3, python3-venv, pip" >&2
    exit 1
  fi
fi

# --- Cursor CLI (для bridge SDK) ---
if ! command -v agent >/dev/null && [[ ! -x "${SERVICE_HOME}/.local/bin/agent" ]]; then
  echo "==> Cursor Agent CLI не найден. Устанавливаю..."
  sudo -u "$SERVICE_USER" bash -c "curl -fsSL https://cursor.com/install | bash" || {
    echo "Не удалось установить Cursor CLI. Продолжаю — задайте CURSOR_API_KEY в ${CONFIG_DIR}/env" >&2
  }
fi

# --- файлы проекта ---
mkdir -p "$INSTALL_DIR"
if ! command -v rsync >/dev/null; then
  if command -v apt-get >/dev/null; then
    apt-get install -y rsync
  fi
fi
if command -v rsync >/dev/null; then
  rsync -a --delete \
  --exclude '.venv' \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude 'data' \
  --exclude '.env' \
  --exclude 'config.yaml' \
  "$SCRIPT_DIR/" "$INSTALL_DIR/"
else
  cp -a "$SCRIPT_DIR"/. "$INSTALL_DIR/"
  rm -rf "${INSTALL_DIR}/.venv" "${INSTALL_DIR}/.git" "${INSTALL_DIR}/data"
fi

python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip -q
"${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}" -q

chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR"

# --- конфигурация ---
mkdir -p "$CONFIG_DIR" "$DATA_DIR/sessions"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$DATA_DIR"

if [[ ! -f "${CONFIG_DIR}/env" ]]; then
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    read -r -p "TELEGRAM_BOT_TOKEN: " TELEGRAM_BOT_TOKEN
  fi
  if [[ -z "${CURSOR_API_KEY:-}" ]]; then
    read -r -p "CURSOR_API_KEY: " CURSOR_API_KEY
  fi
  if [[ -z "${ALLOWED_USER_ID:-}" ]]; then
    read -r -p "Telegram user id (allowed_user_ids): " ALLOWED_USER_ID
  fi
  if [[ -z "${WORKSPACE:-}" ]]; then
    read -r -p "Workspace [${DEFAULT_WORKSPACE}]: " WORKSPACE
  fi
  WORKSPACE="${WORKSPACE:-$DEFAULT_WORKSPACE}"

  umask 077
  cat >"${CONFIG_DIR}/env" <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
CURSOR_API_KEY=${CURSOR_API_KEY}
EOF
  chmod 600 "${CONFIG_DIR}/env"
  chown root:"${SERVICE_USER}" "${CONFIG_DIR}/env"
  chmod 640 "${CONFIG_DIR}/env"

  sed \
    -e "s|workspace: /home/YOUR_USER|workspace: ${WORKSPACE}|" \
    -e "s|- 123456789|- ${ALLOWED_USER_ID}|" \
    -e "s|sessions_dir: /var/lib/cursor-linux-tg-bot/sessions|sessions_dir: ${DATA_DIR}/sessions|" \
    "${INSTALL_DIR}/config.example.yaml" >"${CONFIG_DIR}/config.yaml"
  chmod 644 "${CONFIG_DIR}/config.yaml"
else
  echo "==> ${CONFIG_DIR}/env уже есть — пропускаю настройку секретов"
fi

# --- systemd ---
AGENT_BIN="${SERVICE_HOME}/.local/bin"
cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Telegram bridge to Cursor local agent (Linux)
Documentation=file://${INSTALL_DIR}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${CONFIG_DIR}/env
Environment=PATH=${AGENT_BIN}:/usr/local/bin:/usr/bin:/bin
ExecStart=${INSTALL_DIR}/.venv/bin/cursor-linux-tg-bot -c ${CONFIG_DIR}/config.yaml
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

# Безопасность
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo ""
echo "Готово. Бот установлен и добавлен в автозагрузку."
echo ""
echo "  Статус:  sudo systemctl status ${SERVICE_NAME}"
echo "  Логи:    sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Стоп:    sudo systemctl stop ${SERVICE_NAME}"
echo "  Удалить: sudo ./uninstall.sh"
echo ""
