"""Muestreo del sistema con psutil.

Una pasada por el árbol de procesos por tick. Solo pedimos atributos que salen
de /proc/<pid>/stat y /statm (baratos); nada de memory_full_info ni
open_files, que leen smaps y son caros. La cmdline solo se lee para los
procesos de contenedores, y se cachea.
"""
import os
import platform
import re
import subprocess
import time

import psutil

_ATTRS = ["pid", "ppid", "name", "memory_info", "cpu_times", "create_time"]
MB = 1024 * 1024


def _arg_after(cmdline: list[str], flags: tuple[str, ...]) -> str | None:
    for i, arg in enumerate(cmdline[:-1]):
        if arg in flags:
            return cmdline[i + 1]
    return None


class SystemProbe:
    def __init__(self, cfg: dict):
        self.browser_alias = {
            alias: faction
            for faction, aliases in cfg["browsers"].items()
            for alias in aliases
        }
        fort = cfg["fortresses"]
        self.databases = set(fort["databases"])
        self.daemons = set(fort["daemons"])
        self.editors = set(cfg["dev"]["editors"])
        self.compilers = set(cfg["dev"]["compilers"])
        apps = cfg["apps"]
        self.app_ignore = set(apps["ignore"])
        self.app_ignore_prefix = tuple(apps["ignore_prefix"])
        self.app_min_rss = apps["min_rss_mb"]
        self.app_max = apps["max"]
        self._prev_pids: set[int] = set()
        self._prev_io: dict[str, int] | None = None
        self._prev_cpu: dict[int, tuple[float, float]] = {}  # pid -> (create_time, segundos cpu)
        self._last_at = 0.0
        self._last = {"sessions": [], "boots": [], "visits": {}}
        self._prev_t: float | None = None
        self._container_names: dict[tuple[int, float], str | None] = {}
        psutil.cpu_percent(None)  # inicializa el contador interno

    # -- clasificación ------------------------------------------------------

    def _root_key(self, info: dict) -> tuple[str, str] | None:
        """¿Este proceso es raíz de una facción o contenedor?"""
        name = info["name"] or ""
        if name in self.browser_alias:
            return ("browser", self.browser_alias[name])
        if name == "conmon" or name.startswith("containerd-shim"):
            cname = self._container_name(info)
            if cname:
                return ("container", cname)
        return None

    def _container_name(self, info: dict) -> str | None:
        key = (info["pid"], info["create_time"])
        if key not in self._container_names:
            try:
                cmd = psutil.Process(info["pid"]).cmdline()
            except psutil.Error:
                cmd = []
            # Podman: conmon ... -n <nombre>. Docker: containerd-shim ... -id <id>
            name = _arg_after(cmd, ("-n", "--name"))
            if not name:
                cid = _arg_after(cmd, ("-id", "--id"))
                name = f"docker-{cid[:8]}" if cid else None
            self._container_names[key] = name
        return self._container_names[key]

    def _classify(self, procs: dict[int, dict]) -> dict[int, tuple[str, str] | None]:
        """Asigna a cada pid la clave de su ancestro más cercano que sea raíz."""
        memo: dict[int, tuple[str, str] | None] = {}
        for pid in procs:
            path, cur, result = [], pid, None
            while cur in procs and cur not in memo and len(path) < 64:
                path.append(cur)
                result = self._root_key(procs[cur])
                if result:
                    break
                cur = procs[cur]["ppid"]
            else:
                result = memo.get(cur)
            for p in path:
                memo[p] = result
        return memo

    def _is_app(self, name: str) -> bool:
        return bool(name) and name not in self.app_ignore and not name.startswith(self.app_ignore_prefix)

    @staticmethod
    def _io_counters() -> dict[str, int]:
        net = psutil.net_io_counters(pernic=True)
        disk = psutil.disk_io_counters()
        return {
            "net_rx": sum(v.bytes_recv for k, v in net.items() if k != "lo"),
            "net_tx": sum(v.bytes_sent for k, v in net.items() if k != "lo"),
            "disk_r": disk.read_bytes if disk else 0,
            "disk_w": disk.write_bytes if disk else 0,
        }

    @staticmethod
    def _cpu_temp() -> float | None:
        try:
            temps = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        for chip in ("k10temp", "coretemp", "zenpower", "cpu_thermal"):
            if temps.get(chip):
                return max(t.current for t in temps[chip])
        return None

    def logins(self) -> dict:
        """Sesiones abiertas, reinicios y visitas pasadas según `last` (wtmp/wtmpdb).
        psutil.users() no sirve con wtmpdb; `last` sí. Se consulta 1 vez por minuto."""
        now = time.monotonic()
        if now - self._last_at < 60:
            return self._last
        self._last_at = now
        try:
            out = subprocess.run(["last", "-n", "400", "-w", "--time-format", "iso"],
                                 capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return self._last
        sessions, boots, visits = [], [], {}
        iso = re.compile(r"\d{4}-\d\d-\d\dT")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[0] == "wtmp":
                continue
            if parts[0] == "reboot":
                boots.append({"kernel": parts[3].rstrip("*"), "start": parts[4] if len(parts) > 4 else ""})
                continue
            user, tty = parts[0], parts[1]
            host, rest = ("", parts[2:]) if iso.match(parts[2]) else (parts[2], parts[3:])
            visits[host or "local"] = visits.get(host or "local", 0) + 1
            if "still logged in" in line:
                sessions.append({"user": user, "tty": tty, "host": host, "start": rest[0]})
        self._last = {"sessions": sessions, "boots": boots[:40], "visits": visits}
        return self._last

    # -- muestreo -----------------------------------------------------------

    def sample(self) -> dict:
        now = time.monotonic()
        wall = time.time()
        dt = (now - self._prev_t) if self._prev_t else None
        self._prev_t = now

        procs: dict[int, dict] = {}
        for p in psutil.process_iter(_ATTRS, ad_value=None):
            procs[p.info["pid"]] = p.info

        # CPU por proceso = delta de tiempo de CPU entre ticks (% de un núcleo)
        pcpu: dict[int, float] = {}
        cur_cpu: dict[int, tuple[float, float]] = {}
        for pid, info in procs.items():
            ct = info["cpu_times"]
            if ct is None:
                continue
            total = ct.user + ct.system
            cur_cpu[pid] = (info["create_time"], total)
            prev = self._prev_cpu.get(pid)
            if dt and prev and prev[0] == info["create_time"]:
                pcpu[pid] = max(0.0, (total - prev[1]) / dt * 100)
        self._prev_cpu = cur_cpu
        self._container_names = {
            k: v for k, v in self._container_names.items() if k[0] in procs
        }

        browsers: dict[str, dict] = {}
        fortresses: dict[str, dict] = {}
        editors: dict[str, int] = {}
        compilers: dict[str, dict] = {}
        apps: dict[str, dict] = {}

        def group(table, key, info, **extra):
            g = table.setdefault(key, {"procs": 0, "rss_mb": 0.0, "cpu": 0.0, "started": wall, **extra})
            g["procs"] += 1
            if info["memory_info"]:
                g["rss_mb"] += info["memory_info"].rss / MB
            g["cpu"] += pcpu.get(info["pid"], 0.0)
            if info["create_time"]:
                g["started"] = min(g["started"], info["create_time"])

        owners = self._classify(procs)
        for pid, info in procs.items():
            name = info["name"] or ""
            owner = owners.get(pid)
            if owner and owner[0] == "browser":
                group(browsers, owner[1], info)
            elif owner and owner[0] == "container":
                group(fortresses, f"container:{owner[1]}", info, kind="container", name=owner[1])

            # Servicios y herramientas: coincidencia directa por nombre
            if name in self.databases:
                group(fortresses, f"db:{name}", info, kind="db", name=name)
            elif name in self.daemons:
                group(fortresses, f"daemon:{name}", info, kind="daemon", name=name)
            elif name in self.editors:
                parent = procs.get(info["ppid"])
                if not parent or parent["name"] != name:  # contar instancias, no hijos
                    editors[name] = editors.get(name, 0) + 1
            elif name in self.compilers:
                group(compilers, name, info)
            elif owner is None and self._is_app(name):
                group(apps, name, info)

        # 60% del mínimo: el mundo usa la diferencia como histéresis (no fundar/abandonar en bucle)
        apps = dict(sorted(((n, g) for n, g in apps.items() if g["rss_mb"] >= 0.6 * self.app_min_rss),
                           key=lambda kv: -kv[1]["rss_mb"])[:self.app_max + 3])
        for table in (browsers, fortresses, compilers, apps):
            for g in table.values():
                g["since"] = round(g["started"], 1)   # create_time del proceso más viejo
                g["uptime"] = round(wall - g.pop("started"), 1)
                g["rss_mb"] = round(g["rss_mb"], 1)
                g["cpu"] = round(g["cpu"], 1)

        top = sorted(
            ((pcpu[pid], procs[pid]["name"]) for pid in pcpu if pcpu[pid] > 5),
            reverse=True,
        )[:3]
        pids = set(procs)
        births = len(pids - self._prev_pids) if self._prev_pids else 0
        deaths = len(self._prev_pids - pids) if self._prev_pids else 0
        self._prev_pids = pids
        io = self._io_counters()
        rates = {k: 0.0 for k in io}
        if self._prev_io and dt:
            rates = {k: round(max(0, io[k] - self._prev_io[k]) / dt / MB, 2) for k in io}
        self._prev_io = io

        vm = psutil.virtual_memory()
        return {
            "t": wall,
            "dt": dt or 0.0,
            "cpu": psutil.cpu_percent(None),
            "ram": vm.percent,
            "ram_used_mb": round((vm.total - vm.available) / MB),
            "ram_total_mb": round(vm.total / MB),
            "swap": psutil.swap_memory().percent,
            "load1": round(os.getloadavg()[0], 2),
            "cores": psutil.cpu_count() or 1,
            "procs": len(procs),
            "browsers": browsers,
            "fortresses": fortresses,
            "editors": editors,
            "compilers": compilers,
            "top": [{"name": n, "cpu": round(c, 1)} for c, n in top],
            "apps": apps,
            "births": births,
            "deaths": deaths,
            "cpu_temp": self._cpu_temp(),
            "boot": round(psutil.boot_time()),
            "kernel": platform.release(),
            **self.logins(),               # sessions, boots, visits
            **rates,                       # net_rx, net_tx, disk_r, disk_w en MB/s
        }
