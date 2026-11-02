"""Archipiélago: las máquinas del lab como islas de tu mismo mundo.

Un hilo consulta las máquinas por SSH (reino.remote) cada `every` segundos; en
cada tick su último reporte se convierte al formato del muestreo local
(aplicaciones pesadas y contenedores grandes -> reinos, los medianos -> aldeas,
el resto de los contenedores y los servicios -> fortalezas) y se suma con el id
de la isla como prefijo.

Privacidad: la dirección usuario@máquina queda en la config y en este proceso.
Hacia afuera (estado, mapa, crónica, lo que se comparte) cada isla es un id
(hash) y un nombre inventado; los errores salen como un motivo corto.
"""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .. import paths, remote
from .lore import Lore



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


def host_words(target: str, reported_host: str | None = None) -> set[str]:
    """Lo que identifica a una máquina: usuario, nombre (corto y completo) y el que
    ella misma reporta. No puede aparecer en ningún nombre del mapa."""
    user, host = target.split("@", 1)
    words = {user, host, host.split(".")[0]}
    if reported_host:
        words |= {reported_host, reported_host.split(".")[0]}
    return {w for w in words if len(w) >= 3}


def clean_name(name: str, words: set[str]) -> str:
    """'mihost-webui' -> 'webui' si 'mihost' es el nombre de la máquina."""
    out = name
    for w in sorted(words, key=len, reverse=True):
        out = re.sub(re.escape(w), "", out, flags=re.I)
    out = re.sub(r"[-_.\s]{2,}", "-", out)
    out = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", out)
    return out or "app"


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
    if not browsers and not apps and items:              # toda isla tiene al menos un reino: el más pesado
        rss, name, e, _ = items[0]
        browsers[name] = e
        used.add(name)
    for rss, name, e, is_container in items:                # los contenedores más pesados que quedan
        if is_container and name not in used and len(fortresses) < 4:
            fortresses[f"container:{name}"] = dict(e, kind="container", name=name)
            used.add(name)
    services = 0
    for sv in s["services"]:                                    # y unos pocos servicios (si no, todo es muralla)
        if sv["active"] and services < 3:
            services += 1
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


class Archipelago:
    """Las máquinas del lab dentro de tu mundo: cada una es una isla del mismo mapa.

    Un hilo las consulta por SSH; en cada tick `merge()` suma sus aplicaciones,
    contenedores y servicios al muestreo local con el id de la isla como prefijo
    ('<id>/postgres'), así sus reinos nacen y crecen en su isla y pueden guerrear
    con los de las otras (por mar). La primera vez que una máquina responde, su
    isla emerge del mar."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.fleet = remote.Fleet(self._remote_cfg(cfg))
        self.stop = threading.Event()
        self._cleanup_old()
        self.thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.thread.start()

    @staticmethod
    def _cleanup_old():
        """Las islas como mundos separados (0.4.0 de prueba) ya no se usan."""
        for d in (paths.data_dir() / "islands", paths.state_file().parent / "islands"):
            if d.is_dir():
                for f in d.glob("*.json"):
                    f.unlink(missing_ok=True)
                try:
                    d.rmdir()
                except OSError:
                    pass

    @staticmethod
    def _remote_cfg(cfg: dict) -> dict:
        rc = dict(cfg.get("remote", {}))
        rc["hosts"] = [t for t in rc.get("hosts", []) if valid_target(t)]
        return {"remote": rc}

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

    def _islands(self):
        with self.lock:
            return list(self.fleet.islands.items())

    def merge(self, snap: dict, world, interval: float) -> dict:
        """Suma las máquinas del lab al muestreo local. Las caídas no aportan nada:
        sus reinos se apagan y quedan ruinas hasta que vuelvan."""
        islands = self._islands()
        if not islands:
            return snap
        out = dict(snap, browsers=dict(snap["browsers"]), apps=dict(snap["apps"]),
                   fortresses=dict(snap["fortresses"]), isles={})
        for t, isl in islands:
            iid = island_id(t)
            if not (isl.online and isl.snap):
                out["isles"][iid] = {"online": False}
                continue
            if iid not in world.isles:
                world.add_isle(iid, island_name(self.cfg, iid))
            words = host_words(t, isl.snap.get("host"))
            self._scrub_world(world, iid, words)
            r = to_snap(isl.snap, interval)
            for table in ("browsers", "apps", "fortresses"):
                for key, g in r[table].items():
                    kind, _, name = key.rpartition(":")
                    clean = clean_name(name, words)        # nada del hostname ni del usuario en el mapa
                    key = f"{kind}:{clean}" if kind else clean
                    out[table][f"{iid}/{key}"] = dict(g, name=clean) if "name" in g else g
            out["isles"][iid] = {"online": True, "cpu": r["cpu"]}
        return out

    def _scrub_world(self, world, iid: str, words: set[str]):
        """Reinos y fortalezas que se fundaron con un nombre que traía el de la máquina
        (p.ej. un contenedor 'mihost-webui'): se renombran, y la crónica, el archivo
        de historias y el timelapse se corrigen. Se revisa una vez por isla y conjunto
        de palabras."""
        stamp = (iid, tuple(sorted(words)))
        if stamp in getattr(self, "_scrubbed", set()):
            return
        self.__dict__.setdefault("_scrubbed", set()).add(stamp)
        # en los textos, las palabras cortas solo enteras ('nas' no debe comerse 'semanas')
        pattern = re.compile("|".join(re.escape(w) if len(w) >= 5 else rf"\b{re.escape(w)}\b"
                                      for w in sorted(words, key=len, reverse=True)), re.I)
        renames: dict[str, str] = {}
        for key in [k for k, f in world.factions.items() if f.get("isle") == iid]:
            f = world.factions[key]
            proc = f.get("proc") or key.split("/", 1)[-1]
            if not pattern.search(proc) and not pattern.search(f["realm"]):
                continue
            clean = clean_name(proc, words)
            new_key = f"{iid}/{clean}"
            if new_key in world.factions and world.factions[new_key] is not f:
                new_key = f"{iid}/{clean}~{f['id']}"
            old_realm = f["realm"]
            f.update(name=new_key, proc=clean, realm=world.lore.realm(clean))
            world.factions[new_key] = world.factions.pop(key)
            renames[old_realm] = f["realm"]
        for key in [k for k in world.structures if k.startswith(iid + "/") and pattern.search(k)]:
            st = world.structures.pop(key)
            kind, _, name = key.split("/", 1)[1].rpartition(":")
            clean = clean_name(name, words)
            old_title = st.get("title", "")
            st.update(key=f"{iid}/{kind}:{clean}", name=clean, title=world.lore.structure_name(st["kind"], clean))
            world.structures[st["key"]] = st
            if old_title:
                renames[old_title] = st["title"]
        if not renames:
            return

        def fix(text: str) -> str:
            for old, new in renames.items():
                text = text.replace(old, new)
            return pattern.sub("…", text)
        for e in world.chronicle.entries:
            e["text"] = fix(e["text"])
        for d in (paths.data_dir() / "chronicle", paths.data_dir() / "timelapse", paths.data_dir() / "stories"):
            for f in d.glob("*") if d.is_dir() else []:
                try:
                    text = f.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                fixed = fix(text)
                if fixed != text:
                    f.write_text(fixed, encoding="utf-8")

    def summary(self, world) -> list[dict]:
        out = []
        for t, isl in self._islands():
            iid = island_id(t)
            info = world.isles.get(iid, {})
            out.append({"id": iid, "name": info.get("name") or island_name(self.cfg, iid), "online": isl.online,
                        "reason": None if isl.online else reason(isl.error), "c": info.get("c")})
        return out

    def close(self):
        self.stop.set()
        try:
            self.fleet.close()
        except Exception:
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
