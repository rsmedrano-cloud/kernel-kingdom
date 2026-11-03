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
import time
import urllib.request
from collections import Counter
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


def world_the(world: dict, name: str) -> str:
    """Con artículo, para el relato: 'el Archipiélago de X' (-> 'del'), 'the X Archipelago'."""
    if world.get("isles"):
        from .daemon.lore import Lore
        return words("archipelago_the").format(name=Lore({}, world.get("seed", 1337)).region("archipelago"))
    return name


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
    sections = []

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
    story_text = prose(entries, world, day, name)
    narration = ([{"text": f"{head}.", "where": None}]
                 + [{"text": t, "where": None} for t in _sentences(" ".join(story_text))])
    return {"day": day, "n": n, "title": title, "world": name, "date": date.strftime("%d/%m/%Y"),
            "previously": previously, "sections": sections, "epilogue": epilogue, "narration": narration,
            "prose": story_text}


# -- el relato en prosa: actos, causas y consecuencias ------------------------------

def _names(entries: list[dict], keys: set[str], param: str, limit: int = 4) -> list[str]:
    """Nombres propios distintos de ciertos sucesos, en el orden en que pasaron."""
    out = []
    for e in entries:
        v = (e.get("p") or {}).get(param)
        if e.get("k") in keys and v and v not in out:
            out.append(v)
    return out[:limit]


def _list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + words("annals_and") + items[-1]


def _plural(key: str, items: list[str], **kw) -> str | None:
    """Plantilla singular o plural según cuántos nombres haya."""
    if not items:
        return None
    one, many = words(key)
    return _cap((one if len(items) == 1 else many).format(x=items[0], list=_list(items), **kw))


def _act(entries: list[dict], first: bool, previous: set[str]) -> tuple[list[str], set[str]]:
    """Un acto del día: las cosas en el orden en que se encadenan (cataclismo -> caídas ->
    renacer -> ambición y guerras -> paz), y después lo que lo acompaña (obras, héroes,
    el Soberano, el cielo). Devuelve las frases y qué tipos de cosas pasaron."""
    k = Counter(e.get("k") for e in entries)
    out, seen = [], set()
    era = _names(entries, {"era.new"}, "era", 1)
    if era:
        out.append(words("annals_cataclysm").format(era=era[0]))
        seen.add("era")
    god = next(((e.get("p") or {}) for e in entries if e.get("k") == "era.kernel"), None)
    if god and god.get("kernel"):
        out.append(words("annals_new_god").format(old=god.get("old", "?"), new=god["kernel"]))
    forts = _names(entries, {"fort.silent"}, "the_fort")
    if forts and (era or k["fort.silent"] >= 3):
        out.append(_plural("annals_forts_silent", forts, cause=words("annals_because") if era else ""))
    falls = _names(entries, {"fall"}, "realm")
    if falls:
        out.append(_plural("annals_falls", falls))
        seen.add("fall")
    reborn = _names(entries, {"dyn.reborn"}, "realm") + _names(entries, {"found.realm", "found.app"}, "realm")
    if reborn:
        key = "annals_reborn" if ("fall" in seen or "fall" in previous) else "annals_founded"
        out.append(_plural(key, reborn))
        seen.add("rise")
    wars = _names(entries, {"war.declare", "war.start"}, "war")
    if wars:
        key = "annals_wars_after" if "rise" in seen else "annals_wars"
        out.append(_plural(key, wars))
        seen.add("war")
    battles = [e for e in entries if e.get("k") == "battle"]
    if len(battles) >= 3:
        winners = Counter((e.get("p") or {}).get("a") for e in battles if (e.get("p") or {}).get("a"))
        top = winners.most_common(1)[0][0] if winners else None
        out.append(words("annals_battles").format(n=len(battles), realm=top) if top
                   else words("annals_battles_plain").format(n=len(battles)))
    sacked = _names(entries, {"capital.sacked"}, "b")
    if sacked:
        out.append(_plural("annals_sacked", sacked))
    peace = _names(entries, {"peace", "power.tregua"}, "war")
    if peace:
        out.append(_plural("annals_peace_after" if "war" in seen or "war" in previous else "annals_peace", peace))
        seen.add("peace")
    # lo que acompaña: textos de la crónica tal cual (ya son buenas frases), lo más jugoso
    flavor = [e for e in entries if e.get("kind") in ("heroe", "obra", "poder", "fe", "naturaleza", "calor",
                                                       "storm", "eruption", "mundo")
              and PRIORITY.get(e.get("k", ""), 10) >= 30]
    flavor.sort(key=lambda e: -PRIORITY.get(e.get("k", ""), 10))
    picked = sorted(flavor[:3], key=lambda e: e.get("t", 0))
    if picked:
        heroes = [e for e in picked if e.get("kind") == "heroe"]
        if out:                                        # conector variado, para no repetir la misma frase
            options = words("annals_heroes_open") if heroes else words("annals_meanwhile")
            out.append(options[len(previous) % len(options)])
        out += [e["text"] for e in picked]
    return out, seen


def _opening(entries: list[dict], world: str, day: str) -> str:
    d = dt.date.fromisoformat(day)
    month = words("months")[d.month - 1]
    n = len(entries)
    if n < 40:
        return words("annals_open_calm").format(world=world, day=d.day, month=month, year=d.year)
    from .story import count_parts
    kinds = [p.split(" ", 1)[1] for p in count_parts(entries)[:4]]
    rounded = int(str(n)[:2]) * 10 ** (len(str(n)) - 2) if n >= 100 else n
    return words("annals_open").format(world=world, day=d.day, month=month, year=d.year, n=rounded,
                                       kinds=_list(kinds) if kinds else words("annals_things"))


def prose(entries: list[dict], world: dict, day: str, name: str) -> list[str]:
    """El día como relato: apertura de cronista, un párrafo por momento del día (con lo
    que lo encadena con el anterior) y un cierre."""
    the_world = world_the(world, name)
    paragraphs = [_opening(entries, the_world, day)]
    open_wars = [w["name"] for w in (world.get("wars") or {}).values()
                 if w.get("start", 0) < min((e.get("tick", 0) for e in entries), default=0)]
    if open_wars:
        paragraphs[0] += " " + words("annals_open_wars").format(wars=_list(open_wars[:2]))
    parts = words("story_parts")
    previous: set[str] = set()
    acts = []
    for k, (start, _) in enumerate(parts):
        end = parts[k + 1][0] if k + 1 < len(parts) else 24
        mine = [e for e in entries if start <= time.localtime(e["t"]).tm_hour < end]
        if mine:
            acts.append((k, mine))
    for idx, (k, mine) in enumerate(acts):
        lines, seen = _act(mine, idx == 0, previous)
        if not lines:
            continue
        opener = words("annals_act_open")[k] if idx else ""
        first = lines[0]
        if opener:
            word, _, rest = first.partition(" ")
            if word.rstrip(",") in words("annals_soft_starts"):          # "Pronto, ..." ya lo dice el conector
                first = rest if word.endswith(",") else first
            starts = first.split(" ", 1)[0]
            if starts in words("annals_lower_starts"):                   # nunca un nombre propio
                first = first[:1].lower() + first[1:]
            first = opener + " " + first
        paragraphs.append(" ".join([first] + lines[1:]))
        previous |= seen
    closes = words("annals_close")
    paragraphs.append(closes[sum(map(ord, day)) % len(closes)].format(world=the_world))
    return [i18n.polish(_cap(p)) for p in paragraphs]


def render(ch: dict, markdown: bool = True, bard_text: str | None = None) -> str:
    head = words("annals_chapter").format(n=ch["n"], title=ch["title"])
    out = [f"## {head}" if markdown else head.upper(), "", f"*{ch['world']} — {ch['date']}*" if markdown
           else f"{ch['world']} — {ch['date']}", ""]
    body = [p.strip() for p in bard_text.strip().split("\n") if p.strip()] if bard_text else ch["prose"]
    for para in body:
        out += [para, ""]
    out += [" ".join(ch["epilogue"]), ""]
    if markdown:                                         # en el libro, también el detalle por tema
        out += [f"### {words('annals_details')}", ""]
        for s in ch["sections"]:
            out += [f"**{s['heading']}.** " + " ".join(s["texts"]), ""]
    return "\n".join(out)


def _sentences(text: str) -> list[str]:
    """Para la voz y los subtítulos: frases de largo razonable."""
    import re
    out = []
    for part in re.split(r"(?<=[.!?])\s+", text):
        if part.strip():
            out.append(part.strip())
    return out


# -- bardo opcional (ollama) -------------------------------------------------------

def bard(ch: dict, cfg: dict) -> str | None:
    """Prosa de un modelo de lenguaje. Solo si [bard] enabled = true. Recibe el borrador
    ya ordenado (actos, causas, nombres) y lo escribe con voz de cronista. Los hechos solo
    van a la dirección configurada (por defecto, esta misma PC)."""
    import os
    bc = cfg.get("bard", {})
    if not bc.get("enabled"):
        return None
    prompt = raw("annals.bard_prompt").format(world=ch["world"], title=ch["title"],
                                              previously=" ".join(ch["previously"]) or "-",
                                              draft="\n\n".join(ch["prose"]))
    url = bc.get("url", "http://127.0.0.1:11434").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if bc.get("provider", "ollama") == "openai":           # cualquier servidor compatible (/v1/chat/completions)
        key = os.environ.get(bc.get("api_key_env") or "", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {"model": bc.get("model", ""), "temperature": 0.8,
                "messages": [{"role": "user", "content": prompt}]}
        endpoint = url + ("/chat/completions" if url.endswith("/v1") else "/v1/chat/completions")
    else:
        body = {"model": bc.get("model", "llama3.2"), "prompt": prompt, "stream": False,
                "options": {"temperature": 0.8}}
        endpoint = url + "/api/generate"
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=bc.get("timeout", 180)) as r:
            data = json.loads(r.read().decode())
    except (OSError, ValueError):
        return None
    if "choices" in data:
        text = ((data["choices"] or [{}])[0].get("message") or {}).get("content", "")
    else:
        text = data.get("response", "")
    text = text.strip()
    return text if len(text) > 200 else None              # una respuesta vacía o cortada no sirve


# -- el libro ---------------------------------------------------------------------

def bard_text(day: str) -> str | None:
    f = folder() / f"{day}.bard.txt"
    return f.read_text(encoding="utf-8") if f.exists() else None


def write(day: str, entries: list[dict], cfg: dict | None = None, world: dict | None = None) -> dict | None:
    """Escribe el capítulo del día (md + txt), lo anota en el índice y rearma el libro.
    `world`: el estado del mundo (el daemon pasa una copia del vivo; si no, el guardado)."""
    if not entries:
        return None
    index = _index()
    n = index.get(day, {}).get("n") or 1 + sum(1 for d in index if d < day)
    ch = chapter(day, entries, world, n=n)
    prose = bard(ch, cfg or {}) if cfg else None
    if prose:
        (folder() / f"{day}.bard.txt").write_text(prose, encoding="utf-8")   # el video la reusa
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

