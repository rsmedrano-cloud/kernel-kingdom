"""Archipiélago: cada máquina remota es una isla con su propio mundo.

Un hilo consulta las máquinas por SSH (reino.remote) cada `every` segundos; el
loop del daemon avanza el mundo de cada isla cada dos ticks con el último reporte
convertido al mismo formato que el muestreo local (aplicaciones pesadas y
contenedores grandes -> reinos, los medianos -> aldeas, el resto de los
contenedores y los servicios -> fortalezas).

Privacidad: la dirección usuario@máquina queda en la config y en este proceso.
Hacia afuera (estado, mapa, crónica, lo que se comparte) cada isla es un id
(hash) y un nombre inventado; los errores salen como un motivo corto.
"""
import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from .. import paths, remote
from .lore import Lore
from .world import World

ISLAND_SIZE = 48
STEP_EVERY = 2                          # ticks del daemon por paso de cada isla (son más lentas, y baratas)


valid_target = remote.valid_target


def island_id(target: str) -> str:
    return hashlib.sha256(("kernel-kingdom:" + target).encode()).hexdigest()[:10]


def island_name(cfg: dict, iid: str) -> str:
    return Lore(cfg, int(iid, 16) & 0x7FFFFFFF).region("isle")


def reason(err: str | None) -> str:
    """Motivo corto, sin el texto crudo de ssh (puede traer IPs o nombres)."""
    e = (err or "").lower()
    if "permission denied" in e or "publickey" in e:
        return "noaccess"
    if any(k in e for k in ("timeout", "timed out", "no route", "unreachable", "could not resolve", "refused",
                            "name or service")):
        return "unreachable"
    if "protocolo" in e or "json" in e or "objeto" in e or "grande" in e:
        return "protocol"
    if "servidor dice" in e:
        return "script"
    if "identity file" in e or "no such file" in e:
        return "nokey"
    return "error" if e else "waiting"


def to_snap(s: dict, dt: float) -> dict:
    """Reporte remoto (ya con tasas) -> el formato que espera World.step()."""
    t = s["t"]

    def entry(d, procs=1):
        started = d.get("started") or (s["boot"] or t)
        return {"procs": procs, "rss_mb": round(d.get("rss_mb") or 0.0, 1), "cpu": d.get("cpu", 0.0),
                "since": started, "uptime": max(0.0, t - started)}

    items = [(g["rss_mb"], g["name"], entry(g, g["procs"]), False) for g in s["groups"]]
    items += [(c["rss_mb"] or 0.0, c["name"], entry(c), True) for c in s["containers"] if c["state"] == "running"]
    items.sort(key=lambda it: -it[0])
    browsers, apps, fortresses = {}, {}, {}
    used = set()
    for rss, name, e, _ in items:
        if rss >= 250 and len(browsers) < 4 and name not in used:
            browsers[name] = e
            used.add(name)
    for rss, name, e, _ in items:
        if rss >= 40 and len(apps) < 6 and name not in used:
            apps[name] = e
            used.add(name)
    for rss, name, e, is_container in items:
        if is_container and name not in used and len(fortresses) < 8:
            fortresses[f"container:{name}"] = dict(e, kind="container", name=name)
            used.add(name)
    for sv in s["services"]:
        if sv["active"] and len(fortresses) < 12:
            since = sv["since"] or s["boot"] or t
            fortresses[f"daemon:{sv['name']}"] = {"kind": "daemon", "name": sv["name"], "procs": 1, "rss_mb": 0.0,
                                                  "cpu": 0.0, "since": since, "uptime": max(0.0, t - since)}
    share = dt / max(1.0, s["dt"] or 20.0)
    return {
        "t": t, "dt": dt, "cpu": s["cpu"], "ram": s["ram"],
        "ram_used_mb": round(s["ram_total_mb"] * s["ram"] / 100), "ram_total_mb": s["ram_total_mb"],
        "swap": s["swap"], "load1": s["load1"], "cores": s["cores"], "procs": s["procs"],
        "browsers": browsers, "fortresses": fortresses, "editors": {}, "compilers": {}, "apps": apps,
        "top": [{"name": g["name"], "cpu": g["cpu"]} for g in sorted(s["groups"], key=lambda g: -g["cpu"])[:3]
                if g["cpu"] > 5],
        "births": round(s["births"] * share), "deaths": 0, "cpu_temp": s["cpu_temp"],
        "boot": s["boot"], "kernel": s["kernel"] or "?",
        "sessions": [{"user": "visitante", "tty": f"s{k}", "host": "", "start": ""} for k in range(min(4, s["sessions"]))],
        "boots": [{"kernel": s["kernel"] or "?", "start": ""}], "visits": {},
        "net_rx": s["net_rx"], "net_tx": s["net_tx"], "disk_r": s["disk_r"], "disk_w": s["disk_w"],
        "input": {"active": 0.0, "keys": 0, "kpm": 0, "source": "remote"},
    }


def _offline_snap(prev: dict, t: float, dt: float) -> dict:
    """Máquina caída: sin procesos. Sus reinos se apagan y quedan ruinas."""
    snap = copy.deepcopy(prev)
    snap.update(t=t, dt=dt, cpu=0.0, browsers={}, apps={}, fortresses={}, top=[], births=0, procs=0, sessions=[])
    return snap


class Archipelago:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.fleet = remote.Fleet(self._remote_cfg(cfg))
        self.worlds: dict[str, World] = {}
        self.last_snap: dict[str, dict] = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.thread.start()

    @staticmethod
    def _remote_cfg(cfg: dict) -> dict:
        rc = dict(cfg.get("remote", {}))
        rc["hosts"] = [t for t in rc.get("hosts", []) if valid_target(t)]
        return {"remote": rc}

    # -- consultas (hilo aparte: ssh puede tardar) ------------------------------------

    def _poll_loop(self):
        while not self.stop.wait(1.0):
            with self.lock:
                fleet = self.fleet
            fleet.poll()

    def reload(self, cfg: dict):
        """La lista de máquinas cambió (Menú > Máquinas)."""
        self.cfg = cfg
        with self.lock:
            old = self.fleet
            new = remote.Fleet(self._remote_cfg(cfg))
            for t, isl in old.islands.items():          # las que siguen conservan su historial
                if t in new.islands:
                    new.islands[t] = isl
            self.fleet = new
        keep = {island_id(t) for t in new.islands}
        for iid in [i for i in self.worlds if i not in keep]:
            self.worlds.pop(iid)
            (self._state_dir() / f"{iid}.json").unlink(missing_ok=True)

    # -- mundos ----------------------------------------------------------------------

    @staticmethod
    def _save_dir() -> Path:
        d = paths.data_dir() / "islands"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _state_dir() -> Path:
        d = paths.state_file().parent / "islands"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _world(self, iid: str) -> World:
        w = self.worlds.get(iid)
        if w is None:
            cfg = copy.deepcopy(self.cfg)
            cfg["world"]["size"] = ISLAND_SIZE
            cfg["world"]["seed"] = int(iid, 16) & 0x7FFFFFFF
            cfg["world"]["kingdom"] = island_name(self.cfg, iid)   # nunca el hostname
            data = None
            f = self._save_dir() / f"{iid}.json"
            if f.exists():
                try:
                    data = json.loads(f.read_text())
                except (OSError, ValueError):
                    data = None
            w = World(cfg, data)
            self.worlds[iid] = w
        return w

    def summary(self) -> list[dict]:
        out = []
        with self.lock:
            islands = list(self.fleet.islands.items())
        for t, isl in islands:
            iid = island_id(t)
            out.append({"id": iid, "name": island_name(self.cfg, iid), "online": isl.online,
                        "reason": None if isl.online else reason(isl.error), "seen": bool(isl.snap)})
        return out

    def step(self, tick: int, interval: float, commands: dict[str, list], build_state, write_atomic):
        """Avanza las islas (cada STEP_EVERY ticks) y escribe su estado para la ventana."""
        if tick % STEP_EVERY:
            return
        dt = interval * STEP_EVERY
        summary = self.summary()
        with self.lock:
            islands = list(self.fleet.islands.items())
        for t, isl in islands:
            iid = island_id(t)
            if isl.snap is None and iid not in self.worlds and not (self._save_dir() / f"{iid}.json").exists():
                continue                                # nunca respondió: todavía no hay isla
            w = self._world(iid)
            now = time.time()
            if isl.online and isl.snap:
                snap = to_snap(isl.snap, dt)
                snap["t"] = now
                self.last_snap[iid] = snap
            elif iid in self.last_snap:
                snap = _offline_snap(self.last_snap[iid], now, dt)
            else:
                continue
            events = w.step(snap, commands.get(iid, []))
            w.chronicle.take_fresh()
            state = build_state(w, snap, events, dt)
            state["islands"], state["isle"] = summary, iid
            write_atomic(self._state_dir() / f"{iid}.json", state)

    def save(self, write_atomic):
        for iid, w in self.worlds.items():
            write_atomic(self._save_dir() / f"{iid}.json", w.to_dict())

    def close(self, write_atomic):
        self.stop.set()
        self.save(write_atomic)
        try:
            self.fleet.close()
        except (OSError, Exception):
            pass


def targets_ok(hosts) -> list[str]:
    return [t for t in hosts or [] if valid_target(t)]


def known_scrub_words(cfg: dict) -> set[str]:
    """Usuarios y máquinas de [remote] hosts: para limpiar lo que se comparte."""
    out = set()
    for t in targets_ok(cfg.get("remote", {}).get("hosts")):
        user, host = t.split("@", 1)
        out |= {user, host, host.split(".")[0]}
    return {w for w in out if len(w) >= 3}


def env_has_key(cfg: dict) -> bool:
    return os.path.exists(os.path.expanduser(cfg.get("remote", {}).get("key", "~/.ssh/kernel-kingdom")))
