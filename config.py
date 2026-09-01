"""Конфигурация бота из переменных окружения (.env)."""
import os
from types import SimpleNamespace

from dotenv import load_dotenv

load_dotenv()


def _parse_users(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x.strip().isdigit()]


def _bool(raw, default: bool = True) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _norm_proxy(raw: str) -> str:
    """Приводим схему прокси к виду, понятному aiohttp/aiohttp-socks."""
    raw = raw.strip()
    if raw.startswith("socks5h://"):        # socks5h (DNS через прокси) == socks5 тут
        raw = "socks5://" + raw[len("socks5h://"):]
    if raw and not raw.startswith(("http://", "https://", "socks5://")):
        raw = "http://" + raw               # голый host:port считаем http-прокси
    return raw


_raw_proxy = os.getenv("TG_PROXY_URL", "")

config = SimpleNamespace(
    # Токен бота из @BotFather
    tg_token=os.getenv("TG_TOKEN", ""),
    # Белый список Telegram user_id (через запятую). Узнать свой id: @userinfobot
    allowed_users=_parse_users(os.getenv("ALLOWED_USERS", "")),
    # URL фронтенда Zabbix, например http://zabbix.example.com (без /api_jsonrpc.php)
    zabbix_url=os.getenv("ZABBIX_URL", "").rstrip("/"),
    # Ссылки «открыть в Zabbix»: по умолчанию = ZABBIX_URL.
    # Укажите, если фронтенд доступен с телефона по другому адресу (например, по имени).
    zabbix_web_url=os.getenv("ZABBIX_WEB_URL",
                             os.getenv("ZABBIX_URL", "")).rstrip("/"),
    zabbix_user=os.getenv("ZABBIX_USER", ""),
    zabbix_password=os.getenv("ZABBIX_PASSWORD", ""),
    verify_ssl=_bool(os.getenv("ZABBIX_VERIFY_SSL"), True),
    # Прокси для соединения с Telegram (api.telegram.org).
    # Форматы: http://host:port, http://user:pass@host:port,
    #          socks5://user:pass@host:port  (socks5h:// тоже понимается)
    # Пусто — прямое соединение. MTProto-прокси (tg://...) НЕ поддерживаются.
    tg_proxy=_norm_proxy(_raw_proxy),
    hosts_per_page=max(1, int(os.getenv("HOSTS_PER_PAGE", "8"))),
    # Фоновые уведомления о новых проблемах (рассылка всем ALLOWED_USERS)
    notify_enabled=_bool(os.getenv("NOTIFY_ENABLED"), True),
    notify_min_severity=max(0, min(5, int(os.getenv("NOTIFY_MIN_SEVERITY", "3")))),
    notify_poll=max(20, int(os.getenv("NOTIFY_POLL_SECONDS", "60"))),
)
