# Cursor Linux Telegram Bot

Управляйте Linux-сервером из **Telegram** или **ВКонтакте**: сообщения уходят в локального агента (**Cursor Agent CLI**, **OpenRouter API** или **OpenRouter CLI**), ответы возвращаются в чат.

Проект рассчитан **только на Linux**. Установка — один скрипт, автозапуск — через **systemd**.

```
Telegram / VK  →  cursor-linux-tg-bot  →  Cursor CLI | OpenRouter API | OpenRouter CLI (orc)
```

## Возможности

- Диалог с агентом с сохранением сессии между сообщениями
- Потоковые ответы (обновление статуса по ходу работы)
- **Telegram** и **VK** — можно включить оба или один
- Провайдер агента: **Cursor** (локальный CLI), **OpenRouter** (API + shell/tools) или **OpenRouter CLI** ([orc](https://github.com/Ikarza/openrouter-cli))
- Whitelist пользователей
- Режимы `agent` (выполняет команды) и `plan` (только план)
- **Git**: авто-коммит после ответа, `/commit`, `/undo`, `/git`, `/push`, `/pull`
- **Очередь сообщений** — можно писать несколько команд подряд
- Автозапуск при загрузке системы

## Что понадобится

| Компонент | Где взять | Когда нужен |
|-----------|-----------|-------------|
| Linux с systemd | Ubuntu, Debian, Fedora и др. | всегда |
| Python 3.11+ | Ставится скриптом `install.sh` | всегда |
| [Cursor API key](https://cursor.com/dashboard/integrations) | `cursor_...` | `agent.provider: cursor` |
| [Cursor Agent CLI](https://cursor.com/docs/cli/overview) | Ставится скриптом | `agent.provider: cursor` |
| [OpenRouter API key](https://openrouter.ai/keys) | `sk-or-...` | `agent.provider: openrouter` или `openrouter_cli` |
| [OpenRouter CLI (orc)](https://github.com/Ikarza/openrouter-cli) | `npm install -g openrouter-cli` | `agent.provider: openrouter_cli` |
| Telegram-бот | [@BotFather](https://t.me/BotFather) | Telegram |
| Ваш Telegram user id | [@userinfobot](https://t.me/userinfobot) | Telegram |
| Токен сообщества VK | Управление → Работа с API | VK (опционально) |
| GitHub PAT (`repo`) | GitHub → Settings → Tokens | `/push`, `/pull`, `auto_push` |

## Быстрая установка

```bash
git clone https://github.com/ZTex275/CursorLinuxTgBot.git
cd CursorLinuxTgBot
chmod +x install.sh uninstall.sh update.sh
sudo ./install.sh
```

Скрипт:

1. Проверит, что ОС — Linux
2. Установит Python и зависимости в `.venv`
3. При необходимости поставит Cursor CLI
4. Создаст `.env` и `config.yaml` **в репозитории**
5. Зарегистрирует systemd-сервис и **включит автозагрузку**

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

Запуск systemd-сервиса от root (переменные окружения указывайте **после** `sudo`, иначе они не попадут в скрипт):

```bash
sudo ALLOW_ROOT_SERVICE=1 SERVICE_USER=root ./install.sh
# или короче:
sudo ./install.sh --allow-root
```

## Структура после установки

```
CursorLinuxTgBot/              # клон репозитория (отсюда работает сервис)
  .venv/                       # Python-окружение
  config.yaml                  # основные настройки
  .env                         # секреты (токены, ключи)
  data/sessions/               # id сессий агента по чатам
  install.sh / update.sh / uninstall.sh
```

## Управление сервисом

```bash
sudo systemctl status cursor-linux-tg-bot    # статус
sudo journalctl -u cursor-linux-tg-bot -f    # логи в реальном времени
sudo systemctl restart cursor-linux-tg-bot   # перезапуск
sudo systemctl stop cursor-linux-tg-bot      # остановка
sudo ./update.sh                             # обновить зависимости и перезапустить
```

Удаление:

```bash
sudo ./uninstall.sh
```

> Раньше установка шла в `/opt/cursor-linux-tg-bot` и `/etc/cursor-linux-tg-bot/`. `uninstall.sh` предложит удалить эти каталоги, если они остались.

## Команды в чате

| Команда | Действие |
|---------|----------|
| `/start` | Приветствие и краткая справка |
| `/new` | Новая сессия (сброс контекста и git-чекпоинта) |
| `/mode agent` | Агент выполняет команды и меняет файлы |
| `/mode plan` | Только планирование, без изменений |
| `/status` | provider, workspace, model, mode, git |
| `/git` | Статус репозитория |
| `/commit [текст]` | Закоммитить изменения |
| `/push` | Отправить коммиты на GitHub |
| `/pull` | Получить изменения с GitHub |
| `/undo` | Откатить изменения **последнего** сообщения |
| `/queue` | Сколько сообщений ждёт в очереди |
| Любой текст | Задача для агента (ставится в очередь) |

> **Git обязателен** для `/undo` и авто-коммита: `workspace` должен быть git-репозиторием (`git init`).

### Как работает git

1. **Перед** каждым сообщением бот сохраняет чекпоинт (`HEAD` + `git stash`, если были незакоммиченные файлы).
2. **После** успешного ответа — авто-коммит с комментарием `tg: <ваш текст>` (если `git.auto_commit: true`).
3. При `git.auto_push: true` — автоматический `git push` (нужен `GITHUB_TOKEN`).
4. **`/undo`** — `git reset --hard` к чекпоинту и восстановление stash.
5. **`/push`** / **`/pull`** — ручная синхронизация с GitHub.

### Очередь сообщений

Можно отправить несколько текстовых сообщений подряд — каждое встанет в очередь и выполнится **по порядку**. Лимит — `bot.max_queue_size` (по умолчанию 100).

## Конфигурация

Основной файл: `config.yaml` в корне репозитория  
Секреты: `.env`

Пример — [`config.example.yaml`](config.example.yaml).

```yaml
telegram:
  token: ${TELEGRAM_BOT_TOKEN}
  allowed_user_ids:
    - 123456789

# Опционально — работает параллельно с Telegram
vk:
  token: ${VK_BOT_TOKEN}
  group_id: 123456789
  allowed_user_ids: []   # пусто = разрешить всем

agent:
  provider: cursor       # cursor | openrouter | openrouter_cli
  workspace: /home/myuser
  mode: agent            # agent | plan

cursor:
  api_key: ${CURSOR_API_KEY}
  model: composer-2.5
  setting_sources: []    # ["project"] — подхватить .cursor/rules

openrouter:
  api_key: ${OPENROUTER_API_KEY}
  model: openrouter/free

openrouter_cli:
  api_key: ${OPENROUTER_API_KEY}
  profile: default
  binary: orc

bot:
  system_prefix: |
    Пользователь управляет этим Linux-сервером через Telegram.
    Выполняй запросы на этой машине. Кратко отвечай по-русски.

git:
  enabled: true
  auto_commit: true
  auto_push: true
  github_token: ${GITHUB_TOKEN}
  commit_prefix: "tg: "
```

После правки конфига:

```bash
sudo systemctl restart cursor-linux-tg-bot
```

### OpenRouter вместо Cursor

```yaml
agent:
  provider: openrouter
  workspace: /home/myuser

openrouter:
  api_key: ${OPENROUTER_API_KEY}
  model: anthropic/claude-sonnet-4
```

Cursor CLI и `CURSOR_API_KEY` не нужны. Агент выполняет shell-команды через OpenRouter API.

### OpenRouter CLI (orc) вместо Cursor

```yaml
agent:
  provider: openrouter_cli
  workspace: /home/myuser

openrouter_cli:
  api_key: ${OPENROUTER_API_KEY}
  model: anthropic/claude-3-sonnet-20240229   # или profile: default
  binary: orc
```

Нужны Node.js 22+ и `npm install -g openrouter-cli`. Это чат через внешний CLI — **без shell/tools** на сервере (в отличие от `openrouter`).

### Сравнение провайдеров

| Провайдер | Как работает | Shell / файлы |
|-----------|--------------|---------------|
| `cursor` | Cursor Agent CLI + SDK | да |
| `openrouter` | OpenRouter API + локальные tools | да |
| `openrouter_cli` | subprocess `orc ask` | нет (только чат) |

Активен **один** провайдер — задаётся в `agent.provider`.

### Подключить VK

1. Создайте сообщество → Управление → Работа с API → Ключи доступа  
   Права: «Сообщения сообщества» + «Управление сообществом»
2. Long Poll API → Включить → «Входящее сообщение», v5.199
3. В настройках сообщества включите «Сообщения сообщества»
4. В `config.yaml`:

```yaml
vk:
  token: ${VK_BOT_TOKEN}
  group_id: 123456789        # числовой id без минуса
  allowed_user_ids:
    - 987654321
```

Telegram можно оставить или убрать (оставьте пустой `telegram.token`, если нужен только VK).

### Подключить `.cursor/rules` и MCP

```yaml
cursor:
  workspace: /home/myuser/myproject
  setting_sources:
    - project
    - user
```

## Безопасность

Агент может **запускать shell-команды** и **изменять файлы** в `workspace`. Это полноценный доступ к серверу в рамках выбранной директории.

Рекомендации:

- Обязательно заполните `allowed_user_ids` — иначе бот откроют посторонние
- Сервис работает от обычного пользователя Linux, не от root
- Для разведки без изменений используйте `mode: plan`
- Ограничьте права пользователя сервиса по минимуму
- `GITHUB_TOKEN` храните только в `.env` (права `600`)

## Локальный запуск (без systemd)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp config.example.yaml config.yaml
cp .env.example .env
# заполните .env

set -a && source .env && set +a
cursor-linux-tg-bot -c config.yaml
# или: python run.py -c config.yaml
```

На Windows бот **не запускается** — при старте проверяется `sys.platform == "linux"`.

## Устранение неполадок

| Симптом | Что проверить |
|---------|---------------|
| Сервис не стартует | `sudo journalctl -u cursor-linux-tg-bot -n 50 --no-pager` |
| «Cursor не запустился» | `CURSOR_API_KEY` в `.env` |
| | `sudo -u USER agent login` или `agent --version` |
| Бот не отвечает | Ваш id в `allowed_user_ids` |
| | Токен бота в `.env` |
| `/push` не работает | `GITHUB_TOKEN` в `.env`, remote на GitHub |
| VK молчит | Long Poll включён, сообщения сообщества включены |
| `agent: command not found` | `sudo -u USER bash -c 'curl -fsSL https://cursor.com/install \| bash'` |
| Долгий ответ | Один запрос на чат; дождитесь или `/new` |

Проверка от имени пользователя сервиса:

```bash
sudo -u myuser bash -lc 'set -a && source /path/to/CursorLinuxTgBot/.env && set +a && /path/to/CursorLinuxTgBot/.venv/bin/cursor-linux-tg-bot -c /path/to/CursorLinuxTgBot/config.yaml'
```

## Лицензия

MIT. Используйте на свой риск: агент имеет доступ к выполнению команд на сервере.
