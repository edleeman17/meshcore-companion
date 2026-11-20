# The community channel is joined once per install, and stays left if you leave it.
import asyncio

import pytest

radio_mod = pytest.importorskip("app.radio")
from app.db import DB  # noqa: E402


class Ev:
    type = None
    payload = {}


class FakeCommands:
    def __init__(self): self.set = []
    async def set_channel(self, idx, name, secret):
        self.set.append((idx, name)); return Ev()


class FakeMC:
    def __init__(self): self.commands = FakeCommands()


def make(tmp_path, channels):
    r = radio_mod.Radio.__new__(radio_mod.Radio)
    r.mc, r.db, r.channels, r.max_channels = FakeMC(), DB(tmp_path / "t.db"), dict(channels), 8
    return r


def test_joins_once_into_free_slot(tmp_path, monkeypatch):
    monkeypatch.delenv("DEFAULT_CHANNELS", raising=False)
    r = make(tmp_path, {0: "Public", 1: "#solent"})
    asyncio.run(r._join_default_channels())
    assert r.mc.commands.set == [(2, "#meshcore-companion")] and r.channels[2] == "#meshcore-companion"
    r.channels.pop(2)                       # user leaves it
    asyncio.run(r._join_default_channels())
    assert len(r.mc.commands.set) == 1      # not re-added


def test_already_joined_or_disabled(tmp_path, monkeypatch):
    r = make(tmp_path, {0: "Public", 3: "#MeshCore-Companion"})
    asyncio.run(r._join_default_channels())
    assert r.mc.commands.set == []
    monkeypatch.setenv("DEFAULT_CHANNELS", "")
    r2 = make(tmp_path / "b", {0: "Public"})
    asyncio.run(r2._join_default_channels())
    assert r2.mc.commands.set == []
