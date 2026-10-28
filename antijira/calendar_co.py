"""Calendario nacional colombiano; jornadas completas y sábados de media jornada."""
from datetime import date, timedelta
from functools import lru_cache


def easter(year):
    # Algoritmo gregoriano de Meeus/Jones/Butcher.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=100)
def holidays(year):
    """Ley 51/1983: fijos, trasladables y fiestas relativas a Pascua."""
    result = {date(year, m, d) for m, d in ((1, 1), (5, 1), (7, 20), (8, 7), (12, 8), (12, 25))}
    for m, d in ((1, 6), (3, 19), (6, 29), (8, 15), (10, 12), (11, 1), (11, 11)):
        day = date(year, m, d)
        result.add(day + timedelta(days=(-day.weekday()) % 7))
    sunday = easter(year)
    result.update(sunday + timedelta(days=offset) for offset in (-3, -2, 43, 64, 71))
    return frozenset(result)


def business_end(start, days, saturdays=False):
    """Incluye el inicio. Dos unidades por día; sábado opcional aporta una."""
    current = date.fromisoformat(start)
    remaining = days * 2
    while remaining > 0:
        if current not in holidays(current.year):
            remaining -= 2 if current.weekday() < 5 else int(saturdays and current.weekday() == 5)
        if remaining > 0:
            current += timedelta(days=1)
    return current.isoformat()
