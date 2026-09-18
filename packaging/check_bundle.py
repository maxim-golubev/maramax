"""Check a standalone bundle without opening devices, showing UI, or pasting.

Optional --audio runs the bundled Parakeet engine against an existing file.
Weights must already be cached: network model downloads are disabled.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace


def check(args: argparse.Namespace) -> dict:
    resources = args.bundle / "Contents" / "Resources"
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    # Remove the checkout, user site, and system packages from import lookup.
    sys.path[:] = [
        str(resources / "lib" / f"python{version}"),
        str(resources / "lib" / f"python{version}" / "lib-dynload"),
        str(resources / "lib" / f"python{version.replace('.', '')}.zip"),
        str(resources),
    ]
    from ctypes.macholib import dyld

    frameworks = str(resources.parent / "Frameworks")
    dyld.DEFAULT_FRAMEWORK_FALLBACK.insert(0, frameworks)
    dyld.DEFAULT_LIBRARY_FALLBACK.insert(0, frameworks)

    started = time.perf_counter()
    modules = [
        "parakeet_dictation.app", "parakeet_dictation.recorder",
        "parakeet_dictation.recordings_window", "mlx.core", "parakeet_mlx",
        "parakeet_dictation.preferences", "parakeet_dictation.instance",
        "qwen3_asr_mlx", "pyaudio", "soundfile", "scipy", "numpy",
        "tokenizers", "huggingface_hub", "httpx", "certifi", "AppKit",
    ]
    origins = {}
    for name in modules:
        module = importlib.import_module(name)
        origin = str(module.__file__)
        if not origin.startswith(str(resources) + os.sep):
            raise RuntimeError(f"{name} came from outside the bundle: {origin}")
        origins[name] = origin.removeprefix(str(resources) + os.sep)

    from parakeet_dictation.paths import ensure_runtime_path, ensure_ssl_certs
    import ssl

    ensure_runtime_path()
    ensure_ssl_certs()
    ssl.create_default_context()

    from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
    from parakeet_dictation.indicator import DictationIndicator
    from parakeet_dictation.recordings import RecordingStore
    from parakeet_dictation.config import AppConfig
    from parakeet_dictation.preferences import PreferencesController
    from parakeet_dictation.recordings_window import RecordingsController
    from parakeet_dictation.overlay import OverlayController
    from parakeet_dictation.app import _SETTING_LABELS

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    # Creating the native view does not order it onto the screen.
    indicator = DictationIndicator.alloc().initWithDelegate_(None)
    assert not indicator.panel.isVisible()
    assert not indicator.panel.canBecomeKeyWindow()
    assert not indicator.panel.canBecomeMainWindow()

    with tempfile.TemporaryDirectory(prefix="maramax-bundle-check-") as directory:
        store = RecordingStore(Path(directory))
        delegate = SimpleNamespace(config=AppConfig(), recording_active=False, is_transcribing=False,
                                   transcriber=SimpleNamespace(is_ready=lambda: True, load_error=None))
        preferences = PreferencesController.alloc().initWithDelegate_labels_(delegate, _SETTING_LABELS)
        recordings = RecordingsController.alloc().initWithDelegate_store_(delegate, store)
        overlay = OverlayController.alloc().initWithDelegate_config_(delegate, delegate.config)
        for controller in (preferences, recordings, overlay):
            assert not controller.panel.isVisible()
        assert recordings.sound is None
        pcm = b"\x01\x00" * 16000
        recording = store.save(pcm, {"validation": True})
        assert store.load_pcm(recording.id) == pcm
        store.update(recording.id, status="failed", message="Synthetic validation", raw_text="Original")
        recordings.refresh()
        assert recordings.records[0].raw_text == "Original"
        assert store.list_recordings()[0].status == "failed"
        store.clear()
        assert not store.list_recordings()

    report = {
        "bundle": str(args.bundle),
        "python": sys.version.split()[0],
        "imports": origins,
        "component_check_seconds": round(time.perf_counter() - started, 3),
        "checks": ["isolated bundled imports", "TLS certificates", "hidden passive panel",
                   "hidden settings, transcript, and recovery windows",
                   "audio archive round trip and deletion"],
        "microphone_opened": False,
        "audio_played": False,
    }
    if args.audio:
        from parakeet_dictation.transcription import ParakeetTranscriber, normalize_media
        import mlx.core as mx
        import wave

        started = time.perf_counter()
        transcriber = ParakeetTranscriber()
        transcriber.wait_until_ready()
        load_seconds = time.perf_counter() - started
        normalized = normalize_media(args.audio)
        try:
            with wave.open(normalized, "rb") as audio:
                channels, width, rate = audio.getnchannels(), audio.getsampwidth(), audio.getframerate()
                pcm = audio.readframes(audio.getnframes())
            results = []
            for _ in range(args.repeats):
                started = time.perf_counter()
                text = transcriber.transcribe_pcm(pcm, channels, width, rate)
                results.append({
                    "seconds": round(time.perf_counter() - started, 3), "text": text,
                    "mlx_active_bytes": mx.get_active_memory(),
                    "mlx_cache_bytes": mx.get_cache_memory(),
                    "python_threads": threading.active_count(),
                    "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                })
            durations = [result["seconds"] for result in results]
            report["recognition"] = {
                "model": transcriber.model_id,
                "audio_seconds": len(pcm) / (channels * width * rate),
                "load_and_warm_seconds": round(load_seconds, 3),
                "runs": results,
                "median_seconds": statistics.median(durations),
                "max_seconds": max(durations),
                "scope": "PCM to transcript; excludes capture, UI, clipboard, and insertion",
            }
        finally:
            Path(normalized).unlink(missing_ok=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path("dist/Maramax.app"))
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    args.bundle = args.bundle.resolve()
    args.audio = args.audio.resolve() if args.audio else None
    args.output = args.output.resolve() if args.output else None
    if args.worker:
        report = check(args)
        text = json.dumps(report, indent=2) + "\n"
        if args.output:
            args.output.write_text(text)
        print(text)
        return

    launcher = args.bundle / "Contents" / "MacOS" / "Maramax"
    ping = subprocess.run([str(launcher), "--audio-worker"], input='{"operation":"ping"}\n',
                          capture_output=True, text=True, check=True, timeout=15)
    response = json.loads(ping.stdout)
    assert response["event"] == "pong"
    assert response["command"] == [str(launcher), "--audio-worker"]
    resources = args.bundle / "Contents" / "Resources"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(PYTHONHOME=str(resources), RESOURCEPATH=str(resources),
               PYTHONNOUSERSITE="1", HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
               TOKENIZERS_PARALLELISM="false")
    command = [str(args.bundle / "Contents" / "MacOS" / "python"), "-S",
               str(Path(__file__).resolve()), "--worker", "--bundle", str(args.bundle),
               "--repeats", str(args.repeats)]
    if args.audio:
        command.extend(["--audio", str(args.audio)])
    if args.output:
        command.extend(["--output", str(args.output)])
    subprocess.run(command, env=env, cwd=resources, check=True, timeout=300)


if __name__ == "__main__":
    main()
