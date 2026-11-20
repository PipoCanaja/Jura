import asyncio

from custom_components.jura.core.device import Device, get_processes, get_states
from custom_components.jura.core.progress import STATE_ENJOY, decode

# real frames (decrypted), Jura machine, 2026-11-20
IDLE = "2a 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00"
HOT_WATER_25 = "2a 41 0d ff ff 04 05 ff ff ff ff 00 ff ff ff 00 50 ff 00 00"
HOT_WATER_70 = [
    "2a 41 0d ff ff 00 0e ff ff ff ff 00 ff ff ff 00 00 ff 00 00",
    "2a 41 0d ff ff 04 0e ff ff ff ff 00 ff ff ff 00 1e ff 00 00",
    "2a 41 0d ff ff 07 0e ff ff ff ff 00 ff ff ff 00 32 ff 00 00",
    "2a 41 0d ff ff 0e 0e ff ff ff ff 00 ff ff ff 00 64 ff 00 00",
]
ENJOY = "2a 3e 0d 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
ENJOY_LINGERING = "2a 3e 0d 00 00 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00"


def frame(text: str) -> bytes:
    return bytes.fromhex(text)


def test_decode_idle():
    p = decode(frame(IDLE))
    assert p.state == 0 and p.invalid
    assert not p.preparing
    assert p.ratio is None


def test_decode_hot_water():
    p = decode(frame(HOT_WATER_25))
    assert p.state == 0x41 and p.code == 0x0D
    assert not p.invalid and not p.extended
    assert p.phase == "hot_water"
    assert (p.current, p.max, p.percent) == (4, 5, 80)
    assert p.preparing
    assert p.ratio == 80


def test_decode_hot_water_sequence():
    values = [(decode(frame(f)).current, decode(frame(f)).ratio) for f in HOT_WATER_70]
    # current/max is finer than the machine percentage (30 % for 4/14)
    assert values == [(0, 0), (4, 29), (7, 50), (14, 100)]
    assert decode(frame(HOT_WATER_70[1])).percent == 30


def test_decode_enjoy():
    p = decode(frame(ENJOY))
    assert p.state == STATE_ENJOY and not p.invalid
    assert p.finished and not p.preparing

    p = decode(frame(ENJOY_LINGERING))
    assert p.state == STATE_ENJOY and p.invalid


def test_decode_bypass():
    # same as hot water but p[6] set: bypass water
    p = decode(frame("2a 41 0d ff ff 04 05 ff ff 02 08 00 ff ff ff 00 50 ff 00 00"))
    assert p.phase == "bypass_water"
    assert (p.current, p.max) == (2, 8)


def test_decode_extended():
    # process frame: 0x8F marker, current/max in p[0]/p[1]
    p = decode(frame("2a 74 24 8f 03 0a 00 00 00 00 00 00 00 00 00 00 00 00 00 00"))
    assert p.extended and p.phase is None
    assert (p.current, p.max) == (3, 10)


def make_device() -> tuple[Device, list]:
    asyncio.get_running_loop = lambda: None

    class BLE:
        address = "00:11:22:33:44:55"

    device = Device(
        "Jura",
        "test",
        [{"@Code": "0D", "@Name": "Hotwater Portion"}],
        {},
        0x2A,
        BLE(),
        states={0x3E: "Enjoy", 0x21: "Heating Up"},
        processes={0x24: "Cleaning"},
    )
    device.client.ping = lambda *args: None
    finished = []
    device.register_finished(lambda name, code: finished.append(name))
    return device, finished


def test_device_finished_once():
    device, finished = make_device()
    for f in [ENJOY_LINGERING, *HOT_WATER_70, ENJOY, ENJOY_LINGERING, ENJOY_LINGERING]:
        device.update_progress(frame(f))
    assert finished == ["Hotwater Portion"]
    assert device.state_label() == "idle"


def test_device_finished_missed_frames():
    # product too short to see a preparing frame: lingering -> valid Enjoy
    device, finished = make_device()
    for f in [ENJOY_LINGERING, ENJOY, ENJOY_LINGERING]:
        device.update_progress(frame(f))
    assert finished == ["Hotwater Portion"]


def test_device_no_event_on_startup():
    device, finished = make_device()
    device.update_progress(frame(ENJOY))
    assert finished == []


def test_device_tracking_and_labels():
    device, _ = make_device()
    assert not device.tracking
    device.update_progress(frame(HOT_WATER_25))
    assert device.tracking
    assert device.state_label() == "hot_water"
    device.update_progress(frame(ENJOY))
    assert device.state_label() == "Enjoy"


def test_xml_helpers():
    joe = {
        "PROGRESS_STATE_INTAKE": {
            "ENJOYSCREEN": {"@Value": "3E", "@Name": "Enjoy"},
            "STATE": [
                {"@Value": "21", "@Name": "Heating Up"},
                {"@Value": "FF", "@Name": "P_Mode "},
            ],
        },
        "PROCESSES": {
            "PROCESS": [
                {"@Type": "Cleaning", "@ExecuteCommand": "@TG:24"},
                {"@Type": "Decalc", "@ExecuteCommand": "@TG:25"},
            ]
        },
    }
    assert get_states(joe) == {0x3E: "Enjoy", 0x21: "Heating Up", 0xFF: "P_Mode"}
    assert get_processes(joe) == {0x24: "Cleaning", 0x25: "Decalc"}
    assert get_states({}) == {} and get_processes({}) == {}
