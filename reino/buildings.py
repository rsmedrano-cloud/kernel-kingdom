"""Tipos de edificio de la grilla `bld` (compartido por daemon y renderer)."""

(NONE, HOUSE, STREET, PLAZA, MARKET, TEMPLE, BARRACKS, WORKSHOP, BARN, FARM, PASTURE, LUMBER,
 DOCK, WELL, GARDEN, LIBRARY, TAVERN, GRAVEYARD, WATCHTOWER, MONUMENT, PALACE) = range(21)

NAMES = {
    NONE: "", HOUSE: "casa", STREET: "calle", PLAZA: "plaza", MARKET: "mercado", TEMPLE: "templo",
    BARRACKS: "cuartel", WORKSHOP: "taller", BARN: "granero", FARM: "granja", PASTURE: "pastura",
    LUMBER: "aserradero", DOCK: "muelle", WELL: "pozo", GARDEN: "jardín", LIBRARY: "biblioteca",
    TAVERN: "taberna", GRAVEYARD: "cementerio", WATCHTOWER: "atalaya", MONUMENT: "monumento",
    PALACE: "palacio",
}
# artículo indefinido para la crónica
ARTICLE = {MARKET: "un", TEMPLE: "un", BARRACKS: "un", WORKSHOP: "un", BARN: "un", LUMBER: "un",
           DOCK: "un", LIBRARY: "una", TAVERN: "una", GRAVEYARD: "un", WATCHTOWER: "una",
           MONUMENT: "un", WELL: "un", GARDEN: "un", PLAZA: "una"}

# Edificios únicos de una ciudad y desde cuántas tierras aparecen
SPECIALS = [(MARKET, 18), (TAVERN, 28), (TEMPLE, 40), (BARRACKS, 60), (LIBRARY, 110)]
WORKPLACES = {WORKSHOP, MARKET, LUMBER, DOCK, BARN, TAVERN, LIBRARY}
