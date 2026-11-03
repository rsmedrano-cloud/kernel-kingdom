"""El video del día: el capítulo de los Anales narrado sobre el mapa.

La voz es local y corre en la CPU, sin internet después de la primera descarga
(todo va a data_dir()/voice):
- Kokoro (por defecto): voces naturales; se instala en su propio entorno de Python
  (kokoro-venv) para no mezclar onnxruntime con la app.
- Piper: más liviano y más robótico.
El texto es el relato del día (o la prosa del cronista, si hay). Cada frase se
sintetiza; la cámara va al reino, la isla o la región que nombra la frase y se queda
ahí mientras dura; los subtítulos se dibujan en cada cuadro; ffmpeg junta todo.
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
KOKORO = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/{name}"
KOKORO_PKG = "kokoro-onnx==0.4.7"
KOKORO_MODELS = {"full": ("kokoro-v1.0.onnx", 310), "light": ("kokoro-v1.0.int8.onnx", 88)}
DEFAULT_VOICE = {"kokoro": {"es": "em_alex", "en": "bm_george"},
                 "piper": {"es": "es_ES-davefx-medium", "en": "en_US-ryan-medium"}}
W, H, FPS = 1280, 720, 24
GAP = 0.7                                   # silencio entre partes del relato (s)


class VideoError(Exception):
    pass


def voice_dir() -> Path:
    d = paths.data_dir() / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def engine(cfg: dict) -> str:
    return "piper" if cfg.get("video", {}).get("engine") == "piper" else "kokoro"


def voice_name(cfg: dict) -> str:
    vc = cfg.get("video", {})
    defaults = DEFAULT_VOICE[engine(cfg)]
    return vc.get(f"voice_{i18n.LANG}") or defaults.get(i18n.LANG, defaults["en"])


def _kokoro_model(cfg: dict) -> tuple[str, int]:
    return KOKORO_MODELS["light" if cfg.get("video", {}).get("kokoro_model") == "light" else "full"]


def missing_downloads(cfg: dict) -> list[str]:
    """Lo que falta bajar (para avisar antes)."""
    out, d = [], voice_dir()
    if engine(cfg) == "kokoro":
        if not (d / "kokoro-venv" / "bin" / "python").exists():
            out.append("Kokoro + onnxruntime (~120 MB)")
        name, mb = _kokoro_model(cfg)
        if not (d / name).exists():
            out.append(f"{name} (~{mb} MB)")
        if not (d / "voices-v1.0.bin").exists():
            out.append("voices-v1.0.bin (~27 MB)")
        return out
    if not (d / "piper" / "piper").exists():
        out.append("Piper (~25 MB)")
    if not (d / f"{voice_name(cfg)}.onnx").exists():
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


def ensure_kokoro(cfg: dict) -> tuple[Path, Path, Path]:
    """El entorno de Kokoro (aparte de la app), el modelo y las voces."""
    d = voice_dir()
    py = d / "kokoro-venv" / "bin" / "python"
    if not py.exists():
        import sys
        subprocess.run([sys.executable, "-m", "venv", str(d / "kokoro-venv")], check=True, capture_output=True)
        r = subprocess.run([str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", KOKORO_PKG],
                           capture_output=True, text=True)
        if r.returncode:
            shutil.rmtree(d / "kokoro-venv", ignore_errors=True)
            raise VideoError("no se pudo instalar Kokoro: " + (r.stderr.strip().splitlines() or ["?"])[-1])
    name, _ = _kokoro_model(cfg)
    for f in (name, "voices-v1.0.bin"):
        if not (d / f).exists():
            _download(KOKORO.format(name=f), d / f)
    # el script se copia a la carpeta de voces y corre desde ahí: así ningún módulo de
    # la app (config.py, update.py...) puede tapar a uno de Kokoro
    job = d / "kokoro_job.py"
    job.write_text((Path(__file__).parent / "tts_kokoro.py").read_text(encoding="utf-8"), encoding="utf-8")
    return py, d / name, d / "voices-v1.0.bin"


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


def _finish(raw: Path, out: Path, depth: float = 0.9) -> float:
    """A mono 44.1 kHz con voz de narrador: más grave (`depth` < 1 baja el tono sin cambiar
    la velocidad), con más cuerpo en los graves y un poco de sala."""
    with wave.open(str(raw)) as w:
        rate = w.getframerate()
    depth = min(1.0, max(0.75, depth))
    af = (f"asetrate={rate}*{depth},aresample=44100,atempo={1 / depth:.4f},"
          "bass=g=6:f=95:w=0.8,equalizer=f=3200:t=q:w=1.2:g=-2,"
          "aecho=0.8:0.55:55|110:0.14|0.06,alimiter=limit=0.95")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-af", af, "-ac", "1", "-ar", "44100",
                    str(out)], check=True)
    raw.unlink(missing_ok=True)
    with wave.open(str(out)) as w:
        return w.getnframes() / w.getframerate()


def synthesize(texts: list[str], tmp: Path, cfg: dict, progress=print) -> list[tuple[Path, float]]:
    """Cada texto a un WAV (mono 44.1 kHz). Devuelve [(wav, duración)]."""
    out = []
    depth = float(cfg.get("video", {}).get("depth", 0.88))
    if engine(cfg) == "kokoro":
        py, model, voices = ensure_kokoro(cfg)
        voice = voice_name(cfg)
        lang = "es" if i18n.LANG == "es" else ("en-gb" if voice.startswith("b") else "en-us")
        job = {"model": str(model), "voices": str(voices), "voice": voice, "lang": lang,
               "speed": cfg.get("video", {}).get("speed", 0.92),
               "parts": [{"text": t, "out": str(tmp / f"{k:02d}.raw.wav")} for k, t in enumerate(texts)]}
        progress(i18n.ui("video.speaking", n=len(texts), total=len(texts)))
        r = subprocess.run([str(py), "-I", str(voice_dir() / "kokoro_job.py")], input=json.dumps(job),
                           capture_output=True, text=True, cwd=str(voice_dir()))
        if r.returncode:
            raise VideoError("Kokoro no pudo sintetizar: " + (r.stderr.strip().splitlines() or ["?"])[-1])
        for k in range(len(texts)):
            wav = tmp / f"{k:02d}.wav"
            out.append((wav, _finish(tmp / f"{k:02d}.raw.wav", wav, depth)))
        return out
    exe = ensure_piper()
    model = ensure_voice(voice_name(cfg))
    for k, text in enumerate(texts):
        progress(i18n.ui("video.speaking", n=k + 1, total=len(texts)))
        raw = tmp / f"{k:02d}.raw.wav"
        r = subprocess.run([str(exe), "--model", str(model), "--output_file", str(raw), "--length_scale", "1.08",
                            "--sentence_silence", "0.35"], input=text.encode(), capture_output=True)
        if r.returncode or not raw.exists():
            raise VideoError("Piper no pudo sintetizar la voz")
        wav = tmp / f"{k:02d}.wav"
        out.append((wav, _finish(raw, wav, depth)))
    return out


def narration(day: str, entries: list[dict], state: dict, cfg: dict) -> tuple[dict, list[dict]]:
    """El capítulo y lo que se dice: el título, y después el relato frase por frase (la
    prosa del cronista si la hay), cada frase con el lugar que nombra (reino, isla o región)."""
    ch = annals.chapter(day, entries)
    text = annals.bard_text(day) or annals.bard(ch, cfg) or "\n\n".join(ch["prose"])
    places = {}
    for r in state.get("regions") or []:
        if r.get("name"):
            places[r["name"]] = tuple(r["c"])
    for i in state.get("islands") or []:
        if i.get("c"):
            places[i["name"]] = tuple(i["c"])
    for f in state.get("factions", []):
        if f.get("alive") or f["realm"] not in places:
            places[f["realm"]] = tuple(f["capital"])
    names = sorted(places, key=len, reverse=True)               # "Zenharia" antes que "Zenhar"
    parts = [{"text": i18n.words("annals_chapter").format(n=ch["n"], title=ch["title"]) + ".", "where": None}]
    for sentence in annals._sentences(text):
        where = next((places[n] for n in names if n in sentence), None)
        parts.append({"text": story.scrub(sentence), "where": where})
    return ch, parts


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
    ch, parts = narration(day, entries, state, cfg)
    progress(i18n.ui("video.voice"))
    encoder, ext = _encoder()
    out_dir = out_dir or annals.folder()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"kernel-kingdom-{day}.{ext}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wavs, timeline, t = [], [], GAP
        for part, (wav, dur) in zip(parts, synthesize([p["text"] for p in parts], tmp, cfg, progress)):
            wavs.append(wav)
            timeline.append((t, t + dur, part))
            t += dur + GAP
        voice_wav = tmp / "narration.wav"
        _join(wavs, GAP, voice_wav)
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
               "-r", str(FPS), "-i", "-", "-i", str(voice_wav), *encoder, "-shortest", str(out)]
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
