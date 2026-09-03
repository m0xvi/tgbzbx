# Инструкция по обновлению zabbix-tg-bot

Универсальный порядок обновления — 4 шага:

```
1. остановить бота и сохранить резервную копию .env
2. обновить файлы (git pull ИЛИ распаковать архив поверх)
3. доложить новые переменные в .env (сравнить с env.example) + зависимости
4. запустить и проверить
```

⚠️ Файл `.env` в архиве и в репозитории **нет** — при обновлении он не
затирается, ваши токены/пароли остаются на месте.

---

## Вариант А. Сервер, где бот установлен из git (новая машина)

```bash
cd /opt/tgbzbx                          # папка, куда делали git clone
sudo systemctl stop zabbix-tg-bot

cp .env ~/.env.bak-$(date +%F-%H%M)     # бэкап конфига

git pull                                # забрать свежий код

diff .env env.example                   # ← ДОБАВИТЬ новые переменные (см. таблицу ниже)
nano .env

.venv/bin/pip install -r requirements.txt   # если requirements.txt менялся
.venv/bin/python check_proxy.py             # проверка: всё должно быть ✔

sudo systemctl start zabbix-tg-bot
journalctl -u zabbix-tg-bot -f           # смотреть логи, Ctrl+C — выйти
```

## Вариант Б. Сервер vanessa (нативный python3, systemd, без git)

```bash
# 1. скачать архив на сервер (scp с рабочего машины, wget с github и т.п.)
#    например: wget https://github.com/m0xvi/tgbzbx/archive/refs/heads/master.zip -O /tmp/tgbzbx.zip

sudo systemctl stop zabbix-tg-bot
cp /home/vanessa/zabbix-tg-bot/.env ~/.env.bak-$(date +%F-%H%M)

# 2. распаковать ПОВЕРХ старой папки (только код; .env не пострадает)
unzip -o /tmp/tgbzbx.zip -d /tmp/newbot
cp /tmp/newbot/tgbzbx-master/*.py /tmp/newbot/tgbzbx-master/requirements.txt \
   /tmp/newbot/tgbzbx-master/env.example /tmp/newbot/tgbzbx-master/README.md \
   /tmp/newbot/tgbzbx-master/UPDATE.md /tmp/newbot/tgbzbx-master/install.sh \
   /home/vanessa/zabbix-tg-bot/

# 3. новые переменные и зависимости
cd /home/vanessa/zabbix-tg-bot
diff .env env.example        # доложить недостающее (таблица ниже)
pip3 install -r requirements.txt --quiet

# 4. запуск и проверка
python3 check_proxy.py       # всё ✔
sudo systemctl start zabbix-tg-bot
journalctl -u zabbix-tg-bot -f
```

⚠️ Пока сервис запущен, не запускайте `python3 bot.py` руками — получите
`Conflict: terminated by other getUpdates request`.

---

## Новые переменные .env (проверьте, что у вас есть)

| Переменная | Появилась в | Зачем |
|---|---|---|
| `TG_PROXY_URL` | прокси-апдейт | прокси для доступа к api.telegram.org (`http://…` / `socks5://…`) |
| `ZABBIX_WEB_URL` | «проблемы 2.0» | адрес фронтенда для кнопок «🌐 открыть в Zabbix» (пусто = ZABBIX_URL) |
| `NOTIFY_ENABLED`, `NOTIFY_MIN_SEVERITY`, `NOTIFY_POLL_SECONDS` | уведомления | push о проблемах: вкл/выкл, порог важности (3 = средняя+), период опроса |
| `ADMIN_USERS` | /admin | кому доступны рестарт Zabbix и перезагрузка сервера (пусто = выключено) |
| `ZABBIX_SERVICE_NAME` | /admin | имя systemd-сервиса Zabbix (обычно `zabbix-server`) |

Минимально достаточный `.env` после всех обновлений:

```ini
TG_TOKEN=...
ALLOWED_USERS=...
ZABBIX_URL=http://10.20.9.50/zabbix
ZABBIX_USER=telegram-bot
ZABBIX_PASSWORD=...
TG_PROXY_URL=http://user:pass@host:port

ZABBIX_WEB_URL=http://10.20.9.50/zabbix
NOTIFY_ENABLED=true
NOTIFY_MIN_SEVERITY=3
NOTIFY_POLL_SECONDS=60
ADMIN_USERS=1322981309
ZABBIX_SERVICE_NAME=zabbix-server
```

Для работы `/admin` один раз настройте sudo — раздел «⚙️ Рестарт Zabbix
и сервера» в README.md.

---

## Что проверить после обновления

1. `python3 check_proxy.py` (или `.venv/bin/python check_proxy.py`) — все пункты ✔
2. `journalctl -u zabbix-tg-bot -f` — нет ошибок, есть «Бот запущен (long polling)»
3. В Telegram:
   - `/status` — сводка (+ кнопка «⚙️ Сервер», если вы в ADMIN_USERS)
   - `/hosts` — фильтры `🖥 Активные / ⏸ Остановл. / Все`, `🔍 Поиск узла`, бейджи проблем
   - `/problems` — по умолчанию за 24 ч, кнопки `🕒1ч/24ч/7д/Всё`, `👁 По одной`, `🌐 Zabbix`
   - `/admin` — рестарт Zabbix и сервера (после настройки sudo)
4. Дождитесь срабатывания уведомления «🆕 Упало / ✅ Восстановлено» — при
   следующем инциденте придёт в чат.

## Если после обновления что-то пошло не так

| Симптом | Решение |
|---|---|
| `Conflict: terminated by other getUpdates` | Бот запущен дважды: `pgrep -af bot.py`, лишний процесс убить |
| Новых кнопок нет | Файлы не заменились: `grep -c "ADMIN_USERS" bot.py` (должно быть > 0); повторить копирование |
| `ModuleNotFoundError` | `pip3 install -r requirements.txt` (или `.venv/bin/pip install …`) |
| `SyntaxError` | Нужен Python 3.10+: `python3 -V` |
| Бот не стартует | `journalctl -u zabbix-tg-bot -n 50`; откат: вернуть `.env` из бэкапа и распаковать предыдущий архив / `git checkout <старый коммит>` |

Откат кода никак не трогает данные в Zabbix — бот только вызывает API.

---

## Состав проекта

| Файл | Назначение |
|---|---|
| `bot.py` | весь бот: команды, диалоги, клавиатуры, графики, /admin |
| `zabbix_api.py` | клиент Zabbix 6.0 JSON-RPC (авто-перелогин) |
| `config.py` | чтение `.env` (прокси, уведомления, ADMIN_USERS, …) |
| `check_proxy.py` | проверка готовности: .env → прокси → Telegram → Zabbix |
| `install.sh` | установка «в один клик» на новую машину (venv + .env + systemd) |
| `zabbix-tg-bot.service` | шаблон systemd-юнита (install.sh генерирует свой) |
| `env.example` | образец конфигурации (копировать в `.env`) |
| `README.md` | полная документация |
