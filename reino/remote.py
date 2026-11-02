"""Máquinas remotas (archipiélago): consulta por SSH y normalización.

Protocolo en docs/remote-protocol.md. La PC no manda código: abre SSH con una
llave propia (en el servidor, `command=` fijo en authorized_keys) y lee el JSON
que imprime ese script. Se usa el ssh del sistema con ControlMaster, así cada
consulta reutiliza la conexión abierta en vez de hacer un handshake nuevo.
"""
import json
import os
import subprocess
import time
from pathlib import Path

from . import paths

PROTOCOL = 1
MAX_BYTES = 64 * 1024
OFFLINE_AFTER = 3                      # fallas seguidas para dar la isla por caída


class RemoteError(Exception):
    pass


def control_dir() -> Path:
    d = paths.state_file().parent / "ssh"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def ssh_command(target: str, key: str, ssh: str = "ssh") -> list[str]:
    """Comando de ssh para una consulta. No lleva comando remoto: el servidor
    ejecuta su script fijo pase lo que pase."""
    return [ssh, "-T",
            "-i", os.path.expanduser(key),
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",                 # nunca preguntar nada: si no hay acceso, falla
            "-o", "ConnectTimeout=6",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={control_dir()}/%C",   # %C: hash corto (las rutas de socket tienen tope)
            "-o", "ControlPersist=10m",
            "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
            "-o", "ClearAllForwardings=yes",
            target]


def fetch(target: str, key: str, timeout: float = 10.0, ssh: str = "ssh") -> dict:
    """Una consulta. Devuelve el reporte validado o levanta RemoteError."""
    try:
        r = subprocess.run(ssh_command(target, key, ssh), stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError("timeout")
    except OSError as e:
        raise RemoteError(f"ssh: {e}")
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RemoteError(msg[-1] if msg else f"ssh salió con {r.returncode}")
    if len(r.stdout) > MAX_BYTES:
        raise RemoteError("reporte demasiado grande")
    try:
        data = json.loads(r.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RemoteError("el reporte no es JSON")
    return validate(data)


# -- validación ---------------------------------------------------------------------

def _num(v, default=None, lo=0.0):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return max(lo, float(v))


def _str(v, limit=48):
    return v[:limit] if isinstance(v, str) and v.strip() else None


def validate(d: dict) -> dict:
    """Se queda solo con lo que define el protocolo, con tipos y topes sanos.
    Lo que viene mal se descarta en vez de romper el mundo."""
    if not isinstance(d, dict):
        raise RemoteError("el reporte no es un objeto")
    if d.get("v") != PROTOCOL:
        raise RemoteError(f"versión de protocolo {d.get('v')!r}, se esperaba {PROTOCOL}")
    if d.get("error"):
        raise RemoteError(f"el servidor dice: {_str(d['error'], 120)}")
    cpu, mem = d.get("cpu") or {}, d.get("mem") or {}
    net, disk, procs = d.get("net") or {}, d.get("disk") or {}, d.get("procs") or {}
    out = {
        "host": _str(d.get("host")) or "?",
        "t": _num(d.get("t"), time.time()),
        "boot": _num(d.get("boot")),
        "kernel": _str(d.get("kernel"), 64),
        "arch": _str(d.get("arch"), 16),
        "ticks_total": _num(cpu.get("ticks_total")), "ticks_idle": _num(cpu.get("ticks_idle")),
        "cores": int(_num(cpu.get("cores"), 1, 1)), "load1": _num(cpu.get("load1"), 0.0),
        "temp_c": _num(cpu.get("temp_c"), None, -50),
        "total_mb": _num(mem.get("total_mb"), 0.0), "available_mb": _num(mem.get("available_mb"), 0.0),
        "swap_total_mb": _num(mem.get("swap_total_mb"), 0.0), "swap_free_mb": _num(mem.get("swap_free_mb"), 0.0),
        "rx_bytes": _num(net.get("rx_bytes")), "tx_bytes": _num(net.get("tx_bytes")),
        "read_bytes": _num(disk.get("read_bytes")), "write_bytes": _num(disk.get("write_bytes")),
        "procs": int(_num(procs.get("count"), 0)), "forks": _num(procs.get("forks")),
        "sessions": int(_num(d.get("sessions"), 0)),
        "groups": [], "containers": [], "services": [],
    }
    for g in (d.get("groups") or [])[:25]:
        if isinstance(g, dict) and _str(g.get("name")):
            out["groups"].append({"name": _str(g["name"]), "procs": int(_num(g.get("procs"), 1, 1)),
                                  "rss_mb": _num(g.get("rss_mb"), 0.0), "cpu_s": _num(g.get("cpu_s"), 0.0),
                                  "started": _num(g.get("started"))})
    for c in (d.get("containers") or [])[:40]:
        if isinstance(c, dict) and _str(c.get("name")):
            out["containers"].append({"name": _str(c["name"]), "runtime": _str(c.get("runtime"), 16) or "?",
                                      "state": _str(c.get("state"), 16) or "?", "started": _num(c.get("started")),
                                      "rss_mb": _num(c.get("rss_mb")), "cpu_s": _num(c.get("cpu_s")),
                                      "image": _str(c.get("image"))})
    for s in (d.get("services") or [])[:20]:
        if isinstance(s, dict) and _str(s.get("name")):
            out["services"].append({"name": _str(s["name"]), "active": s.get("active") is True,
                                    "since": _num(s.get("since"))})
    return out


# -- una isla: tasas a partir de dos reportes ------------------------------------------

class Island:
    """Estado de una máquina remota entre consultas: guarda el reporte anterior para
    calcular CPU %, MB/s de red y disco, nacimientos y CPU por grupo."""

    def __init__(self, target: str):
        self.target = target
        self.prev: dict | None = None
        self.snap: dict | None = None
        self.fails = 0
        self.error: str | None = None
        self.last_ok = 0.0

    @property
    def online(self) -> bool:
        return self.snap is not None and self.fails < OFFLINE_AFTER

    def failed(self, err: str):
        self.fails += 1
        self.error = err

    def update(self, rep: dict):
        prev, self.prev = self.prev, rep
        self.fails, self.error, self.last_ok = 0, None, time.time()
        dt = rep["t"] - prev["t"] if prev else 0.0
        rebooted = bool(prev and prev["boot"] and rep["boot"] and abs(rep["boot"] - prev["boot"]) > 5)
        ok = prev is not None and dt > 0 and not rebooted

        def rate(key, scale=1.0):
            if not ok or rep[key] is None or prev[key] is None:
                return 0.0
            return max(0.0, rep[key] - prev[key]) / dt / scale

        cpu = 0.0
        if ok and None not in (rep["ticks_total"], rep["ticks_idle"], prev["ticks_total"], prev["ticks_idle"]):
            busy = (rep["ticks_total"] - prev["ticks_total"]) - (rep["ticks_idle"] - prev["ticks_idle"])
            total = rep["ticks_total"] - prev["ticks_total"]
            cpu = max(0.0, min(100.0, 100.0 * busy / total)) if total > 0 else 0.0
        mb = 1024 * 1024
        prev_cpu = {g["name"]: g["cpu_s"] for g in prev["groups"]} if ok else {}
        prev_ccpu = {c["name"]: c["cpu_s"] for c in prev["containers"]} if ok else {}
        total_mb = rep["total_mb"] or 1.0
        self.snap = {
            "host": rep["host"], "t": rep["t"], "dt": dt if ok else 0.0, "boot": rep["boot"],
            "kernel": rep["kernel"], "arch": rep["arch"], "rebooted": rebooted,
            "cpu": round(cpu, 1), "cores": rep["cores"], "load1": rep["load1"], "cpu_temp": rep["temp_c"],
            "ram": round(100.0 * (1 - rep["available_mb"] / total_mb), 1),
            "ram_total_mb": round(total_mb),
            "swap": round(100.0 * (1 - rep["swap_free_mb"] / rep["swap_total_mb"]), 1) if rep["swap_total_mb"] else 0.0,
            "net_rx": round(rate("rx_bytes", mb), 2), "net_tx": round(rate("tx_bytes", mb), 2),
            "disk_r": round(rate("read_bytes", mb), 2), "disk_w": round(rate("write_bytes", mb), 2),
            "procs": rep["procs"], "births": round(rate("forks") * dt) if ok else 0,
            "sessions": rep["sessions"],
            "groups": [dict(g, cpu=round(max(0.0, g["cpu_s"] - prev_cpu[g["name"]]) / dt * 100, 1)
                            if g["name"] in prev_cpu else 0.0) for g in rep["groups"]],
            "containers": [dict(c, cpu=round(max(0.0, (c["cpu_s"] or 0) - (prev_ccpu.get(c["name"]) or 0)) / dt * 100, 1)
                                if ok and c["cpu_s"] is not None and prev_ccpu.get(c["name"]) is not None else 0.0)
                           for c in rep["containers"]],
            "services": rep["services"],
        }


class Fleet:
    """Todas las máquinas de [remote] hosts, consultadas cada `every` segundos."""

    def __init__(self, cfg: dict, ssh: str = "ssh"):
        rc = cfg.get("remote", {})
        self.key = rc.get("key", "~/.ssh/kernel-kingdom")
        self.every = max(5, rc.get("every", 20))
        self.ssh = ssh
        self.islands = {t: Island(t) for t in rc.get("hosts", []) if isinstance(t, str) and t}
        self.next_at = 0.0

    def poll(self, now: float | None = None, force: bool = False) -> bool:
        """Consulta todas si ya toca. Devuelve True si consultó."""
        now = time.monotonic() if now is None else now
        if not self.islands or (not force and now < self.next_at):
            return False
        self.next_at = now + self.every
        for isl in self.islands.values():
            try:
                isl.update(fetch(isl.target, self.key, ssh=self.ssh))
            except RemoteError as e:
                isl.failed(str(e))
        return True

    def close(self):
        """Cierra las conexiones maestras de ssh (al apagar el daemon)."""
        for isl in self.islands.values():
            cmd = ssh_command(isl.target, self.key, self.ssh)
            subprocess.run(cmd[:-1] + ["-O", "exit", isl.target], capture_output=True, timeout=5)
