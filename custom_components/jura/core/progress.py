"""Decoder for the product progress characteristic (5a401527).

Layout, after decryption (reverse engineered from the J.O.E. app and checked
on a real machine):

    raw[0]   key
    raw[1]   state      (see PROGRESS_STATE_INTAKE in the machine XML)
    raw[2]   code       product code (PRODUCT@Code) or process code (@TG:xx)
    raw[3]   0x8F when the frame is "extended" (processes), else payload start
    raw[15]  0x01 = progress invalid (idle, or the lingering Enjoy screen)
    payload  bytes after state/code (and after 0x8F), with raw[15] removed:
             p[cur] / p[max] depend on the phase (PHASES below)
             p[12] = progress of the whole product (all phases), by steps
                     of 10 (cappuccino: 0-50 % milk, 50-90 % coffee); stays 0
                     for some single-phase products (milk foam)

Grinding phases (0x31, 0x39) carry the coffee strength (actual / max) in
p[0] / p[1], not a progress. Extended frames (0x8F) carry current / max of a
process, or of the heating step in the middle of a product (state 0x21).
"""

from dataclasses import dataclass, field

STATE_IDLE = 0x00
STATE_P_MODE = 0xFF
STATE_ENJOY = 0x3E
EXTENDED = 0x8F
NO_VALUE = 0xFF

# state: (phase, payload index of current value, payload index of max value)
# from the J.O.E. app (Progress.mapStateToProductProgressState)
PHASES = {
    0x19: ("smart_alert_pause", 0, 1),
    0x31: ("milk_grinding", 0, 1),
    0x32: ("milk", 4, 5),
    0x33: ("milk_pause", 9, 10),
    0x34: ("milk_foam", 6, 7),
    0x37: ("milk_coffee_water", 2, 3),
    0x39: ("grinding", 0, 1),
    0x3C: ("coffee_water", 2, 3),
    0x40: ("hot_water_temperature", 8, 8),
    0x41: ("hot_water", 2, 3),  # bypass when p[6] != 0xFF
    0x43: ("steam", 6, 7),
}
BYPASS = ("bypass_water", 6, 7)

# phases where current / max is the coffee strength, not a progress
STRENGTH_PHASES = {0x31, 0x39}

PHASE_NAMES = sorted({v[0] for v in PHASES.values()} | {BYPASS[0]})


@dataclass
class Progress:
    raw: bytes
    state: int = STATE_IDLE
    code: int = 0
    invalid: bool = True
    extended: bool = False
    payload: bytes = field(default=b"", repr=False)
    phase: str | None = None
    current: int | None = None
    max: int | None = None
    percent: int | None = None

    @property
    def preparing(self) -> bool:
        """A product or a process is running."""
        # 0xFF = "P_Mode": the machine shows a menu (e.g. the maintenance
        # menu before a cleaning), nothing is being prepared
        return not self.invalid and self.state not in (
            STATE_IDLE,
            STATE_ENJOY,
            STATE_P_MODE,
        )

    @property
    def finished(self) -> bool:
        return self.state == STATE_ENJOY

    @property
    def strength(self) -> int | None:
        """Coffee strength during grinding phases."""
        return self.current if self.state in STRENGTH_PHASES else None

    @property
    def phase_ratio(self) -> int | None:
        """Progress of the current phase or process step, from current / max."""
        if self.state in STRENGTH_PHASES:
            return None
        if self.current is not None and self.max:
            return min(100, round(self.current * 100 / self.max))
        return None

    @property
    def ratio(self) -> int | None:
        """Overall progress: p[12] for products, current / max for extended
        frames (processes) and for products without p[12] (milk foam alone
        keeps p[12] at 0 during the whole product)."""
        if self.extended:
            return self.phase_ratio
        if self.percent:
            return self.percent
        phase = self.phase_ratio
        return phase if phase is not None else self.percent


def _get(payload: bytes, index: int) -> int | None:
    if index >= len(payload):
        return None
    value = payload[index]
    return None if value == NO_VALUE else value


def decode(raw: bytes | bytearray) -> Progress:
    raw = bytes(raw)
    if len(raw) < 3:
        return Progress(raw=raw)

    body = raw[1:]  # drop the key
    invalid = len(body) > 14 and body[14] == 0x01
    if len(body) > 14:
        body = body[:14] + body[15:]

    state, code = body[0], body[1]
    extended = len(body) > 2 and body[2] == EXTENDED
    payload = body[3:] if extended else body[2:]

    progress = Progress(
        raw=raw,
        state=state,
        code=code,
        invalid=invalid,
        extended=extended,
        payload=payload,
    )
    if invalid:
        return progress

    phase = PHASES.get(state)
    if phase and state == 0x41 and _get(payload, 6) is not None:
        phase = BYPASS

    if phase:
        progress.phase, i_cur, i_max = phase
        progress.current = _get(payload, i_cur)
        progress.max = _get(payload, i_max)
        progress.percent = _get(payload, 12)
    elif extended:
        progress.current = _get(payload, 0)
        progress.max = _get(payload, 1)
    else:
        progress.current = _get(payload, 0)

    return progress
