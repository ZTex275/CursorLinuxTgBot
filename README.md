# Cursor Linux Telegram Bot

Управляйте Linux-сервером из Telegram: сообщения уходят в локальный **Cursor Agent**, ответы возвращаются в чат.

Проект рассчитан **только на Linux**. Установка — один скрипт, автозапуск — через **systemd**.

```
Telegram  →  cursor-linux-tg-bot  →  Cursor SDK  →  shell / файлы на сервере
```

## Возможности

- Диалог с Cursor Agent с сохранением сессии между сообщениями
- Потоковые ответы в Telegram (обновление статуса по ходу работы)
- Whitelist пользователей Telegram
- Режимы `agent` (выполняет команды) и `plan` (только план)
- **Git**: авто-коммит после ответа, `/commit`, `/undo` — откат последнего сообщения
- Автозапуск при загрузке системы

## Что понадобится

| Компонент | Где взять |
|-----------|-----------|
| Linux с systemd | Ubuntu, Debian, Fedora и др. |
| Python 3.11+ | Ставится скриптом `install.sh` |
| [Cursor API key](https://cursor.com/dashboard/integrations) | `cursor_...` |
| [Cursor Agent CLI](https://cursor.com/docs/cli/overview) | Ставится скриптом, если нет |
| Telegram-бот | [@BotFather](https://t.me/BotFather) |
| Ваш Telegram user id | [@userinfobot](https://t.me/userinfobot) |

## Быстрая установка

```bash
git clone https://github.com/ZTex275/CursorLinuxTgBot.git
cd CursorLinuxTgBot
chmod +x install.sh uninstall.sh
sudo ./install.sh
```

Скрипт:

1. Проверит, что ОС — Linux
2. Установит Python и зависимости
3. При необходимости поставит Cursor CLI
4. Развернёт приложение в `/opt/cursor-linux-tg-bot`
5. Создаст конфиг и секреты в `/etc/cursor-linux-tg-bot/`
6. Зарегистрирует systemd-сервис и **включит автозагрузку**

Во время установки нужно ввести токен бота, API key Cursor, свой Telegram id и рабочую директорию агента.

### Установка без вопросов (для скриптов)

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export CURSOR_API_KEY="cursor_..."
export ALLOWED_USER_ID="123456789"
export WORKSPACE="/home/myuser"
sudo -E ./install.sh
```

Другой пользователь сервиса:

```bash
sudo SERVICE_USER=deploy ./install.sh
```

## Структура после установки

```
/opt/cursor-linux-tg-bot/          # код и venv
/etc/cursor-linux-tg-bot/
  env                              # TELEGRAM_BOT_TOKEN, CURSOR_API_KEY
  config.yaml                      # основные настройки
/var/lib/cursor-linux-tg-bot/
  sessions/                        # id сессий Cursor по чатам
```

## Управление сервисом

```bash
sudo systemctl status cursor-linux-tg-bot    # статус
sudo journalctl -u cursor-linux-tg-bot -f    # логи в реальном времени
sudo systemctl restart cursor-linux-tg-bot   # перезапуск
sudo systemctl stop cursor-linux-tg-bot      # остановка
```

Удаление:

```bash
sudo ./uninstall.sh
```

## Команды в Telegram

| Команда | Действие |
|---------|----------|
| `/start` | Приветствие и краткая справка |
| `/new` | Новая сессия Cursor (сброс контекста) |
| `/mode agent` | Агент выполняет команды и меняет файлы |
| `/mode plan` | Только планирование, без изменений |
| `/status` | Текущие workspace, model и mode |
| `/commit [текст]` | Закоммитить изменения (без текста — комментарий из последнего сообщения) |
| `/undo` | Откатить изменения **последнего** сообщения (включая авто-коммит) |
| Любой текст | Задача для Cursor Agent на сервере |

> **Git обязателен** для `/undo` и авто-коммита: `cursor.workspace` должен быть git-репозиторием (`git init`).

### Как работает git

1. **Перед** каждым сообщением бот сохраняет чекпоинт (`HEAD` + `git stash`, если были незакоммиченные файлы).
2. **После** успешного ответа агента — авто-коммит с комментарием `tg: <ваш текст>` (если `git.auto_commit: true`).
3. **`/undo`** — `git reset --hard` к чекпоинту и восстановление stash; убирает и изменения агента, и авто-коммит.
4. **`/commit`** — ручной коммит в любой момент.

## Конфигурация

Основной файл: `/etc/cursor-linux-tg-bot/config.yaml`  
Секреты: `/etc/cursor-linux-tg-bot/env`

Пример — в репозитории: [`config.example.yaml`](config.example.yaml).

```yaml
telegram:
  token: ${TELEGRAM_BOT_TOKEN}
  allowed_user_ids:
    - 123456789          # только эти пользователи

cursor:
  api_key: ${CURSOR_API_KEY}
  model: composer-2.5
  workspace: /home/myuser   # каталог, где агент работает
  mode: agent               # agent | plan
  setting_sources: []       # [] — без .cursor/rules; ["project"] — подхватить правила

bot:
  system_prefix: |          # добавляется к каждому сообщению
    Пользователь управляет Linux-сервером через Telegram.

git:
  enabled: true
  auto_commit: true           # коммит после каждого успешного ответа
  commit_prefix: "tg: "
  max_commit_message_length: 120
```

После правки конфига:

```bash
sudo systemctl restart cursor-linux-tg-bot
```

### Подключить `.cursor/rules` и MCP

Если в `workspace` лежат правила и MCP-конфиги Cursor:

```yaml
cursor:
  workspace: /home/myuser/myproject
  setting_sources:
    - project
    - user
```

## Безопасность

Cursor Agent может **запускать shell-команды** и **изменять файлы** в `workspace`. Это полноценный доступ к серверу в рамках выбранной директории.

Рекомендации:

- Обязательно заполните `allowed_user_ids` — иначе бот откроют посторонние
- Сервис работает от обычного пользователя Linux, не от root
- Для разведки без изменений используйте `mode: plan`
- Ограничьте права пользователя сервиса по минимуму

## Локальный запуск (без systemd)

Для отладки на Linux-машине:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp config.example.yaml config.yaml
cp .env.example .env
# заполните .env

set -a && source .env && set +a
cursor-linux-tg-bot -c config.yaml
```

На Windows бот **не запускается** — при старте проверяется `sys.platform == "linux"`.

## Устранение неполадок

| Симптом | Что проверить |
|---------|---------------|
| Сервис не стартует | `sudo journalctl -u cursor-linux-tg-bot -n 50 --no-pager` |
| «Cursor не запустился» | `CURSOR_API_KEY` в `/etc/cursor-linux-tg-bot/env` |
| | `sudo -u USER agent login` или `agent --version` |
| Бот не отвечает | Ваш id в `allowed_user_ids` |
| | Токен бота в `env` |
| `agent: command not found` | `sudo -u USER bash -c 'curl -fsSL https://cursor.com/install \| bash'` |
| Долгий ответ | Один запрос на чат; дождитесь или `/new` |

Проверка от имени пользователя сервиса:

```bash
sudo -u myuser bash -lc 'source /etc/cursor-linux-tg-bot/env && /opt/cursor-linux-tg-bot/.venv/bin/cursor-linux-tg-bot -c /etc/cursor-linux-tg-bot/config.yaml'
```

## Лицензия

MIT. Используйте на свой риск: агент имеет доступ к выполнению команд на сервере.
