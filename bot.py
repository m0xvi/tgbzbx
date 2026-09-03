"""
Zabbix 6.0 Telegram Bot
=======================
Управление Zabbix через Telegram: узлы (просмотр/создание/удаление),
проблемы с подтверждением, режим обслуживания, графики и последние данные.

Запуск:  cp .env.example .env  →  заполнить  →  python bot.py
"""
from __future__ import annotations

import asyncio
import getpass
import html
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime
from urllib.parse import quote

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, CallbackQuery, ErrorEvent, FSInputFile,
                           InlineKeyboardButton, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import config
from zabbix_api import ZabbixAPI, ZabbixAPIError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("zabbix-tg")

zbx = ZabbixAPI(config.zabbix_url, config.zabbix_user, config.zabbix_password,
                verify_ssl=config.verify_ssl)

router = Router()
dp = Dispatcher()

# --------------------------------------------------------------------------- словари

SEVERITY = {  # severity → (эмодзи, название)
    "0": ("⚪️", "не классифицировано"),
    "1": ("ℹ️", "информация"),
    "2": ("⚠️", "предупреждение"),
    "3": ("🟠", "средняя"),
    "4": ("🔴", "высокая"),
    "5": ("💥", "критическая"),
}
IFACE_TYPE = {0: "Agent", 1: "Agent", 2: "SNMP", 3: "IPMI", 4: "JMX"}
AVAIL = {0: "❔", 1: "🟢", 2: "🟡"}
MODE_TITLES = {
    "v": "🖥 <b>Узлы сети</b> — выберите узел:",
    "g": "📈 <b>Графики</b> — выберите узел:",
    "m": "🔧 <b>Обслуживание</b> — выберите узел:",
    "l": "📊 <b>Последние данные</b> — выберите узел:",
}
HOST_BTN_PREFIX = {"v": "hv", "g": "hg", "m": "hm", "l": "hl"}


# --------------------------------------------------------------------------- утилиты

def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def cut(text: str, n: int = 3900) -> str:
    return text if len(text) <= n else text[:n] + "\n…"


def fmt_age(ts) -> str:
    d = max(0, int(time.time()) - int(ts))
    if d < 60:
        return f"{d} с"
    if d < 3600:
        return f"{d // 60} мин"
    if d < 86400:
        return f"{d // 3600} ч {d % 3600 // 60} мин"
    return f"{d // 86400} д {d % 86400 // 3600} ч"


def fmt_left(ts) -> str:
    d = max(0, int(ts) - int(time.time()))
    h, m = d // 3600, d % 3600 // 60
    return f"{h} ч {m} мин" if h else f"{m} мин"


def fmt_dt(ts) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y %H:%M")


def fmt_val(v) -> str:
    try:
        f = float(v)
        return f"{f:g}"
    except (TypeError, ValueError):
        return str(v)


async def edit_or_answer(cb: CallbackQuery, text: str, kb=None):
    """edit_text с fallback: старое сообщение (>48 ч) отредактировать нельзя."""
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


def zbx_error_text(e: Exception) -> str:
    return f"❌ <b>Ошибка Zabbix API</b>\n<code>{esc(str(e))}</code>"


# --------------------------------------------------------------------------- ACL

class ACLMiddleware(BaseMiddleware):
    """Пускает только пользователей из ALLOWED_USERS (белый список .env)."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not config.allowed_users:
            await event.answer("⛔️ Бот не настроен: пустой ALLOWED_USERS в .env")
            return
        if user is None or user.id not in config.allowed_users:
            log.warning("Отказ в доступе: id=%s username=%s",
                        getattr(user, "id", "?"), getattr(user, "username", "?"))
            if isinstance(event, Message):
                await event.answer(
                    f"⛔️ Нет доступа. Ваш Telegram ID: <code>{user.id}</code> "
                    f"— добавьте его в ALLOWED_USERS в .env")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔️ Нет доступа", show_alert=True)
            return
        return await handler(event, data)


# --------------------------------------------------------------------------- рендеры

HOST_STATUS_FILTERS = (("active", "🖥 Активные"),
                       ("stopped", "⏸ Остановл."),
                       ("all", "Все"))


async def render_host_list(mode: str = "v", page: int = 0,
                           status_filter: str = "active", search: str | None = None):
    """Список узлов: по умолчанию только активные (наблюдение включено).

    Сортировка: свежие проблемы (сутки) → без проблем → старые проблемы.
    В кнопках: счётчик проблем, доступность агента, IP.
    """
    hosts, problems = await asyncio.gather(
        asyncio.to_thread(zbx.get_hosts),
        asyncio.to_thread(zbx.get_problems, None, 0, 200),
    )
    now = int(time.time())
    prob_counts: dict[str, int] = {}
    prob_worst: dict[str, int] = {}
    prob_latest: dict[str, int] = {}
    for p in problems:
        for h in p.get("hosts_list") or []:
            hid = h["hostid"]
            prob_counts[hid] = prob_counts.get(hid, 0) + 1
            prob_worst[hid] = max(prob_worst.get(hid, 0), int(p["severity"]))
            prob_latest[hid] = max(prob_latest.get(hid, 0), int(p["clock"]))

    total = len(hosts)
    n_active = sum(1 for h in hosts if h.get("status") == "0")
    n_maint = sum(1 for h in hosts if h.get("maintenance_status") == "1")
    n_prob = sum(1 for h in hosts if h.get("status") == "0"
                 and h["hostid"] in prob_counts)
    n_fresh = sum(1 for h in hosts if h.get("status") == "0"
                  and prob_latest.get(h["hostid"], 0) >= now - 86400)

    view = list(hosts)
    if status_filter == "active":
        view = [h for h in view if h.get("status") == "0"]
    elif status_filter == "stopped":
        view = [h for h in view if h.get("status") == "1"]
    if search:
        q = search.lower()
        view = [h for h in view
                if q in h["name"].lower() or q in h["host"].lower()
                or any(q in (i.get("ip") or "") or q in (i.get("dns") or "")
                       for i in h.get("interfaces") or [])]

    def tier(h):
        if prob_latest.get(h["hostid"], 0) >= now - 86400:
            return 0                      # свежие проблемы — наверх
        if h["hostid"] in prob_counts:
            return 2                      # старые проблемы — вниз
        return 1                          # без проблем

    view.sort(key=lambda h: (tier(h), -prob_worst.get(h["hostid"], 0),
                             h["name"].lower()))

    per = config.hosts_per_page
    if search:
        pages, page = 1, 0
        chunk = view[:per]
    else:
        pages = max(1, (len(view) + per - 1) // per)
        page = max(0, min(page, pages - 1))
        chunk = view[page * per:(page + 1) * per]

    kb = InlineKeyboardBuilder()
    prefix = HOST_BTN_PREFIX[mode]
    for h in chunk:
        hid = h["hostid"]
        ifs = h.get("interfaces") or []
        ip = (ifs[0].get("ip") or ifs[0].get("dns") or "") if ifs else ""
        cnt = prob_counts.get(hid, 0)
        if cnt:
            worst = str(prob_worst.get(hid, 0))
            icon = {"5": "💥", "4": "🔴", "3": "🟠"}.get(worst, "⚠️")
            fresh = prob_latest.get(hid, 0) >= now - 86400
            label = f"{icon}{cnt}{'🔥' if fresh else ''} {h['name']}"
        elif h.get("maintenance_status") == "1":
            label = f"🔧 {h['name']}"
        else:
            if any(i.get("available") == "1" for i in ifs):
                icon = "🟢"
            elif ifs and all(i.get("available") == "2" for i in ifs):
                icon = "🟡"
            else:
                icon = "❔"
            label = f"{icon} {h['name']}"
        if ip and len(label) + len(ip) < 56:
            label += f" · {ip}"
        cbdata = (f"{prefix}:{hid}:{page}" if mode == "v" else f"{prefix}:{hid}")
        kb.button(text=label[:60], callback_data=cbdata)
    kb.adjust(1)

    if pages > 1:
        row = []
        if page > 0:
            row.append(InlineKeyboardButton(
                text="⬅️", callback_data=f"hp:{page - 1}:{mode}:{status_filter}"))
        row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}",
                                        callback_data="noop"))
        if page < pages - 1:
            row.append(InlineKeyboardButton(
                text="➡️", callback_data=f"hp:{page + 1}:{mode}:{status_filter}"))
        kb.row(*row)

    if not search:
        kb.row(*[InlineKeyboardButton(
                    text=("▸" if s == status_filter else "") + lab,
                    callback_data=f"hf:{s}:{mode}")
                for s, lab in HOST_STATUS_FILTERS])
    kb.row(InlineKeyboardButton(text="🔍 Поиск узла", callback_data=f"hse:{mode}"))

    # ── заголовок
    lines = [MODE_TITLES[mode]]
    if search:
        lines[0] = f"🔍 <b>Поиск «{esc(search)}»</b>"
        found = f"Найдено: <b>{len(view)}</b> из {total}"
        if len(view) > per:
            found += f" — показаны первые {per}, уточните запрос"
        lines.append(found)
    else:
        stats = [f"🟢 активных: <b>{n_active}</b>"]
        if n_prob:
            stats.append(f"с проблемами: <b>{n_prob}</b>"
                         + (f" (свежих: {n_fresh})" if n_fresh else ""))
        if n_maint:
            stats.append(f"🔧 в обслуживании: {n_maint}")
        if total - n_active:
            stats.append(f"⏸ остановлено: {total - n_active}")
        lines.append(" · ".join(stats))
        shown = ("активные" if status_filter == "active"
                 else "остановленные" if status_filter == "stopped" else "все")
        lines.append(f"Показаны: <b>{shown}</b> — {len(view)} шт."
                     + (f" · страница {page + 1}/{pages}" if pages > 1 else ""))
        lines.append("")
        lines.append("<i>💥N — проблемы (свежие 🔥 — сверху), 🟡 — агент недоступен,"
                     " 🔧 — обслуживание</i>")
    return "\n".join(lines), kb.as_markup()


def build_host_view(host: dict, page: int = 0):
    lines = [f"🖥 <b>{esc(host['name'])}</b>"
             + (f" <i>({esc(host['host'])})</i>" if host["host"] != host["name"] else ""),
             f"ID: <code>{host['hostid']}</code>",
             "Статус: " + ("⏸ остановлен" if host.get("status") == "1"
                           else "🟢 под наблюдением")]

    if host.get("maintenance_status") == "1":
        lines.append("🔧 Сейчас в обслуживании (проблемы приглушены)")

    for i in host.get("interfaces") or []:
        icon = AVAIL.get(int(i.get("available", 0)), "❔")
        addr = i.get("ip") if i.get("useip") == "1" else (i.get("dns") or i.get("ip"))
        lines.append(f"{icon} {IFACE_TYPE.get(int(i['type']), i['type'])}: "
                     f"{esc(addr)}:{esc(i['port'])}")
        if i.get("available") == "2" and i.get("error"):
            lines.append(f"    └ {esc(i['error'][:150])}")

    if host.get("groups"):
        lines.append("Группы: " + esc(", ".join(g["name"] for g in host["groups"])))
    if host.get("parentTemplates"):
        lines.append("Шаблоны: " + esc(", ".join(t["name"]
                                                 for t in host["parentTemplates"])))
    if host.get("tags"):
        lines.append("Теги: " + esc(", ".join(f"{t['tag']}={t['value']}"
                                              for t in host["tags"])))
    if host.get("description"):
        lines.append(f"📝 {esc(host['description'])}")

    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Проблемы", callback_data=f"hpr:{host['hostid']}")
    kb.button(text="✏️ Изменить", callback_data=f"he:m:{host['hostid']}")
    kb.button(text="📊 Метрики", callback_data=f"hl:{host['hostid']}")
    kb.button(text="📈 График", callback_data=f"hg:{host['hostid']}")
    kb.button(text="🔧 Обслуживание", callback_data=f"hm:{host['hostid']}")
    web = web_latest_url(host["hostid"])
    if web:
        kb.button(text="🌐 В Zabbix", url=web)
    kb.button(text="🗑 Удалить", callback_data=f"hdel:{host['hostid']}")
    kb.button(text="⬅️ К списку узлов", callback_data=f"hp:{page}:v")
    kb.adjust(2, 2, 2, 1, 1)
    return "\n".join(lines), kb.as_markup()


def web_problems_url(hostid: str | None = None) -> str | None:
    """Ссылка на страницу проблем Zabbix (опционально — конкретного узла)."""
    if not config.zabbix_web_url:
        return None
    from urllib.parse import quote
    url = (f"{config.zabbix_web_url}/zabbix.php?action=problem.view&filter_set=1"
           f"&show=3")  # 3 — только активные
    if hostid:
        url += "&filter_hostids%5B%5D=" + quote(str(hostid))
    return url


def web_latest_url(hostid: str | int) -> str | None:
    """Ссылка на «Последние данные» узла в Zabbix."""
    if not config.zabbix_web_url:
        return None
    from urllib.parse import quote
    return (f"{config.zabbix_web_url}/zabbix.php?action=latest.view&filter_set=1"
            f"&filter_hostids%5B%5D=" + quote(str(hostid)))


DEFAULT_PROBLEM_HOURS = 24   # по умолчанию показываем проблемы за сутки
HOUR_OPTIONS = ((1, "🕒1ч"), (24, "🕒24ч"), (168, "🕒7д"), (0, "Всё"))


async def fetch_problems(hostid=None, min_severity=0, show_acked=True, hours=0):
    """(отфильтрованный список, всего_до_фильтра_времени)"""
    problems = await asyncio.to_thread(zbx.get_problems, hostid, min_severity, 200)
    total = len(problems)
    if not show_acked:
        problems = [p for p in problems if p["acknowledged"] != "1"]
    if hours > 0:
        threshold = int(time.time()) - hours * 3600
        problems = [p for p in problems if int(p["clock"]) >= threshold]
    return problems, total


async def render_problems(hostid=None, min_severity=0, show_acked=True,
                          hours=DEFAULT_PROBLEM_HOURS):
    """Проблемы, сгруппированные по узлам: сводка, фильтры важности и времени."""
    problems, total = await fetch_problems(hostid, min_severity, show_acked, hours)

    if hostid is None:
        title = "⚠️ <b>Проблемы</b>"
    else:
        host = await asyncio.to_thread(zbx.get_host, hostid)
        title = f"⚠️ <b>Проблемы · {esc(host['name'] if host else hostid)}</b>"

    counts: dict[str, int] = {}
    for p in problems:
        counts[p["severity"]] = counts.get(p["severity"], 0) + 1
    unacked = [p for p in problems if p["acknowledged"] != "1"]
    scope = "g" if hostid is None else f"h{hostid}"
    kb = InlineKeyboardBuilder()

    if not problems:
        period = f"за последние {hours} ч" if hours > 0 else "вообще"
        text = f"{title}\n\n✅ Проблем {period} нет"
        if total:
            text += f"\n<i>(есть {total} старее — нажмите «Всё»)</i>"
    else:
        badge = "   ".join(f"{SEVERITY[s][0]} <b>{counts[s]}</b>"
                           for s in ("5", "4", "3", "2", "1") if s in counts)
        period = f" · за {hours} ч" if hours > 0 else ""
        lines = [f"{title} · <b>{len(problems)}</b>{period}", "",
                 badge + (f"   ·   не подтв.: <b>{len(unacked)}</b>" if unacked else ""),
                 ""]
        hidden = total - len(problems)
        if hours > 0 and hidden > 0:
            lines.append(f"<i>…и ещё {hidden} старее {hours} ч — кнопка «Всё»</i>")
            lines.append("")

        groups: dict[str, list] = {}
        for p in problems:
            groups.setdefault(p.get("hosts_str") or "—", []).append(p)
        ordered = sorted(groups.items(),
                         key=lambda kv: (max(int(p["severity"]) for p in kv[1]),
                                         max(int(p["clock"]) for p in kv[1])),
                         reverse=True)
        for gname, plist in ordered:
            worst = max(int(p["severity"]) for p in plist)
            gicon = {"5": "💥", "4": "🔴", "3": "🟠"}.get(str(worst), "⚠️")
            lines.append(f"{gicon} <b>┌ {esc(gname)}</b>  ·  {len(plist)}")
            for p in sorted(plist, key=lambda x: int(x["severity"]), reverse=True):
                emoji, _ = SEVERITY.get(p["severity"], ("❔",))
                flags = ("  ✔" if p["acknowledged"] == "1" else "") + \
                        ("  🔇" if p.get("suppressed") == "1" else "")
                lines.append(f"{emoji} <b>{esc(p['name'])}</b>{flags}")
                meta = f"{fmt_age(p['clock'])} назад"
                if p.get("opdata"):
                    meta += f" · {esc(p['opdata'])}"
                lines.append(f"       <i>{meta}</i>")
            lines.append("")
        text = cut("\n".join(lines).rstrip())

    a = int(show_acked)
    kb.row(*[InlineKeyboardButton(
                text=("▸" if m == min_severity else "") + label,
                callback_data=f"prb:{scope}:{m}:{a}:{hours}")
             for m, label in ((0, "Все"), (3, "🟠+"), (4, "🔴+"), (5, "💥"))])
    kb.row(*[InlineKeyboardButton(
                text=("▸" if h == hours else "") + label,
                callback_data=f"prb:{scope}:{min_severity}:{a}:{h}")
             for h, label in HOUR_OPTIONS])
    single = InlineKeyboardButton(text="👁 По одной",
                                  callback_data=f"prb1:{scope}:{min_severity}:{a}:{hours}:0")
    web = web_problems_url(hostid)
    row = [single] + ([InlineKeyboardButton(text="🌐 Zabbix", url=web)] if web else [])
    kb.row(*row)
    kb.row(InlineKeyboardButton(
               text="🙈 Скрыть подтв." if show_acked else "👁 Показать подтв.",
               callback_data=f"prb:{scope}:{min_severity}:{int(not show_acked)}:{hours}"),
           InlineKeyboardButton(
               text="🔁 Обновить",
               callback_data=f"prb:{scope}:{min_severity}:{a}:{hours}"))
    if len(unacked) > 1:
        kb.row(InlineKeyboardButton(
            text=f"✔️ Подтвердить все ({len(unacked)})",
            callback_data=f"ackall:{scope}:{min_severity}:{a}:{hours}"))
    for p in unacked[:4]:
        kb.row(InlineKeyboardButton(
            text=f"✔ {SEVERITY[p['severity']][0]} {p['name'][:34]}",
            callback_data=f"ack:{p['eventid']}:{scope}:{min_severity}:{a}:{hours}"))
    if hostid is not None:
        kb.row(InlineKeyboardButton(text="⬅️ К узлу", callback_data=f"hv:{hostid}:0"))
    return text, kb.as_markup()


async def render_problem_one(hostid=None, min_severity=0, show_acked=True,
                             hours=DEFAULT_PROBLEM_HOURS, idx=0):
    """Режим «по одной»: карточка проблемы с навигацией ◀️/▶️."""
    problems, _total = await fetch_problems(hostid, min_severity, show_acked, hours)
    scope = "g" if hostid is None else f"h{hostid}"
    a = int(show_acked)
    kb = InlineKeyboardBuilder()
    if not problems:
        kb.row(InlineKeyboardButton(text="⬅️ К списку",
                                    callback_data=f"prb:{scope}:{min_severity}:{a}:{hours}"))
        return "Проблем не найдено (смените фильтры).", kb.as_markup()

    problems.sort(key=lambda p: (int(p["severity"]), int(p["clock"])), reverse=True)
    idx = max(0, min(idx, len(problems) - 1))
    p = problems[idx]
    emoji, sev_name = SEVERITY.get(p["severity"], ("❔", "?"))
    lines = [f"⚠️ <b>Проблема {idx + 1} / {len(problems)}</b>", "",
             f"{emoji} <b>{esc(p['name'])}</b>",
             f"🖥 Узел: <b>{esc(p.get('hosts_str') or '—')}</b>",
             f"Важность: {sev_name}",
             f"Начало: {fmt_dt(p['clock'])} ({fmt_age(p['clock'])} назад)"]
    if p.get("opdata"):
        lines.append(f"Данные: <b>{esc(p['opdata'])}</b>")
    lines.append("Статус: " + ("✔ подтверждена" if p["acknowledged"] == "1"
                               else "не подтверждена")
                 + (", 🔇 приглушена обслуживанием" if p.get("suppressed") == "1" else ""))

    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(
            text="◀️", callback_data=f"prb1:{scope}:{min_severity}:{a}:{hours}:{idx - 1}"))
    nav.append(InlineKeyboardButton(text=f"{idx + 1}/{len(problems)}",
                                    callback_data="noop"))
    if idx < len(problems) - 1:
        nav.append(InlineKeyboardButton(
            text="▶️", callback_data=f"prb1:{scope}:{min_severity}:{a}:{hours}:{idx + 1}"))
    kb.row(*nav)
    if p["acknowledged"] != "1":
        kb.row(InlineKeyboardButton(
            text="✔️ Подтвердить",
            callback_data=f"ack1:{p['eventid']}:{scope}:{min_severity}:{a}:{hours}:{idx}"))
    web = web_problems_url(hostid)
    if web:
        kb.row(InlineKeyboardButton(text="🌐 Открыть в Zabbix", url=web))
    kb.row(InlineKeyboardButton(text="⬅️ К списку",
                                callback_data=f"prb:{scope}:{min_severity}:{a}:{hours}"))
    return "\n".join(lines), kb.as_markup()


async def render_latest(hostid):
    host, items = await asyncio.gather(
        asyncio.to_thread(zbx.get_host, hostid),
        asyncio.to_thread(zbx.get_items, hostid, 30),
    )
    name = host["name"] if host else str(hostid)
    if not items:
        text = f"📊 <b>Метрики {esc(name)}</b>\n\nЧисловых элементов данных не найдено."
    else:
        lines = [f"📊 <b>Последние данные: {esc(name)}</b>", ""]
        for it in items:
            age = f" · {fmt_age(it['lastclock'])} назад" if it.get("lastclock") != "0" else ""
            lines.append(f"• {esc(it['name'])}: <b>{esc(fmt_val(it['lastvalue']))} "
                         f"{esc(it['units'])}</b><i>{age}</i>")
        text = cut("\n".join(lines))

    kb = InlineKeyboardBuilder()
    kb.button(text="📈 График", callback_data=f"hg:{hostid}")
    kb.button(text="⬅️ К узлу", callback_data=f"hv:{hostid}:0")
    kb.adjust(2)
    return text, kb.as_markup()


def draw_graph(points: list[dict], title: str, unit: str, out_path: str) -> str:
    times = [datetime.fromtimestamp(int(p["clock"])) for p in points]
    values = [float(p["value"]) for p in points]

    fig, ax = plt.subplots(figsize=(10, 4), dpi=150)
    ax.plot(times, values, linewidth=1.3, color="#c62828")
    ax.fill_between(times, values, alpha=0.12, color="#c62828")
    ax.set_title(title, fontsize=11)
    ax.grid(True, alpha=0.3)
    ax.set_ylabel(unit or "")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
    fig.autofmt_xdate()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


# --------------------------------------------------------------------------- /start, /help

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🖥 Узлы", callback_data="hp:0:v")
    kb.button(text="⚠️ Проблемы", callback_data="prb")
    kb.button(text="📈 График", callback_data="hp:0:g")
    kb.button(text="🔧 Обслуживание", callback_data="mnt")
    kb.adjust(2, 2)
    await message.answer(
        "🤖 <b>Zabbix Telegram Bot</b> (Zabbix 6.0 LTS)\n\n"
        "<b>Просмотр:</b>\n"
        "/status — сводка: версия, узлы, проблемы, обслуживания\n"
        "/hosts — узлы: карточка, изменение ✏️, метрики, удаление\n"
        "/problems [0–5] — проблемы по узлам, фильтры важности, подтверждение\n"
        "/graph · /latest — график метрики и последние данные\n\n"
        "<b>Действия:</b>\n"
        "/addhost — добавить узел\n"
        "/maintenance — обслуживания: создать / завершить ⏹\n"
        "/notify on|off|0–5 — push-уведомления о новых проблемах\n"
        "/admin — ⚙️ перезапуск Zabbix-сервера и сервера (только ADMIN_USERS)\n\n"
        "/cancel — отменить текущий диалог",
        reply_markup=kb.as_markup(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.")


# --------------------------------------------------------------------------- команды-списки

@router.message(Command("hosts"))
async def cmd_hosts(message: Message):
    text, kb = await render_host_list("v", 0)
    await message.answer(text, reply_markup=kb)


@router.message(Command("problems"))
async def cmd_problems(message: Message, command: Command):
    try:
        min_sev = max(0, min(5, int(command.args or 0)))
    except ValueError:
        min_sev = 0
    text, kb = await render_problems(min_severity=min_sev)
    await message.answer(text, reply_markup=kb)


@router.message(Command("graph"))
async def cmd_graph(message: Message):
    text, kb = await render_host_list("g", 0)
    await message.answer(text, reply_markup=kb)


@router.message(Command("latest"))
async def cmd_latest(message: Message):
    text, kb = await render_host_list("l", 0)
    await message.answer(text, reply_markup=kb)


@router.message(Command("status"))
async def cmd_status(message: Message):
    try:
        version = await asyncio.to_thread(zbx.get_api_version)
    except Exception:
        version = "?"
    hosts, problems, maints = await asyncio.gather(
        asyncio.to_thread(zbx.get_hosts),
        asyncio.to_thread(zbx.get_problems, None, 0, 200),
        asyncio.to_thread(zbx.get_maintenances),
    )
    now = int(time.time())
    monitored = sum(1 for h in hosts if h.get("status") == "0")
    in_maint = sum(1 for h in hosts if h.get("maintenance_status") == "1")
    counts: dict[str, int] = {}
    for p in problems:
        counts[p["severity"]] = counts.get(p["severity"], 0) + 1
    unacked = sum(1 for p in problems if p["acknowledged"] != "1")
    recent24 = sum(1 for p in problems if int(p["clock"]) >= now - 86400)
    badge = " ".join(f"{SEVERITY[s][0]}{counts[s]}"
                     for s in ("5", "4", "3", "2", "1") if s in counts)
    active_m = sum(1 for m in maints
                   if int(m["active_since"]) <= now <= int(m["active_till"]))
    kb = None
    if is_admin(getattr(message.from_user, "id", None)):
        kb = InlineKeyboardBuilder().button(
            text="⚙️ Сервер", callback_data="adm:menu").as_markup()
    await message.answer(
        "📊 <b>Статус Zabbix</b>\n\n"
        f"Версия: <b>{esc(version)}</b>\n"
        f"🖥 Узлов: <b>{len(hosts)}</b> — наблюдаются {monitored}, "
        f"⏸ остановлено {len(hosts) - monitored}, 🔧 в обслуживании {in_maint}\n"
        f"⚠️ Проблем: <b>{len(problems)}</b>"
        + (f" ({badge})" if badge else "")
        + f" · за 24 ч: <b>{recent24}</b>"
        + (f" · не подтв.: <b>{unacked}</b>" if unacked else "") + "\n"
        f"🔧 Активных обслуживаний: {active_m}",
        reply_markup=kb)


# --------------------------------------------------------------------------- /addhost (FSM)

class AddHost(StatesGroup):
    name = State()
    ip = State()
    group_search = State()
    template_search = State()
    confirm = State()


@router.message(Command("addhost"))
async def cmd_addhost(message: Message, state: FSMContext):
    await state.set_state(AddHost.name)
    await message.answer(
        "➕ <b>Добавление узла</b>\n\n"
        "1/5. Введите <b>техническое имя</b> узла (без пробелов, напр. "
        "<code>web-01</code>):\n\n<i>/cancel — прервать</i>")


@router.message(AddHost.name)
async def ah_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or len(name) > 128 or any(c in name for c in " \t\"'"):
        await message.answer("❌ Некорректное имя (без пробелов и кавычек). Попробуйте ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(AddHost.ip)
    await message.answer(f"2/5. Имя: <b>{esc(name)}</b> ✅\n\n"
                         "Введите <b>IP или DNS</b> интерфейса Zabbix agent "
                         "(напр. <code>10.0.0.10</code>):")


@router.message(AddHost.ip)
async def ah_ip(message: Message, state: FSMContext):
    ip = (message.text or "").strip()
    parts = ip.split(".")
    is_ip = len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    is_dns = ip and all(c.isalnum() or c in "-." for c in ip)
    if not (is_ip or is_dns):
        await message.answer("❌ Похоже, это не IP и не DNS-имя. Попробуйте ещё раз:")
        return
    await state.update_data(ip=ip)
    await state.set_state(AddHost.group_search)
    await message.answer(f"3/5. Интерфейс: <b>{esc(ip)}</b>:10050 ✅\n\n"
                         "Введите <b>часть названия группы узлов</b> для поиска "
                         "(напр. <code>linux</code>):")


@router.message(AddHost.group_search)
async def ah_group_search(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Введите минимум 2 символа для поиска группы:")
        return
    groups = await asyncio.to_thread(zbx.get_host_groups, query, 25)
    if not groups:
        await message.answer(f"Группы по запросу «{esc(query)}» не найдены. Попробуйте иначе:")
        return
    await state.update_data(groups_found={g["groupid"]: g["name"] for g in groups})
    kb = InlineKeyboardBuilder()
    for gid, gname in sorted({g["groupid"]: g["name"] for g in groups}.items(),
                             key=lambda x: x[1].lower()):
        kb.button(text=gname[:60], callback_data=f"ah:g:{gid}")
    kb.adjust(1)
    await message.answer(f"4/5. Выберите <b>группу узлов</b>:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("ah:g:"))
async def ah_group_pick(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    gid = cb.data.split(":")[2]
    gname = data.get("groups_found", {}).get(gid)
    if not gname:
        await cb.answer("Список устарел, выполните поиск заново (/addhost)", show_alert=True)
        await state.clear()
        return
    await state.update_data(groupid=gid, group_name=gname)
    await state.set_state(AddHost.template_search)
    kb = InlineKeyboardBuilder()
    kb.button(text="🚫 Без шаблона", callback_data="ah:t:none")
    kb.adjust(1)
    await cb.answer()
    await cb.message.edit_text(
        f"4/5. Группа: <b>{esc(gname)}</b> ✅\n\n"
        f"5/5. Введите <b>часть названия шаблона</b> для поиска "
        f"(напр. <code>linux</code>) или нажмите «без шаблона»:",
        reply_markup=kb.as_markup())


@router.message(AddHost.template_search)
async def ah_template_search(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    if query in ("-", "--", "none"):
        return await ah_show_confirm(message, state, None, None)
    if len(query) < 2:
        await message.answer("Введите минимум 2 символа или «-» для создания без шаблона:")
        return
    templates = await asyncio.to_thread(zbx.get_templates, query, 25)
    if not templates:
        await message.answer(f"Шаблоны по запросу «{esc(query)}» не найдены. "
                             f"Попробуйте иначе или «-» без шаблона:")
        return
    await state.update_data(templates_found={t["templateid"]: t["name"] for t in templates})
    kb = InlineKeyboardBuilder()
    for tid, tname in sorted({t["templateid"]: t["name"] for t in templates}.items(),
                             key=lambda x: x[1].lower()):
        kb.button(text=tname[:60], callback_data=f"ah:t:{tid}")
    kb.button(text="🚫 Без шаблона", callback_data="ah:t:none")
    kb.adjust(1)
    await message.answer("Выберите <b>шаблон</b>:", reply_markup=kb.as_markup())


async def ah_show_confirm(message_or_cb, state: FSMContext, tid, tname):
    data = await state.get_data()
    await state.update_data(templateid=tid, template_name=tname)
    await state.set_state(AddHost.confirm)
    tpl_line = tname if tname else "— без шаблона —"
    text = (f"📋 <b>Проверьте и создайте узел</b>\n\n"
            f"Имя: <b>{esc(data['name'])}</b>\n"
            f"Интерфейс Agent: <code>{esc(data['ip'])}:10050</code>\n"
            f"Группа: {esc(data['group_name'])}\n"
            f"Шаблон: {esc(tpl_line)}")
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data="ah:ok")
    kb.button(text="❌ Отмена", callback_data="ah:cancel")
    kb.adjust(2)
    target = message_or_cb.message if isinstance(message_or_cb, CallbackQuery) else message_or_cb
    await target.answer(text, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("ah:t:"))
async def ah_template_pick(cb: CallbackQuery, state: FSMContext):
    raw = cb.data.split(":")[2]
    if raw == "none":
        await cb.answer()
        return await ah_show_confirm(cb, state, None, None)
    data = await state.get_data()
    tname = data.get("templates_found", {}).get(raw)
    if not tname:
        await cb.answer("Список устарел, выполните поиск заново (/addhost)", show_alert=True)
        await state.clear()
        return
    await cb.answer()
    await ah_show_confirm(cb, state, raw, tname)


@router.message(AddHost.confirm)
async def ah_confirm_text(message: Message):
    await message.answer("Используйте кнопки ✅ Создать / ❌ Отмена ниже.")


@router.callback_query(F.data == "ah:ok")
async def ah_create(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await cb.answer("Создаю…")
    try:
        res = await asyncio.to_thread(
            zbx.create_host,
            data["name"], data["ip"], data["groupid"],
            [data["templateid"]] if data.get("templateid") else None,
        )
        hostid = res.get("hostids", ["?"])[0]
        await state.clear()
        await edit_or_answer(
            cb,
            f"✅ <b>Узел создан!</b>\n\n"
            f"Имя: <b>{esc(data['name'])}</b>\nID: <code>{hostid}</code>\n"
            f"Шаблон: {esc(data.get('template_name') or '—')}\n\n"
            f"Данные появятся через пару минут.",
            InlineKeyboardBuilder()
            .button(text="🖥 Открыть узел", callback_data=f"hv:{hostid}:0")
            .adjust(1).as_markup())
    except ZabbixAPIError as e:
        await cb.message.answer(zbx_error_text(e) +
                                "\n\n<i>Узел не создан. /addhost — начать заново.</i>")
        await state.clear()


@router.callback_query(F.data == "ah:cancel")
async def ah_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Отменено")
    await edit_or_answer(cb, "❌ Добавление узла отменено.")


# --------------------------------------------------------------------------- редактирование узла

class EditHost(StatesGroup):
    name = State()
    ip = State()
    port = State()
    desc = State()
    tpl_add = State()


def _clean_iface(iface: dict) -> dict:
    """Только поля, которые принимает host.update для интерфейса."""
    return {k: str(iface.get(k, "")) for k in
            ("interfaceid", "type", "main", "useip", "ip", "dns", "port")}


def _is_ip(v: str) -> bool:
    parts = v.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


async def render_edit_menu(hostid: str):
    host = await asyncio.to_thread(zbx.get_host, hostid)
    if not host:
        return "Узел не найден.", None
    iface = (host.get("interfaces") or [{}])[0]
    addr = iface.get("ip") if iface.get("useip") == "1" else (iface.get("dns") or "")
    tpls = host.get("parentTemplates") or []
    tpl_names = ", ".join(t["name"] for t in tpls[:5]) + ("…" if len(tpls) > 5 else "")
    lines = ["✏️ <b>Редактирование узла</b>\n",
             f"🖥 <b>{esc(host['name'])}</b>"
             + (f" <i>({esc(host['host'])})</i>" if host["host"] != host["name"] else ""),
             f"🌐 {esc(addr)}:{esc(iface.get('port') or '—')} "
             f"({IFACE_TYPE.get(int(iface.get('type', 1) or 1), 'Agent')})",
             "⏯ Наблюдение: " + ("включено" if host.get("status") == "0"
                                  else "остановлено"),
             f"📋 Шаблонов: {len(tpls)}" + (f" — {esc(tpl_names)}" if tpl_names else ""),
             f"📝 Описание: {esc(host.get('description') or '—')}\n",
             "Что изменить?"]
    status_label = ("⏸ Остановить наблюдение" if host.get("status") == "0"
                    else "▶️ Возобновить наблюдение")
    kb = InlineKeyboardBuilder()
    kb.button(text="🏷 Имя", callback_data=f"he:n:{hostid}")
    kb.button(text="🌐 IP / DNS", callback_data=f"he:i:{hostid}")
    kb.button(text="🔌 Порт", callback_data=f"he:p:{hostid}")
    kb.button(text=status_label, callback_data=f"he:s:{hostid}")
    kb.button(text="📝 Описание", callback_data=f"he:d:{hostid}")
    kb.button(text=f"📋 Шаблоны ({len(tpls)})", callback_data=f"he:t:{hostid}")
    kb.button(text="⬅️ К узлу", callback_data=f"hv:{hostid}:0")
    kb.adjust(2, 2, 2, 1)
    return "\n".join(lines), kb.as_markup()


async def render_tpl_menu(hostid: str):
    host = await asyncio.to_thread(zbx.get_host, hostid)
    if not host:
        return "Узел не найден.", None
    tpls = host.get("parentTemplates") or []
    lines = [f"📋 <b>Шаблоны узла {esc(host['name'])}</b> — {len(tpls)}", ""]
    if not tpls:
        lines.append("Шаблоны не привязаны.")
    kb = InlineKeyboardBuilder()
    for t in tpls:
        lines.append(f"• {esc(t['name'])}")
        kb.button(text=f"− {t['name'][:55]}",
                  callback_data=f"he:tr:{hostid}:{t['templateid']}")
    if tpls:
        kb.adjust(1)
    kb.row(InlineKeyboardButton(text="➕ Добавить шаблон",
                                callback_data=f"he:ts:{hostid}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"he:m:{hostid}"))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(F.data.startswith("he:"))
async def cb_host_edit(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    action = parts[1] if len(parts) > 1 else "m"
    hostid = parts[2] if len(parts) > 2 else ""
    extra = parts[3] if len(parts) > 3 else ""

    if action == "m":
        await state.clear()
        await cb.answer()
        text, kb = await render_edit_menu(hostid)
        return await edit_or_answer(cb, text, kb)

    if action == "s":  # вкл/выкл наблюдение
        host = await asyncio.to_thread(zbx.get_host, hostid)
        new_status = "1" if host.get("status") == "0" else "0"
        try:
            await asyncio.to_thread(zbx.update_host, hostid, {"status": new_status})
        except ZabbixAPIError as e:
            return await cb.answer(f"Ошибка: {e}"[:190], show_alert=True)
        await cb.answer("✅ Статус изменён")
        text, kb = await render_edit_menu(hostid)
        return await edit_or_answer(cb, text, kb)

    if action == "t":
        await state.clear()
        await cb.answer()
        text, kb = await render_tpl_menu(hostid)
        return await edit_or_answer(cb, text, kb)

    if action == "tr":  # открепить шаблон
        host = await asyncio.to_thread(zbx.get_host, hostid)
        keep = [t["templateid"] for t in host.get("parentTemplates") or []
                if t["templateid"] != extra]
        try:
            await asyncio.to_thread(zbx.update_host, hostid,
                                    {"templates": [{"templateid": t} for t in keep]})
        except ZabbixAPIError as e:
            return await cb.answer(f"Ошибка: {e}"[:190], show_alert=True)
        await cb.answer("Шаблон откреплён")
        text, kb = await render_tpl_menu(hostid)
        return await edit_or_answer(cb, text, kb)

    if action == "ta":  # привязать шаблон
        host = await asyncio.to_thread(zbx.get_host, hostid)
        tpls = [t["templateid"] for t in host.get("parentTemplates") or []]
        if extra not in tpls:
            tpls.append(extra)
        try:
            await asyncio.to_thread(zbx.update_host, hostid,
                                    {"templates": [{"templateid": t} for t in tpls]})
        except ZabbixAPIError as e:
            return await cb.answer(f"Ошибка: {e}"[:190], show_alert=True)
        await cb.answer("Шаблон привязан")
        text, kb = await render_tpl_menu(hostid)
        return await edit_or_answer(cb, text, kb)

    if action == "ts":  # поиск шаблона для добавления
        await state.update_data(hostid=hostid)
        await state.set_state(EditHost.tpl_add)
        await cb.answer()
        return await edit_or_answer(
            cb, "Введите <b>часть названия шаблона</b> для поиска:\n\n<i>/cancel — отмена</i>")

    # диалоги ввода: имя / ip / порт / описание
    host = await asyncio.to_thread(zbx.get_host, hostid)
    if not host:
        return await cb.answer("Узел не найден", show_alert=True)
    iface0 = (host.get("interfaces") or [{}])[0]
    prompts = {
        "n": ("🏷 <b>Новое видимое имя</b> узла (до 128 символов)\n"
              f"Текущее: <b>{esc(host['name'])}</b>\n\n<i>/cancel — отмена</i>",
              EditHost.name),
        "i": ("🌐 <b>Новый IP или DNS</b> интерфейса Agent\n"
              f"Текущий: <code>{esc(iface0.get('ip') or iface0.get('dns') or '—')}</code>\n\n"
              "Если ввести DNS-имя — интерфейс переключится в режим DNS.\n\n"
              "<i>/cancel — отмена</i>",
              EditHost.ip),
        "p": (f"🔌 <b>Новый порт</b> интерфейса Agent (1–65535)\n"
              f"Текущий: <code>{esc(iface0.get('port') or '—')}</code>\n\n"
              "<i>/cancel — отмена</i>",
              EditHost.port),
        "d": ("📝 <b>Новое описание</b> (до 500 символов, «-» — очистить)\n"
              f"Текущее: {esc(host.get('description') or '—')}\n\n"
              "<i>/cancel — отмена</i>",
              EditHost.desc),
    }
    if action not in prompts:
        return await cb.answer()
    text, st = prompts[action]
    await state.update_data(hostid=hostid, iface=_clean_iface(iface0))
    await state.set_state(st)
    await cb.answer()
    await edit_or_answer(cb, text)


async def _he_done(message: Message, state: FSMContext, ok_text: str):
    data = await state.get_data()
    await state.clear()
    await message.answer(ok_text)
    text, kb = await render_edit_menu(data["hostid"])
    await message.answer(cut(text), reply_markup=kb)


@router.message(EditHost.name)
async def he_set_name(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val or len(val) > 128:
        return await message.answer("❌ Пусто или длиннее 128 символов. Ещё раз:")
    data = await state.get_data()
    try:
        await asyncio.to_thread(zbx.update_host, data["hostid"], {"name": val})
    except ZabbixAPIError as e:
        await state.clear()
        return await message.answer(zbx_error_text(e))
    await _he_done(message, state, f"✅ Имя изменено на <b>{esc(val)}</b>")


@router.message(EditHost.ip)
async def he_set_ip(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    data = await state.get_data()
    iface = data.get("iface") or {}
    if _is_ip(val):
        iface.update({"useip": "1", "ip": val, "dns": ""})
    elif val and all(c.isalnum() or c in "-." for c in val):
        iface.update({"useip": "0", "dns": val, "ip": ""})
    else:
        return await message.answer("❌ Похоже, это не IP и не DNS-имя. Ещё раз:")
    try:
        await asyncio.to_thread(zbx.update_host, data["hostid"],
                                {"interfaces": [iface]})
    except ZabbixAPIError as e:
        await state.clear()
        return await message.answer(zbx_error_text(e))
    await _he_done(message, state, f"✅ Интерфейс изменён: <code>{esc(val)}</code>")


@router.message(EditHost.port)
async def he_set_port(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val.isdigit() or not (1 <= int(val) <= 65535):
        return await message.answer("❌ Порт — число от 1 до 65535. Ещё раз:")
    data = await state.get_data()
    iface = data.get("iface") or {}
    iface["port"] = val
    try:
        await asyncio.to_thread(zbx.update_host, data["hostid"],
                                {"interfaces": [iface]})
    except ZabbixAPIError as e:
        await state.clear()
        return await message.answer(zbx_error_text(e))
    await _he_done(message, state, f"✅ Порт изменён на <code>{esc(val)}</code>")


@router.message(EditHost.desc)
async def he_set_desc(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if val == "-":
        val = ""
    if len(val) > 500:
        return await message.answer("❌ Максимум 500 символов. Ещё раз (или «-» чтобы очистить):")
    data = await state.get_data()
    try:
        await asyncio.to_thread(zbx.update_host, data["hostid"],
                                {"description": val})
    except ZabbixAPIError as e:
        await state.clear()
        return await message.answer(zbx_error_text(e))
    await _he_done(message, state, "✅ Описание обновлено")


@router.message(EditHost.tpl_add)
async def he_tpl_search(message: Message, state: FSMContext):
    q = (message.text or "").strip()
    if len(q) < 2:
        return await message.answer("Минимум 2 символа:")
    data = await state.get_data()
    templates = await asyncio.to_thread(zbx.get_templates, q, 25)
    if not templates:
        return await message.answer(f"По «{esc(q)}» ничего не найдено. Попробуйте иначе:")
    kb = InlineKeyboardBuilder()
    for t in templates:
        kb.button(text=f"＋ {t['name'][:55]}",
                  callback_data=f"he:ta:{data['hostid']}:{t['templateid']}")
    kb.button(text="❌ Отмена", callback_data=f"he:t:{data['hostid']}")
    kb.adjust(1)
    await message.answer("Выберите шаблон для привязки:", reply_markup=kb.as_markup())


# --------------------------------------------------------------------------- /maintenance

async def render_maintenances():
    ms = await asyncio.to_thread(zbx.get_maintenances)
    now = int(time.time())
    active = [m for m in ms if int(m["active_since"]) <= now <= int(m["active_till"])]
    future = [m for m in ms if int(m["active_since"]) > now]

    def _hosts(m):
        return ", ".join(h["name"] for h in m.get("hosts") or []) or "—"

    lines = ["🔧 <b>Обслуживания</b>", ""]
    kb = InlineKeyboardBuilder()
    if active:
        lines.append("<b>Активные:</b>")
        for m in sorted(active, key=lambda x: int(x["active_till"])):
            lines.append(f"🔧 {esc(m['name'])}")
            lines.append(f"      <i>{esc(_hosts(m))} · до {fmt_dt(m['active_till'])} "
                         f"(осталось {fmt_left(m['active_till'])})</i>")
            kb.button(text=f"⏹ Завершить: {m['name'][:32]}",
                      callback_data=f"mdel:{m['maintenanceid']}")
        lines.append("")
    else:
        lines.append("Активных обслуживаний нет.\n")
    if future:
        lines.append("<b>Запланированные:</b>")
        for m in sorted(future, key=lambda x: int(x["active_since"]))[:5]:
            lines.append(f"🕑 {esc(m['name'])} <i>({fmt_dt(m['active_since'])})</i>")
        lines.append("")
    if active:
        kb.adjust(1)
    kb.row(InlineKeyboardButton(text="➕ Поставить узел на обслуживание",
                                callback_data="hp:0:m"))
    return "\n".join(lines), kb.as_markup()


@router.message(Command("maintenance"))
async def cmd_maintenance(message: Message):
    text, kb = await render_maintenances()
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("mdel:"))
async def cb_mdel(cb: CallbackQuery):
    mid = cb.data.split(":")[1]
    try:
        await asyncio.to_thread(zbx.delete_maintenance, mid)
    except ZabbixAPIError as e:
        return await cb.answer(f"Ошибка: {e}"[:190], show_alert=True)
    await cb.answer("⏹ Обслуживание завершено")
    text, kb = await render_maintenances()
    await edit_or_answer(cb, text, kb)


@router.callback_query(F.data == "mnt")
async def cb_mnt(cb: CallbackQuery):
    await cb.answer()
    await edit_or_answer(cb, "🔧 Открыть список обслуживаний: /maintenance")


@router.callback_query(F.data == "prb")
async def cb_prb(cb: CallbackQuery):
    await cb.answer()
    text, kb = await render_problems()
    await edit_or_answer(cb, text, kb)


@router.callback_query(F.data.startswith("prb:"))
async def cb_prb_filtered(cb: CallbackQuery):
    """prb:<scope>:<min_severity>:<show_acked>:<hours>"""
    parts = cb.data.split(":")
    scope = parts[1]
    min_sev = int(parts[2]) if len(parts) > 2 else 0
    ack = bool(int(parts[3])) if len(parts) > 3 else True
    hours = int(parts[4]) if len(parts) > 4 else DEFAULT_PROBLEM_HOURS
    hostid = scope[1:] if scope.startswith("h") else None
    await cb.answer()
    text, kb = await render_problems(hostid=hostid, min_severity=min_sev,
                                     show_acked=ack, hours=hours)
    await edit_or_answer(cb, text, kb)


@router.callback_query(F.data.startswith("prb1:"))
async def cb_prb_one(cb: CallbackQuery):
    """prb1:<scope>:<min_severity>:<show_acked>:<hours>:<idx> — просмотр по одной."""
    _, scope, min_sev, ack, hours, idx = cb.data.split(":")
    hostid = scope[1:] if scope.startswith("h") else None
    await cb.answer()
    text, kb = await render_problem_one(hostid=hostid, min_severity=int(min_sev),
                                        show_acked=bool(int(ack)), hours=int(hours),
                                        idx=int(idx))
    await edit_or_answer(cb, text, kb)


# ------------------------------------------------------------------- навигация по списку узлов

@router.callback_query(F.data.startswith("hp:"))
async def cb_host_page(cb: CallbackQuery):
    """hp:<page>:<mode>[:<status_filter>]"""
    parts = cb.data.split(":")
    page, mode = int(parts[1]), parts[2]
    status = parts[3] if len(parts) > 3 else "active"
    await cb.answer()
    text, kb = await render_host_list(mode, page, status)
    await edit_or_answer(cb, text, kb)


@router.callback_query(F.data.startswith("hf:"))
async def cb_host_filter(cb: CallbackQuery):
    """hf:<status_filter>:<mode> — переключение активные/остановленные/все."""
    _, status, mode = cb.data.split(":")
    await cb.answer()
    text, kb = await render_host_list(mode, 0, status)
    await edit_or_answer(cb, text, kb)


class HostSearch(StatesGroup):
    query = State()


@router.callback_query(F.data.startswith("hse:"))
async def cb_host_search(cb: CallbackQuery, state: FSMContext):
    mode = cb.data.split(":")[1]
    await state.update_data(search_mode=mode)
    await state.set_state(HostSearch.query)
    await cb.answer()
    await cb.message.answer(
        "🔍 Введите <b>имя узла, IP или DNS</b> (часть слова) для поиска —\n"
        "поиск идёт по всем узлам, включая остановленные.\n\n"
        "<i>/cancel — отмена</i>")


@router.message(HostSearch.query)
async def host_search_run(message: Message, state: FSMContext):
    from aiogram.types import InlineKeyboardMarkup
    data = await state.get_data()
    mode = data.get("search_mode", "v")
    await state.clear()
    text, kb = await render_host_list(mode, 0, "all", search=message.text.strip())
    rows = list(kb.inline_keyboard)
    rows.append([InlineKeyboardButton(
        text="⬅️ К списку узлов", callback_data=f"hp:0:{mode}:active")])
    await message.answer(text + "\n\n<i>(поиск по всем узлам, включая остановленные)</i>",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("hv:"))
async def cb_host_view(cb: CallbackQuery):
    _, hostid, page = cb.data.split(":")
    await cb.answer()
    try:
        host = await asyncio.to_thread(zbx.get_host, hostid)
    except ZabbixAPIError as e:
        return await edit_or_answer(cb, zbx_error_text(e))
    if not host:
        return await edit_or_answer(cb, "Узел не найден (возможно, удалён).")
    text, kb = build_host_view(host, int(page))
    await edit_or_answer(cb, cut(text), kb)


@router.callback_query(F.data.startswith("hpr:"))
async def cb_host_problems(cb: CallbackQuery):
    hostid = cb.data.split(":")[1]
    await cb.answer()
    text, kb = await render_problems(hostid=hostid)
    await edit_or_answer(cb, text, kb)


@router.callback_query(F.data.startswith("hl:"))
async def cb_host_latest(cb: CallbackQuery):
    hostid = cb.data.split(":")[1]
    await cb.answer()
    text, kb = await render_latest(hostid)
    await edit_or_answer(cb, text, kb)


# ------------------------------------------------------------------- обслуживание узла

MAINT_OPTIONS = [(30, "30 минут"), (60, "1 час"), (120, "2 часа"),
                 (240, "4 часа"), (480, "8 часов"), (1440, "24 часа")]


@router.callback_query(F.data.startswith("hm:"))
async def cb_maint_pick(cb: CallbackQuery):
    hostid = cb.data.split(":")[1]
    await cb.answer()
    host = await asyncio.to_thread(zbx.get_host, hostid)
    name = host["name"] if host else str(hostid)
    kb = InlineKeyboardBuilder()
    for minutes, label in MAINT_OPTIONS:
        kb.button(text=label, callback_data=f"hmd:{hostid}:{minutes}")
    kb.button(text="⬅️ К узлу", callback_data=f"hv:{hostid}:0")
    kb.adjust(3, 3, 1)
    await edit_or_answer(cb,
                         f"🔧 <b>Обслуживание {esc(name)}</b>\n\n"
                         f"На сколько минут поставить узел? На время обслуживания "
                         f"проблемы приглушиваются.", kb.as_markup())


@router.callback_query(F.data.startswith("hmd:"))
async def cb_maint_do(cb: CallbackQuery):
    _, hostid, minutes = cb.data.split(":")
    await cb.answer("Создаю обслуживание…")
    host = await asyncio.to_thread(zbx.get_host, hostid)
    name = host["name"] if host else str(hostid)
    try:
        until = int(time.time()) + int(minutes) * 60
        await asyncio.to_thread(
            zbx.create_maintenance, hostid,
            f"TG: {name} ({fmt_dt(until)})", int(minutes))
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ К узлу", callback_data=f"hv:{hostid}:0")
        await edit_or_answer(
            cb,
            f"✅ Обслуживание <b>{esc(name)}</b> создано до "
            f"{fmt_dt(until)} (осталось {fmt_left(until)}).",
            kb.as_markup())
    except ZabbixAPIError as e:
        await edit_or_answer(cb, zbx_error_text(e))


# ------------------------------------------------------------------- графики

@router.callback_query(F.data.startswith("hg:"))
async def cb_graph_items(cb: CallbackQuery):
    hostid = cb.data.split(":")[1]
    await cb.answer()
    host, items = await asyncio.gather(
        asyncio.to_thread(zbx.get_host, hostid),
        asyncio.to_thread(zbx.get_items, hostid, 25),
    )
    name = host["name"] if host else str(hostid)
    if not items:
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ К узлу", callback_data=f"hv:{hostid}:0")
        return await edit_or_answer(cb, f"У узла {esc(name)} нет числовых метрик.",
                                    kb.as_markup())
    kb = InlineKeyboardBuilder()
    for it in items:
        kb.button(text=it["name"][:60],
                  callback_data=f"hgi:{it['itemid']}:{it['value_type']}")
    kb.button(text="⬅️ К узлу", callback_data=f"hv:{hostid}:0")
    kb.adjust(1)
    await edit_or_answer(cb, f"📈 <b>{esc(name)}</b> — выберите метрику:", kb.as_markup())


@router.callback_query(F.data.startswith("hgi:"))
async def cb_graph_period(cb: CallbackQuery):
    _, itemid, vtype = cb.data.split(":")
    await cb.answer()
    kb = InlineKeyboardBuilder()
    for hours, label in [(1, "1 час"), (3, "3 часа"), (24, "24 часа")]:
        kb.button(text=label, callback_data=f"hgp:{itemid}:{vtype}:{hours}")
    kb.button(text="🖥 Список узлов", callback_data="hp:0:g")
    kb.adjust(3, 1)
    await edit_or_answer(cb, "Выберите период:", kb.as_markup())


@router.callback_query(F.data.startswith("hgp:"))
async def cb_graph_draw(cb: CallbackQuery):
    _, itemid, vtype, hours = cb.data.split(":")
    await cb.answer("📈 Строю график…")
    item = await asyncio.to_thread(zbx.get_item, itemid)
    if not item:
        return await cb.message.answer("Метрика не найдена.")
    host_name = (item.get("hosts") or [{}])[0].get("name", "")
    points = await asyncio.to_thread(zbx.get_history, itemid, int(vtype),
                                     int(hours) * 3600)
    if not points:
        return await cb.message.answer(
            f"Нет данных по «{esc(item['name'])}» за последние {hours} ч.")
    path = os.path.join(tempfile.gettempdir(), f"zbx_graph_{itemid}.png")
    await asyncio.to_thread(draw_graph, points,
                            f"{item['name']} — {host_name} ({hours} ч)",
                            item.get("units", ""), path)
    last = points[-1]
    caption = (f"📈 <b>{esc(item['name'])}</b> · {esc(host_name)} · {hours} ч\n"
               f"Последнее: <b>{esc(fmt_val(last['value']))} {esc(item['units'])}</b>"
               f" ({fmt_age(last['clock'])} назад)")
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Обновить", callback_data=f"hgp:{itemid}:{vtype}:{hours}")
    kb.button(text="⬅️ Список узлов", callback_data="hp:0:g")
    kb.adjust(2)
    await cb.message.answer_photo(FSInputFile(path), caption=caption,
                                  reply_markup=kb.as_markup())
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------------- подтверждение проблем

async def _rerender_problems(msg: Message, scope: str, min_sev: int, ack: bool,
                             hours: int = DEFAULT_PROBLEM_HOURS):
    hostid = scope[1:] if scope.startswith("h") else None
    text, kb = await render_problems(hostid=hostid, min_severity=min_sev,
                                     show_acked=ack, hours=hours)
    try:
        await msg.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@router.callback_query(F.data.startswith("ackall:"))
async def cb_ack_all(cb: CallbackQuery):
    """ackall:<scope>:<min_severity>:<show_acked>:<hours> — подтвердить все."""
    parts = cb.data.split(":")
    scope = parts[1]
    min_sev = int(parts[2]) if len(parts) > 2 else 0
    ack = bool(int(parts[3])) if len(parts) > 3 else True
    hours = int(parts[4]) if len(parts) > 4 else DEFAULT_PROBLEM_HOURS
    hostid = scope[1:] if scope.startswith("h") else None
    problems, _ = await fetch_problems(hostid, min_sev, ack, hours)
    eventids = [p["eventid"] for p in problems if p["acknowledged"] != "1"]
    if not eventids:
        return await cb.answer("Нечего подтверждать", show_alert=True)
    try:
        await asyncio.to_thread(zbx.acknowledge, eventids)
    except ZabbixAPIError as e:
        return await cb.answer(f"Ошибка: {e}"[:190], show_alert=True)
    await cb.answer(f"✅ Подтверждено: {len(eventids)}")
    await _rerender_problems(cb.message, scope, min_sev, ack, hours)


@router.callback_query(F.data.startswith("ack1:"))
async def cb_ack_one(cb: CallbackQuery):
    """ack1:<eventid>:<scope>:<min_severity>:<show_acked>:<hours>:<idx>"""
    _, eventid, scope, min_sev, ack, hours, idx = cb.data.split(":")
    try:
        await asyncio.to_thread(zbx.acknowledge, [eventid])
    except ZabbixAPIError as e:
        return await cb.answer(f"Ошибка: {e}"[:190], show_alert=True)
    await cb.answer("✅ Подтверждено")
    hostid = scope[1:] if scope.startswith("h") else None
    text, kb = await render_problem_one(hostid=hostid, min_severity=int(min_sev),
                                        show_acked=bool(int(ack)), hours=int(hours),
                                        idx=int(idx))
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@router.callback_query(F.data.startswith("ack:"))
async def cb_ack(cb: CallbackQuery):
    """ack:<eventid>:<scope>:<min_severity>:<show_acked>:<hours>"""
    parts = cb.data.split(":")
    eventid = parts[1]
    scope = parts[2]
    min_sev = int(parts[3]) if len(parts) > 3 else 0
    ack = bool(int(parts[4])) if len(parts) > 4 else True
    hours = int(parts[5]) if len(parts) > 5 else DEFAULT_PROBLEM_HOURS
    try:
        await asyncio.to_thread(zbx.acknowledge, [eventid])
    except ZabbixAPIError as e:
        return await cb.answer(f"Ошибка: {e}"[:190], show_alert=True)
    await cb.answer("✅ Подтверждено")
    await _rerender_problems(cb.message, scope, min_sev, ack, hours)


# ------------------------------------------------------------------- удаление узла

@router.callback_query(F.data.startswith("hdelok:"))
async def cb_host_delete(cb: CallbackQuery):
    hostid = cb.data.split(":")[1]
    await cb.answer("Удаляю…")
    try:
        await asyncio.to_thread(zbx.delete_host, hostid)
        await edit_or_answer(cb, "🗑 Узел удалён.")
    except ZabbixAPIError as e:
        await edit_or_answer(cb, zbx_error_text(e))


@router.callback_query(F.data.startswith("hdel:"))
async def cb_host_delete_confirm(cb: CallbackQuery):
    hostid = cb.data.split(":")[1]
    await cb.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=f"hdelok:{hostid}")
    kb.button(text="❌ Отмена", callback_data=f"hv:{hostid}:0")
    kb.adjust(2)
    await edit_or_answer(
        cb,
        "⚠️ <b>Точно удалить узел?</b>\nИстория и проблемы узла будут удалены "
        "безвозвратно (данные — тоже, если работает housekeeper).",
        kb.as_markup())


# ------------------------------------------------------------------- управление сервером (/admin)

class AdminReboot(StatesGroup):
    ask = State()


_adm_last = {"rz": 0.0, "rb": 0.0}   # защита от даблклика (cooldown, сек)


def is_admin(uid: int | None) -> bool:
    return bool(config.admin_users) and uid in config.admin_users


def _gate_admin(cb_or_msg) -> bool:
    """Есть ли у отправителя доступ к /admin-действиям."""
    user = getattr(cb_or_msg, "from_user", None)
    return is_admin(getattr(user, "id", None))


def uptime_str() -> str:
    try:
        s = float(open("/proc/uptime").read().split()[0])
        d, rem = divmod(int(s), 86400)
        if d:
            return f"{d} д {rem // 3600} ч"
        return f"{rem // 3600} ч {rem % 3600 // 60} мин"
    except Exception:
        return "?"


def sudoers_hint() -> str:
    """Готовый текст для /etc/sudoers.d/zabbix-tg-bot под текущего пользователя."""
    user = getpass.getuser() or "botuser"
    svc = config.zabbix_service
    return (f"{user} ALL=(root) NOPASSWD: /usr/bin/systemctl restart {svc}, "
            f"/bin/systemctl restart {svc}, /sbin/reboot, /usr/sbin/reboot")


def run_root(variants: list[list[str]]) -> tuple[bool, str]:
    """Выполняет первую сработавшую команду из вариантов (sudo -n, без пароля).
    Возвращает (успех, вывод/текст ошибки)."""
    last = ""
    for cmd in variants:
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if cp.returncode == 0:
                return True, (cp.stdout or "").strip()
            last = (cp.stderr or cp.stdout or f"код выхода {cp.returncode}").strip()
        except FileNotFoundError:
            last = f"команда не найдена: {cmd[0]}"
        except subprocess.TimeoutExpired:
            last = "таймаут выполнения"
    return False, last


def restart_zabbix_cmds() -> list[list[str]]:
    svc = config.zabbix_service
    return [["sudo", "-n", p, "restart", svc]
            for p in ("/usr/bin/systemctl", "/bin/systemctl")]


def reboot_cmds() -> list[list[str]]:
    return [["sudo", "-n", p] for p in ("/sbin/reboot", "/usr/sbin/reboot")]


async def wait_zabbix_up(timeout: int = 90) -> tuple[str | None, int]:
    """Ждём, пока Zabbix API снова ответит. → (версия, секунд)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = await asyncio.to_thread(zbx.get_api_version)
            if v:
                return v, int(time.time() - t0)
        except Exception:
            pass
        await asyncio.sleep(3)
    return None, int(time.time() - t0)


def render_admin_menu() -> tuple[str, object]:
    host = os.uname().nodename
    try:
        load = " ".join(f"{x:.2f}" for x in os.getloadavg())
    except Exception:
        load = "?"
    svc = config.zabbix_service
    text = ("⚙️ <b>Управление сервером</b>\n\n"
            f"Хост: <b>{esc(host)}</b> <i>(машина, где запущен бот)</i>\n"
            f"Uptime: {uptime_str()} · load: {load}\n"
            f"Сервис Zabbix: <code>{esc(svc)}</code>\n\n"
            "<i>Действия выполняются на этом хосте через sudo</i>")
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🔁 Перезапустить {svc}", callback_data="adm:rz")
    kb.button(text="⏻ Перезагрузить сервер", callback_data="adm:rb")
    kb.button(text="⟳ Обновить", callback_data="adm:menu")
    kb.button(text="⬅️ Закрыть", callback_data="adm:close")
    kb.adjust(1, 1, 2)
    return text, kb.as_markup()


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not _gate_admin(message):
        return await message.answer(
            "⛔️ Раздел отключён или нет доступа.\n"
            "<i>Задайте свой Telegram ID в ADMIN_USERS (.env), "
            "чтобы включить управление сервером.</i>")
    text, kb = render_admin_menu()
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(cb: CallbackQuery, state: FSMContext):
    if not _gate_admin(cb):
        return await cb.answer("⛔️ Недоступно (нет в ADMIN_USERS)", show_alert=True)
    action = cb.data.split(":")[1]

    if action == "menu":
        await cb.answer()
        return await edit_or_answer(cb, *render_admin_menu())

    if action == "close":
        await cb.answer()
        try:
            await cb.message.delete()
        except Exception:
            pass
        return

    if action == "rz":  # подтверждение рестарта Zabbix
        await cb.answer()
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Да, перезапустить", callback_data="adm:rzok")
        kb.button(text="❌ Отмена", callback_data="adm:menu")
        kb.adjust(2)
        return await edit_or_answer(
            cb,
            f"⚠️ Перезапустить <b>{esc(config.zabbix_service)}</b>?\n\n"
            f"Сбор данных прервётся на ~30–60 сек, некоторые триггеры могут "
            f"сработать повторно. После рестарта бот сам проверит, что API "
            f"поднялся, и сообщит.", kb.as_markup())

    if action == "rzok":  # сам рестарт Zabbix + ожидание API
        if time.time() - _adm_last["rz"] < 90:
            return await cb.answer("Уже запускалось недавно — подождите",
                                   show_alert=True)
        _adm_last["rz"] = time.time()
        await cb.answer("Перезапускаю…")
        await edit_or_answer(cb, f"🔁 <b>Перезапускаю {esc(config.zabbix_service)}…</b>")
        ok, out = await asyncio.to_thread(run_root, restart_zabbix_cmds())
        if not ok:
            hint = ("sudo требует пароль или команда не разрешена.\n"
                    "Добавьте в <code>/etc/sudoers.d/zabbix-tg-bot</code> "
                    "(создать: <code>sudo visudo -f /etc/sudoers.d/zabbix-tg-bot</code>):\n"
                    f"<code>{esc(sudoers_hint())}</code>")
            return await cb.message.answer(
                f"❌ Не удалось перезапустить: <code>{esc(out[:200])}</code>\n\n{hint}")
        version, secs = await wait_zabbix_up()
        if version:
            await cb.message.answer(
                f"✅ <b>{esc(config.zabbix_service)}</b> перезапущен.\n"
                f"API отвечает (версия {esc(version)}) — поднялся за {secs} сек.")
        else:
            await cb.message.answer(
                f"⚠️ Команда выполнена, но API не ответил за 90 сек.\n"
                f"Проверьте вручную: <code>systemctl status "
                f"{esc(config.zabbix_service)}</code>")

    if action == "rb":  # перезагрузка сервера — шаг 1
        await cb.answer()
        kb = InlineKeyboardBuilder()
        kb.button(text="▶️ Продолжить", callback_data="adm:rbgo")
        kb.button(text="❌ Отмена", callback_data="adm:menu")
        kb.adjust(2)
        return await edit_or_answer(
            cb,
            f"⏻ <b>Перезагрузить сервер {esc(os.uname().nodename)}?</b>\n\n"
            f"• Бот остановится вместе с сервером и вернётся после загрузки "
            f"(обычно 1–3 мин, поднимет его systemd)\n"
            f"• Все процессы на сервере будут прерваны\n\n"
            f"Для подтверждения затем потребуется ввести слово "
            f"<code>перезагрузка</code>.", kb.as_markup())

    if action == "rbgo":  # шаг 2: ввод слова
        await state.set_state(AdminReboot.ask)
        await cb.answer()
        return await edit_or_answer(
            cb,
            "⏻ Для подтверждения перезагрузки отправьте сообщение со словом:\n"
            "<code>перезагрузка</code>\n\n"
            "<i>/cancel — отменить</i>")


@router.message(AdminReboot.ask)
async def admin_reboot_confirm(message: Message, state: FSMContext):
    word = (message.text or "").strip().lower()
    if word not in ("перезагрузка", "reboot"):
        return await message.answer(
            "❌ Не то слово. Введите <code>перезагрузка</code> или /cancel")
    await state.clear()
    if time.time() - _adm_last["rb"] < 120:
        return await message.answer("Перезагрузка уже запрашивалась недавно.")
    _adm_last["rb"] = time.time()
    host = os.uname().nodename
    try:
        await message.answer(
            f"⏻ <b>Перезагружаю сервер {esc(host)}…</b>\n"
            f"Бот вернётся автоматически через 1–3 мин (systemd).")
    except Exception:
        pass
    await asyncio.to_thread(run_root, reboot_cmds())


# ------------------------------------------------------------------- уведомления о проблемах

notify_state = {"enabled": config.notify_enabled,
                "min_severity": config.notify_min_severity}


def format_new_problems(fresh: list[dict]) -> str:
    lines = [f"🆕 <b>Упало ({len(fresh)})</b>", ""]
    for p in fresh:
        emoji, sev_name = SEVERITY.get(p["severity"], ("❔", "?"))
        lines.append(f"{emoji} <b>{esc(p['name'])}</b>")
        meta = [esc(p.get("hosts_str") or "—"), sev_name]
        if p.get("opdata"):
            meta.append(esc(p["opdata"]))
        lines.append(f"      <i>{' · '.join(meta)}</i>")
    return cut("\n".join(lines))


def format_resolved_problems(resolved: list[dict]) -> str:
    lines = [f"✅ <b>Восстановлено ({len(resolved)})</b>", ""]
    for p in resolved:
        lines.append(f"🟢 <b>{esc(p['name'])}</b>")
        lines.append(f"      <i>{esc(p.get('hosts_str') or '—')} · "
                     f"длилось {fmt_age(p['clock'])}</i>")
    return cut("\n".join(lines))


def diff_problems(known: dict, current: dict) -> tuple[list, list]:
    """(новые проблемы, исчезнувшие = восстановленные).
    Подавленные обслуживанием в «новые» не попадают."""
    fresh = [p for eid, p in current.items()
             if eid not in known and p.get("suppressed") != "1"]
    resolved = [p for eid, p in known.items() if eid not in current]
    return fresh, resolved


async def problem_notifier(bot: Bot):
    """Фоновый опрос: новые проблемы → «Упало», исчезнувшие → «Восстановлено»."""
    while True:
        try:
            problems = await asyncio.to_thread(
                zbx.get_problems, None, notify_state["min_severity"], 200)
            current = {p["eventid"]: p for p in problems}
            known = notify_state.get("known")
            if known is not None and notify_state["enabled"]:
                fresh, resolved = diff_problems(known, current)
                kb = None
                web = web_problems_url()
                if web:
                    from aiogram.types import InlineKeyboardMarkup
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="⚠️ Проблемы в Zabbix", url=web)]])
                if fresh:
                    log.info("Упало: %d — рассылаю", len(fresh))
                    text = format_new_problems(fresh[:10])
                    if len(fresh) > 10:
                        text += f"\n\n… и ещё {len(fresh) - 10}"
                    for uid in config.allowed_users:
                        try:
                            await bot.send_message(uid, text, reply_markup=kb)
                        except Exception as e:
                            log.warning("Уведомление для %s не доставлено: %s", uid, e)
                if resolved:
                    log.info("Восстановлено: %d — рассылаю", len(resolved))
                    text = format_resolved_problems(resolved[:10])
                    if len(resolved) > 10:
                        text += f"\n\n… и ещё {len(resolved) - 10}"
                    for uid in config.allowed_users:
                        try:
                            await bot.send_message(uid, text)
                        except Exception as e:
                            log.warning("Уведомление для %s не доставлено: %s", uid, e)
            notify_state["known"] = current
        except Exception as e:
            log.warning("problem_notifier: %s", e)
        await asyncio.sleep(config.notify_poll)


@router.message(Command("notify"))
async def cmd_notify(message: Message, command: Command):
    arg = (command.args or "").strip().lower()
    if arg in ("off", "выкл", "стоп"):
        notify_state["enabled"] = False
    elif arg in ("on", "вкл"):
        notify_state["enabled"] = True
        notify_state["known"] = None   # пересобрать базлайн после паузы
    elif arg.isdigit() and 0 <= int(arg) <= 5:
        notify_state["min_severity"] = int(arg)
        notify_state["known"] = None   # порог изменился — без ложных «упало/поднялось»
    state = "вкл ✅" if notify_state["enabled"] else "выкл ❌"
    emoji, sev_name = SEVERITY.get(str(notify_state["min_severity"]), ("⚪️", "все"))
    await message.answer(
        f"🔔 <b>Уведомления о новых проблемах</b>\n\n"
        f"Состояние: {state}\n"
        f"Порог важности: {emoji} {sev_name} и выше\n"
        f"Опрос Zabbix: каждые {config.notify_poll} с\n"
        f"Получатели: {', '.join(map(str, config.allowed_users)) or '—'}\n\n"
        "<i>/notify on · /notify off — включить/выключить\n"
        "/notify 3 — порог важности (0–5)\n"
        "изменения действуют до перезапуска; постоянные — в .env (NOTIFY_*)</i>")


# ------------------------------------------------------------------- ошибки

@dp.errors()
async def on_error(event: ErrorEvent):
    log.exception("Необработанная ошибка: %s", event.exception)
    try:
        upd = event.update
        msg = upd.message or (upd.callback_query.message if upd.callback_query else None)
        if msg:
            await msg.answer("❌ Произошла ошибка при обработке запроса. "
                             "Подробности — в логах бота.")
    except Exception:
        pass
    return True


# --------------------------------------------------------------------------- main

_notifier_task: asyncio.Task | None = None


@dp.startup()
async def _on_startup(bot: Bot):
    global _notifier_task
    _notifier_task = asyncio.create_task(problem_notifier(bot))
    log.info("Фоновый мониторинг проблем запущен (каждые %d с, важность ≥ %d)",
             config.notify_poll, notify_state["min_severity"])


@dp.shutdown()
async def _on_shutdown(bot: Bot):
    if _notifier_task:
        _notifier_task.cancel()


async def main():
    if not config.tg_token:
        raise SystemExit("TG_TOKEN не задан. Скопируйте .env.example → .env и заполните.")
    if not config.zabbix_url or not config.zabbix_user:
        raise SystemExit("ZABBIX_URL / ZABBIX_USER не заданы в .env")
    if not config.allowed_users:
        log.warning("ALLOWED_USERS пуст — бот будет отклонять ВСЕХ пользователей!")

    try:
        await asyncio.to_thread(zbx.login)
        log.info("Вход в Zabbix API выполнен: %s", config.zabbix_url)
    except Exception as e:
        log.warning("Вход в Zabbix при старте не удался (%s) — попробую ещё раз "
                    "при первом запросе.", e)

    session = None
    if config.tg_proxy:
        try:
            session = AiohttpSession(proxy=config.tg_proxy)
        except Exception as e:
            raise SystemExit(
                f"Не удалось создать сессию с прокси {config.tg_proxy}: {e}\n"
                "Проверьте формат TG_PROXY_URL в .env (http://… или socks5://…)."
            )
        log.info("Telegram: соединение через прокси %s", config.tg_proxy)
    bot = Bot(config.tg_token, session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp.include_router(router)
    dp.message.middleware(ACLMiddleware())
    dp.callback_query.middleware(ACLMiddleware())

    await bot.set_my_commands([
        BotCommand(command="status", description="📊 Статус Zabbix"),
        BotCommand(command="hosts", description="🖥 Узлы сети"),
        BotCommand(command="problems", description="⚠️ Проблемы по узлам"),
        BotCommand(command="addhost", description="➕ Добавить узел"),
        BotCommand(command="maintenance", description="🔧 Обслуживания"),
        BotCommand(command="graph", description="📈 График метрики"),
        BotCommand(command="latest", description="📊 Последние данные"),
        BotCommand(command="notify", description="🔔 Уведомления о проблемах"),
        BotCommand(command="admin", description="⚙️ Рестарт Zabbix / сервера"),
        BotCommand(command="cancel", description="❌ Отменить диалог"),
    ])
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен (long polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nБот остановлен.")
