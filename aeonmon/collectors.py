"""Colectores de datos para Aeon (solo biblioteca estándar).

Fuentes:
  * /var/log/zypp/history        -> qué paquetes cambiaron y cuándo
  * snapper --jsonout list       -> snapshots (cada transactional-update crea uno)
  * zypper --xmlout list-updates -> actualizaciones pendientes
  * os-release, uptime, systemd  -> estado general
"""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import socket
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ZYPP_HISTORY = os.environ.get("AEONMON_HISTORY", "/var/log/zypp/history")
SCHEMA_VERSION = 1


# --------------------------------------------------------------- utilidades
def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: comando no encontrado"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timeout"


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def _parse_local(s: str) -> datetime | None:
    """Fechas 'YYYY-MM-DD HH:MM:SS' en hora local -> datetime con zona."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%a %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).astimezone()
        except ValueError:
            continue
    return None


def rpmvercmp(a: str, b: str) -> int:
    """Comparación de versiones aproximada a rpm (suficiente para up/down)."""
    if a == b:
        return 0
    ta = re.findall(r"\d+|[A-Za-z]+", a)
    tb = re.findall(r"\d+|[A-Za-z]+", b)
    for x, y in zip(ta, tb):
        xd, yd = x.isdigit(), y.isdigit()
        if xd and yd:
            if int(x) != int(y):
                return 1 if int(x) > int(y) else -1
        elif xd != yd:
            return 1 if xd else -1
        elif x != y:
            return 1 if x > y else -1
    return (len(ta) > len(tb)) - (len(ta) < len(tb))


# ------------------------------------------------------- historial de zypper
def _classify(cmdline: str) -> str:
    try:
        words = shlex.split(cmdline)
    except ValueError:
        words = cmdline.split()
    verbs = {
        "dup": "dup", "dist-upgrade": "dup", "up": "update", "update": "update",
        "patch": "patch", "in": "install", "install": "install",
        "rm": "remove", "remove": "remove",
    }
    for w in words[1:]:
        if w in verbs:
            return verbs[w]
    return "other"


def _summarize(entries: list[dict]) -> list[dict]:
    pk: dict[tuple, dict] = {}
    for e in entries:
        d = pk.setdefault((e["name"], e["arch"]), {"in": [], "rm": [], "repo": ""})
        if e["action"] == "install":
            d["in"].append(e["version"])
            d["repo"] = e.get("repo", "")
        else:
            d["rm"].append(e["version"])
    changes = []
    for (name, arch), d in sorted(pk.items()):
        c = {"name": name, "arch": arch, "from": None, "to": None, "repo": d["repo"]}
        if d["in"] and d["rm"]:
            c["from"], c["to"] = d["rm"][0], d["in"][-1]
            cmp = rpmvercmp(c["to"], c["from"])
            c["action"] = "upgraded" if cmp > 0 else "downgraded" if cmp < 0 else "reinstalled"
        elif d["in"]:
            c["to"], c["action"] = d["in"][-1], "installed"
        else:
            c["from"], c["action"] = d["rm"][0], "removed"
        changes.append(c)
    return changes


def parse_history(path: str | None = None) -> list[dict]:
    """Devuelve transacciones (más recientes primero)."""
    path = path or ZYPP_HISTORY
    txs: list[dict] = []
    cur: dict | None = None

    def close():
        nonlocal cur
        if cur and cur["_entries"]:
            cur["changes"] = _summarize(cur.pop("_entries"))
            cur["counts"] = {}
            for c in cur["changes"]:
                cur["counts"][c["action"]] = cur["counts"].get(c["action"], 0) + 1
            cur["total"] = len(cur["changes"])
            txs.append(cur)
        cur = None

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            f = line.split("|")
            if len(f) < 3:
                continue
            when, action = f[0], f[1].strip()  # zypper rellena: "remove |"
            if action == "command":
                close()
                dt = _parse_local(when)
                cmd = f[3] if len(f) > 3 else ""
                cur = {"time": _iso(dt) if dt else when, "epoch": int(dt.timestamp()) if dt else 0,
                       "user": f[2], "command": cmd, "kind": _classify(cmd), "_entries": []}
            elif action in ("install", "remove") and len(f) >= 5:
                if cur is None:  # entradas sin línea 'command'
                    dt = _parse_local(when)
                    cur = {"time": _iso(dt) if dt else when, "epoch": int(dt.timestamp()) if dt else 0,
                           "user": "?", "command": "", "kind": "other", "_entries": []}
                cur["_entries"].append({"action": action, "name": f[2], "version": f[3], "arch": f[4],
                                        "repo": f[5] if len(f) > 5 and action == "install" else ""})
    close()
    txs.sort(key=lambda t: t["epoch"], reverse=True)
    return txs


def collect_updates(limit: int = 20, since: str | None = None, history: str | None = None) -> dict:
    try:
        txs = parse_history(history)
    except OSError as e:
        return {"error": str(e), "transactions": []}
    if since:
        dt = _parse_local(since + " 00:00:00" if len(since) == 10 else since)
        if dt:
            txs = [t for t in txs if t["epoch"] >= dt.timestamp()]
    return {"transactions": txs[:limit], "total_transactions": len(txs)}


# --------------------------------------------------------------- snapshots
def parse_snapper(raw: str) -> list[dict]:
    data = json.loads(raw)
    if isinstance(data, dict):  # {"root": [...]}
        data = data.get("root") or next(iter(data.values()), [])
    out = []
    for s in data:
        def g(*keys, default=None):
            for k in keys:
                if k in s and s[k] not in ("", None):
                    return s[k]
            return default
        dt = _parse_local(str(g("date", default="")))
        def flag(k):  # snapper: true/false en JSON, "+"/"-" en columnas de texto
            return g(k) is True or g(k) in ("+", "yes", "true")
        out.append({
            "number": int(g("number", default=0)),
            "type": g("type", default=""),
            "pre_number": g("pre-number", "pre_number"),
            "time": _iso(dt) if dt else None,
            "epoch": int(dt.timestamp()) if dt else 0,
            "description": g("description", default=""),
            "cleanup": g("cleanup", default=""),
            "userdata": g("userdata", default={}) or {},
            "default": bool(flag("default")),   # el que arrancará en el próximo boot
            "active": bool(flag("active")),     # el que está montado ahora
        })
    return sorted(out, key=lambda x: x["number"], reverse=True)


def collect_snapshots(limit: int = 20) -> dict:
    rc, out, err = _run(["snapper", "--jsonout", "list"])
    if rc != 0:
        return {"error": (err or out).strip() or f"snapper rc={rc}", "snapshots": []}
    try:
        snaps = parse_snapper(out)
    except (ValueError, StopIteration) as e:
        return {"error": f"salida de snapper no válida: {e}", "snapshots": []}
    return {"snapshots": snaps[:limit], "total": len(snaps)}


# ------------------------------------------------------ actualizaciones pendientes
def parse_zypper_updates(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for u in root.iter("update"):
        items.append({"name": u.get("name"), "arch": u.get("arch"), "from": u.get("edition-old"),
                      "to": u.get("edition"), "kind": u.get("kind")})
    return items


def collect_pending() -> dict:
    rc, out, err = _run(["zypper", "--non-interactive", "--xmlout", "list-updates"], timeout=120)
    if rc not in (0, 100, 101):
        return {"error": (err or out).strip()[:300] or f"zypper rc={rc}", "available": None, "items": []}
    try:
        items = parse_zypper_updates(out)
    except ET.ParseError as e:
        return {"error": f"XML inválido: {e}", "available": None, "items": []}
    return {"available": len(items), "items": items,
            "note": "'zypper lu' lista solo actualizaciones sencillas; en Aeon el método real es 'transactional-update dup'."}


# -------------------------------------------------------------------- estado
def _os_release() -> dict:
    d = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip().split("=", 1)
                    d[k] = v.strip('"')
    except OSError:
        pass
    return d


def _boot_time() -> datetime | None:
    try:
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
        return datetime.fromtimestamp(datetime.now().timestamp() - up).astimezone()
    except (OSError, ValueError):
        return None


def _timer(unit: str) -> dict:
    rc, out, _ = _run(["systemctl", "show", unit, "-p", "ActiveState,NextElapseUSecRealtime,LastTriggerUSec"])
    if rc != 0:
        return {}
    kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    return {"active": kv.get("ActiveState"), "next": kv.get("NextElapseUSecRealtime") or None,
            "last": kv.get("LastTriggerUSec") or None}


def collect_status(history: str | None = None) -> dict:
    osr = _os_release()
    boot = _boot_time()
    st = {
        "hostname": socket.gethostname(),
        "os": osr.get("PRETTY_NAME", platform.platform()),
        "os_version": osr.get("VERSION_ID"),  # en Tumbleweed/Aeon = fecha del snapshot de repos
        "kernel": platform.release(),
        "booted_at": _iso(boot) if boot else None,
        "timer": _timer("transactional-update.timer"),
    }
    snaps = collect_snapshots(limit=1000)
    active = next((s for s in snaps["snapshots"] if s["active"]), None)
    default = next((s for s in snaps["snapshots"] if s["default"]), None)
    st["running_snapshot"] = active["number"] if active else None
    st["next_boot_snapshot"] = default["number"] if default else None
    st["reboot_pending"] = bool(active and default and active["number"] != default["number"]) \
        or os.path.exists("/run/reboot-needed")
    if "error" in snaps:
        st["snapper_error"] = snaps["error"]
    ups = collect_updates(limit=1, history=history)
    last = ups["transactions"][0] if ups["transactions"] else None
    st["last_update"] = ({"time": last["time"], "kind": last["kind"], "packages": last["total"]}
                         if last else None)
    last_dup = next((t for t in parse_history(history) if t["kind"] == "dup"), None) if last else None
    st["last_dup"] = {"time": last_dup["time"], "packages": last_dup["total"]} if last_dup else None
    return st


# ------------------------------------------------------------------ fachada
ENDPOINTS = {
    "status": lambda p: collect_status(),
    "updates": lambda p: collect_updates(int(p.get("limit", 20)), p.get("since")),
    "snapshots": lambda p: collect_snapshots(int(p.get("limit", 20))),
    "pending": lambda p: collect_pending(),
}


def respond(endpoint: str, params: dict | None = None) -> dict:
    """Respuesta con envoltorio común; la usan CLI y servidor por igual."""
    params = params or {}
    if endpoint == "all":
        data = {k: ENDPOINTS[k](params) for k in ("status", "updates", "snapshots")}
    elif endpoint in ENDPOINTS:
        data = ENDPOINTS[endpoint](params)
    else:
        raise KeyError(endpoint)
    return {"schema": SCHEMA_VERSION, "generated_at": _iso(datetime.now(timezone.utc)),
            "endpoint": endpoint, "data": data}
