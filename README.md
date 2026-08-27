# Cursor Linux Telegram Bot

Управляйте **Linux-сервером** или **Windows-ПК** из **Telegram** или **ВКонтакте**: сообщения уходят в локального агента (**Cursor Agent CLI**, **OpenRouter API** или **OpenRouter CLI**), ответы возвращаются в чат.

Поддерживаются **Linux** (systemd) и **Windows** (служба Windows). Установка — один скрипт под вашу ОС.

```
Telegram / VK  →  cursor-linux-tg-bot  →  Cursor CLI | OpenRouter API | OpenRouter CLI (orc)
```

## Возможности

- Диалог с агентом с сохранением сессии между сообщениями
- Потоковые ответы (статус выполнения обновляется по ходу работы)
- **Telegram** и **VK** — можно включить оба или один
- **Голосовые сообщения** — распознавание речи (Whisper) и выполнение задачи
- Провайдер агента: **Cursor** (локальный CLI), **OpenRouter** (API + shell/tools) или **OpenRouter CLI** ([orc](https://github.com/Ikarza/openrouter-cli))
- Whitelist пользователей
- Режимы `agent` (выполняет команды) и `plan` (только план)
- **Git**: авто-коммит после ответа, `/commit`, `/undo`, `/git`, `/push`, `/pull`
- **Очередь сообщений** — можно писать несколько команд подряд; очередь сохраняется на диск и восстанавливается после перезапуска
- Автоперезапуск службы после изменений кода бота
- Автозапуск при загрузке системы

## Что понадобится

| Компонент | Где взять | Когда нужен |
|-----------|-----------|-------------|
| Linux с systemd **или** Windows 10/11 | Ubuntu, Debian, Fedora / Windows Server | всегда |
| Python 3.11+ | Ставится скриптом установки | всегда |
| [Cursor API key](https://cursor.com/dashboard/integrations) | `cursor_...` | `agent.provider: cursor` |
| [Cursor Agent CLI](https://cursor.com/docs/cli/overview) | Linux: скриптом; Windows: вручную | `agent.provider: cursor` |
| [OpenRouter API key](https://openrouter.ai/keys) | `sk-or-...` | `agent.provider: openrouter` или `openrouter_cli` |
| [OpenRouter CLI (orc)](https://github.com/Ikarza/openrouter-cli) | `npm install -g openrouter-cli` | `agent.provider: openrouter_cli` |
| Telegram-бот | [@BotFather](https://t.me/BotFather) | Telegram |
| Ваш Telegram user id | [@userinfobot](https://t.me/userinfobot) | Telegram |
| Токен сообщества VK | Управление → Работа с API | VK (опционально) |
| GitHub PAT (`repo`) | GitHub → Settings → Tokens | `/push`, `/pull`, `auto_push` |

## Быстрая установка (Linux)

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

### Установка без вопросов — Linux (для скриптов)

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

## Быстрая установка (Windows)

1. Установите [Python 3.11+](https://www.python.org/downloads/) (галочка «Add Python to PATH»).
2. Установите [Cursor Agent CLI](https://cursor.com/docs/cli/overview) и выполните `agent login`.
3. Клонируйте репозиторий и откройте **PowerShell от администратора** в каталоге проекта:

```powershell
git clone https://github.com/ZTex275/CursorLinuxTgBot.git
cd CursorLinuxTgBot
.\install.ps1
```

Скрипт:

1. Создаст `.venv` и установит зависимости
2. Создаст `.env` и `config.yaml` в репозитории (спросит токен, ключ Cursor, user id, workspace)
3. Создаст обёртку `run-bot.cmd` для загрузки `.env`
4. Зарегистрирует службу Windows `cursor-linux-tg-bot` с **автозапуском** при загрузке системы
5. Запустит службу

### Установка без вопросов — Windows

Заполните `.env` и `config.yaml` вручную до запуска `install.ps1`, либо отредактируйте их после — скрипт не перезапишет существующие файлы.

### Управление службой (Windows)

```powershell
Get-Service cursor-linux-tg-bot          # статус
Restart-Service cursor-linux-tg-bot      # перезапуск
Stop-Service cursor-linux-tg-bot         # остановка
.\update.ps1                             # обновить зависимости и перезапустить
```

Логи: **Event Viewer** → Windows Logs → Application (источник — python или cmd).

Удаление:

```powershell
.\uninstall.ps1
```

Скрипт удалит службу; по запросу — `run-bot.cmd` и `.venv`. Репозиторий (`.env`, `config.yaml`, `data/`) не трогается.

## Структура после установки

```
CursorLinuxTgBot/              # клон репозитория (отсюда работает сервис)
  .venv/                       # Python-окружение
  config.yaml                  # основные настройки
  .env                         # секреты (токены, ключи)
  data/sessions/               # id сессий агента по чатам
  data/queue/pending.json      # очередь сообщений на диске
  install.sh / update.sh / uninstall.sh       # Linux
  install.ps1 / update.ps1 / uninstall.ps1    # Windows
  run-bot.cmd                  # обёртка запуска (Windows)
```

## Управление сервисом (Linux)

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
| `/provider cursor\|openrouter\|openrouter_cli` | Сменить провайдера агента |
| `/model <модель>` | Сменить модель |
| `/status` | provider, workspace, model, mode, git |
| `/git` | Статус репозитория |
| `/commit [текст]` | Закоммитить изменения |
| `/push` | Отправить коммиты на GitHub |
| `/pull` | Получить изменения с GitHub |
| `/undo` | Откатить изменения **последнего** сообщения |
| `/stop` | Прервать текущую задачу, откатить её изменения и очистить очередь |
| `/queue` | Сколько сообщений ждёт в очереди |
| Голосовое сообщение | Распознаётся речь и ставится в очередь как текст |
| Любой текст | Задача для агента (ставится в очередь) |

> **Git обязателен** для `/undo` и авто-коммита: `workspace` должен быть git-репозиторием (`git init`).

### Как работает git

1. **Перед** каждым сообщением бот сохраняет чекпоинт (`HEAD` + `git stash`, если были незакоммиченные файлы).
2. **После** успешного ответа — авто-коммит с комментарием `tg: <ваш текст>` (если `git.auto_commit: true`).
3. При `git.auto_push: true` — автоматический `git push` (нужен `GITHUB_TOKEN`).
4. **`/undo`** — `git reset --hard` к чекпоинту и восстановление stash.
5. **`/push`** / **`/pull`** — ручная синхронизация с GitHub.

Длинные тексты сообщений можно обрезать для commit message через `git.max_commit_message_length` (0 = без ограничения).

### Очередь сообщений

Можно отправить несколько текстовых или голосовых сообщений подряд — каждое встанет в очередь и выполнится **по порядку**. Лимит — `bot.max_queue_size` (по умолчанию 100).

Необработанные сообщения сохраняются в `data/queue/pending.json` и восстанавливаются после перезапуска бота. После успешной обработки запись удаляется.

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
  workspace: /home/myuser   # на Windows: C:\Users\you
  mode: agent            # agent | plan

cursor:
  api_key: ${CURSOR_API_KEY}
  model: composer-2.5
  setting_sources: []    # ["project"] — подхватить .cursor/rules
  auto_compact: true
  max_turns_before_compact: 10

openrouter:
  api_key: ${OPENROUTER_API_KEY}
  model: openrouter/free

openrouter_cli:
  api_key: ${OPENROUTER_API_KEY}
  profile: default
  binary: orc

bot:
  system_prefix: |
    Пользователь управляет этой машиной через Telegram.
    Выполняй запросы на этой машине. Кратко отвечай по-русски.
  stream_edit_interval_sec: 5.0
  max_queue_size: 100
  voice:
    enabled: true
    model: base          # tiny | base | small
    language: ru

service:
  auto_restart: true
  service_name: cursor-linux-tg-bot
  restart_delay_sec: 3
  pip_on_reload: true

git:
  enabled: true
  auto_commit: true
  auto_push: true
  github_token: ${GITHUB_TOKEN}
  commit_prefix: "tg: "
  max_commit_message_length: 0
```

После правки конфига:

```bash
# Linux
sudo systemctl restart cursor-linux-tg-bot
```

```powershell
# Windows
Restart-Service cursor-linux-tg-bot
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

Нужны Node.js 22+ и `npm install -g openrouter-cli`. Это чат через внешний CLI — **без shell/tools** на машине (в отличие от `openrouter`).

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

Агент может **запускать shell-команды** и **изменять файлы** в `workspace`. Это полноценный доступ к машине в рамках выбранной директории.

Рекомендации:

- Обязательно заполните `allowed_user_ids` — иначе бот откроют посторонние
- На Linux сервис работает от обычного пользователя, не от root
- На Windows служба запускается от Local System — ограничьте `workspace` и права пользователя при необходимости
- Для разведки без изменений используйте `mode: plan`
- Ограничьте права пользователя сервиса по минимуму
- `GITHUB_TOKEN` храните только в `.env` (права `600` на Linux)

## Локальный запуск (без службы)

### Linux

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

### Windows

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

copy config.example.yaml config.yaml
copy .env.example .env
# заполните .env

Get-Content .env | ForEach-Object {
  if ($_ -match '^([^#=]+)=(.*)$') { Set-Item -Path "env:$($matches[1])" -Value $matches[2] }
}
python run.py -c config.yaml
```

## Устранение неполадок

| Симптом | Что проверить |
|---------|---------------|
| Сервис не стартует (Linux) | `sudo journalctl -u cursor-linux-tg-bot -n 50 --no-pager` |
| Служба не стартует (Windows) | Event Viewer → Application; проверьте `run-bot.cmd`, `.env`, `config.yaml` |
| «Cursor не запустился» | `CURSOR_API_KEY` в `.env` |
| | Linux: `sudo -u USER agent login` или `agent --version` |
| | Windows: `agent login` и `agent --version` в cmd |
| Бот не отвечает | Ваш id в `allowed_user_ids` |
| | Токен бота в `.env` |
| `/push` не работает | `GITHUB_TOKEN` в `.env`, remote на GitHub |
| VK молчит | Long Poll включён, сообщения сообщества включены |
| `agent: command not found` (Linux) | `sudo -u USER bash -c 'curl -fsSL https://cursor.com/install \| bash'` |
| Голос не распознаётся | `bot.voice.enabled: true`; при первом запуске скачивается модель Whisper |
| Долгий ответ | Один запрос на чат; дождитесь или `/new` / `/stop` |

Проверка от имени пользователя сервиса (Linux):

```bash
sudo -u myuser bash -lc 'set -a && source /path/to/CursorLinuxTgBot/.env && set +a && /path/to/CursorLinuxTgBot/.venv/bin/cursor-linux-tg-bot -c /path/to/CursorLinuxTgBot/config.yaml'
```

## Лицензия

MIT. Используйте на свой риск: агент имеет доступ к выполнению команд на машине.
