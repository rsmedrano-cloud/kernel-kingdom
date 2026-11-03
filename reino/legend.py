"""El legendarium: la memoria del mundo entre un día y otro.

Cada capítulo de los Anales deja un resumen de su día (qué guerras se movieron y
cómo, quién nació y murió, quién subió al trono, qué cayó y qué renació, si tu isla
durmió, si alguna isla del lab empezó una edad nueva). La historia completa se arma
juntando esos resúmenes: cuántos días lleva una guerra, cuántas guerras hubo entre
los mismos reinos, cuántos monarcas tuvo una casa, cuántas veces cayó un reino.
Con eso el relato de cada día tiene hilo: no empieza de cero.
"""
import json
from collections import Counter
from pathlib import Path

from . import paths


def _file() -> Path:
    d = paths.data_dir() / "annals"
    d.mkdir(parents=True, exist_ok=True)
    return d / "legend.json"


def load() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"days": {}}


def save(legend: dict):
    _file().write_text(json.dumps(legend, ensure_ascii=False), encoding="utf-8")


def _p(e: dict) -> dict:
    return e.get("p") or {}


def day_summary(entries: list[dict]) -> dict:
    """Lo que el día aporta a la historia del mundo (solo nombres y números)."""
    wars: dict[str, dict] = {}

    def war(name, a=None, b=None):
        w = wars.setdefault(name, {"a": a, "b": b, "declared": False, "ended": False, "winner": None,
                                   "battles": 0, "first": None})
        w["a"] = w["a"] or a
        w["b"] = w["b"] or b
        return w

    out = {"wars": wars, "born": [], "died": [], "rulers": [], "falls": [], "reborn": [], "founded": [],
           "sacked": [], "ages": [], "slept": None}
    for e in entries:
        k, p = e.get("k"), _p(e)
        if k in ("war.declare", "war.start") and p.get("war"):
            w = war(p["war"], p.get("a"), p.get("b"))
            w["declared"] = True
            w["first"] = w["first"] or e.get("t")
        elif k == "battle" and p.get("war"):
            w = war(p["war"], p.get("a"), p.get("b"))
            w["battles"] += 1
            w["first"] = w["first"] or e.get("t")
        elif k in ("peace", "power.tregua") and p.get("war"):
            w = war(p["war"], p.get("a"), p.get("b"))
            w["ended"] = True
            w["winner"] = p.get("winner") or w["winner"]
        elif k in ("hero.born", "power.heroe") and p.get("hero"):
            out["born"].append([p["hero"], p.get("realm"), p.get("job")])
        elif k in ("hero.died", "hero.fell") and p.get("hero"):
            out["died"].append([p["hero"], p.get("realm"), p.get("war")])
        elif k == "dyn.succession" and p.get("realm"):
            out["rulers"].append([p["realm"], p.get("ruler"), p.get("old")])
        elif k == "fall" and p.get("realm"):
            out["falls"].append(p["realm"])
        elif k == "dyn.reborn" and p.get("realm"):
            out["reborn"].append(p["realm"])
        elif k in ("found.realm", "found.app") and p.get("realm"):
            out["founded"].append(p["realm"])
        elif k == "capital.sacked" and p.get("b"):
            out["sacked"].append([p["b"], p.get("a")])
        elif k == "isle.age" and p.get("isle"):
            out["ages"].append([p["isle"], p.get("n")])
        elif k == "home.slept":
            out["slept"] = p.get("n")
    return out


def record(day: str, entries: list[dict]) -> dict:
    """Guarda (o actualiza) el resumen del día y devuelve el legendarium."""
    legend = load()
    legend.setdefault("days", {})[day] = day_summary(entries)
    save(legend)
    return legend


def history(legend: dict, until: str) -> dict:
    """La historia hasta `until` (inclusive): guerras con sus días y batallas, rivalidades
    entre reinos, linajes, caídas y renacimientos, héroes."""
    wars: dict[str, dict] = {}
    feuds: Counter = Counter()
    falls: Counter = Counter()
    rulers: Counter = Counter()
    heroes: dict[str, dict] = {}
    for day in sorted(d for d in legend.get("days", {}) if d <= until):
        s = legend["days"][day]
        for name, w in s.get("wars", {}).items():
            h = wars.setdefault(name, {"a": w.get("a"), "b": w.get("b"), "start": day, "days": [], "battles": 0,
                                       "ended": None, "winner": None})
            if day not in h["days"]:
                h["days"].append(day)
            h["battles"] += w.get("battles", 0)
            if w.get("declared") and h["start"] == day and w.get("a") and w.get("b"):
                feuds[tuple(sorted((w["a"], w["b"])))] += 1
            if w.get("ended"):
                h["ended"], h["winner"] = day, w.get("winner") or h["winner"]
        for realm in s.get("falls", []):
            falls[realm] += 1
        for realm, *_ in s.get("rulers", []):
            rulers[realm] += 1
        for hero, realm, job in s.get("born", []):
            heroes.setdefault(hero, {"realm": realm, "job": job, "born": day, "died": None})
        for hero, realm, war in s.get("died", []):
            heroes.setdefault(hero, {"realm": realm, "job": None, "born": None, "died": None})["died"] = day
    return {"wars": wars, "feuds": feuds, "falls": falls, "rulers": rulers, "heroes": heroes}
