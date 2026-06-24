# Cursor Linux Telegram Bot

Тонкий мост: **Telegram → Cursor SDK (локальный агент)** на Linux-сервере. Полноценный Telegram-бот не нужен — достаточно конфига и нескольких переменных окружения.

## Почему не только Cursor Automations?

| Способ | Telegram | Управление Linux |
|--------|----------|------------------|
| **Этот проект (SDK)** | ✅ | ✅ локальный агент на сервере |
| Cursor Automations | ❌ (есть Slack, webhook, cron) | ⚠️ cloud / webhook, не «просто Telegram» |
| Cursor CLI (`agent -p`) | ⚠️ нужен свой wrapper | ✅ |

Cursor Automations можно подключить через **webhook** (бот шлёт HTTP на automation URL), но для диалога с Linux удобнее **Cursor SDK** — он держит сессию, стримит ответ и запускает команды на машине.

## Быстрый старт (Linux)

### 1. Cursor на сервере

```bash
# CLI (нужен для bridge SDK)
curl https://cursor.com/install -fsSL | bash
agent login
# или export CURSOR_API_KEY=cursor_...  # https://cursor.com/dashboard/integrations
```

### 2. Telegram-бот

1. Создайте бота у [@BotFather](https://t.me/BotFather), получите токен.
2. Узнайте свой user id у [@userinfobot](https://t.me/userinfobot).

### 3. Установка

```bash
git clone <repo> /opt/cursor-linux-tg-bot
cd /opt/cursor-linux-tg-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp config.example.yaml config.yaml
cp .env.example .env
# заполните TELEGRAM_BOT_TOKEN и CURSOR_API_KEY в .env
# отредактируйте config.yaml: allowed_user_ids, workspace
```

### 4. Запуск

```bash
source .env
cursor-linux-tg-bot -c config.yaml
```

### 5. systemd (опционально)

```bash
sudo cp deploy/cursor-linux-tg-bot.service /etc/systemd/system/
# поправьте User и пути в unit-файле
sudo systemctl daemon-reload
sudo systemctl enable --now cursor-linux-tg-bot
```

## config.yaml

Основные поля:

- `telegram.token` — `${TELEGRAM_BOT_TOKEN}`
- `telegram.allowed_user_ids` — whitelist (обязательно для безопасности)
- `cursor.workspace` — каталог на Linux, где агент работает
- `cursor.mode` — `agent` (действия) или `plan` (только план)
- `bot.system_prefix` — системная инструкция для каждого сообщения

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие |
| `/new` | Новая сессия Cursor (сброс контекста) |
| `/mode agent\|plan` | Режим агента |
| `/status` | workspace, model, mode |
| Любой текст | Отправляется в Cursor Agent |

## Безопасность

- Агент может **выполнять shell-команды** и менять файлы в `workspace`.
- Ограничьте `allowed_user_ids`.
- Запускайте от отдельного пользователя Linux с минимальными правами.
- Рассмотрите `cursor.mode: plan` для read-only планирования.

## Альтернатива: только Cursor CLI

Если не нужен SDK, на Linux можно вызывать:

```bash
agent -p "ваш промпт" --output-format text
```

Но для Telegram-диалога всё равно нужен тонкий процесс (этот проект ~200 строк). SDK даёт resume-сессии и стриминг.

## Альтернатива: Cursor Automation + webhook

1. В Cursor создайте Automation с триггером **Webhook**.
2. Напишите минимальный HTTP-сервис: Telegram → POST на URL automation.
3. Минус: нет нативного двустороннего чата, сложнее стримить ответ в Telegram.

Для управления Linux из Telegram рекомендуется этот SDK-мост.

## MCP и правила проекта

Чтобы агент видел `.cursor/rules`, MCP и skills на сервере:

```yaml
cursor:
  setting_sources:
    - project
    - user
```

Файлы должны лежать в `cursor.workspace`.

## Troubleshooting

| Проблема | Решение |
|----------|---------|
| `Cursor не запустился` | `agent login` или `CURSOR_API_KEY` |
| `workspace does not exist` | создайте каталог, укажите абсолютный путь |
| Бот не отвечает | проверьте `allowed_user_ids` |
| Долгие задачи | один запрос на чат; дождитесь ответа или `/new` |
