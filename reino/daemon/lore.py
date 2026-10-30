"""Lore procedural: nombres, gobernantes y crónica.

Todo es determinista a partir de la semilla del mundo + la clave de la cosa
nombrada, así el mismo proceso siempre recibe el mismo nombre de reino.
Los textos salen de reino/locales (castellano o inglés): monarcas, héroes y
deidades guardan claves, y su nombre visible se arma en el idioma activo.
"""
import colorsys
import random
import re
import time
import zlib

from ..i18n import polish, tr, words

_ONSET = ["Al", "Bel", "Cor", "Dar", "El", "Fen", "Gal", "Har", "Is", "Kel", "Lor", "Mar", "Nor",
          "Or", "Per", "Ral", "Sel", "Tor", "Ul", "Val", "Wen", "Yr", "Zar", "Ame", "Bri", "Cas",
          "Dor", "Eri", "Gwen", "Ilm", "Ly", "Mor", "Ny", "Os", "Rho", "Sy", "Tam", "Vey"]
_CODA = ["a", "e", "ia", "ion", "ric", "wen", "dor", "mund", "ys", "eth", "an", "ora", "iel",
         "ar", "is", "ana", "ald", "ina", "o", "uin"]
_REALM_SUFFIX = ["aria", "ia", "har", "heim", "dor", "mar", "grad", "lund", "ovia", "esia"]
_PLACE_SUFFIX = ["burgo", "holm", "gard", "fort", "mar", "peña", "roca", "val", "cumbre"]
_ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII",
          "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX"]
# Mundos guardados con versiones anteriores (todo en castellano)
_OLD_TRAITS = {"valiente": "brave", "sabio": "wise", "codicioso": "greedy", "piadoso": "pious",
               "astuto": "cunning", "cruel": "cruel", "bondadoso": "kind", "errante": "wanderer"}
_OLD_EPITHETS = {"Voraz": "greedy", "Prolífic": "prolific", "Longev": "longlived", "Ardiente": "fiery",
                 "Conquistador": "conqueror"}


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def rng_for(seed: int, key: str) -> random.Random:
    return random.Random(seed ^ zlib.crc32(key.encode()))


def roman(n: int) -> str:
    return _ROMAN[n] if n < len(_ROMAN) else str(n)


def _stem(name: str) -> str:
    letters = re.sub(r"[^a-zA-Z]", "", name).lower()[:6] or "nadie"
    stem = letters.rstrip("aeiou") if len(letters.rstrip("aeiou")) >= 3 else letters
    if not re.search(r"[aeiou]", stem):
        stem = stem[0] + "a" + stem[1:]
    return stem.capitalize()


def adjective(color: list[int]) -> str:
    h, _, _ = colorsys.rgb_to_hls(*(c / 255 for c in color))
    return next(name for limit, name in words("hues") if h < limit)


def the(title: str) -> str:
    """'Bastión de X' -> 'el Bastión de X'; 'Bastion of X' -> 'the Bastion of X'."""
    articles = words("fort_article")
    if not articles:
        return f"the {title}"
    return f"{articles.get(title.split()[0], 'el')} {title}"


def landmark_name(key: str | None) -> str:
    return words("landmark_a").get(key or "", words("landmark_a")["molino"])


def fmt_mb(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


class Lore:
    def __init__(self, cfg: dict, seed: int):
        self.seed = seed
        lore = cfg.get("lore", {})
        self.realm_names = lore.get("realms", {})
        self.place_names = lore.get("places", {})

    # -- nombres -------------------------------------------------------------

    def person(self, key: str) -> tuple[str, bool]:
        """(nombre, es_mujer) de un personaje."""
        rng = rng_for(self.seed, "person:" + key)
        name = rng.choice(_ONSET) + rng.choice(_CODA)
        return name, name.endswith(("a", "ia", "ina", "ana", "ora", "iel", "wen", "ys"))

    def realm(self, proc_name: str) -> str:
        if proc_name in self.realm_names:
            return self.realm_names[proc_name]
        return _stem(proc_name) + rng_for(self.seed, "realm:" + proc_name).choice(_REALM_SUFFIX)

    def place(self, name: str) -> str:
        if name in self.place_names:
            return self.place_names[name]
        return _stem(name) + rng_for(self.seed, "place:" + name).choice(_PLACE_SUFFIX)

    def region(self, key: str) -> str:
        """Nombre propio de una región del mapa (Belwen, Torgard, Ilmaval...)."""
        rng = rng_for(self.seed, "region:" + key)
        name = rng.choice(_ONSET) + rng.choice(_CODA)
        return name + rng.choice(_PLACE_SUFFIX) if rng.random() < 0.45 else name

    def structure_name(self, kind: str, name: str) -> str:
        return f"{words('kind_title').get(kind, words('kind_title')['daemon'])} {words('of')} {self.place(name)}"

    def kingdom(self, host: str) -> str:
        return self.realm(host)

    def era_index(self, boot: int) -> int:
        return rng_for(self.seed, f"era:{boot}").randrange(len(words("eras")))

    @staticmethod
    def era_name(era: dict) -> str:
        eras = words("eras")
        return eras[era.get("idx", 0) % len(eras)]

    # -- reinos y monarcas -----------------------------------------------------

    @staticmethod
    def realm_title(f: dict) -> tuple[str, str]:
        tiles = f.get("tiles", 0)
        if f.get("kind") == "app":
            key = "village" if tiles >= 8 else "hamlet"
        else:
            key = "empire" if tiles >= 120 else "kingdom" if tiles >= 40 else "principality"
        return words("realm_titles")[key]

    def full_realm(self, f: dict) -> str:
        title, _ = self.realm_title(f)
        pattern = words("app_pattern") if f.get("kind") == "app" else words("realm_pattern")
        return pattern.format(title=title, adj=adjective(f["color"]), realm=f["realm"])

    def the_full(self, f: dict) -> str:
        _, article = self.realm_title(f)
        return f"{article} {self.full_realm(f)}"

    def new_ruler(self, f: dict, now: float) -> dict:
        n = f.get("dynasty", 0) + 1
        f["dynasty"] = n
        name, fem = self.person(f"{f['name']}:{n // 3}")   # cambia de casa cada 3 reinados
        return {"name": name, "fem": fem, "n": (n - 1) % 3 + 1, "since": now}

    @staticmethod
    def ruler_str(r: dict | None) -> str:
        if not r:
            return words("nobody")
        fem = r.get("fem", r.get("title") == "Reina")
        title, article = words("ruler_titles")["queen" if fem else "king"]
        epithet = ""
        key = r.get("ep") or next((k for old, k in _OLD_EPITHETS.items() if old in (r.get("epithet") or "")), None)
        if key:
            epithet = " " + words("epithets")[key][0 if fem else 1]
        return f"{article} {title} {r['name']} {roman(r['n'])}{epithet}".strip()

    @staticmethod
    def epithet_key(f: dict, ruler: dict, now: float) -> str | None:
        options = []
        if f.get("rss_mb", 0) > 3000:
            options.append("greedy")
        if f.get("procs", 0) >= 25:
            options.append("prolific")
        if now - ruler["since"] > 8 * 3600:
            options.append("longlived")
        if f.get("cpu", 0) > 80:
            options.append("fiery")
        if f.get("tiles", 0) >= 120:
            options.append("conqueror")
        if not options:
            return None
        return options[zlib.crc32(ruler["name"].encode()) % len(options)]

    # -- héroes, dioses, guerras, extranjeros ---------------------------------

    def hero(self, key: str) -> dict:
        rng = rng_for(self.seed, "hero:" + key)
        name, fem = self.person("hero:" + key)
        return {"name": name, "fem": fem, "trait_key": rng.choice(sorted(words("traits"))),
                "job_idx": rng.randrange(len(words("jobs")))}

    @staticmethod
    def hero_trait(h: dict) -> str:
        key = _OLD_TRAITS.get(h.get("trait_key"), h.get("trait_key", "brave"))
        return words("traits")[key][1 if h.get("fem") else 0]

    @staticmethod
    def hero_key(h: dict) -> str:
        return _OLD_TRAITS.get(h.get("trait_key"), h.get("trait_key", "brave"))

    def hero_str(self, h: dict) -> str:
        male, fem = words("hero_article")
        return words("hero_pattern").format(name=h["name"], article=fem if h.get("fem") else male,
                                            Trait=cap(self.hero_trait(h)))

    @staticmethod
    def hero_job(h: dict) -> str:
        jobs = words("jobs")
        return jobs[h.get("job_idx", 0) % len(jobs)][1 if h.get("fem") else 0]

    def deity(self, proc_name: str) -> dict:
        rng = rng_for(self.seed, "deity:" + proc_name)
        name = _stem(proc_name)[:4] + rng.choice(["ath", "oth", "iel", "ura", "amon", "ix", "ael", "or"])
        return {"name": name, "ep_idx": rng.randrange(len(words("deity_epithets"))),
                "cult_idx": rng.randrange(len(words("cults")))}

    @staticmethod
    def deity_epithet(c: dict) -> str:
        if "ep_idx" not in c:
            return c.get("epithet", "")
        eps = words("deity_epithets")
        return eps[c["ep_idx"] % len(eps)]

    @staticmethod
    def cult_name(c: dict) -> str:
        if "cult_idx" not in c:
            return c.get("cult", c["name"])
        cults = words("cults")
        return cults[c["cult_idx"] % len(cults)].format(d=c["name"])

    def war_name(self, a: dict, b: dict, key: str, place: str | None = None) -> str:
        rng = rng_for(self.seed, "war:" + key)
        return rng.choice(words("wars")).format(noun=rng.choice(words("war_nouns")), a=a["realm"], b=b["realm"],
                                                place=place or self.place("frontera:" + key),
                                                group=rng.choice(words("war_groups")))

    def foreign_land(self, host: str) -> str:
        rng = rng_for(self.seed, "land:" + host)
        return rng.choice(_ONSET) + rng.choice(["mar", "heim", "dor", "esia", "ovia", "garth", "luna"])

    @staticmethod
    def monument(ruler: str, rng: random.Random) -> str:
        return rng.choice(words("monuments")).format(ruler=ruler)


# -- crónica -----------------------------------------------------------------

class Chronicle:
    """Crónica del reino. Cada entrada: {t, tick, kind, text, x?, y?}."""

    MAX = 300

    def __init__(self, entries: list[dict] | None, seed: int):
        self.entries = entries or []
        self.fresh: list[dict] = []      # entradas nuevas, para el archivo diario (historia del día)
        self.rng = random.Random(seed + len(self.entries))
        self._cooldown: dict[str, int] = {}

    def add(self, tick: int, kind: str, text: str, where: tuple[int, int] | None = None,
            cooldown_key: str | None = None, cooldown: int = 0, tkey: str | None = None):
        if cooldown_key:
            if tick < self._cooldown.get(cooldown_key, -1):
                return
            self._cooldown[cooldown_key] = tick + cooldown
        e = {"t": round(time.time()), "tick": tick, "kind": kind, "text": polish(text)}
        if tkey:
            e["k"] = tkey                # qué plantilla fue (para resumir el día)
        if where:
            e["x"], e["y"] = where
        self.entries.append(e)
        self.fresh.append(e)
        del self.entries[:-self.MAX]

    def take_fresh(self) -> list[dict]:
        out, self.fresh = self.fresh, []
        return out

    def tr(self, key: str, **kw) -> str:
        return tr(key, self.rng, **kw)

    def recent(self, n: int = 40) -> list[dict]:
        return self.entries[-n:]
