#!/usr/bin/env python3
"""MCP "pi-tools": herramientas acotadas del servidor doméstico para nanobot.

Corre como proceso hijo de nanobot (stdio) fuera del sandbox del shell, así el LLM
nunca ve las credenciales. Casi todo es solo lectura; la única acción que cambia algo
es pausar el bloqueo de Pi-hole (máximo 10 minutos) y reanudarlo.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

PIHOLE = os.environ.get("PIHOLE_URL", "http://127.0.0.1:8089").rstrip("/") + "/api"
PASSWORD = os.environ.get("PIHOLE_PASSWORD", "")
# El proceso corre sin bus de systemd ni sudo (NoNewPrivileges), así que el estado de
# los servicios se deduce mirando /proc: nombre de servicio -> texto que debe aparecer.
SERVICES = {
    "pihole-FTL": "pihole-FTL",
    "unbound": "unbound",
    "tailscaled": "tailscaled",
    "syncthing": "syncthing",
    "dashboard": "/root/dashboard.py",
    "nanobot": "nanobot serve",
    "minidlna": "minidlnad",
}
MAX_PAUSE_MIN = 10

mcp = FastMCP("pi-tools")
_session = {"sid": "", "at": 0.0}


def _auth() -> None:
    body = json.dumps({"password": PASSWORD}).encode()
    req = urllib.request.Request(f"{PIHOLE}/auth", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as r:
        _session["sid"] = json.loads(r.read())["session"].get("sid") or ""
        _session["at"] = time.time()


def _api(method: str, path: str, payload: dict | None = None) -> dict:
    """Llamada a la API de Pi-hole; renueva la sesión si expiró."""
    for attempt in (1, 2):
        if not _session["sid"] or time.time() - _session["at"] > 240:
            _auth()
        headers = {"Content-Type": "application/json"}
        if _session["sid"]:
            headers["X-FTL-SID"] = _session["sid"]
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f"{PIHOLE}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                body = r.read()
                _session["at"] = time.time()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1:
                _session["sid"] = ""
                continue
            raise
    return {}


def _run(*cmd: str, timeout: int = 4) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def _running_services() -> dict:
    cmdlines = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdlines.append(f.read().replace(b"\0", b" ").decode(errors="ignore"))
            except OSError:
                continue
    return {name: ("activo" if any(pat in c for c in cmdlines) else "no encontrado")
            for name, pat in SERVICES.items()}


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(n)))


@mcp.tool()
def pi_status() -> dict:
    """Estado del Raspberry Pi: temperatura, RAM, disco, carga, uptime y servicios."""
    temp = None
    try:
        temp = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    except Exception:  # noqa: BLE001
        pass
    mem = {}
    for line in open("/proc/meminfo"):
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = int(v.split()[0]) // 1024
    disks = {}
    for name, path in (("sd", "/"), ("ssd", "/mnt/ssd")):
        try:
            u = shutil.disk_usage(path)
            disks[name] = {"usado_gb": round(u.used / 1e9, 1), "total_gb": round(u.total / 1e9, 1),
                           "pct": round(100 * u.used / u.total)}
        except Exception:  # noqa: BLE001
            disks[name] = "no disponible"
    up = float(open("/proc/uptime").read().split()[0])
    return {
        "temperatura_c": temp,
        "ram_mb": {"total": mem.get("MemTotal"), "disponible": mem.get("MemAvailable")},
        "disco": disks,
        "carga_1_5_15": [round(x, 2) for x in os.getloadavg()],
        "uptime_horas": round(up / 3600, 1),
        "servicios": _running_services(),
    }


@mcp.tool()
def net_check() -> dict:
    """Diagnóstico de red: gateway, internet y DNS local (Pi-hole)."""
    gw = ""
    for line in _run("ip", "route", "show", "default").splitlines():
        parts = line.split()
        if "via" in parts:
            gw = parts[parts.index("via") + 1]
            break

    def ping(ip: str) -> str:
        out = _run("ping", "-c", "1", "-W", "2", ip, timeout=4)
        for token in out.split():
            if token.startswith("time="):
                return f"ok ({token[5:]} ms)"
        return "sin respuesta"

    dns = _run("dig", "+short", "+time=2", "+tries=1", "google.com", "@127.0.0.1")
    return {
        "gateway": {"ip": gw, "ping": ping(gw) if gw else "sin gateway"},
        "internet": {"1.1.1.1": ping("1.1.1.1"), "8.8.8.8": ping("8.8.8.8")},
        "dns_local": "ok" if dns else "no resuelve",
    }


@mcp.tool()
def pihole_summary() -> dict:
    """Resumen de Pi-hole: consultas, bloqueadas, porcentaje, dominios en listas y estado del bloqueo."""
    s = _api("GET", "/stats/summary")
    q = s.get("queries", {})
    blocking = _api("GET", "/dns/blocking")
    return {
        "consultas_total": q.get("total"),
        "bloqueadas": q.get("blocked"),
        "porcentaje_bloqueado": round(q.get("percent_blocked", 0), 1),
        "dominios_unicos": q.get("unique_domains"),
        "clientes_activos": s.get("clients", {}).get("active"),
        "dominios_en_listas": s.get("gravity", {}).get("domains_being_blocked"),
        "bloqueo": blocking.get("blocking"),
        "pausa_restante_s": blocking.get("timer"),
    }


@mcp.tool()
def pihole_top_clients(count: int = 10) -> list:
    """Dispositivos con más consultas DNS (nombre, ip, consultas)."""
    d = _api("GET", f"/stats/top_clients?count={_clamp(count, 1, 25)}")
    return [{"nombre": c.get("name") or c.get("ip"), "ip": c.get("ip"), "consultas": c.get("count")}
            for c in d.get("clients", [])]


@mcp.tool()
def pihole_top_blocked(count: int = 10) -> list:
    """Dominios más bloqueados por Pi-hole."""
    d = _api("GET", f"/stats/top_domains?blocked=true&count={_clamp(count, 1, 25)}")
    return [{"dominio": x.get("domain"), "bloqueos": x.get("count")} for x in d.get("domains", [])]


@mcp.tool()
def pihole_devices() -> list:
    """Dispositivos con lease DHCP en la LAN (nombre, ip, mac, cuándo vence)."""
    d = _api("GET", "/dhcp/leases")
    out = []
    for lease in d.get("leases", []):
        out.append({"nombre": lease.get("name") or "(sin nombre)", "ip": lease.get("ip"),
                    "mac": lease.get("hwaddr"),
                    "vence": time.strftime("%Y-%m-%d %H:%M", time.localtime(lease.get("expires", 0)))})
    return sorted(out, key=lambda x: tuple(int(p) for p in (x["ip"] or "0.0.0.0").split(".")))


@mcp.tool()
def pihole_recent_queries(client_ip: str = "", count: int = 20) -> list:
    """Últimas consultas DNS, opcionalmente de un dispositivo (por IP). Máximo 50."""
    path = f"/queries?length={_clamp(count, 1, 50)}"
    if client_ip:
        if not all(c.isdigit() or c == "." for c in client_ip):
            return [{"error": "client_ip inválida"}]
        path += f"&client_ip={client_ip}"
    d = _api("GET", path)
    return [{"hora": time.strftime("%H:%M:%S", time.localtime(q.get("time", 0))),
             "dominio": q.get("domain"), "estado": q.get("status"),
             "cliente": (q.get("client") or {}).get("name") or (q.get("client") or {}).get("ip")}
            for q in d.get("queries", [])]


@mcp.tool()
def pihole_pause(minutes: int = 5) -> dict:
    """Pausa el bloqueo de anuncios de Pi-hole por N minutos (1 a 10) y lo reanuda solo.
    Úsalo solo si el usuario lo pide explícitamente (p. ej. una web no abre por el bloqueo)."""
    minutes = _clamp(minutes, 1, MAX_PAUSE_MIN)
    r = _api("POST", "/dns/blocking", {"blocking": False, "timer": minutes * 60})
    return {"bloqueo": r.get("blocking"), "se_reanuda_en_min": minutes}


@mcp.tool()
def pihole_resume() -> dict:
    """Reanuda el bloqueo de Pi-hole ahora mismo."""
    r = _api("POST", "/dns/blocking", {"blocking": True})
    return {"bloqueo": r.get("blocking")}


if __name__ == "__main__":
    mcp.run()
