"""Advert positions are self-reported and sometimes nonsense: longitude 2081, a
"Santa" at the pole, 0,0 from a node with no GPS fix. Everything the bridge hands
out goes through ok_pos, so maps, routes and hop guesses never use them."""


def ok_pos(lat, lon) -> bool:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return abs(lat) <= 85 and abs(lon) <= 180 and not (abs(lat) < .01 and abs(lon) < .01)


def clean(d: dict, lat: str = "lat", lon: str = "lon") -> dict:
    """Zero an impossible position in place (0,0 = "no location" to the app)."""
    if (d.get(lat) or d.get(lon)) and not ok_pos(d.get(lat), d.get(lon)):
        d[lat] = d[lon] = 0.0
    return d
