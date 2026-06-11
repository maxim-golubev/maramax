from pathlib import Path

from setuptools import setup


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "Maramax"
VERSION = "0.3.0"

OPTIONS = {
    "argv_emulation": False,
    "strip": False,
    "optimize": 0,
    "packages": [
        "parakeet_dictation",
        "parakeet_mlx",
        "qwen3_asr_mlx",
        "tokenizers",
        "_soundfile_data",
        "cffi",
        "pycparser",
        "rumps",
        "pyperclip",
        "dotenv",
        # huggingface_hub 1.x networking stack (httpx); incomplete bundling
        # makes from_pretrained silently fall back to local-path resolution.
        "huggingface_hub",
        "httpx",
        "httpcore",
        "anyio",
        "h11",
        "idna",
        "certifi",
        "hf_xet",
    ],
    "includes": [
        "AppKit",
        "Foundation",
        "PyObjCTools",
        "PyObjCTools.AppHelper",
        "pyaudio",
        "numpy",
        "wave",
        "ctypes",
        "soundfile",
        "_soundfile",
        "_cffi_backend",
    ],
    "excludes": [
        "mlx",
        "mlx.core",
        "scipy",
        "charset_normalizer",
    ],
    "resources": [str(ROOT / "assets")],
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "com.maramax.dictation",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Maramax needs microphone access to transcribe your speech locally.",
    },
}


setup(
    app=["maramax_app.py"],
    name=APP_NAME,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
