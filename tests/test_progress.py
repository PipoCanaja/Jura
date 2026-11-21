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
    assert p.phase_ratio == 80


def test_decode_hot_water_sequence():
    values = [
        (decode(frame(f)).current, decode(frame(f)).phase_ratio, decode(frame(f)).ratio)
        for f in HOT_WATER_70
    ]
    # phase current/max is finer than the machine percentage (30 % for 4/14)
    assert values == [(0, 0, 0), (4, 29, 30), (7, 50, 50), (14, 100, 100)]


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
    assert p.ratio == 30


# real cappuccino, 2026-11-21: grinding, heating, milk foam, coffee water, Enjoy
CAPPUCCINO = [
    ("2a 31 04 05 05 00 0a ff ff 00 1d 22 00 00 11 00 00 ff 00 00", "milk_grinding", None, 0),
    ("2a 21 04 8f 03 0a 00 00 00 00 00 00 00 00 00 00 00 00 00 00", None, 30, 30),
    ("2a 34 04 05 05 00 0a ff ff 0b 1d 22 00 00 11 00 14 ff 00 00", "milk_foam", 38, 20),
    ("2a 34 04 05 05 00 0a ff ff 1d 1d 22 00 00 11 00 32 ff 00 00", "milk_foam", 100, 50),
    ("2a 37 04 05 05 05 0a ff ff 1d 1d 22 00 00 11 00 46 ff 00 00", "milk_coffee_water", 50, 70),
    ("2a 37 04 05 05 09 0a ff ff 1d 1d 22 00 00 11 00 5a ff 00 00", "milk_coffee_water", 90, 90),
]


def test_decode_cappuccino():
    for raw, phase, phase_ratio, ratio in CAPPUCCINO:
        p = decode(frame(raw))
        assert p.preparing and p.code == 0x04, raw
        assert (p.phase, p.phase_ratio, p.ratio) == (phase, phase_ratio, ratio), raw

    # grinding: current/max is the coffee strength
    p = decode(frame(CAPPUCCINO[0][0]))
    assert (p.current, p.max, p.strength) == (5, 5, 5)


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
    device.register_finished(lambda name, code, last: finished.append(name))
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


def test_device_cappuccino_percent():
    device, finished = make_device()
    device.products.append({"@Code": "04", "@Name": "Cappuccino"})
    percents = []
    for raw, *_ in CAPPUCCINO:
        device.update_progress(frame(raw))
        percents.append(device.progress_percent())
    device.update_progress(frame("2a 3e 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"))
    # heating inside the product counts as "not started yet"
    assert percents == [0, 0, 20, 50, 70, 90]
    assert device.progress_percent() is None
    assert finished == ["Cappuccino"]


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


def test_decode_short_frames():
    # never raise on truncated frames
    for raw in [b"", b"\x2a", b"\x2a\x41", b"\x2a\x41\x0d", bytes(10)]:
        decode(raw)


# real milk foam started on the machine panel, quantity raised during the
# product (max 6 -> 19), then interrupted: p[12] stays 0
MILK_FOAM = [
    "2a 21 08 8f 05 0a 00 00 00 00 00 00 00 00 00 00 00 00 00 00",
    "2a 34 08 ff ff ff ff ff ff 00 06 ff ff ff 00 00 00 ff 00 00",
    "2a 34 08 ff ff ff ff ff ff 09 13 ff ff ff 00 00 00 ff 00 00",
    "2a 34 08 ff ff ff ff ff ff 0b 13 ff ff ff 00 00 00 ff 00 00",
]
MILK_FOAM_ENJOY = "2a 3e 08 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"


def test_decode_milk_foam_without_percent():
    ratios = [decode(frame(f)).ratio for f in MILK_FOAM[1:]]
    assert ratios == [0, 47, 58]
    assert decode(frame(MILK_FOAM[1])).phase == "milk_foam"


def test_device_interrupted_milk_foam():
    device, _ = make_device()
    device.products.append({"@Code": "08", "@Name": "Milk Foam"})
    events = []
    device.register_finished(lambda name, code, last: events.append((name, last)))
    device.update_progress(frame(ENJOY_LINGERING))
    percents = []
    for f in MILK_FOAM:
        device.update_progress(frame(f))
        percents.append(device.progress_percent())
    device.update_progress(frame(MILK_FOAM_ENJOY))
    assert percents == [0, 0, 47, 58]
    # interrupted at 58 %: the event carries the last progress seen
    assert events == [("Milk Foam", 58)]


# real milk system rinsing (CappuRinse, @TG:23), 2026-11-21: state 0x9A,
# extended frames, no Enjoy screen: the last frame is flagged invalid
CAPPU_RINSE = [
    "2a 9a 23 8f 02 0f 00 00 00 00 00 00 00 00 00 00 00 00 00 00",
    "2a 9a 23 8f 07 0f 00 00 00 00 00 00 00 00 00 00 00 00 00 00",
    "2a 9a 23 8f 0e 0f 00 00 00 00 00 00 00 00 00 00 00 00 00 00",
    "2a 9a 23 8f 0f 0f 00 00 00 00 00 00 00 00 00 00 00 00 00 00",
]
CAPPU_RINSE_END = "2a 9a 23 8f 0f 0f 00 00 00 00 00 00 00 00 00 01 00 00 00 00"


def test_device_process_finished_without_enjoy():
    device, _ = make_device()
    device.processes[0x23] = "CappuRinse"
    device.states[0x9A] = "Cappu Rinse process"
    events = []
    device.register_finished(lambda name, code, last: events.append((name, last)))
    device.update_progress(frame(ENJOY_LINGERING))
    percents = []
    for f in CAPPU_RINSE:
        device.update_progress(frame(f))
        percents.append(device.progress_percent())
    assert device.state_label() == "Cappu Rinse process"
    device.update_progress(frame(CAPPU_RINSE_END))
    device.update_progress(frame(CAPPU_RINSE_END))
    assert percents == [13, 47, 93, 100]
    assert events == [("CappuRinse", 100)]
    assert device.state_label() == "idle"


# real milk system cleaning (CappuClean, @TG:21), 2026-11-21, started from
# the machine menu (state 0xFF "P_Mode" frames first)
P_MODE_MENU = [
    "2a ff 55 00 00 00 00 01 02 6d 6e 63 00 64 00 00 00 00 00 00",
    "2a ff 50 00 70 01 03 66 75 00 08 48 77 00 ff 00 00 00 00 00",
]
CAPPU_CLEAN = [
    ("2a 90 21 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00", "Cappu Clean Start"),
    ("2a 92 21 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00", "Cappu Clean add cleaner"),
    ("2a 21 21 8f 01 0a 00 00 00 00 00 00 00 00 00 00 00 00 00 00", "Heating Up"),
    ("2a 93 21 8f 02 0f 00 00 00 00 00 00 00 00 00 00 00 00 00 00", "Cappu Clean process"),
    ("2a 93 21 8f 06 0f 00 00 00 00 00 00 00 00 00 00 00 00 00 00", "Cappu Clean process"),
    ("2a 94 21 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00", "Cappu Clean add water"),
    ("2a 93 21 8f 0a 0f 00 00 00 00 00 00 00 00 00 00 00 00 00 00", "Cappu Clean process"),
]


def test_p_mode_menu_is_not_preparing():
    device, _ = make_device()
    for f in P_MODE_MENU:
        device.update_progress(frame(f))
        assert not device.progress.preparing
        assert device.progress_percent() is None


def test_device_cappu_clean_sequence():
    device, _ = make_device()
    device.processes[0x21] = "CappuClean"
    device.states.update(
        {0x21: "Heating Up", 0x90: "Cappu Clean Start", 0x92: "Cappu Clean add cleaner",
         0x93: "Cappu Clean process", 0x94: "Cappu Clean add water"}
    )
    events = []
    device.register_finished(lambda name, code, last: events.append((name, last)))
    device.update_progress(frame(IDLE))
    for f in P_MODE_MENU:
        device.update_progress(frame(f))
    for f, label in CAPPU_CLEAN:
        device.update_progress(frame(f))
        assert device.progress.preparing
        assert device.state_label() == label
    assert device.progress_percent() == 67
    # the machine dropped the connection at the end of the cleaning; the
    # next frame read after reconnecting is idle
    device.update_progress(frame(IDLE))
    assert events == [("CappuClean", 67)]
