"""¿Hay una ventana tapando el fondo?

Conexión Wayland propia (independiente de la de GTK) que escucha:
  * ext_foreign_toplevel_list_v1   -> lista de ventanas
  * zcosmic_toplevel_info_v1 (v2+) -> estado (maximizada, minimizada,
                                      pantalla completa), outputs y geometría
  * ext_workspace_manager_v1       -> qué workspaces están visibles
  * wl_output (v4)                 -> nombre del conector (DP-1, HDMI-A-1...)

Todo es por eventos: el fd del socket se registra en el main loop de GLib y
solo nos despertamos cuando el compositor manda algo.

Probar a mano:  python -m reino.render.occlusion [DP-1]
"""
import sys

from ..wl import Connection, Reader, u32

MAXIMIZED, MINIMIZED, ACTIVATED, FULLSCREEN = 0, 1, 2, 3
WS_ACTIVE = 1


class _Toplevel:
    __slots__ = ("ext", "cosmic", "app_id", "title", "states", "outputs", "geometry", "workspaces")

    def __init__(self, ext: int):
        self.ext, self.cosmic = ext, None
        self.app_id = self.title = ""
        self.states: set[int] = set()
        self.outputs: set[int] = set()
        self.geometry: dict[int, tuple[int, int, int, int]] = {}
        self.workspaces: set[int] = set()


class OcclusionWatcher:
    def __init__(self, coverage: float = 0.90):
        self.coverage = coverage
        self.output_name: str | None = None              # conector del monitor del wallpaper
        self.output_size: tuple[int, int] | None = None  # tamaño lógico de ese monitor
        self.covered = False
        self._info: int | None = None
        self._toplevels: dict[int, _Toplevel] = {}       # id ext -> toplevel
        self._outputs: dict[int, str] = {}               # id wl_output -> nombre
        self._ws_active: dict[int, bool] = {}
        self.conn = Connection(self._on_global)

    def fileno(self) -> int:
        return self.conn.fileno()

    def dispatch(self) -> bool:
        """Lee todo lo disponible. Devuelve True si cambió `covered`."""
        self.conn.dispatch()
        return self.recompute()

    def recompute(self) -> bool:
        """Reevalúa (p.ej. al cambiar output_name). True si cambió `covered`."""
        before = self.covered
        self.covered = self._compute()
        return self.covered != before

    # -- eventos -------------------------------------------------------------

    def _on_global(self, name: int, iface: str, ver: int):
        c = self.conn
        if iface == "ext_foreign_toplevel_list_v1":
            c.bind(name, iface, 1, self._list_event)
        elif iface == "zcosmic_toplevel_info_v1" and ver >= 2:
            self._info = c.bind(name, iface, min(ver, 3))
            for tl in self._toplevels.values():
                self._attach_cosmic(tl)
        elif iface == "ext_workspace_manager_v1":
            c.bind(name, iface, 1, self._ws_manager_event)
        elif iface == "wl_output" and ver >= 4:
            oid = c.bind(name, iface, 4)
            self._outputs[oid] = ""
            c.adopt(oid, lambda op, r, oid=oid: self._output_event(oid, op, r))

    def _output_event(self, oid: int, op: int, r: Reader):
        if op == 4:                                        # name
            self._outputs[oid] = r.s()

    def _list_event(self, op: int, r: Reader):
        if op == 0:                                        # toplevel (new_id)
            ext = r.u()
            tl = self._toplevels[ext] = _Toplevel(ext)
            self.conn.adopt(ext, lambda op, r, tl=tl: self._ext_event(tl, op, r))
            self._attach_cosmic(tl)

    def _ext_event(self, tl: _Toplevel, op: int, r: Reader):
        if op == 0:                                        # closed
            self._toplevels.pop(tl.ext, None)
            if tl.cosmic:
                self.conn.send(tl.cosmic, 0)               # zcosmic_toplevel_handle_v1.destroy
                self.conn.forget(tl.cosmic)
            self.conn.send(tl.ext, 0)                      # ext handle destroy
            self.conn.forget(tl.ext)
        elif op == 2:
            tl.title = r.s()
        elif op == 3:
            tl.app_id = r.s()

    def _attach_cosmic(self, tl: _Toplevel):
        if self._info is None or tl.cosmic is not None:
            return
        tl.cosmic = self.conn.new_id(lambda op, r, tl=tl: self._cosmic_event(tl, op, r))
        self.conn.send(self._info, 1, u32(tl.cosmic) + u32(tl.ext))   # get_cosmic_toplevel

    def _cosmic_event(self, tl: _Toplevel, op: int, r: Reader):
        if op == 4:
            tl.outputs.add(r.u())
        elif op == 5:
            out = r.u()
            tl.outputs.discard(out)
            tl.geometry.pop(out, None)
        elif op == 8:
            tl.states = set(r.u32_array())
        elif op == 9:
            out, x, y, w, h = r.u(), r.i(), r.i(), r.i(), r.i()
            tl.geometry[out] = (x, y, w, h)
        elif op == 10:
            tl.workspaces.add(r.u())
        elif op == 11:
            tl.workspaces.discard(r.u())

    def _ws_manager_event(self, op: int, r: Reader):
        if op == 1:                                        # workspace (new_id)
            ws = r.u()
            self._ws_active[ws] = False
            self.conn.adopt(ws, lambda op, r, ws=ws: self._ws_event(ws, op, r))

    def _ws_event(self, ws: int, op: int, r: Reader):
        if op == 3:                                        # state
            self._ws_active[ws] = bool(r.u() & WS_ACTIVE)
        elif op == 5:                                      # removed
            self._ws_active.pop(ws, None)
            self.conn.send(ws, 0)
            self.conn.forget(ws)

    # -- decisión ------------------------------------------------------------

    def visible_toplevels(self):
        for tl in self._toplevels.values():
            if MINIMIZED in tl.states:
                continue
            if tl.workspaces and not any(self._ws_active.get(w, True) for w in tl.workspaces):
                continue
            yield tl

    def _compute(self) -> bool:
        mine = {oid for oid, name in self._outputs.items()
                if self.output_name is None or name == self.output_name}
        rects = []
        for tl in self.visible_toplevels():
            on = tl.outputs & mine
            if not on:
                continue
            if MAXIMIZED in tl.states or FULLSCREEN in tl.states:
                return True
            rects += [tl.geometry[o] for o in on if o in tl.geometry]
        # Ventanas en mosaico (tiling) que entre todas tapan la pantalla
        if self.output_size and self.output_name and rects:
            return _coverage(rects, *self.output_size) >= self.coverage
        return False


def _coverage(rects, w: int, h: int, cols: int = 64, rows: int = 36) -> float:
    """Fracción aproximada de la pantalla cubierta por la unión de rectángulos."""
    hit = 0
    for j in range(rows):
        py = (j + 0.5) * h / rows
        for i in range(cols):
            px = (i + 0.5) * w / cols
            if any(x <= px < x + rw and y <= py < y + rh for x, y, rw, rh in rects):
                hit += 1
    return hit / (cols * rows)


if __name__ == "__main__":
    import select

    watcher = OcclusionWatcher()
    if len(sys.argv) > 1:
        watcher.output_name = sys.argv[1]
    print("Escuchando... (maximizá/minimizá ventanas; Ctrl+C para salir)")
    try:
        while True:
            select.select([watcher], [], [])
            watcher.dispatch()
            names = dict(watcher._outputs)
            print(f"\ncovered={watcher.covered}  outputs={list(names.values())}  "
                  f"workspaces activos={sum(watcher._ws_active.values())}/{len(watcher._ws_active)}")
            for tl in watcher.visible_toplevels():
                st = ",".join(n for n, v in (("max", 0), ("min", 1), ("act", 2), ("full", 3)) if v in tl.states)
                geo = {names.get(o, o): g for o, g in tl.geometry.items()}
                print(f"  {tl.app_id[:28]:<28} [{st}] {geo}")
    except KeyboardInterrupt:
        pass
