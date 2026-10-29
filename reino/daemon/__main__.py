"""Daemon de monitoreo: muestrea el sistema, avanza el mundo y publica state.json.

    python -m reino.daemon            # loop normal
    python -m reino.daemon --once     # una muestra, imprime el estado y sale
    python -m reino.daemon --chronicle  # imprime la crónica guardada
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .. import config, i18n, paths, story
from .inputs import InputActivity
from .probes import SystemProbe
from .world import World


def write_atomic(path: Path, data: dict):
    """tmp + rename: el renderer nunca lee un JSON a medio escribir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    os.replace(tmp, path)


def take_commands() -> list[dict]:
    """Lee y borra los pedidos de poderes que dejó el renderer (máx. 10 por tick)."""
    folder = paths.commands_dir()
    out = []
    for f in sorted(folder.glob("*.json"))[:10] if folder.exists() else []:
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            pass
        f.unlink(missing_ok=True)
    return out


def build_state(world: World, snap: dict, events: list[dict], interval: float) -> dict:
    state = world.to_state()
    state.update(
        t=snap["t"],
        interval=interval,
        events=events,
        metrics={k: snap.get(k) for k in ("cpu", "ram", "ram_used_mb", "ram_total_mb", "swap", "load1",
                                          "cores", "procs", "top", "net_rx", "net_tx", "disk_r", "disk_w",
                                          "cpu_temp", "births", "deaths")},
    )
    return state


def main():
    ap = argparse.ArgumentParser(prog="reino.daemon")
    ap.add_argument("--config", type=Path, help="config extra a mezclar")
    ap.add_argument("--interval", type=float)
    ap.add_argument("--once", action="store_true", help="imprimir un estado y salir")
    ap.add_argument("--reset", action="store_true", help="regenerar el mundo desde cero")
    ap.add_argument("--chronicle", action="store_true", help="imprimir la crónica guardada y salir")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cfg = config.load(args.config)
    i18n.set_lang(cfg.get("language", "es"))
    interval = args.interval or cfg["daemon"]["interval"]
    save_path, state_path = paths.save_file(), paths.state_file()

    if args.chronicle:
        world = World.load_or_new(cfg, save_path)
        for e in world.chronicle.entries:
            print(time.strftime("%d/%m %H:%M", time.localtime(e["t"])), e["text"])
        return

    try:
        os.nice(cfg["daemon"]["nice"])
    except OSError:
        pass

    probe = SystemProbe(cfg)
    inputs = InputActivity()
    world = World(cfg) if args.reset else World.load_or_new(cfg, save_path)
    stop = threading.Event()

    if args.once:
        inputs.wait(min(interval, 1.0), stop)  # para tener deltas de CPU
        snap = probe.sample()
        snap["input"] = inputs.take(snap["dt"] or 1.0)
        events = world.step(snap)
        json.dump(build_state(world, snap, events, interval), sys.stdout, ensure_ascii=False)
        return

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    print(f"kernel-kingdom daemon: estado en {state_path}, mundo en {save_path}, "
          f"actividad vía {inputs.source}", file=sys.stderr)
    if inputs.keyboards.denied and not inputs.keyboards.fds:
        print("kernel-kingdom daemon: sin acceso a /dev/input -> no se cuentan teclas, solo presencia", file=sys.stderr)
    next_save = time.monotonic() + cfg["daemon"]["save_every"]
    day = time.strftime("%Y-%m-%d")
    while not stop.is_set():
        t0 = time.monotonic()
        snap = probe.sample()
        snap["input"] = inputs.take(snap["dt"] or interval)
        events = world.step(snap, take_commands())
        story.archive(world.chronicle.take_fresh())        # historia del día, sin límite de entradas
        today = time.strftime("%Y-%m-%d")
        if today != day:                                   # cambió el día: se escribe el relato de ayer
            story.export(day, world.kingdom, world.lore.era_name(world.era))
            day = today
        write_atomic(state_path, build_state(world, snap, events, interval))
        if t0 >= next_save:
            write_atomic(save_path, world.to_dict())
            next_save = t0 + cfg["daemon"]["save_every"]
        cost = time.monotonic() - t0
        if args.verbose:
            w = world.weather
            print(f"tick {world.tick} {cost * 1000:.0f}ms cpu={snap['cpu']:.0f}% ram={snap['ram']:.0f}% "
                  f"net={snap['net_rx']:.1f}/{snap['net_tx']:.1f}MB/s act={snap['input']['active']} "
                  f"storm={w['storm']} events={len(events)}", file=sys.stderr)
        deadline = t0 + interval
        while not stop.is_set() and (left := deadline - time.monotonic()) > 0:
            inputs.wait(min(left, 0.5), stop)

    write_atomic(save_path, world.to_dict())


if __name__ == "__main__":
    main()
