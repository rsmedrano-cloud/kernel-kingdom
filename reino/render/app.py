"""Ventana GTK4: superficie layer-shell en la capa BACKGROUND + bucle de frames.

Presupuesto de CPU/GPU:
  * Tope duro de FPS (30). Sin clima activo baja a idle_fps.
  * El frame se dibuja a baja resolución (~850x480) y la GPU lo escala con
    filtro NEAREST: se ve pixelado y no gastamos CPU escalando.
  * Con el fondo tapado (ventana maximizada / pantalla completa / tiling que
    cubre el monitor) se eliminan TODOS los timers: el proceso queda dormido en
    poll() hasta que el compositor avise que algo cambió. 0% CPU, 0% GPU.

Interacción: clic = ficha de lo que hay (castillo, reino, casilla); el mouse
resalta la casilla. En modo ventana (sin bordes): rueda = zoom hacia el cursor,
arrastrar = mover el mapa (o la ventana, sin zoom), doble clic = sin zoom,
+/- y 0 también; Esc/q cierra, f pantalla completa, h HUD, c crónica.
"""
import json
import signal
import sys
import time
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402
except (ValueError, ImportError):
    LayerShell = None

from .. import paths  # noqa: E402
from ..i18n import ui, words  # noqa: E402
from .iso import POWER_KEYS, Painter, detail_for, fit_scale, zoom_scales  # noqa: E402
from .occlusion import OcclusionWatcher  # noqa: E402


def log(msg: str):
    print(f"kernel-kingdom: {msg}", file=sys.stderr, flush=True)


class WorldView(Gtk.Widget):
    """Dos texturas chicas escaladas con NEAREST (lo hace la GPU): el mundo (con
    zoom) y la interfaz (siempre a escala base, para que el texto no crezca)."""

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.layers: list[list] = [[None, 1.0], [None, 1.0]]   # [textura, escala]

    def do_snapshot(self, snapshot):
        for tex, scale in self.layers:
            if tex is not None:
                rect = Graphene.Rect().init(0, 0, tex.get_width() * scale, tex.get_height() * scale)
                snapshot.append_scaled_texture(tex, Gsk.ScalingFilter.NEAREST, rect)


def _texture(surf) -> Gdk.Texture:
    # cairo ARGB32 en little-endian == B8G8R8A8 premultiplicado
    return Gdk.MemoryTexture.new(surf.get_width(), surf.get_height(), Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                 GLib.Bytes.new(bytes(surf.get_data())), surf.get_stride())


def _prefs() -> dict:
    try:
        return json.loads((paths.data_dir() / "prefs.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_prefs(**kw):
    prefs = _prefs() | kw
    f = paths.data_dir() / "prefs.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(prefs))


def _find_monitor(spec: str) -> Gdk.Monitor | None:
    monitors = list(Gdk.Display.get_default().get_monitors())
    if spec.isdigit():
        return monitors[int(spec)] if int(spec) < len(monitors) else None
    return next((m for m in monitors if m.get_connector() == spec), None)


class Wallpaper:
    def __init__(self, app: Gtk.Application, args, cfg: dict):
        rc = cfg["render"]
        self.max_fps = min(30, args.fps or rc["max_fps"])
        self.idle_fps = min(self.max_fps, rc["idle_fps"])
        self.pixel_scale = rc["pixel_scale"]
        self.interactive = rc.get("interactive", True)
        self.state_path = Path(args.state)
        self.painter = Painter(cfg["daemon"]["interval"], rc.get("hemisphere", "auto"))
        self.painter.show_chronicle = rc.get("show_chronicle", True)
        self.painter.show_powers = rc.get("show_powers", True)
        self.painter.hour_override = args.hour
        self.painter.season_override = getattr(args, "season", None)

        self._mtime = None
        self._frame_src = None
        self._poll_src = None
        self._fps = 0
        self._scale = 1.0
        self._base = 1.0
        self.zoom = 0                  # niveles de zoom sobre la escala base (solo en ventana)
        self._pointer = (0.0, 0.0)
        self._drag = None
        self.paused = False

        self.win = Gtk.ApplicationWindow(application=app, title="kernel-kingdom")
        self.view = WorldView()
        self.win.connect("realize", self._on_realize)

        self.layered = not args.windowed and LayerShell is not None and LayerShell.is_supported()
        if self.layered:
            self.win.set_child(self.view)
            LayerShell.init_for_window(self.win)
            LayerShell.set_namespace(self.win, "kernel-kingdom")
            LayerShell.set_layer(self.win, LayerShell.Layer.BACKGROUND)
            for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                         LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
                LayerShell.set_anchor(self.win, edge, True)
            LayerShell.set_exclusive_zone(self.win, -1)
            LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
            if args.monitor is not None:
                monitor = _find_monitor(args.monitor)
                if monitor:
                    LayerShell.set_monitor(self.win, monitor)
                else:
                    log(f"monitor {args.monitor!r} no encontrado, elige el compositor")
        else:
            # Sin bordes ni barra de título: la ventana entera se arrastra para moverla
            self.win.set_decorated(False)
            self.win.set_default_size(1600, 900)
            self.win.set_child(self.view)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._on_key)
            self.win.add_controller(keys)
            scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
            scroll.connect("scroll", self._on_scroll)
            self.view.add_controller(scroll)
            drag = Gtk.GestureDrag(button=1)
            drag.connect("drag-begin", self._on_drag_begin)
            drag.connect("drag-update", self._on_drag_update)
            self.view.add_controller(drag)

        self.painter.window_mode = not self.layered
        self.interactive = self.interactive or not self.layered   # la ventana siempre responde
        if self.interactive:
            click = Gtk.GestureClick(button=1)
            click.connect("released", self._on_click)
            self.view.add_controller(click)
            right = Gtk.GestureClick(button=3)
            right.connect("released", self._on_right_click)
            self.view.add_controller(right)
            motion = Gtk.EventControllerMotion()
            motion.connect("motion", self._on_motion)
            motion.connect("leave", self._on_leave)
            self.view.add_controller(motion)

        self.occl = None
        if self.layered and not args.no_occlusion:
            try:
                self.occl = OcclusionWatcher(rc["coverage"])
                chan = GLib.IOChannel.unix_new(self.occl.fileno())
                GLib.io_add_watch(chan, GLib.PRIORITY_DEFAULT,
                                  GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                                  self._on_wayland)
            except (OSError, KeyError, ConnectionError) as e:
                log(f"sin detección de oclusión ({e})")
                self.occl = None

        self.win.present()
        self._resume()
        self.update_check = cfg.get("update", {}).get("check", True)
        if not self.layered:
            GLib.idle_add(self._first_run)
            self.win.connect("close-request", lambda *_: _save_prefs(last_seen=time.time()) and False)
            GLib.timeout_add_seconds(60, self._mark_seen)

    # -- entrada del usuario -------------------------------------------------

    def _on_key(self, ctrl, keyval, keycode, state):
        name = Gdk.keyval_name(keyval) or ""
        if name == "Escape" and (self.painter.selected_power or self.painter.menu_open):
            self.painter.select_power(None)
            self.painter.toggle_menu(False)
        elif name in ("Escape", "q"):
            self.win.close()
        elif name in "123456789" and len(name) == 1 and self.painter.show_powers:
            key = POWER_KEYS[int(name) - 1]
            self.painter.select_power(None if key == self.painter.selected_power else key)
        elif name in ("f", "F11"):
            self.win.unfullscreen() if self.win.is_fullscreen() else self.win.fullscreen()
        elif name == "h":
            self.painter.toggle_view("status")
        elif name == "c":
            self.painter.toggle_view("chron")
        elif name == "r":
            self.painter.toggle_view("realms")
        elif name == "m":
            self.painter.toggle_menu()
        elif name == "e" and not self.layered:          # exportar la historia del día
            self._export_story(time.monotonic())
        elif name in ("Page_Up", "Page_Down"):
            self.painter.scroll_panel("chron", 5 if name == "Page_Up" else -5)
        elif name in ("plus", "equal", "KP_Add"):
            self._zoom_at(1, *self._pointer)
        elif name in ("minus", "KP_Subtract"):
            self._zoom_at(-1, *self._pointer)
        elif name in ("0", "KP_0"):
            self._zoom_reset()
        else:
            return False
        self._render()
        return True

    def _on_click(self, gesture, n_press, x, y):
        if self.paused or (self._drag and self._drag["moved"]):
            return
        now = time.monotonic()
        p = self.painter
        ux, uy = x / self._base, y / self._base
        region = p.ui_region(ux, uy)
        if p.dialog:                                # el diálogo espera respuesta: el resto no responde
            action = p.dialog_action_at(ux, uy)
            if action:
                self._dialog_action(action, now)
            self._render()
            return
        if region == "menu_btn":                    # abrir / cerrar el menú
            p.toggle_menu()
        elif region == "menu":
            self._menu_action(p.menu_action_at(ux, uy), now)
        elif region == "chron_dl":
            self._export_story(now)
        elif region == "chron_share":
            self._share_day(now)
        elif region == "chron_size":
            p.cycle_chronicle()
        elif p.menu_open:                           # clic afuera: cierra el menú
            p.toggle_menu(False)
        elif realm := p.hud_realm_at(ux, uy):       # clic en un reino de la lista: ir a verlo
            self._focus_realm(realm, now)
        elif entry := p.chron_entry_at(ux, uy):     # clic en la crónica: ir a donde pasó
            self._focus_tile(entry["x"], entry["y"], now)
        elif p.selected_power:                      # con un poder elegido, el clic lo lanza
            tile = p.pick(x / self._scale, y / self._scale)
            if tile:
                self._send_power(p.selected_power, tile, now)
            p.select_power(None)
        elif n_press == 2 and not self.layered:
            self._zoom_reset()
            return
        elif region is None:
            p.inspect(x / self._scale, y / self._scale, now, ui_pos=(ux, uy))
        self._render()

    def _menu_action(self, action, now: float):
        p = self.painter
        if not action:
            return
        if action[0] == "toggle":
            p.toggle_view(action[1])
        elif action[0] == "power":
            p.select_power(None if action[1] == p.selected_power else action[1])
            p.toggle_menu(False)                    # elegido el poder, el menú se cierra para apuntar
        elif action[0] == "story":
            self._export_story(now)
        elif action[0] == "share":
            p.toggle_menu(False)
            self._share_day(now)
        elif action[0] == "update":
            p.toggle_menu(False)
            self._do_update()
        elif action[0] == "language":
            self._switch_language(now)
        elif action[0] == "wallpaper":
            self._set_wallpaper(not p.wallpaper_on)
        elif action[0] == "quit":
            self.win.close()                        # el daemon (el mundo) sigue corriendo aparte
        elif action[0] == "new_world":
            if p.confirm_new_until > now:           # segundo clic: confirmado
                p.confirm_new_until = 0
                self._send_command({"action": "new_world"})
                p.toast(ui("world.new_sent"), now)
                p.toggle_menu(False)
            else:
                p.confirm_new_until = now + 5
                p._ui_dirty = True

    # -- fondo de pantalla: la pregunta de la primera vez y el interruptor del menú ----

    def _first_run(self):
        from .. import cli
        self.painter.wallpaper_on = cli.wallpaper_status()
        self.painter._ui_dirty = True
        prefs = _prefs()
        if not prefs.get("wallpaper_asked"):
            self.painter.show_dialog(ui("dialog.wp.title"),
                                     [(ui("dialog.wp.yes"), "wp_yes"), (ui("dialog.wp.no"), "wp_no")],
                                     ui("dialog.wp.note"))
        else:
            self._show_away(prefs.get("last_seen"))
        _save_prefs(last_seen=time.time())
        if self.update_check:
            self._check_update(prefs)
        self._render()
        return GLib.SOURCE_REMOVE

    def _mark_seen(self):
        _save_prefs(last_seen=time.time())
        return GLib.SOURCE_CONTINUE

    def _show_away(self, last_seen: float | None):
        """Al volver después de un rato: qué pasó mientras tanto."""
        from .. import story
        if not last_seen or time.time() - last_seen < 20 * 60:
            return
        entries = story.since(last_seen)
        if len(entries) < 3:
            return
        mins = int((time.time() - last_seen) / 60)
        span = f"{mins // 60} h {mins % 60:02d} min" if mins >= 60 else f"{mins} min"
        parts = story.count_parts(entries)
        lines = [ui("dialog.away.counts", events=len(entries), parts=", ".join(parts) or "-")]
        lines += [f"- {t}" for t in story.highlights(entries, 5)]
        self.painter.show_dialog(ui("dialog.away.title", time=span), [(ui("dialog.away.ok"), "close")],
                                 lines=lines)

    def _dialog_action(self, action: str, now: float):
        self.painter.close_dialog()
        if action in ("wp_yes", "wp_no"):
            _save_prefs(wallpaper_asked=True)
            if action == "wp_yes":
                self._set_wallpaper(True)
        elif action == "relaunch":
            from .. import cli
            cli.relaunch_window()
            self.win.get_application().quit()

    def _switch_language(self, now: float):
        """Castellano <-> inglés: queda en la config y el daemon escribe en ese idioma
        de ahora en más (los fondos de pantalla lo siguen a través del estado)."""
        from .. import config, i18n
        lang = "en" if i18n.LANG == "es" else "es"
        config.set_top_level("language", lang)
        self._send_command({"action": "language", "lang": lang})
        i18n.set_lang(lang)
        self.painter._ui_dirty = True
        self.painter.toast(ui("toast.language"), now, secs=8)

    # -- actualizaciones ---------------------------------------------------------

    def _check_update(self, prefs: dict):
        """Una consulta a GitHub cada 6 horas como mucho (sin trabar la ventana)."""
        import threading
        from .. import __version__, update

        def show(version):
            if version and update._parse(version) > update._parse(__version__):
                self.painter.update_available = version
                self.painter._ui_dirty = True
                self.painter.toast(ui("update.available", version=version), time.monotonic(), secs=8)
                self._render()
            return GLib.SOURCE_REMOVE

        if time.time() - prefs.get("update_checked", 0) < 6 * 3600:
            show(prefs.get("update_latest"))
            return

        def work():
            version = update.latest()
            if version:
                _save_prefs(update_checked=time.time(), update_latest=version)
            GLib.idle_add(show, version)
        threading.Thread(target=work, daemon=True).start()

    def _do_update(self):
        import threading
        from .. import cli
        version = self.painter.update_available or "main"
        self.painter.toast(ui("update.working", version=version), time.monotonic(), secs=30)
        self._render()

        def work():
            ok, detail, _ = cli.apply_update()
            GLib.idle_add(done, ok, detail)

        def done(ok, detail):
            p = self.painter
            if ok:
                p.update_available = None
                _save_prefs(update_checked=0)
                p.show_dialog(ui("dialog.updated.title", version=version),
                              [(ui("dialog.updated.restart"), "relaunch"), (ui("dialog.updated.later"), "close")],
                              ui("dialog.updated.note"))
            else:
                p.toast(ui("update.failed", detail=detail), time.monotonic(), ok=False, secs=10)
            self._render()
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()

    # -- compartir ---------------------------------------------------------------

    def _share_day(self, now: float):
        """Tarjeta PNG + relato en Markdown, sin IPs ni usuarios, en Imágenes/Kernel Kingdom.
        El texto corto queda en el portapapeles, listo para pegar."""
        from .. import story
        from ..i18n import tr
        p = self.painter
        day = time.strftime("%Y-%m-%d")
        entries = story.load(day)
        if not entries:
            p.toast(ui("share.empty"), now, ok=False)
            return
        st = p.state or {}
        kingdom = st.get("kingdom", "")
        date = time.strftime("%d/%m/%Y")
        parts = story.count_parts(entries)
        title = tr("story.title", kingdom=kingdom, date=date)
        subtitle = ui("share.subtitle", date=date, parts=", ".join(parts[:4]) or tr("story.none"))
        lines = [story.scrub(t) for t in story.highlights(entries, 6)]
        pics = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or str(Path.home() / "Pictures")
        folder = Path(pics) / "Kernel Kingdom"
        folder.mkdir(parents=True, exist_ok=True)
        png, md = folder / f"kernel-kingdom-{day}.png", folder / f"kernel-kingdom-{day}.md"
        p.share_card(title, subtitle, lines, ui("share.footer"), now).write_to_png(str(png))
        md.write_text(story.scrub(story.build(day, entries, kingdom, st.get("era_name", ""))), encoding="utf-8")
        text = "\n".join([title, subtitle, ""] + [f"- {t}" for t in lines] + ["", ui("share.footer")])
        self.win.get_clipboard().set(text)
        p.toast(ui("share.saved", path=str(folder).replace(str(Path.home()), "~")), now, secs=8)
        try:
            Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)
        except GLib.Error:
            pass

    def _set_wallpaper(self, on: bool):
        """Prende o apaga el fondo (servicios de systemd) sin trabar la ventana."""
        import threading
        from .. import cli

        def work():
            msg = ui("toast.wallpaper_on", monitors=", ".join(cli.wallpaper_on())) if on else \
                (cli.wallpaper_off() or ui("toast.wallpaper_off"))
            GLib.idle_add(done, msg)

        def done(msg):
            self.painter.wallpaper_on = on
            self.painter._ui_dirty = True
            self.painter.toast(msg, time.monotonic(), secs=6)
            self._render()
            return GLib.SOURCE_REMOVE
        threading.Thread(target=work, daemon=True).start()

    def _export_story(self, now: float):
        from .. import story
        st = self.painter.state or {}
        out = story.export(None, st.get("kingdom"), st.get("era_name", ""))
        if out:
            self.painter.toast(ui("story.saved", path=str(out[0]).replace(str(Path.home()), "~")), now, secs=6)
        else:
            from ..i18n import tr
            self.painter.toast(tr("story.empty", date=time.strftime("%d/%m/%Y")), now, ok=False)

    def _on_right_click(self, gesture, n_press, x, y):
        if self.painter.selected_power:
            self.painter.select_power(None)
            self._render()

    def _send_command(self, cmd: dict):
        """Deja un pedido para el daemon (lo procesa en el próximo tick)."""
        folder = paths.commands_dir()
        folder.mkdir(parents=True, exist_ok=True)
        name = folder / f"{time.time_ns()}.json"
        tmp = name.with_suffix(".tmp")
        tmp.write_text(json.dumps(cmd))
        tmp.rename(name)

    def _send_power(self, power: str, tile: tuple[int, int], now: float):
        """El daemon valida el costo y lo aplica en el próximo tick."""
        self._send_command({"power": power, "x": tile[0], "y": tile[1]})
        self.painter.toast(ui("powers.sent", power=words("powers")[power][0]), now)

    # zoom y paneo (modo ventana) ---------------------------------------------

    def _zoom_at(self, delta: int, x: float, y: float):
        scales = zoom_scales(self._base)
        new = max(0, min(len(scales) - 1, self.zoom + delta))
        if new == self.zoom:
            return
        world_pt = self.painter.to_world(x / self._scale, y / self._scale)
        self.zoom = new
        scale = scales[new]
        w, h = self.view.get_width(), self.view.get_height()
        fw, fh = int(-(-w // scale)), int(-(-h // scale))
        if self.zoom == 0:
            self.painter.pan = [0.0, 0.0]
        else:
            self.painter.keep_point(world_pt, x / scale, y / scale, fw, fh)
        self._render()

    def _zoom_reset(self):
        self.zoom = 0
        self.painter.pan = [0.0, 0.0]
        self._render()

    def _on_scroll(self, ctrl, dx, dy):
        x, y = self._pointer
        panel = self.painter.ui_region(x / self._base, y / self._base)
        if panel in ("hud", "chron"):
            # rueda sobre un panel: recorrerlo (crónica: hacia atrás en el tiempo)
            step = 1 if dy > 0 else -1
            self.painter.scroll_panel(panel, step if panel == "hud" else -step)
            self._render()
        else:
            self._zoom_at(-1 if dy > 0 else 1, x, y)
        return True

    def _focus_tile(self, x: int, y: int, now: float):
        if self.zoom < 2 and not self.layered:
            self.zoom = min(2, len(zoom_scales(self._base)) - 1)
        self.painter.focus_tile(x, y)
        self._render()
        w, h = self.view.get_width(), self.view.get_height()
        self.painter.inspect(w / 2 / self._scale, h / 2 / self._scale, now, ui_pos=(w / 2 / self._base, h / 2 / self._base))
        self._render()

    def _focus_realm(self, fid: int, now: float):
        """Viaja a la capital de un reino (con algo de zoom) y abre su ficha."""
        f = next((f for f in (self.painter.state or {}).get("factions", []) if f["id"] == fid), None)
        if not f:
            return
        if self.zoom < 2 and not self.layered:
            self.zoom = min(2, len(zoom_scales(self._base)) - 1)
        self.painter.focus_tile(*f["capital"])
        self._render()                                  # recalcula cámara con el zoom nuevo
        w, h = self.view.get_width(), self.view.get_height()
        cx, cy = w / 2, h / 2
        self.painter.inspect(cx / self._scale, cy / self._scale, now, ui_pos=(cx / self._base, cy / self._base))

    def _on_drag_begin(self, gesture, x, y):
        self._drag = {"start": (x, y), "pan": list(self.painter.pan), "moved": False}

    def _on_drag_update(self, gesture, ox, oy):
        d = self._drag
        if d is None or (not d["moved"] and abs(ox) + abs(oy) < 5):
            return
        d["moved"] = True
        if self.zoom == 0:          # sin zoom: arrastrar mueve la ventana (no hay barra de título)
            sx, sy = d["start"]
            self.win.get_surface().begin_move(gesture.get_device(), 1, sx + ox, sy + oy,
                                              gesture.get_current_event_time())
            gesture.reset()
            self._drag = None
            return
        self.painter.pan = [d["pan"][0] - ox / self._scale, d["pan"][1] - oy / self._scale]
        self._render()

    def _on_motion(self, ctrl, x, y):
        self._pointer = (x, y)
        if self.paused:
            return
        tile = self.painter.pick(x / self._scale, y / self._scale)
        if tile != self.painter.hover:
            self.painter.hover = tile
            self._render()

    def _on_leave(self, ctrl):
        self.painter.hover = None

    # -- monitor y oclusión --------------------------------------------------

    def _on_realize(self, win):
        surface = win.get_surface()
        if self.layered and not self.interactive:
            surface.set_input_region(cairo.Region())   # los clics atraviesan el fondo
        surface.connect("enter-monitor", self._on_enter_monitor)

    def _on_enter_monitor(self, surface, monitor: Gdk.Monitor):
        geo = monitor.get_geometry()
        log(f"en monitor {monitor.get_connector()} {geo.width}x{geo.height}")
        if self.occl:
            self.occl.output_name = monitor.get_connector()
            self.occl.output_size = (geo.width, geo.height)
            self.occl.recompute()
            self._apply_occlusion()

    def _on_wayland(self, chan, cond):
        try:
            if self.occl.dispatch():
                self._apply_occlusion()
            return GLib.SOURCE_CONTINUE
        except (ConnectionError, OSError) as e:
            log(f"oclusión desactivada: {e}")
            self.occl = None
            self._resume()
            return GLib.SOURCE_REMOVE

    def _apply_occlusion(self):
        if self.occl.covered and not self.paused:
            self._pause()
        elif not self.occl.covered and self.paused:
            self._resume()

    def _pause(self):
        self.paused = True
        for src in (self._frame_src, self._poll_src):
            if src:
                GLib.source_remove(src)
        self._frame_src = self._poll_src = None
        self._fps = 0
        log("pausado: fondo tapado")

    def _resume(self):
        if self.paused:
            log("reanudado")
        self.paused = False
        self._poll_state()
        if not self._poll_src:
            self._poll_src = GLib.timeout_add_seconds(1, self._poll_state)
        self._render()
        self._schedule(self._wanted_fps())

    # -- bucle ---------------------------------------------------------------

    def _wanted_fps(self) -> int:
        return self.max_fps if self.painter.wants_fast(time.monotonic()) else self.idle_fps

    def _schedule(self, fps: int):
        if self._frame_src:
            GLib.source_remove(self._frame_src)
            self._frame_src = None
        self._fps = fps
        if fps > 0 and not self.paused:
            self._frame_src = GLib.timeout_add(max(1, round(1000 / fps)), self._on_frame)

    def _on_frame(self):
        self._render()
        want = self._wanted_fps()
        if want != self._fps:
            self._frame_src = None      # este source muere al devolver REMOVE
            self._schedule(want)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _poll_state(self):
        """stat() una vez por segundo: más simple y robusto que inotify con rename."""
        if not self.layered:
            self._check_focus_request()
        try:
            mtime = self.state_path.stat().st_mtime_ns
            if mtime != self._mtime:
                self._mtime = mtime
                self.painter.set_state(json.loads(self.state_path.read_text()), time.monotonic())
                if self._fps == 0 and not self.paused:   # idle_fps = 0: pintar solo al cambiar
                    self._render()
                    self._schedule(self._wanted_fps())
                elif self._fps < self._wanted_fps():
                    self._schedule(self._wanted_fps())
        except (OSError, ValueError):
            pass
        return GLib.SOURCE_CONTINUE

    def _check_focus_request(self):
        """Alguien tocó una notificación: ir a ese lugar del mapa."""
        from ..notify import focus_file
        f = focus_file()
        if not f.exists():
            return
        try:
            req = json.loads(f.read_text())
            f.unlink()
        except (OSError, ValueError):
            return
        if time.time() - req.get("t", 0) < 60 and self.painter.state:
            self.win.present()
            self._focus_tile(req["x"], req["y"], time.monotonic())

    def _render(self):
        w, h = self.view.get_width(), self.view.get_height()
        if w <= 0 or h <= 0:
            return
        now = time.monotonic()
        base = fit_scale(w, h, self.painter.content_size(), self.pixel_scale)
        scales = zoom_scales(base)
        self.zoom = min(self.zoom, len(scales) - 1)
        scale = scales[self.zoom]
        fw, fh = int(-(-w // scale)), int(-(-h // scale))
        # con zoom, sprites al doble de resolución: la textura mide fw*u y se escala scale/u
        self.painter.set_detail(detail_for(scale) if self.zoom else 1)
        self.view.layers[0] = [_texture(self.painter.frame(fw, fh, now)), scale / self.painter.u]
        ui, changed = self.painter.ui(int(-(-w // base)), int(-(-h // base)), now)
        if changed or self.view.layers[1][0] is None or self.view.layers[1][1] != base:
            self.view.layers[1] = [_texture(ui), base]
        self._scale, self._base = scale, base
        self.view.queue_draw()


def run(args, cfg: dict):
    # La ventana es una sola: abrirla de nuevo (lanzador o terminal) trae al frente la existente.
    # Los fondos de pantalla son uno por monitor, así que esos sí pueden ser varios.
    if args.windowed:
        app = Gtk.Application(application_id="dev.kernelkingdom.Window", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    else:
        app = Gtk.Application(application_id="dev.kernelkingdom.Wallpaper", flags=Gio.ApplicationFlags.NON_UNIQUE)
    holder = {}

    def activate(a):
        if "wp" in holder:
            holder["wp"].win.present()
        else:
            holder["wp"] = Wallpaper(a, args, cfg)
    app.connect("activate", activate)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, app.quit)
    app.run([])
