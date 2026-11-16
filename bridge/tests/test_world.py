import io
import json
import time

from app import world


def _resp(objs, chunk_text=None):
    data = json.dumps(objs).encode()
    return io.BytesIO(data)


def test_stream_parser_survives_tiny_chunks():
    objs = [{"a": i, "s": "x" * (i % 7), "emoji": "📡"} for i in range(200)]
    got = list(world._stream_objects(_resp(objs), chunk=7))   # objects split across many reads
    assert got == objs


def test_fetch_keeps_recent_infrastructure_with_real_positions(tmp_path, monkeypatch):
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    nodes = [
        {"public_key": "aa" * 32, "type": 2, "adv_name": "Hill", "adv_lat": 50.9, "adv_lon": -1.1, "last_advert": now,
         "params": {"freq": 869.618, "bw": 62.5, "sf": 8}},
        {"public_key": "bb" * 32, "type": 1, "adv_name": "Person", "adv_lat": 50.9, "adv_lon": -1.1, "last_advert": now},
        {"public_key": "cc" * 32, "type": 2, "adv_name": "Bogus", "adv_lat": 85.7, "adv_lon": 2081.0, "last_advert": now},
        {"public_key": "dd" * 32, "type": 2, "adv_name": "Old", "adv_lat": 51, "adv_lon": 0, "last_advert": "2020-01-01T00:00:00.000Z"},
        {"public_key": "ee" * 32, "type": 2, "adv_name": "Other preset", "adv_lat": 50.9, "adv_lon": -1.0, "last_advert": now,
         "params": {"freq": 869.525, "bw": 250, "sf": 11}},
    ]
    monkeypatch.setattr(world.urllib.request, "urlopen", lambda *a, **k: _ctx(nodes))
    world._cache.update(ts=0, nodes=[], error=None)
    out = world.nodes(tmp_path, (50.85, -1.0), {"freq": 869.618, "bw": 62.5, "sf": 8})
    assert [n["name"] for n in out["nodes"]] == ["Hill"]
    assert (tmp_path / "world-nodes.json").exists()


class _ctx(io.BytesIO):
    def __init__(self, objs):
        super().__init__(json.dumps(objs).encode())
