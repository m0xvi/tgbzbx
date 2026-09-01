"""
Проверка готовности бота к запуску:  python check_proxy.py

По шагам проверяет:
  1. .env — заполненность обязательных полей
  2. Прокси → Telegram (запрос getMe: связность + валидность токена)
  3. Zabbix API — вход (user.login) и права (host.get)

Код выхода: 0 — всё готово к запуску bot.py, 1 — есть проблемы.
"""
from __future__ import annotations

import asyncio
import re
import sys

import aiohttp

from config import config
from zabbix_api import ZabbixAPI, ZabbixAPIError

PASS = "\033[92m✔\033[0m"   # зелёная галка
FAIL = "\033[91m✘\033[0m"   # красный крест
WARN = "\033[93m⚠\033[0m"   # жёлтое предупреждение


def mask(secret: str, keep: int = 8) -> str:
    if not secret:
        return "—"
    if len(secret) <= keep:
        return secret[:2] + "***"
    return secret[:keep] + "***"


def mask_proxy(p: str) -> str:
    """Прячет пароль в URL прокси вида scheme://user:pass@host:port."""
    return re.sub(r"(//[^:/@]+:)[^@]+(@)", r"\1***\2", p)


def no_color():  # если терминал не умеет ANSI
    global PASS, FAIL, WARN
    PASS, FAIL, WARN = "OK", "X", "!"


async def check_telegram() -> bool:
    print("\n── 2. Telegram (api.telegram.org) " + ("через прокси " + mask_proxy(config.tg_proxy)
                                                    if config.tg_proxy else "(прямое соединение)"))
    if not config.tg_token:
        print(f"  {FAIL} TG_TOKEN не задан в .env")
        return False

    connector: aiohttp.TCPConnector | None = None
    try:
        if config.tg_proxy.startswith("socks5://"):
            from aiohttp_socks import ProxyConnector  # ставится вместе с aiogram[proxy]
            connector = ProxyConnector.from_url(config.tg_proxy)
        url = f"https://api.telegram.org/bot{config.tg_token}/getMe"
        async with aiohttp.ClientSession(connector=connector,
                                         timeout=aiohttp.ClientTimeout(total=25)) as http:
            async with http.get(url, proxy=None if connector else config.tg_proxy) as resp:
                body = await resp.json()
                if resp.status == 200 and body.get("ok"):
                    me = body["result"]
                    print(f"  {PASS} Связность есть, токен валиден. Бот: @{me.get('username')}")
                    return True
                if resp.status == 401:
                    print(f"  {WARN} Сеть/прокси работают, но токен НЕВЕРЕН (HTTP 401).")
                    print(f"      Проверьте TG_TOKEN у @BotFather (сейчас: {mask(config.tg_token)}…)")
                    return False
                print(f"  {FAIL} Telegram ответил HTTP {resp.status}: {str(body)[:200]}")
                return False
    except ImportError as e:
        print(f"  {FAIL} {e}")
        print("      Для SOCKS5-прокси установите зависимости: pip install -r requirements.txt")
        return False
    except Exception as e:  # aiohttp, aiohttp_socks, таймауты и т.п.
        root = e
        while getattr(root, "__cause__", None):
            root = root.__cause__
        print(f"  {FAIL} Подключение не удалось: {type(e).__name__}: {str(e)[:150]}")
        print(f"      Первопричина: {type(root).__name__}: {str(root)[:150]}")
        if isinstance(e, TimeoutError) or isinstance(root, TimeoutError):
            print("      → таймаут: прокси не отвечает или слишком медленный")
        if config.tg_proxy:
            print(f"      → проверьте прокси {mask_proxy(config.tg_proxy)}: жив ли, верен ли тип")
            scheme = "socks5h" if config.tg_proxy.startswith("socks5") else "http"
            print(f"        быстрая проверка из консоли:")
            print(f"        curl -x {scheme}://host:port https://api.telegram.org -IsS --max-time 10")
        else:
            print("      → прямой доступ к api.telegram.org закрыт: задайте TG_PROXY_URL в .env")
        return False


def check_zabbix() -> bool:
    print("\n── 3. Zabbix API")
    if not config.zabbix_url or not config.zabbix_user:
        print(f"  {FAIL} ZABBIX_URL / ZABBIX_USER не заданы в .env")
        return False
    print(f"  Сервер: {config.zabbix_url}, пользователь: {config.zabbix_user}")
    zbx = ZabbixAPI(config.zabbix_url, config.zabbix_user, config.zabbix_password,
                    verify_ssl=config.verify_ssl)
    try:
        zbx.login()
    except ZabbixAPIError as e:
        msg = str(e)
        print(f"  {FAIL} Вход не удался: {msg}")
        if "Incorrect user name or password" in msg or "name or password" in msg:
            print("      → неверный логин/пароль, или пользователь заблокирован после")
            print("        неудачных попыток (Administration → Users → разблокировать)")
        elif "API access" in msg or "412" in msg:
            print("      → в роли пользователя выключен API access")
        elif "refused" in msg or "Некорректный ответ" in msg:
            print("      → фронтенд Zabbix недоступен по этому URL с машины бота")
        return False
    except Exception as e:  # сетевые ошибки (requests): DNS, refusal, таймаут, SSL…
        print(f"  {FAIL} Сетевая ошибка: {type(e).__name__}: {str(e)[:160]}")
        print(f"      → фронтенд Zabbix {config.zabbix_url} недоступен с машины бота")
        if "SSL" in str(e) or "certificate" in str(e):
            print("      → проблема с сертификатом: ZABBIX_VERIFY_SSL=false для самоподписанного")
        return False
    print(f"  {PASS} Вход выполнен (user.login)")

    try:
        hosts = zbx.get_hosts()
        print(f"  {PASS} host.get: доступно узлов — {len(hosts)}")
        if not hosts:
            print(f"      {WARN} прав на группы узлов у пользователя нет — создание тоже не сработает")
        return True
    except Exception as e:
        print(f"  {WARN} host.get не прошёл: {str(e)[:160]}")
        print("      → проверьте права группы пользователей на группы узлов (read-write)")
        return False


async def main() -> int:
    if not sys.stdout.isatty():
        no_color()
    ok = True

    print("════ Проверка конфигурации zabbix-tg-bot ════")
    print("\n── 1. .env")
    checks = [
        ("TG_TOKEN", bool(config.tg_token), mask(config.tg_token)),
        ("ALLOWED_USERS", bool(config.allowed_users),
         ", ".join(map(str, config.allowed_users)) or "ПУСТО!"),
        ("ZABBIX_URL", bool(config.zabbix_url), config.zabbix_url),
        ("ZABBIX_USER", bool(config.zabbix_user), config.zabbix_user),
        ("ZABBIX_PASSWORD", bool(config.zabbix_password),
         "задан" if config.zabbix_password else "ПУСТО!"),
        ("TG_PROXY_URL", True, mask_proxy(config.tg_proxy) or "(не используется)"),
    ]
    for name, good, value in checks:
        mark = PASS if good else FAIL
        print(f"  {mark} {name}: {value}")
        ok &= good
    if not config.allowed_users:
        print(f"      {WARN} без ALLOWED_USERS бот будет отклонять всех пользователей")

    ok &= await check_telegram()
    ok &= check_zabbix()

    print("\n" + "═" * 46)
    if ok:
        print(f"{PASS} Всё готово — запускайте:  python bot.py")
        return 0
    print(f"{FAIL} Есть проблемы — исправьте и повторите:  python check_proxy.py")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
