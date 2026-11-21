import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from bleak import BLEDevice, BleakClient, BleakError
from bleak_retry_connector import establish_connection

from . import encryption

_LOGGER = logging.getLogger(__name__)

# keep the connection open this long after the last activity
ACTIVE_TIME = 120
# drop a queued fire-and-forget command if it was not sent within this time
COMMAND_TIME = 15
# the official app sends the heartbeat every 6 seconds (10 is too late)
HEARTBEAT_INTERVAL = 6
# main loop resolution
TICK = 1
# max time to wait for a queued read/write when the caller awaits it
OP_TIMEOUT = 30
# max time to wait for the connection when a read is requested
CONNECT_TIMEOUT = 20

HEARTBEAT = bytes([0x00, 0x7F, 0x80])

# statistics request types (CoffeeMachineBleCommandParser.d in the J.O.E. app)
STATS_PRODUCTS = 1
STATS_MAINTENANCE_COUNTERS = 4
STATS_MAINTENANCE_PERCENT = 8


class UUIDs:
    """BLE characteristic UUIDs."""

    # https://github.com/Jutta-Proto/protocol-bt-cpp?tab=readme-ov-file#bluetooth-characteristics
    # Start product
    START_PRODUCT = "5a401525-ab2e-2548-c435-08c300000710"
    # Heartbeat
    P_MODE = "5a401529-ab2e-2548-c435-08c300000710"
    # Statistics
    STATS_COMMAND = "5a401533-ab2e-2548-c435-08c300000710"
    STATS_DATA = "5A401534-ab2e-2548-c435-08c300000710"
    # Status
    MACHINE_STATUS = "5a401524-ab2e-2548-c435-08c300000710"
    # Product / process progress
    PROGRESS = "5a401527-ab2e-2548-c435-08c300000710"


@dataclass
class Operation:
    """One GATT operation, executed by the connection loop."""

    uuid: str
    data: bytes | None = None  # None means read
    decrypt: bool = False
    # fire-and-forget commands are replaced by a newer one for the same uuid
    replaceable: bool = False
    # write only: False sends data as is (no key, no encryption)
    encrypt: bool = True
    expires: float = 0
    future: asyncio.Future | None = field(default=None, repr=False)

    @property
    def is_read(self) -> bool:
        return self.data is None


class Client:
    """BLE client.

    A single loop owns the GATT connection. Every read and write goes through
    a queue executed by this loop, so heartbeats, commands and polling never
    run concurrently on the connection.
    """

    def __init__(self, device: BLEDevice, callback: Callable = None, key: int = None):
        self.device = device
        self.callback = callback
        self.client: BleakClient | None = None
        self.loop = asyncio.get_running_loop()
        self.key = key

        self.ping_task: asyncio.Task | None = None
        self.ping_time = 0

        self.queue: deque[Operation] = deque()
        self.wake = asyncio.Event()
        self.connected = asyncio.Event()
        # statistics requests share the STATS_COMMAND / STATS_DATA pair:
        # a second request would overwrite the first one
        self.stats_lock = asyncio.Lock()

        # debug: called for every frame (see core/debug.py)
        self.tracer = None
        # debug: characteristics read periodically while connected {uuid: seconds}
        self.poll: dict[str, float] = {}
        self._poll_time: dict[str, float] = {}
        # debug: keep the connection open regardless of ping_time
        self.keep_alive = False

    def _active(self) -> bool:
        return self.keep_alive or time.time() < self.ping_time

    def ping(self):
        """Keep (or open) the connection for ACTIVE_TIME seconds."""
        self.ping_time = time.time() + ACTIVE_TIME

        if not self.ping_task:
            self.ping_task = self.loop.create_task(self._ping_loop())

    def ping_cancel(self):
        """Close the connection as soon as possible."""
        self.ping_time = 0
        self.keep_alive = False
        self.wake.set()

    def send(self, data: bytes, uuid: str = UUIDs.START_PRODUCT):
        """Queue a fire-and-forget command.

        A newer command for the same characteristic replaces a pending one,
        and a command not sent within COMMAND_TIME seconds is dropped.
        """
        for op in list(self.queue):
            if op.replaceable and op.uuid == uuid:
                self.queue.remove(op)

        self.queue.append(
            Operation(
                uuid=uuid,
                data=bytes(data),
                replaceable=True,
                expires=time.time() + COMMAND_TIME,
            )
        )
        self.ping()
        self.wake.set()

    async def write(
        self, data: bytes, uuid: str, timeout: float = OP_TIMEOUT, encrypt: bool = True
    ):
        """Queue a write and wait for its result. Raises on GATT error."""
        op = Operation(uuid=uuid, data=bytes(data), encrypt=encrypt)
        await self._run(op, timeout)

    async def read(
        self,
        uuid: str,
        decrypt: bool = False,
        timeout: float = OP_TIMEOUT,
        ping: bool = True,
    ):
        """Queue a read and wait for its (optionally decrypted) value.

        With ping=False the read neither opens nor extends the connection:
        it returns None when not connected (background polling).
        """
        if not ping and not self.connected.is_set():
            return None
        op = Operation(uuid=uuid, decrypt=decrypt)
        return await self._run(op, timeout, ping=ping)

    async def wait_connected(self, timeout: float = CONNECT_TIMEOUT) -> bool:
        """Open the connection if needed and wait until it is ready."""
        self.ping()
        try:
            await asyncio.wait_for(self.connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self, op: Operation, timeout: float, ping: bool = True):
        op.future = self.loop.create_future()
        self.queue.append(op)
        if ping:
            self.ping()
        self.wake.set()
        return await asyncio.wait_for(op.future, timeout)

    def _trace(self, direction: str, uuid: str, plain, raw, error=None):
        if self.tracer:
            try:
                self.tracer.add(direction, uuid, plain, raw, error)
            except Exception as e:
                _LOGGER.debug("trace error", exc_info=e)

    def _decrypt(self, data) -> bytes | None:
        if self.key is None:
            return None
        return encryption.encdec(list(data), self.key)

    async def _execute(self, op: Operation):
        if op.is_read:
            try:
                data = await self.client.read_gatt_char(op.uuid)
            except Exception as e:
                self._trace("R", op.uuid, None, None, e)
                raise
            plain = self._decrypt(data)
            self._trace("R", op.uuid, plain, data)
            if op.decrypt and plain is not None:
                return plain
            return data

        if op.encrypt:
            plain = bytearray(op.data)
            plain[0] = self.key
            raw = encrypt(op.data, self.key)
        else:
            plain = raw = bytes(op.data)
        try:
            await self.client.write_gatt_char(op.uuid, data=raw, response=True)
        except Exception as e:
            self._trace("W", op.uuid, plain, raw, e)
            raise
        self._trace("W", op.uuid, plain, raw)
        return None

    async def _poll(self):
        now = time.time()
        for uuid, interval in list(self.poll.items()):
            if now - self._poll_time.get(uuid, 0) < interval:
                continue
            self._poll_time[uuid] = now
            try:
                await self._execute(Operation(uuid=uuid))
            except Exception as e:
                _LOGGER.debug("poll error %s", uuid, exc_info=e)
                if not self.client.is_connected:
                    raise

    async def _process_queue(self):
        while self.queue:
            op = self.queue.popleft()

            if op.future and op.future.done():
                continue  # caller gave up (timeout)

            if op.replaceable and time.time() > op.expires:
                _LOGGER.debug("command expired: %s", op)
                continue

            try:
                result = await self._execute(op)
            except Exception as e:
                if op.future:
                    op.future.set_exception(e)
                else:
                    _LOGGER.warning("command error: %s", op, exc_info=e)
                if not self.client or not self.client.is_connected:
                    raise
                continue

            if op.future:
                op.future.set_result(result)

    async def _heartbeat(self):
        # important dummy write to keep the connection
        # https://github.com/Jutta-Proto/protocol-bt-cpp?tab=readme-ov-file#heartbeat
        try:
            await self._execute(Operation(uuid=UUIDs.P_MODE, data=HEARTBEAT))
            _LOGGER.debug("heartbeat sent")
        except Exception as e:
            # we log as info as this is expected if the device is off
            _LOGGER.info("heartbeat error", exc_info=e)
            if not self.client.is_connected:
                raise

    async def _wait_tick(self):
        try:
            await asyncio.wait_for(self.wake.wait(), TICK)
        except asyncio.TimeoutError:
            pass
        self.wake.clear()

    async def _ping_loop(self):
        while self._active():
            try:
                self.client = await establish_connection(
                    BleakClient, self.device, self.device.address
                )
                self.connected.set()
                if self.callback:
                    self.callback(True)

                last_heartbeat = 0
                while self._active():
                    await self._process_queue()

                    if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                        await self._heartbeat()
                        last_heartbeat = time.time()

                    await self._poll()

                    await self._wait_tick()
            except TimeoutError:
                pass
            except BleakError as e:
                _LOGGER.debug("ping error", exc_info=e)
            except Exception as e:
                _LOGGER.warning("ping error", exc_info=e)

            self.connected.clear()

            if self.client:
                try:
                    await self.client.disconnect()
                except Exception as e:
                    _LOGGER.debug("disconnect error", exc_info=e)
                self.client = None

            if self.callback:
                self.callback(False)

            await asyncio.sleep(1)

        self._fail_pending()
        self.ping_task = None

    def _fail_pending(self):
        while self.queue:
            op = self.queue.popleft()
            if op.future and not op.future.done():
                op.future.set_exception(ConnectionError("connection closed"))

    async def read_statistics_data(
        self, timeout: int = 20, retries: int = 30, stats_type: int = STATS_PRODUCTS
    ) -> bytes | None:
        """Read statistics data from the device.

        stats_type: STATS_PRODUCTS (product counters),
        STATS_MAINTENANCE_COUNTERS or STATS_MAINTENANCE_PERCENT.
        """
        _LOGGER.debug("Reading Jura statistics (type %d)...", stats_type)

        if not await self.wait_connected(timeout):
            _LOGGER.debug("Failed to establish connection")
            return None

        async with self.stats_lock:
            return await self._read_statistics(timeout, retries, stats_type)

    async def _read_statistics(
        self, timeout: int, retries: int, stats_type: int
    ) -> bytes | None:
        try:
            # Send statistics request command
            # https://github.com/Jutta-Proto/protocol-bt-cpp?tab=readme-ov-file#writing-1
            await self.write(stats_request(stats_type), UUIDs.STATS_COMMAND)

            # Wait for statistics to be ready
            # https://github.com/Jutta-Proto/protocol-bt-cpp?tab=readme-ov-file#reading
            for _ in range(retries):
                status = await self.read(UUIDs.STATS_COMMAND)
                if status and status[1] != 225:  # 225 means not ready
                    break
                await asyncio.sleep(0.8)
            else:
                _LOGGER.error("Device not ready for statistics reading")
                return None

            # Read statistics data
            # https://github.com/Jutta-Proto/protocol-bt-cpp?tab=readme-ov-file#statistics-data
            return await self.read(UUIDs.STATS_DATA, decrypt=True)
        except Exception as e:
            _LOGGER.info("Error reading statistics", exc_info=e)
            return None

    async def read_machine_status(self) -> bytes | None:
        """Read machine status from the device."""
        _LOGGER.debug("Reading Jura machine status...")

        if not await self.wait_connected():
            _LOGGER.debug("Failed to establish connection")
            return None

        try:
            data = await self.read(UUIDs.MACHINE_STATUS, decrypt=True)
            _LOGGER.debug(f"Machine status data: {data}")
            return data
        except Exception as e:
            _LOGGER.warning("Error reading machine status", exc_info=e)
            return None


def encrypt(data: bytes | list, key: int) -> bytes:
    data = bytearray(data)
    data[0] = key
    return encryption.encdec(data, key)


def stats_request(stats_type: int) -> bytes:
    """[key, type (16 bits), ...]: products use a product bitmask (all here),
    the other types a single 0x01 byte."""
    if stats_type == STATS_PRODUCTS:
        return bytes([0x00, 0x00, STATS_PRODUCTS, 0xFF, 0xFF])
    return bytes([0x00, 0x00, stats_type, 0x01, 0x00])
