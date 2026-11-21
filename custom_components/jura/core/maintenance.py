"""Maintenance statistics: counters (type 4) and percentages (type 8).

From the J.O.E. app (MaintenanceCounterStatisticsParser,
MaintenanceStatusStatisticsParser): each process type has a fixed position
in the statistics data,
- counters: 2 bytes (big endian) per position,
- percentages: 1 byte per position, 0xFF = not available.

Which process types a machine shows comes from its XML:
STATISTIC / MAINTENANCEPAGE / BANK @TG:43 (counters) and @TG:C0 (percentages).
"""

POSITIONS = {
    "Cleaning": 0,
    "FilterChange": 1,
    "Decalc": 2,
    "CappuRinse": 3,
    "CoffeeRinse": 4,
    "CappuClean": 5,
}

NAMES = {
    "Cleaning": "Cleaning",
    "FilterChange": "Filter Change",
    "Decalc": "Descaling",
    "CappuRinse": "Milk System Rinsing",
    "CoffeeRinse": "Coffee Rinsing",
    "CappuClean": "Milk System Cleaning",
}

BANK_COUNTERS = "@TG:43"
BANK_PERCENT = "@TG:C0"


def as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def get_maintenance(joe: dict) -> dict[str, list[str]]:
    """Process types listed by the machine XML for each maintenance bank."""
    result = {"counters": [], "percent": []}
    # JOE / STATISTIC / MAINTENANCEPAGE (fallback: directly under JOE)
    page = (joe.get("STATISTIC") or {}).get("MAINTENANCEPAGE") or joe.get(
        "MAINTENANCEPAGE"
    ) or {}
    for bank in as_list(page.get("BANK")):
        command = (bank.get("@Command") or "").upper()
        name = (bank.get("@Name") or "").lower()
        if command == BANK_COUNTERS or name == "maintenance counter":
            key = "counters"
        elif command == BANK_PERCENT or name == "maintenance percent":
            key = "percent"
        else:
            continue
        for item in as_list(bank.get("TEXTITEM")):
            kind = item.get("@Type")
            if kind in POSITIONS:
                result[key].append(kind)
    return result


def parse_counters(data: bytes, kinds: list[str]) -> dict[str, int]:
    counters = {}
    for kind in kinds:
        pos = POSITIONS[kind] * 2
        if pos + 2 > len(data):
            continue
        value = int.from_bytes(data[pos : pos + 2], "big")
        if value != 0xFFFF:
            counters[kind] = value
    return counters


def parse_percent(data: bytes, kinds: list[str]) -> dict[str, int]:
    percent = {}
    for kind in kinds:
        pos = POSITIONS[kind]
        if pos >= len(data):
            continue
        value = data[pos]
        if value != 0xFF:
            percent[kind] = value
    return percent
