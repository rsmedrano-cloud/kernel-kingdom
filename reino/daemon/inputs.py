"""Actividad del usuario: tipeo y presencia.

Dos fuentes, se usa lo que haya:
  * evdev (/dev/input/event*): cuenta pulsaciones de teclado. Solo cuenta,
    nunca guarda qué tecla fue. Requiere estar en el grupo `input`.
  * ext_idle_notifier_v1 (Wayland): sin permisos especiales. Solo sabe si
    estás activo (teclado/mouse) o ausente, no cuántas teclas.
"""
import glob
import os
import select
import struct
import threading
import time

from ..wl import Connection, Reader, u32

EV_KEY, EV_REP = 1, 20
KEY_A, KEY_SPACE = 30, 57
_EVENT = struct.Struct("llHHi")   # struct input_event en 64 bits


def _caps(path: str) -> int:
    try:
        words = open(path).read().split()
    except OSError:
        return 0
    return int("".join(w.zfill(16) for w in words) or "0", 16)


class Keyboards:
    def __init__(self):
        self.fds: list[int] = []
        self.denied = False
        self.count = 0
        for dev in sorted(glob.glob("/sys/class/input/event*")):
            keys = _caps(f"{dev}/device/capabilities/key")
            evs = _caps(f"{dev}/device/capabilities/ev")
            if not (keys >> KEY_A & 1 and keys >> KEY_SPACE & 1 and evs >> EV_REP & 1):
                continue
            try:
                self.fds.append(os.open(f"/dev/input/{os.path.basename(dev)}", os.O_RDONLY | os.O_NONBLOCK))
            except PermissionError:
                self.denied = True
            except OSError:
                pass

    def read(self, fd: int):
        try:
            data = os.read(fd, _EVENT.size * 128)
        except BlockingIOError:
            return
        except OSError:  # teclado desconectado
            self.fds.remove(fd)
            os.close(fd)
            return
        for off in range(0, len(data) - _EVENT.size + 1, _EVENT.size):
            _, _, etype, code, value = _EVENT.unpack_from(data, off)
            if etype == EV_KEY and value == 1 and code < 0x100:
                self.count += 1


class IdleWatch:
    """Activo/ausente según el compositor (timeout corto = 'está usando la compu')."""

    def __init__(self, timeout_ms: int = 5000):
        self.timeout = timeout_ms / 1000
        self.active = True
        self.changed = time.monotonic()
        found: dict[str, tuple[int, int]] = {}
        self.conn = Connection(lambda name, iface, ver: found.setdefault(iface, (name, ver)))
        self.conn.roundtrip()
        if "ext_idle_notifier_v1" not in found or "wl_seat" not in found:
            raise RuntimeError("el compositor no ofrece ext_idle_notifier_v1")
        seat = self.conn.bind(found["wl_seat"][0], "wl_seat", 1)
        name, ver = found["ext_idle_notifier_v1"]
        notifier = self.conn.bind(name, "ext_idle_notifier_v1", min(ver, 2))
        note = self.conn.new_id(self._event)
        # v2: get_input_idle_notification ignora inhibidores (videos en pantalla completa)
        self.conn.send(notifier, 2 if ver >= 2 else 1, u32(note) + u32(timeout_ms) + u32(seat))

    def _event(self, op: int, r: Reader):
        now = time.monotonic()
        if op == 0:      # idled: en realidad dejaste de tocar `timeout` segundos antes
            self.active, self.changed = False, now - self.timeout
        elif op == 1:    # resumed
            self.active, self.changed = True, now


class InputActivity:
    def __init__(self):
        self.keyboards = Keyboards()
        try:
            self.idle: IdleWatch | None = IdleWatch()
        except (OSError, KeyError, RuntimeError, TimeoutError, ConnectionError):
            self.idle = None
        self._last = time.monotonic()
        self._active_time = 0.0
        self._idle_since: float | None = None
        self._lock = threading.Lock()

    @property
    def source(self) -> str:
        if self.keyboards.fds:
            return "evdev"
        return "idle" if self.idle else "none"

    def _fds(self) -> list:
        fds = list(self.keyboards.fds)
        if self.idle:
            fds.append(self.idle.conn)
        return fds

    def wait(self, timeout: float, stop: threading.Event):
        """Duerme hasta `timeout` atendiendo los eventos de entrada que lleguen."""
        fds = self._fds()
        if not fds:
            stop.wait(timeout)
            return
        ready, _, _ = select.select(fds, [], [], timeout)
        for f in ready:
            if self.idle and f is self.idle.conn:
                self._account()
                try:
                    self.idle.conn.dispatch()
                except ConnectionError:
                    self.idle = None
            else:
                self.keyboards.read(f)

    def _account(self):
        """Acumula tiempo activo hasta ahora antes de que cambie el estado."""
        now = time.monotonic()
        if self.idle and self.idle.active:
            self._active_time += now - max(self._last, self.idle.changed)
        self._last = now

    def take(self, dt: float) -> dict:
        self._account()
        keys, self.keyboards.count = self.keyboards.count, 0
        if self.idle:
            active = min(1.0, self._active_time / dt) if dt else 0.0
            idle_for = 0.0 if self.idle.active else time.monotonic() - self.idle.changed
        else:  # sin Wayland: usar el teclado como señal de presencia
            active = 1.0 if keys else 0.0
            idle_for = 0.0
        self._active_time = 0.0
        return {
            "source": self.source,
            "keys": keys,
            "kpm": round(keys * 60 / dt) if dt else 0,
            "active": round(active, 2),
            "idle_for": round(idle_for),
            "kbd_denied": self.keyboards.denied and not self.keyboards.fds,
        }
