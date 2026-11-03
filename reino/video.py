"""El video del día: el capítulo de los Anales narrado sobre el mapa.

La voz es Piper (TTS libre, corre en la CPU, sin internet después de la primera
descarga): el programa y la voz se bajan una sola vez a data_dir()/voice. Cada
parte del relato se sintetiza; la cámara va a donde pasó lo que se cuenta y se
queda ahí mientras dura la frase; los subtítulos se dibujan en cada cuadro; ffmpeg
junta imagen y voz (con un filtro de "narrador": un poco más grave, con cuerpo).
"""
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

from . import annals, i18n, paths, story

PIPER = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_{arch}.tar.gz"
VOICES = "https://huggingface.co/rhasspy/piper-voices/resolve/main/{path}"
DEFAULT_VOICE = {"es": "es_ES-davefx-medium", "en": "en_US-ryan-medium"}
W, H, FPS = 1280, 720, 24
GAP = 0.7                                   # silencio entre partes del relato (s)


class VideoError(Exception):
    pass


def voice_dir() -> Path:
    d = paths.data_dir() / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def voice_name(cfg: dict) -> str:
    vc = cfg.get("video", {})
    return vc.get(f"voice_{i18n.LANG}") or DEFAULT_VOICE.get(i18n.LANG, DEFAULT_VOICE["en"])


def missing_downloads(cfg: dict) -> list[str]:
    """Lo que falta bajar (para avisar antes): el programa y/o la voz."""
    out = []
    if not (voice_dir() / "piper" / "piper").exists():
        out.append("Piper (~25 MB)")
    if not (voice_dir() / f"{voice_name(cfg)}.onnx").exists():
        out.append(f"{voice_name(cfg)} (~60 MB)")
    return out


def _download(url: str, dest: Path):
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "kernel-kingdom"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dest)


def ensure_piper() -> Path:
    exe = voice_dir() / "piper" / "piper"
    if exe.exists():
        return exe
    arch = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine())
    if not arch:
        raise VideoError(f"Piper no tiene binario para {platform.machine()}")
    tgz = voice_dir() / "piper.tar.gz"
    _download(PIPER.format(arch=arch), tgz)
    with tarfile.open(tgz) as t:
        root = voice_dir().resolve()
        for m in t.getmembers():                       # nada fuera de la carpeta, nada de links raros
            target = (root / m.name).resolve()
            if not str(target).startswith(str(root)) or m.islnk() or (m.issym() and os.path.isabs(m.linkname)):
                raise VideoError("el paquete de Piper trae rutas inesperadas")
        try:
            t.extractall(root, filter="data")
        except TypeError:                              # Python sin filtros de tar (3.11 viejo)
            t.extractall(root)
    tgz.unlink(missing_ok=True)
    return exe


def ensure_voice(name: str) -> Path:
    model = voice_dir() / f"{name}.onnx"
    if model.exists() and model.with_suffix(".onnx.json").exists():
        return model
    try:
        code, speaker, quality = name.split("-")
    except ValueError:
        raise VideoError(f"nombre de voz raro: {name}")
    base = f"{code[:2]}/{code}/{speaker}/{quality}/{name}"
    for suffix in (".onnx.json", ".onnx"):
        _download(VOICES.format(path=base + suffix), voice_dir() / f"{name}{suffix}")
    return model


def _encoder() -> tuple[list[str], str]:
    """El mejor encoder disponible: H.264 (mp4), si no VP9 (webm), si no AV1 (mp4)."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if " libx264 " in out:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k"], "mp4"
    if " libvpx-vp9 " in out:
        return ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "38", "-deadline", "realtime", "-cpu-used", "8",
                "-row-mt", "1", "-pix_fmt", "yuv420p", "-c:a", "libopus", "-b:a", "96k"], "webm"
    if " libsvtav1 " in out:
        return ["-c:v", "libsvtav1", "-preset", "10", "-crf", "32", "-pix_fmt", "yuv420p",
                "-c:a", "libopus", "-b:a", "96k"], "mp4"
    raise VideoError("ffmpeg no tiene un encoder de video que sirva (x264, VP9 o AV1)")


def _synth(exe: Path, model: Path, text: str, out: Path) -> float:
    """Una parte del relato a WAV (con el filtro de narrador). Devuelve la duración."""
    raw = out.with_suffix(".raw.wav")
    r = subprocess.run([str(exe), "--model", str(model), "--output_file", str(raw), "--length_scale", "1.08",
                        "--sentence_silence", "0.35"], input=text.encode(), capture_output=True)
    if r.returncode or not raw.exists():
        raise VideoError("Piper no pudo sintetizar la voz")
    rate = json.loads(model.with_suffix(".onnx.json").read_text())["audio"]["sample_rate"]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-af",
                    f"asetrate={rate}*0.93,aresample=44100,atempo=1.04,bass=g=4:f=110,"
                    f"aecho=0.8:0.6:45|90:0.15|0.06", "-ac", "1", "-ar", "44100", str(out)], check=True)
    raw.unlink(missing_ok=True)
    with wave.open(str(out)) as w:
        return w.getnframes() / w.getframerate()


def _join(parts: list[Path], gap: float, out: Path):
    """Pega los WAV con un silencio entre partes (todos mono 44.1 kHz)."""
    with wave.open(str(out), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(44100)
        silence = b"\0\0" * int(44100 * gap)
        dst.writeframes(silence)
        for p in parts:
            with wave.open(str(p)) as src:
                dst.writeframes(src.readframes(src.getnframes()))
            dst.writeframes(silence)


def make(day: str | None = None, out_dir: Path | None = None, cfg: dict | None = None, progress=print) -> Path:
    """Arma el video del día y devuelve la ruta. `progress(texto)` recibe avisos."""
    import cairo
    from .render.iso import Painter, TW, detail_for, fit_scale, zoom_scales

    cfg = cfg or {}
    day = day or time.strftime("%Y-%m-%d")
    entries = story.load(day)
    if not entries:
        raise VideoError(i18n.tr("story.empty", date=day))
    try:
        state = json.loads(paths.state_file().read_text())
    except (OSError, ValueError):
        raise VideoError("no encuentro el estado del mundo (¿está corriendo el daemon?)")
    ch = annals.chapter(day, entries)
    parts = [dict(p, text=story.scrub(p["text"])) for p in ch["narration"]]
    progress(i18n.ui("video.voice"))
    exe = ensure_piper()
    model = ensure_voice(voice_name(cfg))
    encoder, ext = _encoder()
    out_dir = out_dir or annals.folder()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"kernel-kingdom-{day}.{ext}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wavs, timeline, t = [], [], GAP
        for k, part in enumerate(parts):
            progress(i18n.ui("video.speaking", n=k + 1, total=len(parts)))
            wav = tmp / f"{k:02d}.wav"
            dur = _synth(exe, model, part["text"], wav)
            wavs.append(wav)
            timeline.append((t, t + dur, part))
            t += dur + GAP
        narration = tmp / "narration.wav"
        _join(wavs, GAP, narration)
        total = t + 1.5

        # imagen: la cámara viaja a donde pasó cada cosa y se queda mientras se cuenta
        p = Painter()
        p.window_mode = True
        p.view["labels"] = True
        p.set_state(state, 0.0)
        base = fit_scale(W, H, p.content_size(), "auto")
        scales = zoom_scales(base)
        close = min(range(1, len(scales)), key=lambda k: abs(scales[k] - W / (TW * 18)), default=0)
        ww, wh = p.world_size()

        def target(part):
            if part["where"]:
                x, y = part["where"]
                cx, cy = p.tile_xy(x, y, p.elev_at(x, y))
                return close, [cx - ww / 2, cy - wh / 2]
            return 0, [0.0, 0.0]

        frame = cairo.ImageSurface(cairo.FORMAT_RGB24, W, H)
        ctx = cairo.Context(frame)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}",
               "-r", str(FPS), "-i", "-", "-i", str(narration), *encoder, "-shortest", str(out)]
        ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        frames = int(total * FPS)
        pan, zoom = [0.0, 0.0], 0
        try:
            for f in range(frames):
                now = f / FPS
                cur = next((s for s in timeline if s[0] - GAP <= now < s[1] + GAP), None)
                z, goal = target(cur[2]) if cur else (0, [0.0, 0.0])
                if z != zoom:                               # cambio de escala: corte directo
                    zoom, pan = z, list(goal)
                pan = [a + (b - a) * 0.08 for a, b in zip(pan, goal)]
                scale = scales[zoom]
                fw, fh = int(-(-W // scale)), int(-(-H // scale))
                p.set_detail(detail_for(scale) if zoom else 1)
                p.pan = pan
                world = p.frame(fw, fh, 100.0 + now)
                ctx.save()
                ctx.scale(scale / p.u, scale / p.u)
                ctx.set_source_surface(world, 0, 0)
                ctx.get_source().set_filter(cairo.FILTER_NEAREST)
                ctx.paint()
                ctx.restore()
                if f < FPS * (timeline[0][1] + GAP):        # título del capítulo (es lo que se está diciendo)
                    _title_card(ctx, ch, min(1.0, now / 0.8))
                elif cur and now >= cur[0]:
                    _subtitle(ctx, cur[2]["text"])
                frame.flush()
                ff.stdin.write(bytes(frame.get_data()))
                if f % (FPS * 10) == 0:
                    progress(i18n.ui("video.frames", pct=int(100 * f / frames)))
        finally:
            ff.stdin.close()
            ff.wait()
        if ff.returncode:
            raise VideoError("ffmpeg no pudo armar el video")
    return out


def _wrap(ctx, text: str, width: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and ctx.text_extents(trial).x_advance > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + ([cur] if cur else [])


def _subtitle(ctx, text: str):
    ctx.select_font_face("Liberation Serif", 0, 1)
    ctx.set_font_size(28)
    lines = _wrap(ctx, text, W - 200)[:3]
    h = 38 * len(lines) + 24
    y0 = H - h - 30
    ctx.rectangle(80, y0, W - 160, h)
    ctx.set_source_rgba(0.05, 0.05, 0.08, 0.72)
    ctx.fill()
    for k, line in enumerate(lines):
        w = ctx.text_extents(line).x_advance
        ctx.move_to((W - w) / 2, y0 + 40 + 38 * k)
        ctx.set_source_rgb(0.97, 0.95, 0.88)
        ctx.show_text(line)


def _title_card(ctx, ch: dict, alpha: float):
    ctx.rectangle(0, 0, W, H)
    ctx.set_source_rgba(0.04, 0.04, 0.07, 0.55 * alpha)
    ctx.fill()
    ctx.select_font_face("Liberation Serif", 0, 1)
    for text, size, y, col in ((ch["world"], 30, H / 2 - 70, (0.85, 0.82, 0.72)),
                               (i18n.words("annals_chapter").format(n=ch["n"], title=ch["title"]), 46, H / 2,
                                (0.96, 0.84, 0.43)),
                               (ch["date"], 26, H / 2 + 56, (0.85, 0.82, 0.72))):
        ctx.set_font_size(size)
        lines = _wrap(ctx, text, W - 160)[:2]
        for k, line in enumerate(lines):
            w = ctx.text_extents(line).x_advance
            ctx.move_to((W - w) / 2, y + k * (size + 8))
            ctx.set_source_rgba(*col, alpha)
            ctx.show_text(line)
