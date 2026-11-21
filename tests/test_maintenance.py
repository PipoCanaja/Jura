from custom_components.jura.core.client import stats_request
from custom_components.jura.core.maintenance import (
    get_maintenance,
    parse_counters,
    parse_percent,
)

JOE = {
    "STATISTIC": {
    "MAINTENANCEPAGE": {
        "BANK": [
            {
                "@Command": "@TG:43",
                "@Name": "Maintenance Counter",
                "TEXTITEM": [
                    {"@Type": "Cleaning", "@Text": "33"},
                    {"@Type": "FilterChange", "@Text": "34"},
                    {"@Type": "Decalc", "@Text": "35"},
                    {"@Type": "CappuRinse", "@Text": "40"},
                    {"@Type": "CoffeeRinse", "@Text": "36"},
                    {"@Type": "CappuClean", "@Text": "41"},
                ],
            },
            {
                "@Command": "@TG:C0",
                "@Name": "Maintenance Percent",
                "TEXTITEM": [
                    {"@Type": "Cleaning", "@Text": "37"},
                    {"@Type": "FilterChange", "@Text": "39"},
                    {"@Type": "Decalc", "@Text": "38"},
                ],
            },
        ]
    }
    }
}


def test_stats_request():
    # byte 0 is replaced by the key when sent
    assert stats_request(1) == bytes([0x00, 0x00, 0x01, 0xFF, 0xFF])
    assert stats_request(4) == bytes([0x00, 0x00, 0x04, 0x01, 0x00])
    assert stats_request(8) == bytes([0x00, 0x00, 0x08, 0x01, 0x00])


def test_get_maintenance():
    m = get_maintenance(JOE)
    assert m["counters"] == [
        "Cleaning",
        "FilterChange",
        "Decalc",
        "CappuRinse",
        "CoffeeRinse",
        "CappuClean",
    ]
    assert m["percent"] == ["Cleaning", "FilterChange", "Decalc"]
    assert get_maintenance({}) == {"counters": [], "percent": []}


def test_parse_counters():
    # Cleaning 12, FilterChange n/a, Decalc 3, CappuRinse 300, CoffeeRinse 0
    data = bytes.fromhex("000c ffff 0003 012c 0000 0005")
    kinds = get_maintenance(JOE)["counters"]
    assert parse_counters(data, kinds) == {
        "Cleaning": 12,
        "Decalc": 3,
        "CappuRinse": 300,
        "CoffeeRinse": 0,
        "CappuClean": 5,
    }
    # short frame: missing positions are skipped
    assert parse_counters(bytes.fromhex("000c"), kinds) == {"Cleaning": 12}


def test_parse_percent():
    data = bytes.fromhex("28 ff 5a 00 00 00")
    assert parse_percent(data, ["Cleaning", "FilterChange", "Decalc"]) == {
        "Cleaning": 40,
        "Decalc": 90,
    }


def test_parse_percent_real_frame():
    # real machine 2026-11-21 (water filter in use, so no descaling): Cleaning
    # 0x50, FilterChange 0x32, Decalc 0xFF, then zeros
    data = bytes.fromhex("50 32 ff" + " 00" * 197)
    kinds = ["Cleaning", "FilterChange", "Decalc"]
    assert parse_percent(data, kinds) == {"Cleaning": 80, "FilterChange": 50}
