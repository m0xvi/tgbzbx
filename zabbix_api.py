"""
Тонкий клиент Zabbix HTTP API (JSON-RPC). Совместим с Zabbix 5.4+ —
проверено на 6.0 LTS и 7.2/7.4.

Аутентификация:
  * вход — user.login (username/password), возвращает токен сессии;
  * токен передаётся в заголовке Authorization: Bearer <token> —
    работает во всех поддерживаемых версиях (передача "auth" в теле
    запроса удалена начиная с Zabbix 7.2);
  * при истечении сессии клиент сам перелогинивается и повторяет запрос.
"""
from __future__ import annotations

import time

import requests


class ZabbixAPIError(Exception):
    """Ошибка, возвращённая Zabbix API."""


class ZabbixAPI:
    def __init__(self, url: str, username: str, password: str,
                 verify_ssl: bool = True, timeout: int = 30):
        self.api_url = url.rstrip("/") + "/api_jsonrpc.php"
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.token: str | None = None
        self._id = 0
        self._http = requests.Session()
        self._http.headers.update({
            "Content-Type": "application/json-rpc",
            "User-Agent": "zabbix-tg-bot/1.0",
        })

    # ------------------------------------------------------------------ низкий уровень

    def login(self) -> str:
        result = self._call(
            "user.login",
            {"username": self.username, "password": self.password},
            with_auth=False,
        )
        self.token = result
        return self.token

    def _call(self, method: str, params: dict, with_auth: bool = True,
              allow_relogin: bool = True):
        if with_auth and not self.token:
            self.login()

        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}
        headers = {}
        if with_auth:
            headers["Authorization"] = f"Bearer {self.token}"

        resp = self._http.post(self.api_url, json=payload, headers=headers,
                               timeout=self.timeout, verify=self.verify_ssl)

        if resp.status_code == 412:
            raise ZabbixAPIError(
                "Zabbix API вернул HTTP 412 — доступ к API запрещён для этого "
                "пользователя. Проверьте роль пользователя (User role → API access)."
            )
        try:
            data = resp.json()
        except ValueError:
            raise ZabbixAPIError(
                f"Некорректный ответ Zabbix API: HTTP {resp.status_code}, "
                f"тело: {resp.text[:200]}"
            )

        if "error" in data:
            err = data["error"]
            msg = err.get("data") or err.get("message") or "неизвестная ошибка"
            # сессия истекла — перелогиниваемся один раз и повторяем запрос
            if with_auth and allow_relogin and (
                resp.status_code == 401
                or any(s in msg for s in ("Not authorized", "re-login",
                                          "Session terminated",
                                          "session was not found"))
            ):
                self.login()
                return self._call(method, params, with_auth=True, allow_relogin=False)
            raise ZabbixAPIError(f"{method}: {msg}")

        return data.get("result")

    # ------------------------------------------------------------------ узлы

    def get_hosts(self, limit: int = 500) -> list[dict]:
        return self._call("host.get", {
            "output": ["hostid", "host", "name", "status", "maintenance_status"],
            "selectInterfaces": ["ip", "dns", "port", "available", "type", "useip"],
            "selectGroups": ["groupid", "name"],
            "sortfield": "name",
            "limit": limit,
        })

    def get_host(self, hostid: str | int) -> dict | None:
        res = self._call("host.get", {
            "output": ["hostid", "host", "name", "status", "maintenance_status",
                       "description"],
            "selectInterfaces": ["interfaceid", "ip", "dns", "port", "available",
                                 "type", "useip", "main", "error"],
            "selectGroups": ["groupid", "name"],
            "selectParentTemplates": ["templateid", "name"],
            "selectTags": ["tag", "value"],
            "hostids": [str(hostid)],
        })
        return res[0] if res else None

    def create_host(self, host: str, ip: str, groupid: str | int,
                    templateids: list | None = None,
                    port: str = "10050", visible_name: str | None = None) -> dict:
        params: dict = {
            "host": host,
            "groups": [{"groupid": str(groupid)}],
            "interfaces": [{
                "type": 1,        # Zabbix agent
                "main": 1,
                "useip": 1,
                "ip": ip,
                "dns": "",
                "port": str(port),
            }],
        }
        if visible_name:
            params["name"] = visible_name
        if templateids:
            params["templates"] = [{"templateid": str(t)} for t in templateids]
        return self._call("host.create", params)

    def delete_host(self, hostid: str | int) -> dict:
        return self._call("host.delete", [str(hostid)])

    def update_host(self, hostid: str | int, fields: dict) -> dict:
        """Изменение узла: host.update — name, status, description,
        interfaces (полный объект с interfaceid), templates (полный список)…"""
        return self._call("host.update", {"hostid": str(hostid), **fields})

    # ------------------------------------------------------------------ группы и шаблоны

    def get_host_groups(self, search: str | None = None, limit: int = 25) -> list[dict]:
        params: dict = {"output": ["groupid", "name"], "sortfield": "name",
                        "limit": limit}
        if search:
            params["search"] = {"name": search}
        return self._call("hostgroup.get", params)

    def get_templates(self, search: str | None = None, limit: int = 25) -> list[dict]:
        params: dict = {"output": ["templateid", "name"], "sortfield": "name",
                        "limit": limit}
        if search:
            params["search"] = {"name": search}
        return self._call("template.get", params)

    # ------------------------------------------------------------------ проблемы

    def get_problems(self, hostids: list | None = None,
                     min_severity: int = 0, limit: int = 25) -> list[dict]:
        """Активные проблемы (+ имена узлов через trigger.get).

        В problem.get (6.0) нет selectHosts, поэтому имена узлов достаём
        отдельным запросом trigger.get по objectid (triggerid) проблем.
        """
        params: dict = {
            "output": ["eventid", "objectid", "name", "severity", "clock",
                       "acknowledged", "suppressed", "opdata"],
            "recent": False,
            "sortfield": ["eventid"],
            "sortorder": "DESC",
            "limit": 200,
        }
        if min_severity > 0:
            params["severities"] = list(range(min_severity, 6))
        if hostids:
            params["hostids"] = [str(h) for h in hostids]

        problems = self._call("problem.get", params)
        if not problems:
            return []

        trigger_ids = list({p["objectid"] for p in problems})
        triggers = self._call("trigger.get", {
            "output": ["triggerid"],
            "selectHosts": ["hostid", "name"],
            "triggerids": trigger_ids,
        })
        hosts_map = {t["triggerid"]: t["hosts"] for t in triggers}
        for p in problems:
            p["hosts_list"] = hosts_map.get(p["objectid"], [])
            p["hosts_str"] = ", ".join(h["name"] for h in p["hosts_list"])

        # сначала критичные, затем свежие
        problems.sort(key=lambda p: (int(p["severity"]), int(p["clock"])),
                      reverse=True)
        return problems[:limit]

    def acknowledge(self, eventids: list, message: str = "Подтверждено из Telegram-бота") -> dict:
        return self._call("event.acknowledge", {
            "eventids": [str(e) for e in eventids],
            "action": 1,  # acknowledge
            "message": message,
        })

    # ------------------------------------------------------------------ обслуживание

    def get_maintenances(self, limit: int = 100) -> list[dict]:
        return self._call("maintenance.get", {
            "output": ["maintenanceid", "name", "active_since", "active_till",
                       "maintenance_type"],
            "selectHosts": ["hostid", "name"],
            "limit": limit,
        })

    def create_maintenance(self, hostid: str | int, name: str,
                           minutes: int) -> dict:
        """Разовое обслуживание узла начиная с текущего момента."""
        now = int(time.time())
        return self._call("maintenance.create", {
            "name": name,
            "active_since": now,
            "active_till": now + minutes * 60,
            "maintenance_type": 0,  # 0 — с сбором данных, 1 — без
            "hosts": [{"hostid": str(hostid)}],
            "timeperiods": [{
                "timeperiod_type": 0,      # one time
                "period": minutes * 60,
                "start_date": now,
            }],
        })

    def delete_maintenance(self, maintenanceid: str | int) -> dict:
        return self._call("maintenance.delete", [str(maintenanceid)])

    # ------------------------------------------------------------------ метрики и история

    def get_items(self, hostid: str | int, limit: int = 30) -> list[dict]:
        """Числовые элементы данных узла (value_type 0 — float, 3 — unsigned)."""
        return self._call("item.get", {
            "output": ["itemid", "name", "lastvalue", "lastclock", "units",
                       "value_type"],
            "hostids": [str(hostid)],
            "filter": {"value_type": [0, 3]},
            "sort": "name",
            "limit": limit,
        })

    def get_item(self, itemid: str | int) -> dict | None:
        res = self._call("item.get", {
            "output": ["itemid", "name", "units", "value_type", "lastvalue"],
            "itemids": [str(itemid)],
            "selectHosts": ["hostid", "name"],
        })
        return res[0] if res else None

    def get_history(self, itemid: str | int, value_type: int, seconds_back: int,
                    limit: int = 5000) -> list[dict]:
        return self._call("history.get", {
            "output": "extend",
            "itemids": [str(itemid)],
            "history": int(value_type),
            "time_from": int(time.time()) - seconds_back,
            "sortfield": "clock",
            "sortorder": "ASC",
            "limit": limit,
        })

    # ------------------------------------------------------------------ прочее

    def get_api_version(self) -> str:
        """Версия Zabbix (apiinfo.version)."""
        return self._call("apiinfo.version", {}, with_auth=False)
