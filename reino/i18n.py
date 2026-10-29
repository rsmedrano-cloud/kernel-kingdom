"""Textos en varios idiomas. `language = "es" | "en"` en config.toml.

Cada locale (reino/locales/<código>.py) define TEXT: clave -> plantilla o
lista de variantes (se elige una al azar), y WORDS: vocabulario estructurado
(títulos, epítetos, nombres de edificios...). La crónica se escribe en el
idioma activo cuando ocurre cada suceso.
"""
import importlib
import random

_LOCALES: dict[str, object] = {}
LANG = "es"


def _locale(code: str):
    if code not in _LOCALES:
        _LOCALES[code] = importlib.import_module(f"reino.locales.{code}")
    return _LOCALES[code]


def set_lang(code: str):
    global LANG
    try:
        _locale(code)
        LANG = code
    except ModuleNotFoundError:
        LANG = "es"


def tr(key: str, rng: random.Random | None = None, **kw) -> str:
    """Plantilla traducida y formateada; si hay variantes elige una."""
    text = _locale(LANG).TEXT.get(key)
    if text is None:
        text = _locale("es").TEXT[key]
    if isinstance(text, (list, tuple)):
        text = (rng or random).choice(text)
    return text.format(**kw)


def words(key: str):
    """Vocabulario estructurado del idioma activo (listas, dicts)."""
    w = _locale(LANG).WORDS
    return w[key] if key in w else _locale("es").WORDS[key]


def polish(text: str) -> str:
    """Retoques gramaticales del idioma (contracciones del castellano)."""
    if LANG == "es":
        text = text.replace(" a el ", " al ").replace(" de el ", " del ")
    return text


def ui(key: str, **kw):
    """Textos de interfaz (HUD, fichas, poderes)."""
    table = _locale(LANG).UI
    text = table[key] if key in table else _locale("es").UI[key]
    return text.format(**kw) if kw and isinstance(text, str) else text


def raw(key: str):
    """Entrada del catálogo TEXT sin formatear (para estructuras como story.count)."""
    text = _locale(LANG).TEXT.get(key)
    return text if text is not None else _locale("es").TEXT[key]
