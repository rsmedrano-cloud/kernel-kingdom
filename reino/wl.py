"""Cliente Wayland mínimo en Python puro (solo mensajes sin file descriptors).

Alcanza para escuchar protocolos de información (toplevels, workspaces, idle)
sin depender de pywayland. Cada objeto tiene un handler(opcode, reader).
"""
import os
import select
import socket
import struct
from typing import Callable


def u32(v: int) -> bytes:
    return struct.pack("<I", v)


def wl_str(s: str) -> bytes:
    b = s.encode() + b"\0"
    return u32(len(b)) + b + b"\0" * (-len(b) % 4)


class Reader:
    def __init__(self, body: bytes):
        self.b, self.o = body, 0

    def u(self) -> int:
        v = struct.unpack_from("<I", self.b, self.o)[0]
        self.o += 4
        return v

    def i(self) -> int:
        v = struct.unpack_from("<i", self.b, self.o)[0]
        self.o += 4
        return v

    def blob(self) -> bytes:
        n = self.u()
        v = self.b[self.o:self.o + n]
        self.o += n + (-n % 4)
        return v

    def s(self) -> str:
        return self.blob().rstrip(b"\0").decode(errors="replace")

    def u32_array(self) -> list[int]:
        raw = self.blob()
        return list(struct.unpack(f"<{len(raw) // 4}I", raw))


Handler = Callable[[int, Reader], None]


class Connection:
    def __init__(self, on_global: Callable[[int, str, int], None] | None = None):
        path = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        if not path.startswith("/"):
            path = os.path.join(os.environ["XDG_RUNTIME_DIR"], path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        self.sock.connect(path)
        self.sock.setblocking(False)
        self.on_global = on_global
        self._buf = b""
        self._next_id = 2
        self._handlers: dict[int, Handler | None] = {}
        self.registry = self.new_id(self._registry_event)
        self.send(1, 1, u32(self.registry))           # wl_display.get_registry

    def fileno(self) -> int:
        return self.sock.fileno()

    def new_id(self, handler: Handler | None = None) -> int:
        oid = self._next_id
        self._next_id += 1
        self._handlers[oid] = handler
        return oid

    def adopt(self, oid: int, handler: Handler | None):
        """Registrar un objeto creado por el servidor (argumento new_id de un evento)."""
        self._handlers[oid] = handler

    def forget(self, oid: int):
        self._handlers.pop(oid, None)

    def send(self, obj: int, opcode: int, payload: bytes = b""):
        self.sock.sendall(struct.pack("<II", obj, ((8 + len(payload)) << 16) | opcode) + payload)

    def bind(self, name: int, iface: str, version: int, handler: Handler | None = None) -> int:
        oid = self.new_id(handler)
        self.send(self.registry, 0, u32(name) + wl_str(iface) + u32(version) + u32(oid))
        return oid

    def roundtrip(self, timeout: float = 2.0):
        """Bloquea hasta que el servidor procesó todo lo enviado (wl_display.sync)."""
        done = []
        cb = self.new_id(lambda op, r: done.append(True))
        self.send(1, 0, u32(cb))
        while not done:
            if not select.select([self.sock], [], [], timeout)[0]:
                raise TimeoutError("el compositor no respondió")
            self.dispatch()
        self.forget(cb)

    def dispatch(self):
        while True:
            try:
                chunk = self.sock.recv(65536)
            except BlockingIOError:
                break
            if not chunk:
                raise ConnectionError("el compositor cerró la conexión")
            self._buf += chunk
        while len(self._buf) >= 8:
            obj, size_op = struct.unpack_from("<II", self._buf)
            size = size_op >> 16
            if len(self._buf) < size:
                break
            body, self._buf = self._buf[8:size], self._buf[size:]
            op = size_op & 0xFFFF
            if obj == 1:
                if op == 0:
                    r = Reader(body)
                    _, code, msg = r.u(), r.u(), r.s()
                    raise ConnectionError(f"error de protocolo Wayland {code}: {msg}")
                continue  # delete_id
            handler = self._handlers.get(obj)
            if handler:
                handler(op, Reader(body))

    def _registry_event(self, op: int, r: Reader):
        if op == 0 and self.on_global:
            self.on_global(r.u(), r.s(), r.u())
