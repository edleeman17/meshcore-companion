"""Mesh topology from heard paths: which repeaters relay to which.

Every flood packet's path lists the repeaters it went through, in order, so two
neighbouring hashes are two repeaters that heard each other. Hashes are 1–2 byte
key prefixes and collide, so each hop is resolved to the located node with that
prefix nearest the next point back along the path (the last hop is near us),
preferring repeaters and rooms. Hops we can't place break the chain: no edge is
drawn across a gap.
"""
import math

MAX_HOP_KM = 150   # LoRa links longer than this are a wrong guess, not a real hop


def km(a: tuple, b: tuple) -> float:
    r = math.pi / 180
    x = (b[1] - a[1]) * r * math.cos((a[0] + b[0]) / 2 * r)
    y = (b[0] - a[0]) * r
    return math.hypot(x, y) * 6371


def build(packets: list[dict], heard: list[dict], me: tuple | None, top: int = 600) -> dict:
    located = [h for h in heard if h.get("lat") or h.get("lon")]
    by_prefix: dict[str, list[dict]] = {}
    for h in located:
        for size in (1, 2):
            by_prefix.setdefault(h["pubkey"][:2 * size], []).append(h)

    def resolve(prefix: str, near: tuple | None):
        cands = by_prefix.get(prefix, [])
        pool = [c for c in cands if c.get("type") in (2, 3)] or cands
        if not pool:
            return None
        if near is None:
            return pool[0] if len(pool) == 1 else None
        best = min(pool, key=lambda c: km(near, (c["lat"], c["lon"])))
        return best if km(near, (best["lat"], best["lon"])) <= MAX_HOP_KM else None

    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def node(h):
        n = nodes.setdefault(h["pubkey"], {"key": h["pubkey"], "name": h.get("name") or h["pubkey"][:8],
                                           "type": h.get("type"), "lat": h["lat"], "lon": h["lon"], "relayed": 0})
        return n

    def edge(a: str, b: str, ts: int, snr=None):
        k = tuple(sorted((a, b)))
        e = edges.setdefault(k, {"a": k[0], "b": k[1], "count": 0, "last": 0, "snr": []})
        e["count"] += 1
        e["last"] = max(e["last"], ts)
        if snr is not None and len(e["snr"]) < 200:
            e["snr"].append(snr)

    for p in packets:
        path, n = p.get("path") or "", p.get("path_len") or 0
        if not path or not n or p.get("ptype") == "TRACE":
            continue
        size = max(1, len(path) // (2 * n))
        hops = [path[i:i + 2 * size] for i in range(0, len(path), 2 * size)]
        near, prev_key = me, "me"   # walk back from us towards the sender
        for i, h in enumerate(reversed(hops)):
            r = resolve(h, near)
            if not r:
                near, prev_key = None, None
                continue
            node(r)["relayed"] += 1
            if prev_key:
                # the last hop -> us carries the SNR we measured
                edge(r["pubkey"], prev_key, p["ts"], p.get("snr") if prev_key == "me" else None)
            near, prev_key = (r["lat"], r["lon"]), r["pubkey"]

    out_edges = []
    for e in edges.values():
        s = e.pop("snr")
        e["snr"] = round(sum(s) / len(s), 1) if s else None
        out_edges.append(e)
    out_edges.sort(key=lambda e: -e["count"])
    total = len(out_edges)
    # the busiest links are the backbone; the long tail is mostly noise and too
    # much for a phone to draw. Always keep every link to us.
    out_edges = [e for i, e in enumerate(out_edges) if i < top or "me" in (e["a"], e["b"])]
    used = {k for e in out_edges for k in (e["a"], e["b"])}
    return {"me": {"key": "me", "lat": me[0], "lon": me[1]} if me else None,
            "nodes": [n for n in nodes.values() if n["key"] in used], "edges": out_edges, "total_edges": total}
