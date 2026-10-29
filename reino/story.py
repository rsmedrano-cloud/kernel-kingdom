"""La historia del día: la crónica archivada, contada como un relato.

El daemon archiva cada entrada en data_dir()/chronicle/AAAA-MM-DD.jsonl (sin el
límite de 300 de la crónica en memoria). De ahí se arma un texto con título,
resumen de lo que pasó, partes del día, personajes y cierre. Sale en Markdown
y en texto plano (pensado para pasarlo por un lector de voz).
"""
import datetime as dt
import json
import time
from collections import Counter
from pathlib import Path

from . import paths
from .i18n import raw, tr, words

# Qué plantillas cuentan para el resumen
_COUNT = {
    "war": ("war.start", "war.declare"), "battle": ("battle",), "peace": ("peace",),
    "found": ("found.realm", "found.app"), "fall": ("fall",), "dyn": ("dyn.succession", "dyn.reborn"),
    "hero": ("hero.born", "power.heroe"), "dead": ("hero.died", "hero.fell", "hero.exile"),
    "storm": ("storm.start",), "capital": ("capital.moved",), "visit": ("visit.arrive", "visit.local"),
    "power": ("power.rayo", "power.lluvia", "power.bendicion", "power.bosque", "power.faro", "power.fiesta"),
}


# Los hechos grandes del día (para un relato corto o para leer en voz alta)
_HIGHLIGHTS = {"war.declare", "war.start", "peace", "capital.sacked", "capital.moved", "fall", "dyn.succession",
               "dyn.reborn", "found.realm", "storm.start", "eruption.start", "monument", "exp.build.done",
               "visit.arrive", "era.new", "era.kernel", "exp.siege.held", "exp.siege.breach"}


def archive_file(day: str) -> Path:
    return paths.data_dir() / "chronicle" / f"{day}.jsonl"


def archive(entries: list[dict]):
    """Agrega entradas al archivo del día de cada una."""
    by_day: dict[str, list[dict]] = {}
    for e in entries:
        by_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(e["t"])), []).append(e)
    for day, items in by_day.items():
        f = archive_file(day)
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as out:
            for e in items:
                out.write(json.dumps(e, ensure_ascii=False) + "\n")


def backfill(entries: list[dict]):
    """Completa el archivo de hoy con entradas de la crónica guardada que son
    anteriores a lo ya archivado (p.ej. de antes de actualizar o reiniciar)."""
    day = time.strftime("%Y-%m-%d")
    have = load(day)
    first = min((e["t"] for e in have), default=float("inf"))
    seen = {(e["t"], e["text"]) for e in have}
    older = [e for e in entries if time.strftime("%Y-%m-%d", time.localtime(e["t"])) == day
             and e["t"] < first and (e["t"], e["text"]) not in seen]
    if not older:
        return 0
    f = archive_file(day)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in older + have), encoding="utf-8")
    return len(older)


def load(day: str) -> list[dict]:
    f = archive_file(day)
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def build(day: str, entries: list[dict], kingdom: str, era: str = "", markdown: bool = True) -> str:
    date = dt.date.fromisoformat(day).strftime("%d/%m/%Y")
    if not entries:
        return tr("story.empty", date=date)
    keys = Counter(e.get("k", "") for e in entries)
    counts = raw("story.count")
    parts = [counts[name][0 if n == 1 else 1].format(n=n) for name, ks in _COUNT.items()
             if (n := sum(keys[k] for k in ks))]
    title = tr("story.title", kingdom=kingdom, date=date)
    out = [f"# {title}" if markdown else title, ""]
    if era:
        out += [tr("story.era", era=era), ""]
    out += [tr("story.summary", events=len(entries), parts=", ".join(parts) if parts else tr("story.none")), ""]

    big = [e["text"] for e in entries if e.get("k") in _HIGHLIGHTS][:14]
    if big:
        out += [f"## {tr('story.highlights')}" if markdown else tr("story.highlights").upper(), ""]
        out += [f"- {t}" if markdown else t for t in big] + [""]

    sections = words("story_parts")
    for k, (start, name) in enumerate(sections):
        end = sections[k + 1][0] if k + 1 < len(sections) else 24
        texts = [e["text"] for e in entries if start <= time.localtime(e["t"]).tm_hour < end]
        if not texts:
            continue
        out += [f"## {name}" if markdown else name.upper(), ""]
        for j in range(0, len(texts), 5):                 # párrafos de cinco sucesos
            out += [" ".join(texts[j:j + 5]), ""]

    heroes = [e["text"] for e in entries if e.get("k") in ("hero.born", "power.heroe", "hero.deed", "hero.died", "hero.fell")]
    if heroes:
        out += [f"## {tr('story.heroes')}" if markdown else tr("story.heroes").upper(), ""]
        out += [f"- {t}" if markdown else t for t in heroes] + [""]
    out.append(tr("story.end", kingdom=kingdom))
    return "\n".join(out) + "\n"


def export(day: str | None = None, kingdom: str | None = None, era: str = "") -> tuple[Path, Path] | None:
    """Escribe stories/AAAA-MM-DD.md y .txt. Devuelve las rutas (o None si no hubo crónica)."""
    day = day or time.strftime("%Y-%m-%d")
    entries = load(day)
    if not entries:
        return None
    if kingdom is None:
        try:
            saved = json.loads(paths.save_file().read_text())
            kingdom = saved.get("kingdom", "")
        except (OSError, ValueError):
            kingdom = ""
    folder = paths.data_dir() / "stories"
    folder.mkdir(parents=True, exist_ok=True)
    md, txt = folder / f"{day}.md", folder / f"{day}.txt"
    md.write_text(build(day, entries, kingdom, era, markdown=True), encoding="utf-8")
    txt.write_text(build(day, entries, kingdom, era, markdown=False), encoding="utf-8")
    return md, txt
