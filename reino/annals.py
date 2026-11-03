"""Los Anales: cada día del mundo contado como un capítulo.

Un capítulo tiene título (sale del suceso más grande del día: "El Saqueo de
Zenhar", "La Llegada de Gwenia"...), un "en el capítulo anterior" con lo que
quedó abierto, secciones por tema (guerra, coronas, obras, héroes, el Soberano,
el mundo) con lo importante primero y lo repetido resumido, y un epílogo con el
estado del mundo. Los capítulos se guardan en data_dir()/annals y se juntan en
un libro (annals.md) que crece día a día.

Opcional: un "bardo" (un modelo local vía ollama, [bard] en la config) reescribe
el capítulo en prosa. Si no está o falla, queda el relato armado acá.
"""
import datetime as dt
import json
import urllib.request
from pathlib import Path

from . import i18n, paths
from .i18n import raw, words

# Temas, en el orden en que se cuentan: tipo de entrada de la crónica -> sección
THEMES = [
    ("war", {"guerra", "paz"}),
    ("crowns", {"dinastia", "caida", "fundacion", "hito", "fe"}),
    ("works", {"obra", "academia", "fortaleza", "expedicion"}),
    ("heroes", {"heroe"}),
    ("sovereign", {"poder", "soberano"}),
    ("world", None),                    # todo lo demás: clima, naturaleza, migraciones, islas, eras
]

# Qué tan importante es cada suceso (lo demás vale 10; lo muy repetido, 2)
PRIORITY = {
    "power.dragon": 100, "dragon.leave": 60, "capital.sacked": 95, "isle.rise": 90, "fall": 90,
    "wonder.done": 85, "war.declare": 80, "war.start": 80, "era.new": 75, "era.kernel": 75, "peace": 75,
    "power.tregua": 70, "walls.breach": 70, "exp.siege.breach": 70, "dyn.succession": 65, "dyn.reborn": 65,
    "capital.moved": 60, "monument": 60, "hero.born": 55, "hero.died": 55, "hero.fell": 55, "power.heroe": 55,
    "exp.fleet.sail": 45, "raid.sail": 50, "raid.back": 40, "pirates": 50, "eruption.start": 50,
    "storm.start": 40, "found.realm": 45, "found.app": 30, "walls.held": 40, "exp.siege.held": 40,
    "wonder.coloso.held": 40, "hero.deed": 35, "power.maravilla": 45, "battle": 30, "milestone": 25,
    "drift": 2, "boom": 2, "nature.grows": 2, "nature.shrinks": 2, "nature.felled": 2, "exp.plant.start": 2,
    "exp.explore.start": 4, "visit.arrive": 8, "visit.leave": 2, "visit.local": 2, "visit.local_leave": 2,
    "afk.leave": 2, "afk.back": 2, "fort.silent": 3, "fort.relit": 3,
}

# Para titular el día: suceso -> nombre propio que lleva el título (en orden de preferencia)
TITLE_KEYS = [
    ("power.dragon", "name"), ("capital.sacked", "b"), ("isle.rise", "isle"), ("fall", "realm"),
    ("wonder.done", "title"), ("war.declare", "war"), ("war.start", "war"), ("era.new", None),
    ("peace", "war"), ("power.tregua", "war"), ("walls.breach", "b"), ("eruption.start", None),
    ("dyn.succession", "realm"), ("monument", "realm"), ("hero.born", "hero"), ("storm.start", None),
]


def folder() -> Path:
    d = paths.data_dir() / "annals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index() -> dict:
    try:
        return json.loads((folder() / "index.json").read_text())
    except (OSError, ValueError):
        return {}


def _world() -> dict:
    try:
        return json.loads(paths.save_file().read_text())
    except (OSError, ValueError):
        return {}


def world_name(world: dict) -> str:
    """Nombre de lo que se narra: el archipiélago si hay islas del lab, si no tu reino."""
    if world.get("isles"):
        from .daemon.lore import Lore
        lore = Lore({}, world.get("seed", 1337))
        return words("archipelago").format(name=lore.region("archipelago"))
    return world.get("kingdom", "")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _title(entries: list[dict], world: str) -> str:
    keys = {}
    for e in entries:
        keys.setdefault(e.get("k"), e)
    titles = words("annals_titles")
    for key, param in TITLE_KEYS:
        e = keys.get(key)
        if not e:
            continue
        value = (e.get("p") or {}).get(param) if param else ""
        if param and not value:
            continue
        return _cap(titles[key].format(x=value, world=world))
    return titles["default"].format(world=world)


def _drift_line(entries: list[dict]) -> str | None:
    """'Los colonos de X avanzan hacia el norte' x 20 -> una sola línea."""
    moves = {}
    for e in entries:
        p = e.get("p") or {}
        if e.get("k") == "drift" and p.get("realm"):
            moves[p["realm"]] = p.get("dir") or ""
    if len(moves) < 2:
        return None
    parts = [words("annals_drift_item").format(realm=r, dir=d) if d else r for r, d in list(moves.items())[:5]]
    return words("annals_drift").format(list=", ".join(parts))


def chapter(day: str, entries: list[dict], world: dict | None = None, n: int | None = None) -> dict:
    """Arma el capítulo del día. Devuelve un dict con título, secciones y partes para narrar."""
    world = world if world is not None else _world()
    name = world_name(world)
    index = _index()
    if n is None:
        n = 1 + sum(1 for d in index if d < day)
    prev = max((d for d in index if d < day), default=None)
    title = _title(entries, name)
    date = dt.date.fromisoformat(day)
    headings = words("annals_headings")
    sections, narration = [], []

    previously = []
    if prev:
        previously.append(words("annals_previously").format(title=index[prev]["title"]))
    first_tick = min((e.get("tick", 0) for e in entries), default=0)
    open_wars = [w["name"] for w in (world.get("wars") or {}).values() if w.get("start", 0) < first_tick]
    if open_wars:
        previously.append(words("annals_open_wars").format(wars=", ".join(open_wars[:3])))

    for theme, kinds in THEMES:
        mine = [e for e in entries if (kinds is None and not any(e.get("kind") in k for _, k in THEMES if k))
                or (kinds is not None and e.get("kind") in kinds)]
        if not mine:
            continue
        ranked = sorted(mine, key=lambda e: (-PRIORITY.get(e.get("k", ""), 10), e.get("t", 0)))
        lines, seen = [], set()
        for e in ranked:
            if PRIORITY.get(e.get("k", ""), 10) <= 2 or e["text"] in seen:
                continue
            seen.add(e["text"])
            lines.append(e)
            if len(lines) == 5:
                break
        drift = _drift_line(mine) if theme == "world" else None
        rest = [e for e in mine if e not in lines]
        texts = [e["text"] for e in sorted(lines, key=lambda e: e.get("t", 0))]   # lo elegido, en orden
        if drift:
            texts.append(drift)
        if len(rest) > 3:                                  # lo demás, contado: "Además: 214 batallas, ..."
            from .story import count_parts
            parts = count_parts(rest)
            texts.append(words("annals_also").format(parts=", ".join(parts[:4])) if parts
                         else words("annals_more").format(n=len(rest)))
        if not texts:
            continue
        sections.append({"theme": theme, "heading": headings[theme], "texts": texts})
        for e in sorted(lines[:2], key=lambda e: e.get("t", 0)):     # para la voz: lo más importante
            narration.append({"text": e["text"], "where": (e["x"], e["y"]) if "x" in e else None})

    epilogue = []
    realms = sorted((f for f in world.get("factions", []) if f.get("alive") and f.get("kind") == "browser"),
                    key=lambda f: -f.get("tiles", 0))[:3]
    if realms:
        from .daemon.lore import Lore
        items = [words("annals_realm_item").format(realm=f["realm"], n=f.get("tiles", 0),
                                                   ruler=Lore.ruler_str(f.get("ruler"))) for f in realms]
        epilogue.append(words("annals_powers").format(list="; ".join(items)))
    if world.get("wars"):
        epilogue.append(words("annals_wars_on").format(wars=", ".join(w["name"] for w in world["wars"].values())))
    epilogue.append(words("annals_end").format(n=n, world=name))

    head = words("annals_chapter").format(n=n, title=title)
    narration = ([{"text": f"{name}. {head}.", "where": None}]
                 + [{"text": t, "where": None} for t in previously[:1]]
                 + narration[:8]
                 + [{"text": t, "where": None} for t in epilogue[-2:]])
    return {"day": day, "n": n, "title": title, "world": name, "date": date.strftime("%d/%m/%Y"),
            "previously": previously, "sections": sections, "epilogue": epilogue, "narration": narration}


def render(ch: dict, markdown: bool = True, bard_text: str | None = None) -> str:
    head = words("annals_chapter").format(n=ch["n"], title=ch["title"])
    out = [f"## {head}" if markdown else head.upper(), "", f"*{ch['world']} — {ch['date']}*" if markdown
           else f"{ch['world']} — {ch['date']}", ""]
    if ch["previously"]:
        out += [(" ".join(ch["previously"])), ""]
    if bard_text:
        out += [p.strip() for p in bard_text.strip().split("\n") if p.strip()]
        out += ["", f"### {words('annals_details')}" if markdown else words("annals_details").upper(), ""]
    for s in ch["sections"]:
        out += [f"### {s['heading']}" if markdown else s["heading"].upper(), ""]
        out += [" ".join(s["texts"]), ""]
    out += [" ".join(ch["epilogue"]), ""]
    return "\n".join(out)


# -- bardo opcional (ollama) -------------------------------------------------------

def bard(ch: dict, cfg: dict) -> str | None:
    """Prosa de un modelo local. Solo si [bard] enabled = true; los hechos no salen del
    servidor que configures (por defecto, esta misma PC)."""
    bc = cfg.get("bard", {})
    if not bc.get("enabled"):
        return None
    facts = [t for s in ch["sections"] for t in s["texts"]][:30]
    prompt = raw("annals.bard_prompt").format(world=ch["world"], title=ch["title"], facts="\n".join(f"- {f}" for f in facts))
    body = json.dumps({"model": bc.get("model", "llama3.2"), "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.8}}).encode()
    req = urllib.request.Request(bc.get("url", "http://127.0.0.1:11434").rstrip("/") + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=bc.get("timeout", 180)) as r:
            text = json.loads(r.read().decode()).get("response", "").strip()
        return text or None
    except (OSError, ValueError):
        return None


# -- el libro ---------------------------------------------------------------------

def write(day: str, entries: list[dict], cfg: dict | None = None, world: dict | None = None) -> dict | None:
    """Escribe el capítulo del día (md + txt), lo anota en el índice y rearma el libro.
    `world`: el estado del mundo (el daemon pasa una copia del vivo; si no, el guardado)."""
    if not entries:
        return None
    index = _index()
    n = index.get(day, {}).get("n") or 1 + sum(1 for d in index if d < day)
    ch = chapter(day, entries, world, n=n)
    prose = bard(ch, cfg or {}) if cfg else None
    d = folder()
    (d / f"{day}.md").write_text(render(ch, True, prose), encoding="utf-8")
    (d / f"{day}.txt").write_text(render(ch, False, prose), encoding="utf-8")
    index[day] = {"n": n, "title": ch["title"], "lang": i18n.LANG}
    (d / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    book(ch["world"])
    return ch


def book(world: str = "") -> Path:
    """annals.md: todos los capítulos, del primero al último, con índice."""
    index = _index()
    d = folder()
    days = sorted(index)
    title = words("annals_book").format(world=world or "")
    toc = [f"- [{words('annals_chapter').format(n=index[x]['n'], title=index[x]['title'])}](#{x})" for x in days]
    parts = [f"# {title}", "", *toc, ""]
    for x in days:
        f = d / f"{x}.md"
        if f.exists():
            parts += [f'<a id="{x}"></a>', "", f.read_text(encoding="utf-8"), ""]
    out = d / "annals.md"
    out.write_text("\n".join(parts), encoding="utf-8")
    return out

