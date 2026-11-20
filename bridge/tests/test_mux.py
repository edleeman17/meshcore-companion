import asyncio
import logging

import mux


def test_client_dropped_when_radio_down(monkeypatch):
    """Radio down + client hangs up mid-command: the mux must notice and close it
    (it used to wait forever on the radio and leak the socket, 2026-11-19)."""
    monkeypatch.setattr(mux, "REPLY_IDLE", 0.3)

    async def run():
        m = mux.Mux("127.0.0.1:1", "127.0.0.1:0")       # radio_loop not started: radio stays down
        server = await asyncio.start_server(m.on_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        for _ in range(3):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(mux.frame(b"<", bytes([mux.GET_CONTACTS])))
            await w.drain()
            await asyncio.sleep(0.05)
            w.close()
        await asyncio.sleep(1.0)
        server.close()
        return len(m.clients), m.owner

    clients, owner = asyncio.run(run())
    assert clients == 0
    assert owner is None


def test_repeat_filter_caps_identical_lines():
    f = mux.RepeatFilter(every=60)
    rec = lambda: logging.LogRecord("asyncio", logging.ERROR, "", 0, "accept failed", None, None)
    passed = sum(f.filter(rec()) for _ in range(1000))
    assert passed == 1
    other = logging.LogRecord("mux", logging.INFO, "", 0, "client gone", None, None)
    assert f.filter(other)
