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


def _ordinal(n: int) -> str:
    ords = words("ordinals")
    return ords[n - 1] if 0 < n <= len(ords) else words("ordinal_n").format(n=n)


def _isles(world: dict) -> tuple[str, dict, dict]:
    """(nombre de tu isla, id -> nombre de cada isla del lab, reino -> nombre de su isla)."""
    home = world.get("kingdom", "")
    names = {iid: info.get("name", "") for iid, info in (world.get("isles") or {}).items()}
    of = {f["realm"]: names.get(f.get("isle"), home) for f in world.get("factions", [])}
    return home, names, of


def _home_night(entries: list[dict], home: str, of: dict) -> list[str]:
    """Tu isla durmió (la PC estuvo apagada) y/o empezó una edad nueva: lo que cayó y renació."""
    out = []
    slept = next(((e.get("p") or {}) for e in entries if e.get("k") == "home.slept"), None)
    era = _names(entries, {"era.new"}, "era", 1)
    if slept:
        out.append(words("annals_slept").format(isle=home, hours=slept.get("hours", "?")))
    if era:
        out.append(words("annals_cataclysm").format(era=era[0]))
    god = next(((e.get("p") or {}) for e in entries if e.get("k") == "era.kernel"), None)
    if god and god.get("kernel"):
        out.append(words("annals_new_god").format(old=god.get("old", "?"), new=god["kernel"]))
    if not out:
        return []
    forts = _names(entries, {"fort.silent"}, "the_fort")
    if forts:
        out.append(_plural("annals_forts_silent", forts, cause=words("annals_because")))
    falls = [r for r in _names(entries, {"fall"}, "realm", 8) if of.get(r, home) == home][:4]
    if falls:
        out.append(_plural("annals_falls", falls))
    reborn = [r for r in _names(entries, {"dyn.reborn", "found.realm", "found.app"}, "realm", 8)
              if of.get(r, home) == home][:4]
    if reborn:
        out.append(_plural("annals_reborn", reborn))
    return out


def _wars(entries: list[dict], today: dict, hist: dict, of: dict, home: str, day: str) -> list[str]:
    """Las guerras con su historia: nuevas (¿cruzaron el mar? ¿cuántas van entre ellos?),
    las que siguen (¿cuántos días llevan?) y las que terminaron (¿quién ganó?)."""
    wars = today.get("wars", {})
    if not wars:
        return []
    ranked = sorted(wars.items(), key=lambda kv: (-(kv[1]["declared"] or kv[1]["ended"]), -kv[1]["battles"]))
    out, told = [], 0
    for name, w in ranked[:3]:
        a, b = w.get("a"), w.get("b")
        h = hist["wars"].get(name, {})
        started_today = h.get("start") == day
        if started_today and a and b:
            ia, ib = of.get(a, home), of.get(b, home)
            if ia != ib:
                line = words("annals_war_sail").format(isle_a=ia, isle_b=ib, a=a, b=b, war=name)
            else:
                line = words("annals_war_new").format(a=a, b=b, war=name)
            feud = hist["feuds"].get(tuple(sorted((a, b))), 1)
            if feud >= 2:
                line = line.rstrip(".") + words("annals_feud").format(nth=_ordinal(feud)) + "."
            out.append(_cap(line))
        elif not started_today and w["ended"]:              # una sola frase: llevaba N días y terminó
            win = w.get("winner")
            out.append(_cap(words("annals_war_old_won" if win else "annals_war_old_draw").format(
                war=name, d=len(h.get("days", [])) or 2, winner=win)))
        elif not started_today:
            out.append(_cap(words("annals_war_goes").format(war=name, d=len(h.get("days", [])) or 2)))
        if w["battles"] >= 2:
            out.append(words("annals_war_battles").format(n=w["battles"]))
        if w["ended"] and started_today:
            win = w.get("winner")
            out.append(_cap(words("annals_war_won" if win else "annals_war_draw").format(war=name, winner=win)))
        told += 1
    for b_, a_ in today.get("sacked", [])[:2]:
        out.append(words("annals_war_sack").format(b=b_, a=a_ or "?"))
    rest = len(wars) - told
    if rest >= 2:
        out.append(words("annals_more_wars").format(n=rest))
    return out


def _islands(entries: list[dict], today: dict, names: dict, of: dict, home: str, skip_home: bool) -> list[str]:
    """Lo que pasó en cada isla (sus reinos caídos, renacidos y fundados; sus edades)."""
    out = []
    ages = {isle: n for isle, n in today.get("ages", [])}
    for isle in [home] + list(names.values()):
        if isle == home and skip_home:
            continue
        falls = [r for r in dict.fromkeys(today.get("falls", [])) if of.get(r, home) == isle][:3]
        risen = [r for r in dict.fromkeys(today.get("reborn", []) + today.get("founded", []))
                 if of.get(r, home) == isle][:3]
        bits = []
        if isle in ages:
            bits.append(words("annals_isle_age").format(n=_ordinal(int(ages[isle] or 1))))
        if falls:
            bits.append(_plural("annals_isle_falls", falls))
        if risen:
            bits.append(_plural("annals_isle_rise", risen))
        if bits:
            out.append(words("annals_in_isle").format(isle=isle) + " " + _list([b[:1].lower() + b[1:] if b.split(" ", 1)[0] in words("annals_lower_starts") else b for b in bits]) + ".")
    return out


def _heroes(entries: list[dict], today: dict, hist: dict, day: str) -> list[str]:
    out = []
    born = today.get("born", [])
    if born:
        items = [words("annals_hero_item").format(hero=h, realm=r) if r else h for h, r, _ in born[:3]]
        more = len(born) - 3
        line = _plural("annals_heroes_born", items)
        if more > 0:
            line = line.rstrip(".") + words("annals_and_more").format(n=more) + "."
        out.append(line)
    deed = next((e["text"] for e in entries if e.get("k") == "hero.deed"), None)
    if deed:
        out.append(deed)
    died = today.get("died", [])
    if died:
        items = []
        for h, r, war in died[:3]:
            born_day = (hist["heroes"].get(h) or {}).get("born")
            if war:
                items.append(words("annals_hero_fell").format(hero=h, war=war))
            elif born_day and born_day < day:
                d = (dt.date.fromisoformat(day) - dt.date.fromisoformat(born_day)).days
                items.append(words("annals_hero_old").format(hero=h, d=d))
            else:
                items.append(h)
        out.append(_plural("annals_heroes_died", items))
    return out


def _marvels(entries: list[dict]) -> list[str]:
    keys = ("power.dragon", "dragon.leave", "wonder.done", "monument", "power.maravilla", "eruption.start",
            "pirates", "raid.sail", "isle.rise")
    picked = sorted((e for e in entries if e.get("k") in keys),
                    key=lambda e: -PRIORITY.get(e.get("k", ""), 10))[:3]
    return [e["text"] for e in sorted(picked, key=lambda e: e.get("t", 0))]


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


def prose(entries: list[dict], world: dict, day: str, name: str, today: dict | None = None,
          hist: dict | None = None) -> list[str]:
    """El día como relato por hilos: apertura y marco del mundo, la noche de tu isla, las
    guerras con su historia, lo que pasó en cada isla del lab, los héroes, las maravillas
    y un cierre. Cada nombre se cuenta una vez."""
    from . import legend
    if today is None:
        today = legend.day_summary(entries)
    if hist is None:
        leg = legend.load()
        leg.setdefault("days", {})[day] = today
        hist = legend.history(leg, day)
    the_world = world_the(world, name)
    home, names, of = _isles(world)
    paragraphs = [_opening(entries, the_world, day)]
    if names:
        paragraphs[0] += " " + _cap(words("annals_frame").format(world=the_world, home=home,
                                                                  others=_list(list(names.values()))))
    night = _home_night(entries, home, of)
    for block in (night, _wars(entries, today, hist, of, home, day),
                  _islands(entries, today, names, of, home, skip_home=bool(night)),
                  _heroes(entries, today, hist, day), _marvels(entries)):
        if block:
            paragraphs.append(" ".join(block))
    closes = words("annals_close")
    paragraphs.append(closes[sum(map(ord, day)) % len(closes)].format(world=the_world))
    return [i18n.polish(_cap(p)) for p in paragraphs]

def render(ch: dict, markdown: bool = True, bard_text: str | None = None) -> str:
    head = words("annals_chapter").format(n=ch["n"], title=ch["title"])
    out = [f"## {head}" if markdown else head.upper(), "", f"*{ch['world']} — {ch['date']}*" if markdown
           else f"{ch['world']} — {ch['date']}", ""]
    if ch["previously"]:
        out += [" ".join(ch["previously"]), ""]
    if bard_text:                                        # la prosa del cronista (IA), si la hay
        out += [p.strip() for p in bard_text.strip().split("\n") if p.strip()]
        out += ["", f"### {words('annals_details')}" if markdown else words("annals_details").upper(), ""]
    for s in ch["sections"]:                             # los resúmenes por tema, con todo su detalle
        out += [f"### {s['heading']}" if markdown else s["heading"].upper(), ""]
        out += [" ".join(s["texts"]), ""]
    out += [" ".join(ch["epilogue"]), ""]
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
    from . import legend
    legend.record(day, entries)                          # el día entra en la memoria del mundo
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

