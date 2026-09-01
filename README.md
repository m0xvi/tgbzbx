# Zabbix Telegram Bot (Zabbix 6.0 LTS)

Бот для управления Zabbix через Telegram: просмотр и **создание узлов**, проблемы
с подтверждением, режим обслуживания, графики метрик и последние значения.

```
Telegram ←→ Bot API ←→ этот бот (Python, aiogram 3) ←→ Zabbix HTTP API (JSON-RPC) ←→ Zabbix 6.0
```

## Возможности

| Команда / действие | Что делает |
|---|---|
| `/status` | 📊 Сводная панель: версия Zabbix, узлы (наблюдаются/остановлены/в обслуживании), проблемы по важности, обслуживания |
| `/hosts` | Узлы, **по умолчанию только активные** (⏸ остановленные скрыты): фильтры-кнопки 🖥 Активные / ⏸ Остановленные / Все, **🔍 поиск** по имени/IP/DNS, в кнопках — счётчик проблем (💥N, свежие 🔥 — сверху), доступность агента (🟢/🟡) и IP; карточка: группы, шаблоны, теги, ошибки агента |
| из карточки узла | ⚠️ проблемы · ✏️ **изменить** · 📊 метрики · 📈 график · 🔧 обслуживание · 🗑 удаление |
| ✏️ изменение узла | Имя, IP/DNS, порт, описание, вкл/выкл наблюдения, привязка/отвязка шаблонов (`host.update`) |
| `/problems [0-5]` | Проблемы, **сгруппированные по узлам**, **по умолчанию за 24 ч**: сводка по важности, фильтры (🟠+/🔴+/💥, 🕒1ч/24ч/7д/всё, скрыть подтверждённые), режим **«👁 По одной»** с навигацией ◀️▶️, подтверждение одной или всех, кнопка **🌐 Zabbix** (открыть в браузере) |
| из карточки узла | кнопка **🌐 В Zabbix** — «Последние данные» узла в браузере |
| `/addhost` | Диалог создания узла: имя → IP/DNS → группа (поиск) → шаблон (поиск) → подтверждение |
| `/maintenance` | Обслуживания: список, постановка на 30 мин … 24 ч, завершение кнопкой ⏹ |
| `/graph` | График любой числовой метрики за 1 / 3 / 24 ч (картинка PNG) |
| `/latest` | Последние значения метрик узла |
| `/notify on/off/0-5` | 🔔 Push-уведомления о новых проблемах всем пользователям из `ALLOWED_USERS` (порог важности настраивается) |
| `/cancel` | Прервать диалог |

Безопасность: белый список Telegram ID (`ALLOWED_USERS`), бот отвечает только им.

## Требования

- Python **3.10+**
- Zabbix **6.0 LTS** (фронтенд доступен по HTTP/HTTPS с машины, где крутится бот)
- Исходящий доступ к `api.telegram.org:443` (long polling — вебхук и белый IP не нужны)

## Установка

```bash
cd zabbix-tg-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env   # заполнить (см. ниже)
python check_proxy.py               # ← проверит .env, прокси, Telegram и Zabbix
python bot.py
```

### Установка на новой машине (с install.sh)


 1. Зависимости системы и клон
 ```bash
sudo apt install -y git python3 python3-venv
git clone https://github.com/m0xvi/tgbzbx.git
cd tgbzbx
```

 2. Первый запуск — создаст .venv, поставит зависимости, создаст .env

```bash
./install.sh --no-systemd
```

 3. Заполнить конфиг
 ```bash
nano .env
```

 4. Финальная установка: проверит всё и поставит systemd-сервис
 ```bash
sudo ./install.sh
```

Скрипт сам: создаст venv, поставит зависимости, скопирует env.example → .env (учёл, что в репо он без точки), подскажет какие поля заполнить, прогонит check_proxy.py и сгенерирует systemd-юнит с правильными путями и пользователем (не нужно править vanessa вручную). Протестировал оба сценария — работает.

### Если без скрипта (вручную)

```bash
git clone https://github.com/m0xvi/tgbzbx.git && cd tgbzbx
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp env.example .env && nano .env
.venv/bin/python check_proxy.py     # проверка
# systemd: поправить в zabbix-tg-bot.service User и пути, ExecStart = /path/.venv/bin/python bot.py
sudo cp zabbix-tg-bot.service /etc/systemd/system/ && sudo systemctl enable --now zabbix-tg-bot
```

## Настройка Zabbix (важно!)

Бот работает под отдельным пользователем Zabbix — не под админом.

1. **Группа пользователей.** *Administration → User groups → Create user group*
   - имя: `Telegram bots`
   - вкладка *Permissions*: добавьте нужные группы узлов с правами
     **Read-write** (без этого `host.create` будет падать с
     «No permissions…»). Например, создайте группу узлов `TG managed hosts`
     и дайте на неё read-write.

2. **Пользователь.** *Administration → Users → Create user*
   - имя: `telegram-bot`, группа: `Telegram bots`
   - *User type*: **Admin** (пользователь типа User не может создавать узлы)
   - пароль — сюда же идёт в `.env`

3. **Роль (опционально, но рекомендуется).** *Administration → User roles*:
   скопируйте роль Admin, включите **API access → Enabled** и ограничьте
   **Allowed methods**, например:

   ```
   host.get, host.create, host.delete, hostgroup.get, template.get,
   problem.get, trigger.get, event.acknowledge, maintenance.get,
   maintenance.create, item.get, history.get, user.login
   ```

   Назначьте эту роль пользователю. Тогда даже при утечке пароля бот не сможет
   сделать больше задуманного.

4. **GUI-доступ** пользователю можно выставить *Disabled* — API продолжит работать.

Проверка, что API живой (с машины с ботом):

```bash
curl -s http://zabbix.example.com/api_jsonrpc.php \
  -H 'Content-Type: application/json-rpc' \
  -d '{"jsonrpc":"2.0","method":"user.login","params":{"username":"telegram-bot","password":"ПАРОЛЬ"},"id":1}'
```

Должен вернуться токен вида `"result":"8f0c1a2b..."`.

## Настройка Telegram

1. Создайте бота у **@BotFather** (`/newbot`) → токен в `TG_TOKEN`.
2. Узнайте свой Telegram ID у **@userinfobot** → в `ALLOWED_USERS`
   (несколько — через запятую).

## Прокси для Telegram

Если `api.telegram.org` недоступен с сервера напрямую (типично для РФ) —
пропишите прокси в `.env`:

```ini
# HTTP-прокси:
TG_PROXY_URL=http://1.2.3.4:8080
TG_PROXY_URL=http://user:pass@1.2.3.4:8080
# SOCKS5:
TG_PROXY_URL=socks5://user:pass@1.2.3.4:1080
```

- Прокси используется **только для Telegram**; запросы к Zabbix идут напрямую.
- Поддерживаются `http://`, `https://`, `socks5://` (`socks5h://` и «голый»
  `host:port` тоже понимаются — приводятся к правильному виду автоматически).
- **MTProto-прокси (`tg://proxy?...`) не подходят** — они только для клиентов
  Telegram, Bot API умеет работать лишь через HTTP(S)/SOCKS5.
- Быстрая проверка прокси из консоли:

```bash
# SOCKS5:
curl -x socks5h://user:pass@1.2.3.4:1080 https://api.telegram.org -IsS --max-time 10
# HTTP:
curl -x http://user:pass@1.2.3.4:8080 https://api.telegram.org -IsS --max-time 10
```

Ответ `HTTP/2 302` или `HTTP/1.1 302` — прокси работает.

## Как проверить всё перед запуском

```bash
python check_proxy.py
```

Скрипт по шагам проверяет и человекочитаемо сообщает:
1. заполненность `.env` (токен, белый список, URL/пользователь Zabbix, прокси);
2. **прокси → Telegram**: запрос `getMe` — связность и валидность токена
   (различает «прокси недоступен», «токен неверен», «прямой доступ закрыт»);
3. **Zabbix**: `user.login` + `host.get` (вход и права на группы узлов).

Код выхода `0` — всё готово, можно запускать `python bot.py`.

## .env

| Переменная | Описание |
|---|---|
| `TG_TOKEN` | токен из @BotFather |
| `TG_PROXY_URL` | прокси для Telegram: `http://…` или `socks5://…` (пусто — напрямую) |
| `ALLOWED_USERS` | белый список Telegram user_id через запятую |
| `ZABBIX_URL` | URL фронтенда Zabbix, например `http://zabbix.example.com` |
| `ZABBIX_WEB_URL` | адрес для кнопок «открыть в Zabbix», если с телефона фронтенд доступен по другому имени (пусто — берётся `ZABBIX_URL`) |
| `ZABBIX_USER` / `ZABBIX_PASSWORD` | пользователь API, созданный выше |
| `ZABBIX_VERIFY_SSL` | `false` для самоподписанного сертификата |
| `HOSTS_PER_PAGE` | узлов на страницу (по умолчанию 8) |
| `NOTIFY_ENABLED` | рассылать новые проблемы (`true`/`false`) |
| `NOTIFY_MIN_SEVERITY` | порог важности уведомлений 0–5 (по умолчанию 3 — средняя и выше) |
| `NOTIFY_POLL_SECONDS` | период опроса Zabbix в секундах (по умолчанию 60) |

## Уведомления: что упало и что поднялось

Бот в фоне опрашивает Zabbix (каждые `NOTIFY_POLL_SECONDS` секунд) и присылает
всем пользователям из `ALLOWED_USERS`:

- 🆕 **«Упало (N)»** — новые проблемы важностью от `NOTIFY_MIN_SEVERITY`
  (с кнопкой «⚠️ Проблемы в Zabbix»);
- ✅ **«Восстановлено (N)»** — проблемы, которые исчезли (сервис поднялся),
  с длительностью «длилось X».

Проблемы, приглушённые обслуживанием, не считаются «новыми». Первый опрос
после старта — базлайн, уведомлений не шлёт; смена порога (`/notify 4`) тоже
пересобирает базлайн без ложных срабатываний.

Управление на лету: `/notify off`, `/notify on`, `/notify 4`
(действует до перезапуска; постоянные значения — в `.env`).

> 💡 Если в Zabbix копятся годовалые проблемы от выключенных узлов —
> на странице проблем в боте они не мешают: по умолчанию показываются
> только последние 24 ч, остальное — по кнопке «Всё». А чтобы старьё
> не приходило в уведомлениях, поднимите порог: `/notify 3` или выше.

## Автозапуск (systemd)

В проекте есть готовый юнит **`zabbix-tg-bot.service`** под запуск системным
`python3` (без venv). Поправьте при необходимости `User` и путь в
`WorkingDirectory`, затем:

```bash
# если раньше ставили сервис с .venv — уберите его:
sudo systemctl disable --now zabbix-tg-bot 2>/dev/null

sudo cp zabbix-tg-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zabbix-tg-bot
journalctl -u zabbix-tg-bot -f
```

⚠️ Перед запуском сервиса остановите ручной запуск бота (Ctrl+C) — два
одновременно работающих процесса будут конфликтовать
(`Conflict: terminated by other getUpdates request`).

Если бот крутится в venv — просто поменяйте в юните строку на
`ExecStart=/opt/zabbix-tg-bot/.venv/bin/python bot.py`.

## Структура проекта

```
bot.py         — весь бот: команды, диалоги (FSM), клавиатуры, графики
zabbix_api.py  — клиент Zabbix 6.0 JSON-RPC (авто-перелогин при истечении сессии)
check_proxy.py — проверка готовности: .env, прокси→Telegram, вход в Zabbix
config.py      — чтение .env (в т.ч. нормализация TG_PROXY_URL)
requirements.txt, .env.example
```

## Типовые проблемы

| Симптом | Причина / решение |
|---|---|
| Бот молчит, в логах `ClientConnectorError` / таймауты | Нет доступа к `api.telegram.org` — задайте `TG_PROXY_URL` (проверка: `python check_proxy.py`) |
| `ProxyConnectionError: Couldn't connect to proxy` | Прокси мёртв или указан не тот тип/порт — см. curl-проверку выше |
| `host.get: доступно узлов — 0` | У группы пользователя `telegram-bot` нет прав на группы узлов: Administration → User groups → Permissions → Host group permissions → Add → **Read-write** (для групп шаблонов достаточно Read) |
| `HTTP 412` / «API запрещён» | В роли пользователя выключен API access |
| `user.login: Incorrect user name or password` | Неверный `ZABBIX_USER/PASSWORD`, или пользователь заблокирован |
| `host.create: No permissions…` | У группы пользователя нет Read-write на группу узлов |
| График пустой | У элемента нет данных за период, или метрика не числовая |
| `Conflict: terminated by other getUpdates request` | Бот уже запущен в другом месте (второй процесс) |
| timeouts до Zabbix | Фронтенд Zabbix недоступен с хоста бота / firewall |

## Как расширять

Все методы Zabbix API уже доступны через `zbx._call(method, params)` —
например, `zbx._call("host.update", {...})` для изменения узла или
`zbx._call("hostgroup.create", {...})`. Документация методов:
[Zabbix 6.0 API](https://www.zabbix.com/documentation/6.0/en/manual/api/reference).
