import asyncio

from custom_components.jura.core import client as client_module
from custom_components.jura.core.client import Client, UUIDs
from custom_components.jura.core.debug import Tracer, resolve_uuid
from custom_components.jura.core.encryption import encdec

from .test_client import KEY, FakeBleakClient, make_client


def test_resolve_uuid():
    full = "5a401527-ab2e-2548-c435-08c300000710"
    assert resolve_uuid("27") == full
    assert resolve_uuid("1527") == full
    assert resolve_uuid("5A401527") == full
    assert resolve_uuid(full.upper()) == full


def test_tracer_records_frames():
    tracer = Tracer()
    calls = []
    tracer.listeners.append(lambda: calls.append(1))
    tracer.recording = []

    tracer.add("R", UUIDs.PROGRESS, b"\x2a\x3e\x03", b"\x77\x00\x00")
    tracer.add("W", UUIDs.P_MODE, b"\x2a\x47\xff", b"\x00", RuntimeError("boom"))

    assert tracer.last["1527"]["plain"] == "2a 3e 03"
    assert tracer.last["1527"]["name"] == "PROGRESS"
    assert "1529" not in tracer.last  # failed frames are not "last"
    assert tracer.frames[-1]["error"] == "RuntimeError: boom"
    assert len(tracer.recording) == 2
    assert len(calls) == 2


def test_trace_and_poll(monkeypatch):
    progress = bytes([KEY, 0x3E, 0x03, 0x00])
    fake = FakeBleakClient(reads={UUIDs.PROGRESS: progress})

    async def main():
        client = make_client(fake, monkeypatch)
        client.tracer = Tracer()
        client.poll = {UUIDs.PROGRESS: 0}
        # raw write: sent as is, byte 0 not replaced
        await client.write(b"\x01\x02", UUIDs.P_MODE, encrypt=False)
        await asyncio.sleep(0.05)
        client.ping_cancel()
        await client.ping_task if client.ping_task else None
        return client.tracer

    tracer = run_loop(main())
    assert tracer.last["1527"]["plain"] == progress.hex(" ")
    raw_writes = [f for f in tracer.frames if f["dir"] == "W" and f["plain"] == "01 02"]
    assert raw_writes and raw_writes[0]["raw"] == "01 02"
    # heartbeat is traced too
    assert any(f["plain"] == f"{KEY:02x} 7f 80" for f in tracer.frames)


def run_loop(coro):
    return asyncio.run(coro)
