#!/usr/bin/env bash
# ============================================================================
# Установщик zabbix-tg-bot (https://github.com/m0xvi/tgbzbx)
#
# Запускать из корня склонированного репозитория:
#   ./install.sh              # venv + зависимости + .env + systemd-сервис
#   ./install.sh --no-systemd # только venv + зависимости + .env
#
# После установки: отредактируйте .env и выполните
#   .venv/bin/python check_proxy.py
# ============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
SERVICE_NAME="zabbix-tg-bot"
NO_SYSTEMD=0
[ "${1:-}" = "--no-systemd" ] && NO_SYSTEMD=1

msg()  { printf '\033[1;32m✔\033[0m %s\n' "$*"; }
info() { printf '\033[1;34m→\033[0m %s\n' "$*"; }
err()  { printf '\033[1;91m✘\033[0m %s\n' "$*" >&2; }

cd "$APP_DIR"

# ── 1. Python ────────────────────────────────────────────────────────────────
if ! command -v python3 >/dev/null; then
    err "python3 не найден. Установите: sudo apt install -y python3 python3-venv python3-pip"
    exit 1
fi
PYVER_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)')
if [ "$PYVER_OK" != "1" ]; then
    err "Нужен Python 3.10+, у вас: $(python3 -V)"
    exit 1
fi
msg "Python: $(python3 -V)"

# ── 2. Виртуальное окружение + зависимости ───────────────────────────────────
if [ ! -d .venv ]; then
    info "Создаю виртуальное окружение .venv ..."
    python3 -m venv .venv || {
        err "Не удалось создать venv. Обычно лечится: sudo apt install -y python3-venv"
        exit 1
    }
fi
info "Ставлю зависимости (aiogram, matplotlib, ...) — это пару минут ..."
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
msg "Зависимости установлены"

# ── 3. .env ──────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    SRC=""
    for cand in env.example .env.example; do
        [ -f "$cand" ] && SRC="$cand" && break
    done
    if [ -n "$SRC" ]; then
        cp "$SRC" .env
        msg "Создан .env из $SRC — теперь заполните его (см. следующий шаг)"
    else
        err "Не найден env.example — создайте .env вручную по README"
        exit 1
    fi
fi

if grep -q "^TG_TOKEN=1234567890" .env 2>/dev/null || grep -q "^TG_TOKEN=$" .env 2>/dev/null; then
    printf '\n'
    err "════ .env ещё не заполнен ════"
    echo "  Откройте $(pwd)/.env и укажите:"
    echo "    TG_TOKEN        — токен у @BotFather"
    echo "    ALLOWED_USERS   — ваш Telegram ID (@userinfobot)"
    echo "    ZABBIX_URL      — фронтенд Zabbix, напр. http://localhost/zabbix"
    echo "    ZABBIX_USER/PASSWORD — API-пользователь Zabbix"
    echo "    TG_PROXY_URL    — прокси для Telegram (если прямой доступ закрыт)"
    printf '\n'
    echo "  Затем проверьте всё:  .venv/bin/python check_proxy.py"
    [ "$NO_SYSTEMD" = "1" ] && exit 0
    echo "  и повторите:  sudo ./install.sh   (для установки systemd-сервиса)"
    exit 0
fi
msg ".env найден и заполнен"

# ── 4. Проверка связности ────────────────────────────────────────────────────
info "Проверяю конфигурацию (прокси → Telegram, Zabbix API) ..."
if ! ./.venv/bin/python check_proxy.py; then
    err "Проверка не прошла — исправьте .env по подсказкам выше и повторите"
    exit 1
fi

# ── 5. systemd ───────────────────────────────────────────────────────────────
if [ "$NO_SYSTEMD" = "1" ]; then
    msg "Готово (без systemd). Запуск вручную: .venv/bin/python bot.py"
    exit 0
fi

if [ "$(id -u)" != "0" ]; then
    if command -v sudo >/dev/null; then
        info "Повторяю установку с правами root для настройки systemd ..."
        exec sudo "$0" "$@"
    else
        err "Для установки сервиса запустите: sudo ./install.sh"
        exit 1
    fi
fi

info "Устанавливаю systemd-сервис ($SERVICE_NAME) ..."
UNIT=/etc/systemd/system/$SERVICE_NAME.service
cat > "$UNIT" <<EOF
# Автоматически сгенерировано install.sh ($(date '+%Y-%m-%d %H:%M:%S'))
[Unit]
Description=Zabbix Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$(id -gn "$RUN_USER")
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python bot.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chown -R "$RUN_USER" "$APP_DIR" 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now $SERVICE_NAME
sleep 2
systemctl --no-pager --lines=5 status $SERVICE_NAME || true
msg "Сервис установлен и запущен: systemctl status $SERVICE_NAME"
echo
echo "  Логи в реальном времени:  journalctl -u $SERVICE_NAME -f"
echo "  ⚠ Если этот же бот уже запущен на другой машине — остановите его там:"
echo "    sudo systemctl stop $SERVICE_NAME  (на старой машине)"
echo "    иначе Telegram будет отдавать Conflict: terminated by other getUpdates"
