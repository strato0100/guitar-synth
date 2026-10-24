"""Nombre de acorde a partir de un conjunto de notas MIDI."""

from .dsp import NOTE_NAMES

# (sufijo, intervalos desde la raíz). El orden marca la preferencia cuando
# un mismo conjunto de notas admite varios nombres.
CHORD_TYPES = [
    ("", (0, 4, 7)),
    ("m", (0, 3, 7)),
    ("5", (0, 7)),
    ("7", (0, 4, 7, 10)),
    ("m7", (0, 3, 7, 10)),
    ("maj7", (0, 4, 7, 11)),
    ("sus4", (0, 5, 7)),
    ("sus2", (0, 2, 7)),
    ("6", (0, 4, 7, 9)),
    ("m6", (0, 3, 7, 9)),
    ("add9", (0, 2, 4, 7)),
    ("madd9", (0, 2, 3, 7)),
    ("7sus4", (0, 5, 7, 10)),
    ("9", (0, 2, 4, 7, 10)),
    ("m9", (0, 2, 3, 7, 10)),
    ("maj9", (0, 2, 4, 7, 11)),
    ("dim", (0, 3, 6)),
    ("m7b5", (0, 3, 6, 10)),
    ("dim7", (0, 3, 6, 9)),
    ("aug", (0, 4, 8)),
    ("mMaj7", (0, 3, 7, 11)),
]


def _build_templates():
    templates = [(suffix, frozenset(iv)) for suffix, iv in CHORD_TYPES]
    # En la guitarra es habitual omitir la quinta en acordes de 4+ notas
    templates += [(suffix, frozenset(iv) - {7}) for suffix, iv in CHORD_TYPES
                  if len(iv) >= 4 and 7 in iv]
    return templates


CHORD_TEMPLATES = _build_templates()


def _match(pcs, bass):
    matches = []
    for root in pcs:
        relative = frozenset((p - root) % 12 for p in pcs)
        for rank, (suffix, intervals) in enumerate(CHORD_TEMPLATES):
            if relative == intervals:
                # Preferir la raíz en el bajo, luego el orden de la tabla
                matches.append((root != bass, rank, root, suffix))
    if not matches:
        return None
    _, _, root, suffix = min(matches)
    name = NOTE_NAMES[root] + suffix
    if root != bass:
        name += "/" + NOTE_NAMES[bass]
    return name, root


def identify_chord(notes):
    """
    Devuelve {"name", "root", "exact"} para una lista de notas MIDI,
    o None si no se reconoce ningún acorde.
    """
    if not notes:
        return None
    bass = min(notes) % 12
    pcs = frozenset(n % 12 for n in notes)

    if len(pcs) == 1:
        return {"name": NOTE_NAMES[bass], "root": bass, "exact": True}

    result = _match(pcs, bass)
    if result:
        return {"name": result[0], "root": result[1], "exact": True}

    # Aproximación: probar quitando una nota (posible falso positivo)
    if len(pcs) >= 3:
        for pc in sorted(pcs - {bass}):
            result = _match(pcs - {pc}, bass)
            if result:
                return {"name": result[0], "root": result[1], "exact": False}
    return None
