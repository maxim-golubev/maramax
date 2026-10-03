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
        "parakeet_dictation.app", "parakeet_dictation.recorder", "parakeet_dictation.audio_worker",
        "parakeet_dictation.recordings_window", "mlx.core", "parakeet_mlx", "parakeet_mlx.alignment",
        "parakeet_dictation.preferences", "parakeet_dictation.instance",
        "parakeet_dictation.updater", "parakeet_dictation.update_offer", "parakeet_dictation.update_window",
        "parakeet_dictation.update_prompt", "parakeet_dictation.replacements_editor",
        "parakeet_dictation.welcome", "parakeet_dictation.shortcut_picker",
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
    from parakeet_dictation.hotkeys import DEFAULT_DICTATE
    from parakeet_dictation.recordings import RecordingStatus

    # The spectrogram front end pulls in a filter-bank dependency that no
    # import above touches; build one and run audio through it. No model.
    import mlx.core as mx
    from parakeet_mlx.audio import PreprocessArgs, get_logmel

    front_end = PreprocessArgs(sample_rate=16000, normalize="per_feature", window_size=0.025,
                               window_stride=0.01, window="hann", features=128, n_fft=512, dither=1e-5)
    mel = get_logmel(mx.random.normal((16000,)), front_end)
    mx.eval(mel)
    if mel.shape[-1] != 128:
        raise RuntimeError(f"Spectrogram front end produced shape {mel.shape}, expected 128 features")

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    # Creating the native view does not order it onto the screen.
    indicator = DictationIndicator.alloc().initWithDelegate_(None)
    assert not indicator.panel.isVisible()
    assert not indicator.panel.canBecomeKeyWindow()
    assert not indicator.panel.canBecomeMainWindow()

    with tempfile.TemporaryDirectory(prefix="maramax-bundle-check-") as directory:
        store = RecordingStore(Path(directory))
        delegate = SimpleNamespace(
            config=AppConfig(), is_busy=False,
            transcriber=SimpleNamespace(load_error=None, status_message=lambda: "Speech model ready"),
            qwen=SimpleNamespace(status_message=lambda: "High-accuracy model ready"),
            updates=SimpleNamespace(status_text=lambda: "Not checked yet.", can_check=lambda: True),
            current_shortcut=lambda: DEFAULT_DICTATE, choose_shortcut=lambda key, modifiers: None,
            pause_shortcut=lambda: None, resume_shortcut=lambda: None,
            paste_permitted=lambda: False, choose_delivery=lambda paste: None,
            open_accessibility_settings=lambda: None, finish_welcome=lambda: None,
        )
        preferences = PreferencesController.alloc().initWithDelegate_labels_(delegate, _SETTING_LABELS)
        recordings = RecordingsController.alloc().initWithDelegate_store_(delegate, store)
        overlay = OverlayController.alloc().initWithDelegate_(delegate)
        from parakeet_dictation.update_prompt import UpdatePromptWindow
        from parakeet_dictation.update_window import UpdateProgressWindow
        from parakeet_dictation.welcome import WelcomeController
        update_window = UpdateProgressWindow.alloc().initWithCancel_(lambda: None)
        update_prompt = UpdatePromptWindow.alloc().initWithChoice_(lambda choice: None)
        welcome = WelcomeController.alloc().initWithDelegate_(delegate)
        for controller in (preferences, recordings, overlay, update_window, update_prompt, welcome):
            assert not controller.panel.isVisible()
        assert recordings.sound is None
        pcm = b"\x01\x00" * 16000
        recording = store.save(pcm, {"validation": True})
        assert store.load_pcm(recording.id) == pcm
        store.update(recording.id, status=RecordingStatus.FAILED, message="Synthetic validation", raw_text="Original")
        recordings.refresh()
        assert recordings.records[0].raw_text == "Original"
        assert store.list_recordings()[0].status == RecordingStatus.FAILED
        store.clear()
        assert not store.list_recordings()

    report = {
        "bundle": str(args.bundle),
        "python": sys.version.split()[0],
        "imports": origins,
        "component_check_seconds": round(time.perf_counter() - started, 3),
        "checks": ["isolated bundled imports", "TLS certificates", "spectrogram front end", "hidden passive panel",
                   "hidden settings, transcript, recovery, and update windows",
                   "audio archive round trip and deletion"],
        "microphone_opened": False,
        "audio_played": False,
    }
    if args.audio:
        from parakeet_dictation.audio_format import seconds as audio_seconds
        from parakeet_dictation.transcription import ParakeetTranscriber, normalize_media
        import wave

        started = time.perf_counter()
        transcriber = ParakeetTranscriber()
        transcriber.wait_until_ready()
        load_seconds = time.perf_counter() - started
        normalized = normalize_media(args.audio)
        try:
            # normalize_media produces the app's own PCM format.
            with wave.open(normalized, "rb") as audio:
                pcm = audio.readframes(audio.getnframes())
            results = []
            for _ in range(args.repeats):
                started = time.perf_counter()
                text = transcriber.transcribe_pcm(pcm)
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
                "audio_seconds": audio_seconds(pcm),
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
    # Two requests to one helper: it must stay alive between recordings (the
    # app keeps it on standby) and exit by itself when its input closes.
    ping = subprocess.run([str(launcher), "--audio-worker"], input='{"operation":"ping"}\n' * 2,
                          capture_output=True, text=True, check=True, timeout=15)
    responses = [json.loads(line) for line in ping.stdout.splitlines()]
    assert [response["event"] for response in responses] == ["pong", "pong"]
    assert responses[0]["command"] == [str(launcher), "--audio-worker"]
    resources = args.bundle / "Contents" / "Resources"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(PYTHONHOME=str(resources), RESOURCEPATH=str(resources),
               PYTHONNOUSERSITE="1", HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
               TOKENIZERS_PARALLELISM="false", PYTHONDONTWRITEBYTECODE="1")
    # -B: the bundle is signed, and bytecode written into it would break the seal.
    command = [str(args.bundle / "Contents" / "MacOS" / "python"), "-S", "-B",
               str(Path(__file__).resolve()), "--worker", "--bundle", str(args.bundle),
               "--repeats", str(args.repeats)]
    if args.audio:
        command.extend(["--audio", str(args.audio)])
    if args.output:
        command.extend(["--output", str(args.output)])
    subprocess.run(command, env=env, cwd=resources, check=True, timeout=300)
    # Whatever the check ran, the bundle must still be exactly what was signed.
    sealed = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(args.bundle)],
                            capture_output=True, text=True)
    if sealed.returncode != 0:
        sys.exit(f"The bundle check changed the bundle; its signature no longer holds: {sealed.stderr.strip()}")


if __name__ == "__main__":
    main()
