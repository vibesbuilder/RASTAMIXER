# -*- coding: utf-8 -*-
"""
Shared helpers for the Overlay Mixer tests.

load_mixer() loads the mixer script without opening the production log
file or creating any production folders on import. MixerTestCase redirects
all path constants to fresh temporary folders for each test – production
folders are never touched.

By default the newest version from the version folders is tested
(vX.Y.Z\\overlay_mixer_watchdog_vX.Y.Z.py), because the tests always describe
the latest state. A different script can be selected with the environment
variable VAH_MIXER_SCRIPT (e.g. run_tests.bat overlay_mixer_watchdog.py for
the currently deployed file).
"""
import atexit
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parent.parent
PRODUCTION_SCRIPT = PROJECT_DIR / "overlay_mixer_watchdog.py"
MODULE_NAME = "overlay_mixer_watchdog"


def newest_version_script() -> Path:
    candidates = []
    for folder in PROJECT_DIR.glob("v*.*.*"):
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", folder.name)
        script = folder / f"overlay_mixer_watchdog_{folder.name}.py"
        if match and script.is_file():
            candidates.append((tuple(int(x) for x in match.groups()), script))
    return max(candidates)[1] if candidates else PRODUCTION_SCRIPT


def selected_script() -> Path:
    chosen = os.environ.get("VAH_MIXER_SCRIPT")
    if not chosen:
        return newest_version_script()
    path = Path(chosen)
    return path if path.is_absolute() else PROJECT_DIR / path


SCRIPT_PATH = selected_script()
LOGGER_NAME = "overlay_mixer"

# Path constants of the script -> name in the test folder
PATH_CONSTANTS = {
    "SCHNEIDEN_DIR": "Eingang",
    "ATMO_DIR": "Atmo",
    "FERTIG_DIR": "Import",
    "FEHLER_DIR": "Fehler",
    "ARCHIV_DIR": "Archive",
    "LOG_FILE": "overlay_mixer.log",
    "PID_FILE": "overlay_mixer.pid",
    "LOCAL_LOG_FILE": "logs/overlay_mixer.log",  # since v1.4.1
    "LOCK_FILE": "overlay_mixer.lock",           # since v1.4.1
    "CONFIG_FILE": "overlay_mixer.ini",          # since v2.0.0: never read the production configuration
}
DIR_CONSTANTS = ("SCHNEIDEN_DIR", "ATMO_DIR", "FERTIG_DIR", "FEHLER_DIR", "ARCHIV_DIR")

# Atmo lengths of the production atmo files according to ffprobe (as of 2026-10-08)
PRODUCTION_ATMO_SECONDS = {
    "Ambiente-Musik 32 Sekunden.mp3": 31.562336,
    "Ambiente-Musik 42 Sekunden.mp3": 42.076281,
    "Ambiente-Musik 53 Sekunden.mp3": 52.626440,
    "Ambiente-Musik 63 Sekunden.mp3": 63.133560,
}

# Silent test atmos for the FFmpeg tests (lengths close to the production atmos)
TEST_ATMO_SECONDS = {
    "Atmo 32.mp3": 31.5,
    "Atmo 42.mp3": 42.0,
    "Atmo 53.mp3": 52.5,
    "Atmo 63.mp3": 63.0,
}

_mixer = None
_atmo_fixture_dir = None


def load_mixer():
    """Loads the production script once and safely as a module."""
    global _mixer
    if _mixer is not None:
        return _mixer

    sys.dont_write_bytecode = True  # no __pycache__ in the production folder
    sys.stderr.write(f"\nTested script: {SCRIPT_PATH.relative_to(PROJECT_DIR)}\n\n")
    spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    # Up to v1.4.0 the script already opens the production log and creates production folders on import.
    # Files are therefore only "opened" in memory during the import.
    with mock.patch("logging.handlers.RotatingFileHandler._open", lambda self: io.StringIO()), \
         mock.patch("pathlib.Path.mkdir"):
        spec.loader.exec_module(module)

    # Disable the script's console output; tests check logs with assertLogs.
    module.logger.handlers.clear()
    module.logger.addHandler(logging.NullHandler())

    # Safety net: even without MixerTestCase, no path points to production.
    guard = Path(tempfile.gettempdir()) / "vah_overlay_mixer_tests_guard"
    for name, leaf in PATH_CONSTANTS.items():
        if hasattr(module, name):
            setattr(module, name, guard / leaf)

    _mixer = module
    return module


class MixerTestCase(unittest.TestCase):
    """Base class: fresh temporary folders per test, path constants redirected."""

    def setUp(self):
        self.mixer = load_mixer()
        tmp = tempfile.TemporaryDirectory(prefix="vah_test_", ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, leaf in PATH_CONSTANTS.items():
            if not hasattr(self.mixer, name):
                continue
            path = self.root / leaf
            if name in DIR_CONSTANTS:
                path.mkdir()
            patcher = mock.patch.object(self.mixer, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.eingang = self.mixer.SCHNEIDEN_DIR
        self.atmo = self.mixer.ATMO_DIR
        self.fertig = self.mixer.FERTIG_DIR
        self.fehler = self.mixer.FEHLER_DIR
        self.archiv = self.mixer.ARCHIV_DIR

    def install_test_atmos(self):
        """Copies the silent test atmos into the atmo test folder."""
        for f in atmo_fixture_dir().iterdir():
            shutil.copy2(f, self.atmo / f.name)

    @staticmethod
    def names(folder: Path):
        return sorted(p.name for p in folder.iterdir())


def wait_until(condition, timeout=5.0, interval=0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def start_thread(target, *args) -> threading.Thread:
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    return t


# -------------------------
# FFmpeg helpers
# -------------------------
FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
requires_ffmpeg = unittest.skipUnless(FFMPEG_AVAILABLE, "ffmpeg/ffprobe not in PATH")


def _run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result


def make_tone(path, seconds, frequency=880, channels=2, sample_rate=44100, codec=None, volume_db=None) -> Path:
    """
    Sine tone file; format and encoder follow from the extension (or codec).
    Loudness without volume_db: -21.4 LUFS (i.e. below the loudness target of -18 LUFS since v1.10.0).
    """
    source = f"sine=frequency={frequency}:duration={seconds}:sample_rate={sample_rate}"
    if volume_db is not None:
        source += f",volume={volume_db}dB"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", source,
           "-ac", str(channels)]
    if codec:
        cmd += ["-c:a", codec]
    cmd.append(str(path))
    _run(cmd)
    return Path(path)


def make_tone_with_spike(path, seconds, spike_at=2.0) -> Path:
    """Quiet sine tone (approx. -28.5 LUFS) with a short loud peak (5 ms, approx. -3 dBTP) – like a pop."""
    expr = (f"0.05*sin(2*PI*880*t)+if(between(t,{spike_at},{spike_at + 0.005}),0.9*sin(2*PI*440*t),0)")
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
          "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=44100:d={seconds}", "-ac", "2", str(path)])
    return Path(path)


def make_silence(path, seconds) -> Path:
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
          "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(seconds), str(path)])
    return Path(path)


def make_video_with_audio(path, seconds) -> Path:
    """MP4 with a video and an audio track (e.g. a phone video)."""
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
          "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}",
          "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
          "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)])
    return Path(path)


def probe(path) -> dict:
    result = _run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    return json.loads(result.stdout)


def duration(path) -> float:
    return float(probe(path)["format"]["duration"])


def format_tags(path) -> dict:
    """Tags as ffprobe reads them (keys in lower case)."""
    tags = probe(path)["format"].get("tags", {})
    return {k.lower(): v for k, v in tags.items()}


def title_tag(path) -> str:
    return format_tags(path).get("title")


def id3_frames(path):
    """
    Reads the ID3v2 tag directly from the file: (version, [(frame ID, content), ...]).
    TXXX contents are returned as (description, value), COMM as (language, description, text),
    text frames as text.
    """
    data = Path(path).read_bytes()
    if data[:3] != b"ID3":
        return None, []
    version = data[3]
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    encodings = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}
    frames, pos = [], 10
    while pos + 10 <= 10 + size:
        frame_id = data[pos:pos + 4]
        if frame_id == b"\0\0\0\0":
            break
        raw = data[pos + 4:pos + 8]
        if version == 4:
            frame_size = (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]
        else:
            frame_size = int.from_bytes(raw, "big")
        body = data[pos + 10:pos + 10 + frame_size]
        pos += 10 + frame_size
        name = frame_id.decode("latin-1")
        if name.startswith("T") and body:
            text = body[1:].decode(encodings.get(body[0], "latin-1")).rstrip("\0")
            frames.append((name, tuple(text.split("\0", 1)) if name == "TXXX" else text))
        elif name == "COMM" and len(body) >= 4:
            encoding = encodings.get(body[0], "latin-1")
            separator = b"\0\0" if body[0] in (1, 2) else b"\0"
            description, _, text = body[4:].partition(separator)
            frames.append((name, (body[1:4].decode("latin-1"), description.decode(encoding),
                                  text.decode(encoding).rstrip("\0"))))
        else:
            frames.append((name, body))
    return version, frames


def explorer_properties(folder, names, properties):
    """
    Properties as shown by Windows Explorer (Shell.Application, language-independent),
    e.g. "System.Comment", "System.Music.AlbumTitle", "System.Music.Genre".
    Returns one list of values as text per file; multiple values are joined with "; ".
    """
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$f = (New-Object -ComObject Shell.Application).Namespace($env:VAH_DIR); "
        "foreach ($n in ($env:VAH_NAMES -split '\\|')) { $i = $f.ParseName($n); "
        "$values = foreach ($p in ($env:VAH_PROPS -split '\\|')) { @($i.ExtendedProperty($p)) -join '; ' }; "
        "[Console]::Out.WriteLine(@($values) -join '|') }")
    env = {**os.environ, "VAH_DIR": str(folder), "VAH_NAMES": "|".join(names), "VAH_PROPS": "|".join(properties)}
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    if result.returncode != 0 or result.stderr.strip():
        raise RuntimeError(f"Cannot read Explorer properties: {result.stderr}")
    return [line.split("|") for line in result.stdout.splitlines()]


def silences(path, noise_db=-50, min_seconds=0.5):
    """List of (start, end) of all silent sections according to silencedetect."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_seconds}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", result.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", result.stderr)]
    return list(zip(starts, ends))


def max_volume_db(path) -> float:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    match = re.search(r"max_volume: (-?[\d.]+) dB", result.stderr)
    if not match:
        raise RuntimeError(f"volumedetect returned no result: {result.stderr}")
    return float(match.group(1))


def loudness(path) -> float:
    """Integrated loudness (LUFS, EBU R128) – measured independently of the mixer."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    matches = re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", result.stderr)
    if not matches:
        raise RuntimeError(f"ebur128 returned no result: {result.stderr[-500:]}")
    return float(matches[-1])


def atmo_fixture_dir() -> Path:
    """Creates the silent test atmos once per test run."""
    global _atmo_fixture_dir
    if _atmo_fixture_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="vah_test_atmo_", ignore_cleanup_errors=True)
        atexit.register(tmp.cleanup)
        for name, seconds in TEST_ATMO_SECONDS.items():
            make_silence(Path(tmp.name) / name, seconds)
        _atmo_fixture_dir = Path(tmp.name)
    return _atmo_fixture_dir
