"""Síntesis con Kokoro, en su propio entorno (data_dir()/voice/kokoro-venv).

Lo ejecuta reino.video con el Python de ese entorno, así kokoro-onnx y onnxruntime
no se mezclan con la app. Lee por stdin un JSON:
    {"model": ruta, "voices": ruta, "voice": "em_alex", "lang": "es", "speed": 0.92,
     "parts": [{"text": "...", "out": "/tmp/00.wav"}, ...]}
escribe un WAV mono por parte e imprime la lista de duraciones (segundos) en JSON.
No importa nada de reino: solo la biblioteca estándar, numpy y kokoro_onnx.
"""
import json
import sys
import wave


def main():
    import numpy as np
    from kokoro_onnx import Kokoro

    job = json.load(sys.stdin)
    k = Kokoro(job["model"], job["voices"])
    durations = []
    for part in job["parts"]:
        samples, rate = k.create(part["text"], voice=job["voice"], speed=job.get("speed", 0.92),
                                 lang=job.get("lang", "es"))
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
        with wave.open(part["out"], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())
        durations.append(len(samples) / rate)
    json.dump(durations, sys.stdout)


if __name__ == "__main__":
    main()
