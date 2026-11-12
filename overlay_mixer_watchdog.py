# overlay_mixer_watchdog_v2.0.1.py
# -*- coding: utf-8 -*-
import configparser
import json
import math
import os
import sys
import time
import shutil
import subprocess
import logging
import platform
import re
import ctypes
import threading
from ctypes import wintypes
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Queue, Empty
from threading import Thread
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# -------------------------
# VERSION
# -------------------------
VERSION = "2.0.2"

# -------------------------
# CONFIGURATION (built-in defaults)
# -------------------------
# The settings below can be overridden in overlay_mixer.ini next to the script
# (template: overlay_mixer.example.ini). Without that file these defaults apply.
BASE_DIR        = Path(__file__).resolve().parent
CONFIG_FILE     = BASE_DIR / "overlay_mixer.ini"    # local configuration (not part of the repository)
SCHNEIDEN_DIR   = BASE_DIR / "input"                # watched folder for new overlays
ATMO_DIR        = BASE_DIR / "atmo"                 # background music beds (*.mp3)
FERTIG_DIR      = BASE_DIR / "output"               # finished MP3 files
FEHLER_DIR      = BASE_DIR / "error"                # overlays that could not be processed
ARCHIV_DIR      = BASE_DIR / "archive"              # processed overlays are moved here
LOG_FILE        = BASE_DIR / "overlay_mixer.log"    # second log, e.g. on a network share (opened/closed for every entry)
LOCAL_LOG_FILE  = BASE_DIR / "logs" / "overlay_mixer.log"   # local log, works without the network share as well
PID_FILE        = BASE_DIR / "overlay_mixer.pid"
LOCK_FILE       = BASE_DIR / "overlay_mixer.lock"      # prevents a second instance

FFMPEG_BIN      = "ffmpeg"                      # expected in PATH
FFPROBE_BIN     = "ffprobe"                     # expected in PATH

LEADIN_SECONDS  = 5
BUFFER_SECONDS  = 2
BITRATE         = "320k"
OUTPUT_PREFIX   = "VAH "                        # output name starts with "VAH " (never twice, see output_stem)

ALLOWED_EXTS    = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4"}

# ID3 tags (TXXX) for the playout software, only if a date was detected in the file name
TAG_ENABLE_DATE    = "Enable Date"              # detected date - ENABLE_DAYS_BEFORE
TAG_EXPIRY_DATE    = "Expiry Date"              # detected date + EXPIRY_DAYS_AFTER
ENABLE_DAYS_BEFORE = 14
EXPIRY_DAYS_AFTER  = 1                          # otherwise the software disables the item one day too early
TAG_DATE_FORMAT    = "%Y-%m-%d"                 # date format in the tags (match the playout software)

# Media type for EVERY generated file (ID3 frame TMED; not shown by Windows Explorer)
ID3_MEDIA_TYPE       = "News"                   # empty ("") = write nothing

# ID3 label for EVERY generated file (independent of the date)
# ID3_TEXT_FIELD: "Album" (TALB), "Genre" (TCON), "Comment" (comment frame COMM)
#                 or a custom name, e.g. "Category" (-> TXXX frame with that name).
#                 Not allowed (ignored): "title", TAG_ENABLE_DATE, TAG_EXPIRY_DATE,
#                 and while ID3_MEDIA_TYPE is set: "TMED", "Media Type".
ID3_TEXT_FIELD       = "Album"
ID3_TEXT             = "Veranstaltungshinweis"  # empty ("") = write nothing
ID3_COMMENT_LANGUAGE = "deu"                    # language code (ISO 639-2), only for "Comment"

# Loudness of the voice track: raise quiet overlays before mixing - raise only, never lower.
# Procedure: measure loudness (EBU R128). If it is more than LOUDNESS_TOLERANCE_LU below the target:
#            remove lows below LOUDNESS_HIGHPASS_HZ, amplify to the target, limit the peaks.
#            The mix itself (atmo, 5 s lead-in, amix) stays unchanged.
LOUDNESS_TARGET_LUFS   = -18.0   # target loudness of the voice track before mixing; None = off
LOUDNESS_TOLERANCE_LU  = 1.0     # up to this far below the target the voice track stays unchanged
LOUDNESS_PEAK_LIMIT_DB = -2.0    # peak limit after raising (dBFS, -24 to 0)
LOUDNESS_HIGHPASS_HZ   = 80      # high-pass filter against rumble/plosives, only when raising; 0 = off
LOUDNESS_MAX_GAIN_DB   = 20.0    # raise by at most this much (protection e.g. for almost silent files)

RESCAN_SECONDS        = 60      # additionally scan the input folder regularly for missed files
FAILED_RETRY_SECONDS  = 600     # failed file still in the input folder: retry no earlier than this
HEARTBEAT_SECONDS     = 3600    # "still running" entry in the log
STARTUP_RETRY_SECONDS = 10      # wait between attempts while a network drive is still missing at startup
ERROR_REPORT_SECONDS  = 300     # log recurring errors at most this often

# -------------------------
# CONFIGURATION FILE
# -------------------------
class ConfigError(Exception):
    """Invalid configuration file (syntax error, unknown setting, invalid value)."""


def _parse_text(raw: str) -> str:
    """Text value; surrounding double quotes are removed (keeps spaces, "" = empty text)."""
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1]
    return raw

def _parse_path(raw: str) -> Path:
    text = _parse_text(raw)
    if not text:
        raise ValueError("a path must not be empty")
    return Path(os.path.expandvars(text))

def _parse_int(raw: str) -> int:
    return int(_parse_text(raw))

def _parse_float(raw: str) -> float:
    return float(_parse_text(raw))

def _parse_number(raw: str):
    """Whole numbers stay int (so the log keeps showing e.g. "LeadIn=5s"), others become float."""
    text = _parse_text(raw)
    try:
        return int(text)
    except ValueError:
        return float(text)

def _parse_optional_float(raw: str):
    """Number, or None for "off"/"none"/empty (switches the feature off)."""
    text = _parse_text(raw).strip()
    if text.lower() in ("", "off", "none"):
        return None
    return float(text)


# [section] -> setting -> (constant in this script, parser)
CONFIG_SETTINGS = {
    "paths": {
        "input_dir":   ("SCHNEIDEN_DIR", _parse_path),
        "atmo_dir":    ("ATMO_DIR", _parse_path),
        "output_dir":  ("FERTIG_DIR", _parse_path),
        "error_dir":   ("FEHLER_DIR", _parse_path),
        "archive_dir": ("ARCHIV_DIR", _parse_path),
        "log_file":    ("LOG_FILE", _parse_path),
    },
    "programs": {
        "ffmpeg":  ("FFMPEG_BIN", _parse_text),
        "ffprobe": ("FFPROBE_BIN", _parse_text),
    },
    "mix": {
        "leadin_seconds": ("LEADIN_SECONDS", _parse_number),
        "buffer_seconds": ("BUFFER_SECONDS", _parse_number),
        "bitrate":        ("BITRATE", _parse_text),
    },
    "output": {
        "prefix": ("OUTPUT_PREFIX", _parse_text),
    },
    "tags": {
        "enable_date_tag":    ("TAG_ENABLE_DATE", _parse_text),
        "expiry_date_tag":    ("TAG_EXPIRY_DATE", _parse_text),
        "enable_days_before": ("ENABLE_DAYS_BEFORE", _parse_int),
        "expiry_days_after":  ("EXPIRY_DAYS_AFTER", _parse_int),
        "date_format":        ("TAG_DATE_FORMAT", _parse_text),
        "media_type":         ("ID3_MEDIA_TYPE", _parse_text),
        "text_field":         ("ID3_TEXT_FIELD", _parse_text),
        "text":               ("ID3_TEXT", _parse_text),
        "comment_language":   ("ID3_COMMENT_LANGUAGE", _parse_text),
    },
    "loudness": {
        "target_lufs":   ("LOUDNESS_TARGET_LUFS", _parse_optional_float),
        "tolerance_lu":  ("LOUDNESS_TOLERANCE_LU", _parse_float),
        "peak_limit_db": ("LOUDNESS_PEAK_LIMIT_DB", _parse_float),
        "highpass_hz":   ("LOUDNESS_HIGHPASS_HZ", _parse_number),
        "max_gain_db":   ("LOUDNESS_MAX_GAIN_DB", _parse_float),
    },
}


def load_config(path: Path) -> dict:
    """
    Reads the configuration file and returns {constant name: value}. A missing file
    returns {} (all defaults stay). Syntax errors, unknown sections or settings and
    invalid values raise ConfigError - better to stop than to watch the wrong folder.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    parser = configparser.ConfigParser(interpolation=None)      # "%" in date_format stays literal
    try:
        with open(path, encoding="utf-8-sig") as f:               # utf-8-sig: Notepad may add a BOM
            parser.read_file(f)
    except (configparser.Error, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot read {path.name}: {e}") from e
    values = {}
    for section in parser.sections():
        settings = CONFIG_SETTINGS.get(section.lower())
        if settings is None:
            raise ConfigError(f"unknown section [{section}]")
        for key, raw in parser.items(section):
            if key not in settings:
                raise ConfigError(f"unknown setting '{key}' in [{section}]")
            name, parse = settings[key]
            try:
                values[name] = parse(raw.strip())
            except ValueError as e:
                raise ConfigError(f"invalid value for '{key}' in [{section}]: {raw!r} ({e})") from e
    return values

def apply_config(values: dict) -> None:
    """Replaces the built-in defaults by the values from the configuration file."""
    globals().update(values)

# -------------------------
# LOGGING (file + console) with date/time
# -------------------------
logger = logging.getLogger("overlay_mixer")
logger.setLevel(logging.INFO)

# Uniform format with date + time
fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


class ShortLivedFileHandler(RotatingFileHandler):
    """
    Like RotatingFileHandler, but opens the file for every entry and closes it again
    right away. In v1.4.0 the log file on the network share stayed open for days; entries did
    not arrive (NUL bytes instead of text).
    """

    def __init__(self, filename, **kwargs):
        super().__init__(filename, delay=True, **kwargs)
        self._last_error_report = None

    def emit(self, record):
        try:
            super().emit(record)
        finally:
            if self.stream:
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None

    def handleError(self, record):
        # If the network share is unreachable, do not print a traceback for every line.
        now = time.monotonic()
        if self._last_error_report is None or now - self._last_error_report >= ERROR_REPORT_SECONDS:
            self._last_error_report = now
            sys.stderr.write(f"Log {self.baseFilename} not writable: {sys.exc_info()[1]}\n")


def setup_logging():
    """
    Set up local log, second log (LOG_FILE) and console. Only called in main(), so that
    importing the script does not fail because of a network drive (it is sometimes not
    connected yet right after logon).
    """
    logger.handlers.clear()
    handlers = []
    try:
        LOCAL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(LOCAL_LOG_FILE, maxBytes=1_000_000, backupCount=7, encoding="utf-8"))
    except OSError as e:
        sys.stderr.write(f"Local log {LOCAL_LOG_FILE} not available: {e}\n")
    handlers.append(ShortLivedFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=7, encoding="utf-8"))
    handlers.append(logging.StreamHandler(sys.stdout))
    for handler in handlers:
        handler.setFormatter(fmt)
        logger.addHandler(handler)


def install_thread_excepthook():
    """Write unhandled errors in threads to the log (in v1.4.0 only to the console)."""
    def hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        logger.critical(f"Unhandled error in thread {name}",
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = hook


# -------------------------
# Windows helpers
# -------------------------
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS  = 0x0080
STD_INPUT_HANDLE       = 0xFFFFFFF6             # (DWORD)-10

_kernel32_dll = None


def _kernel32():
    """kernel32 with complete function prototypes (handles are 64 bits wide on 64-bit)."""
    global _kernel32_dll
    if _kernel32_dll is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                  ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        k.SetFileTime.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.GetStdHandle.argtypes = [wintypes.DWORD]
        k.GetStdHandle.restype = wintypes.HANDLE
        k.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetConsoleMode.restype = wintypes.BOOL
        k.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.SetConsoleMode.restype = wintypes.BOOL
        _kernel32_dll = k
    return _kernel32_dll


def disable_quick_edit() -> bool:
    """
    Turns off QuickEdit mode of the console window. Otherwise a click into the window
    (text selection) halts every console output - and with it the processing -
    until someone presses Enter or Esc.
    """
    if os.name != "nt":
        return False
    try:
        kernel32 = _kernel32()
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = wintypes.DWORD()
        if not handle or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # no console window
        new_mode = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
        return bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:
        return False


def acquire_single_instance_lock(lock_path: Path = None):
    """
    Locks the lock file exclusively. Returns the open file object (keep it open until
    the end) or None if another instance is already running. The operating system
    releases the lock as soon as the process ends - even after a crash.
    """
    lock_path = LOCK_FILE if lock_path is None else lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def release_single_instance_lock(handle) -> None:
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        handle.close()


def write_pid():
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        logger.error(f"Could not write PID file: {e}")

def get_duration_seconds(path: Path) -> float:
    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr}")
    return float(result.stdout.strip())

def wait_for_stable_file(path: Path, stable_secs: float = 2.0, timeout: float = 120.0) -> bool:
    start = time.time()
    last_size = -1
    stable_since = None
    while time.time() - start < timeout:
        if not path.exists():
            time.sleep(0.5)
            continue
        size = path.stat().st_size
        if size == last_size:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= stable_secs:
                return True
        else:
            stable_since = None
        last_size = size
        time.sleep(0.5)
    return False

def unique_path(base_path: Path) -> Path:
    if not base_path.exists():
        return base_path
    stem = base_path.stem
    suffix = base_path.suffix
    parent = base_path.parent
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1

def output_stem(source_stem: str) -> str:
    """
    Name of the output file without extension: OUTPUT_PREFIX + name of the overlay.
    If the name already starts with the prefix (case-insensitive, followed by a space,
    "_", "-" or a digit), it is not added twice but normalized:
    "vah open stage" -> "VAH open stage", "VAH20251119 ..." -> "VAH 20251119 ...",
    also repeated: "VAH vah jazz trio" -> "VAH jazz trio".
    Only the beginning counts; "VAH" further back in the name stays.
    """
    word = OUTPUT_PREFIX.strip(" _-")
    while word:
        match = re.match(rf"{re.escape(word)}(?:[ _-]+|(?=\d))", source_stem, flags=re.IGNORECASE)
        if not match or not source_stem[match.end():].strip(" _-"):
            break
        source_stem = source_stem[match.end():]
    return f"{OUTPUT_PREFIX}{source_stem}"

def extract_date_from_filename(path: Path):
    """
    Looks for a date in the file name in the format YYYYMMDD (8 digits) and - if none
    is found - in the format YYYY-MM-DD. YYYYMMDD takes precedence.
    Returns a date object or None.
    """
    patterns = [
        (r'(?<!\d)(\d{8})(?!\d)', "%Y%m%d"),
        (r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)', "%Y-%m-%d"),
    ]
    for pattern, date_format in patterns:
        for match in re.findall(pattern, path.stem):
            try:
                return datetime.strptime(match, date_format).date()
            except ValueError:
                continue
    return None

def schedule_tags(event_date) -> dict:
    """ID3 tags for the playout software: Enable Date = date - 14 days, Expiry Date = date + 1 day."""
    enable_date = event_date - timedelta(days=ENABLE_DAYS_BEFORE)
    expiry_date = event_date + timedelta(days=EXPIRY_DAYS_AFTER)
    return {
        TAG_ENABLE_DATE: enable_date.strftime(TAG_DATE_FORMAT),
        TAG_EXPIRY_DATE: expiry_date.strftime(TAG_DATE_FORMAT),
    }

def id3_media_type():
    """Value for the Media Type field (TMED) of every file - or None if nothing is written."""
    return ID3_MEDIA_TYPE or None

def log_id3_media_type_setting() -> None:
    """Log at startup which media type is written."""
    media_type = id3_media_type()
    if media_type is None:
        logger.info("ID3 media type: off (no value configured)")
    else:
        logger.info(f"ID3 media type: TMED = {media_type}")

def _media_type_field_names() -> set:
    """Field names for TMED - taken while ID3_MEDIA_TYPE is set."""
    return {"tmed", "media type"} if id3_media_type() is not None else set()

def _reserved_id3_fields() -> set:
    """Fields the mixer sets itself and which the ID3 text must not overwrite."""
    return {"title", TAG_ENABLE_DATE.lower(), TAG_EXPIRY_DATE.lower()} | _media_type_field_names()

def is_comment_field(field) -> bool:
    """'Comment' is written as a real comment frame (COMM), not via FFmpeg."""
    return (field or "").strip().lower() == "comment"

def id3_text_tag():
    """
    (field name, text) for the ID3 label of every file - or None if nothing is
    written (no text, no field name or a reserved field name).
    """
    field = (ID3_TEXT_FIELD or "").strip()
    if not ID3_TEXT or not field or field.lower() in _reserved_id3_fields():
        return None
    return field, ID3_TEXT

def log_id3_text_setting() -> None:
    """Log at startup which ID3 label is active (so configuration errors are noticed)."""
    field = (ID3_TEXT_FIELD or "").strip()
    if not ID3_TEXT:
        logger.info("ID3 text: off (no text configured)")
    elif not field:
        logger.info("ID3 text: off (no field name configured)")
    elif field.lower() in _media_type_field_names():
        logger.warning(f"ID3 text: field name '{field}' is reserved (TMED is set via ID3_MEDIA_TYPE) "
                       f"| no text will be written")
    elif field.lower() in _reserved_id3_fields():
        logger.warning(f"ID3 text: field name '{field}' is reserved | no text will be written")
    elif is_comment_field(field):
        logger.info(f"ID3 text: {field} = {ID3_TEXT} (comment frame COMM)")
    else:
        logger.info(f"ID3 text: {field} = {ID3_TEXT}")

def _synchsafe(value: int) -> bytes:
    """Size in ID3v2.4 format (4 bytes of 7 bits each)."""
    return bytes((value >> shift) & 0x7F for shift in (21, 14, 7, 0))

def write_id3_comment(path: Path, text: str, language: str = None) -> None:
    """
    Adds a comment frame (COMM) to the ID3v2.4 tag written by FFmpeg.
    FFmpeg itself only writes "comment" as a TXXX frame, which players do not show as a comment.
    Encoding: Latin-1, otherwise UTF-16 - Windows Explorer does not show a UTF-8 comment
    (tested on Windows 11, likewise only with a matching language "deu").
    The file is replaced via a temporary file in the same folder; on an error it stays
    unchanged.
    """
    path = Path(path)
    data = path.read_bytes()
    header = data[:10]
    # expected: "ID3", version 2.4, no flags (no extended header/footer/unsynchronisation)
    if len(header) < 10 or header[:3] != b"ID3" or header[3] != 4 or header[5] != 0 \
            or any(b & 0x80 for b in header[6:10]):
        raise ValueError(f"no suitable ID3v2.4 tag at start of file ({path.name})")
    size = (header[6] << 21) | (header[7] << 14) | (header[8] << 7) | header[9]

    lang = (language or ID3_COMMENT_LANGUAGE).encode("ascii", "replace")[:3].ljust(3, b" ")
    try:
        encoding, empty_description, payload = b"\x00", b"\x00", text.encode("latin-1")
    except UnicodeEncodeError:
        encoding, empty_description, payload = b"\x01", "\ufeff".encode("utf-16-le") + b"\x00\x00", \
            ("\ufeff" + text).encode("utf-16-le")
    body = encoding + lang + empty_description + payload        # encoding, language, empty description, text
    frame = b"COMM" + _synchsafe(len(body)) + b"\x00\x00" + body

    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(header[:6] + _synchsafe(size + len(frame)) + frame + data[10:])
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

def measure_loudness(path: Path, audio_filter: str = None) -> tuple:
    """
    Integrated loudness (LUFS) and true peak (dBTP) according to EBU R128 (FFmpeg loudnorm, measurement only).
    audio_filter is applied before measuring (e.g. the high-pass filter). Silence yields -inf.
    """
    af = (audio_filter + "," if audio_filter else "") + "loudnorm=print_format=json"
    cmd = [FFMPEG_BIN, "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", af, "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr)
    if result.returncode != 0 or not match:
        details = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else f"Code {result.returncode}"
        raise RuntimeError(f"Loudness measurement failed: {details}")
    data = json.loads(match.group(0))
    return float(data["input_i"]), float(data["input_tp"])

def log_loudness_setting() -> None:
    """Log at startup whether and how quiet voice tracks are raised."""
    if LOUDNESS_TARGET_LUFS is None:
        logger.info("Loudness: off (no target configured)")
        return
    highpass = f"high-pass {LOUDNESS_HIGHPASS_HZ:g} Hz" if LOUDNESS_HIGHPASS_HZ else "no high-pass"
    logger.info(f"Loudness: target {LOUDNESS_TARGET_LUFS:.1f} LUFS | raise only "
                f"(tolerance {LOUDNESS_TOLERANCE_LU:.1f} LU, at most +{LOUDNESS_MAX_GAIN_DB:.1f} dB) | "
                f"{highpass} | peaks limited to {LOUDNESS_PEAK_LIMIT_DB:.1f} dB")

def loudness_filter(overlay_path: Path):
    """
    FFmpeg filter chain for the voice track - or None if it is mixed unchanged.
    Raise only: if the voice track is quieter than target - tolerance, it is amplified
    (after the high-pass filter) to the target; a limiter catches the peaks. If the
    measurement fails, it is mixed unchanged (warning in the log) and processing continues.
    """
    if LOUDNESS_TARGET_LUFS is None:
        return None
    target = LOUDNESS_TARGET_LUFS
    highpass = f"highpass=f={LOUDNESS_HIGHPASS_HZ:g}" if LOUDNESS_HIGHPASS_HZ else None
    try:
        value, peak = measure_loudness(overlay_path)
        if not math.isfinite(value) or value <= -70.0:
            logger.warning(f"Loudness not measurable ({overlay_path.name}): file is silent | voice track unchanged")
            return None
        if value >= target - LOUDNESS_TOLERANCE_LU:
            logger.info(f"Loudness: {value:.1f} LUFS, peak {peak:.1f} dBTP | "
                        f"sufficient (target {target:.1f} LUFS) | unchanged")
            return None
        filtered = measure_loudness(overlay_path, highpass)[0] if highpass else value
    except Exception as e:
        logger.warning(f"Loudness not measurable ({overlay_path.name}): {e} | voice track unchanged")
        return None

    gain = target - filtered
    if gain > LOUDNESS_MAX_GAIN_DB:
        logger.warning(f"Loudness: gain capped at +{LOUDNESS_MAX_GAIN_DB:.1f} dB (would need +{gain:.1f} dB)")
        gain = LOUDNESS_MAX_GAIN_DB
    logger.info(f"Loudness: {value:.1f} LUFS, peak {peak:.1f} dBTP | raised by +{gain:.1f} dB "
                f"(target {target:.1f} LUFS, peaks limited to {LOUDNESS_PEAK_LIMIT_DB:.1f} dB)")
    limiter = (f"alimiter=limit={LOUDNESS_PEAK_LIMIT_DB:g}dB:level=disabled:attack=5:release=50"
               f":latency=1")   # latency=1: no shift, the overlay still starts at exactly LEADIN_SECONDS
    return ",".join(part for part in (highpass, f"volume={gain:.2f}dB", limiter) if part)

def choose_best_atmo(overlay_seconds: float) -> Path | None:
    need = overlay_seconds + LEADIN_SECONDS + BUFFER_SECONDS
    candidates = []
    for p in ATMO_DIR.glob("*.mp3"):
        try:
            dur = get_duration_seconds(p)
            if dur >= need:
                candidates.append((dur, p))
        except Exception as e:
            logger.warning(f"Could not determine atmo duration ({p}): {e}")
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])  # shortest suitable one first
    return candidates[0][1]

def mix_with_ffmpeg(atmo: Path, overlay: Path, out_path: Path, title: str, extra_tags: dict = None,
                    overlay_filter: str = None) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    delay_ms = int(LEADIN_SECONDS * 1000)
    # overlay_filter (e.g. raising the loudness) only affects the voice track, before the delay
    pre = f"{overlay_filter}," if overlay_filter else ""
    filter_complex = f"[1]{pre}adelay={delay_ms}:all=1[ov];[0][ov]amix=inputs=2:dropout_transition=0"
    cmd = [
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(atmo), "-i", str(overlay),
        "-filter_complex", filter_complex,
        "-c:a", "libmp3lame", "-b:a", BITRATE,
        "-metadata", f"title={title}",
    ]
    # FFmpeg writes known keys (album, genre, ...) to the matching standard ID3 frame,
    # custom keys as ID3v2 TXXX frames (description = key).
    for key, value in (extra_tags or {}).items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(out_path))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")

def remove_partial_output(out_path: Path) -> None:
    """Remove a half-written output file after an FFmpeg error (otherwise it ends up in the import folder)."""
    try:
        if out_path.exists():
            out_path.unlink()
            logger.warning(f"Removed incomplete output file: {out_path.name}")
    except OSError as e:
        logger.error(f"Could not remove incomplete output file ({out_path.name}): {e}")

def _datetime_to_filetime(dt: datetime):
    timestamp = dt.timestamp()
    ft = int((timestamp + 11644473600) * 10000000)
    return wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)

def set_windows_creation_time(file_path: Path, target_dt: datetime) -> None:
    """
    Sets the Created date of a file on Windows.
    """
    FILE_WRITE_ATTRIBUTES = 0x0100
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    kernel32 = _kernel32()

    handle = kernel32.CreateFileW(
        str(file_path),
        FILE_WRITE_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        0,
        None
    )

    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        ctime = _datetime_to_filetime(target_dt)
        if not kernel32.SetFileTime(handle, ctypes.byref(ctime), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)

def set_output_dates(file_path: Path, target_date) -> None:
    """
    1. Sets Last Modified to the date detected in the file name.
       The existing time of day of the generated file is kept.
    2. Before setting the Created date, checks how many days lie between
       Last Modified and the current Created date.
    3. If it is less than 14 days, TODAY + 1 day is used as the Created date.
       Otherwise 14 days before Last Modified is set.
    """
    stat = file_path.stat()

    current_modified_dt = datetime.fromtimestamp(stat.st_mtime)
    new_modified_dt = current_modified_dt.replace(
        year=target_date.year,
        month=target_date.month,
        day=target_date.day
    )

    os.utime(file_path, (stat.st_atime, new_modified_dt.timestamp()))
    logger.info(
        f"Last Modified set: {file_path.name} -> {new_modified_dt.strftime('%d.%m.%Y %H:%M:%S')}"
    )

    if os.name == "nt":
        current_created_dt = datetime.fromtimestamp(stat.st_ctime)
        day_diff = abs((current_created_dt.date() - new_modified_dt.date()).days)

        if day_diff < 14:
            tomorrow = datetime.now() + timedelta(days=1)
            new_created_dt = current_created_dt.replace(
                year=tomorrow.year,
                month=tomorrow.month,
                day=tomorrow.day
            )
            logger.info(
                f"Created rule active: Last Modified and current Created are "
                f"{day_diff} days apart (< 14). Using TODAY + 1 day for Created."
            )
        else:
            created_target_date = target_date - timedelta(days=14)
            new_created_dt = current_created_dt.replace(
                year=created_target_date.year,
                month=created_target_date.month,
                day=created_target_date.day
            )
            logger.info(
                f"Created rule standard: Last Modified and current Created are "
                f"{day_diff} days apart. Using 14 days before Last Modified for Created."
            )

        set_windows_creation_time(file_path, new_created_dt)
        logger.info(
            f"Created set: {file_path.name} -> {new_created_dt.strftime('%d.%m.%Y %H:%M:%S')}"
        )
    else:
        logger.warning("Could not set Created date: this is only supported on Windows.")

def move_to_error(src: Path, reason: str):
    FEHLER_DIR.mkdir(parents=True, exist_ok=True)
    dest = unique_path(FEHLER_DIR / src.name)
    shutil.move(str(src), str(dest))
    logger.error(f"Moved to error folder: {src.name} -> {dest.name} | Reason: {reason}")

def process_overlay(overlay_path: Path) -> bool:
    """Processes one overlay. Returns True if an output file was created."""
    try:
        if overlay_path.suffix.lower() not in ALLOWED_EXTS:
            logger.info(f"Ignored (file type not allowed): {overlay_path.name}")
            return False

        if not overlay_path.exists():
            # e.g. reported twice and already processed (v1.4.0 waited 120 s here)
            logger.info(f"Skipped (no longer in input folder): {overlay_path.name}")
            return False

        logger.info(f"Detected: {overlay_path.name}")

        detected_date = extract_date_from_filename(overlay_path)
        if detected_date is not None:
            logger.info(f"Date detected in file name: {detected_date.strftime('%d.%m.%Y')}")
        else:
            logger.info("No valid date detected in file name.")

        if not wait_for_stable_file(overlay_path):
            move_to_error(overlay_path, "File did not become stable (timeout)")
            return False

        overlay_sec = get_duration_seconds(overlay_path)
        atmo_path = choose_best_atmo(overlay_sec)
        if atmo_path is None:
            move_to_error(overlay_path, f"No suitable atmo available (overlay {overlay_sec:.2f}s; needs >= overlay+{LEADIN_SECONDS}+{BUFFER_SECONDS})")
            return False

        atmo_sec = get_duration_seconds(atmo_path)
        base_name = overlay_path.stem
        out_name = f"{output_stem(base_name)}.mp3"
        out_path = unique_path(FERTIG_DIR / out_name)
        title = out_path.stem
        tags = schedule_tags(detected_date) if detected_date is not None else {}

        # ID3 label of every file: media type (TMED) and album/genre/custom name via FFmpeg,
        # comment afterwards by the mixer itself
        media_type = id3_media_type()
        text_tag = id3_text_tag()
        metadata = dict(tags)
        if media_type is not None:
            metadata["TMED"] = media_type           # FFmpeg writes the key "TMED" (upper case) directly as frame TMED
        if text_tag is not None and not is_comment_field(text_tag[0]):
            metadata[text_tag[0]] = text_tag[1]

        overlay_filter = loudness_filter(overlay_path)      # raise a quiet voice track (or None)

        try:
            mix_with_ffmpeg(atmo_path, overlay_path, out_path, title, metadata, overlay_filter)
        except Exception:
            remove_partial_output(out_path)
            raise

        if tags:
            logger.info("ID3 tags set: " + " | ".join(f"{key}={value}" for key, value in tags.items()))

        if media_type is not None:
            logger.info(f"ID3 media type set: TMED={media_type}")

        if text_tag is not None:
            field, text = text_tag
            try:
                if is_comment_field(field):
                    write_id3_comment(out_path, text)   # before Last Modified/Created, as the file is replaced
                logger.info(f"ID3 text set: {field}={text}")
            except Exception as e:
                logger.warning(f"Could not write ID3 comment ({out_path.name}): {e}")

        if detected_date is not None:
            try:
                set_output_dates(out_path, detected_date)
            except Exception as e:
                logger.warning(f"Could not set file dates ({out_path.name}): {e}")

        # Success -> move to the archive
        ARCHIV_DIR.mkdir(parents=True, exist_ok=True)
        archiv_dest = unique_path(ARCHIV_DIR / overlay_path.name)
        try:
            shutil.move(str(overlay_path), str(archiv_dest))
            logger.info(f"Archived: {overlay_path.name} -> {archiv_dest.name}")
        except Exception as e:
            logger.warning(f"Could not archive overlay ({overlay_path.name}): {e}")

        # Clearly visible success entry
        logger.info(
            f"SUCCESS: {out_path.name} created | Overlay={overlay_sec:.2f}s | "
            f"Atmo={atmo_sec:.2f}s | LeadIn={LEADIN_SECONDS}s | Buffer={BUFFER_SECONDS}s | Version={VERSION}"
        )
        return True

    except Exception as e:
        if overlay_path.exists():
            try:
                move_to_error(overlay_path, f"Error: {e}")
            except Exception as move_error:
                # In v1.4.0 exactly this case ended the worker thread.
                logger.error(f"Error: {e} | Could not move {overlay_path.name} to error folder: {move_error}")
        else:
            logger.error(f"Error (file no longer present): {e}")
        return False

# -------------------------
# Watchdog handler + queue
# -------------------------
def _path_key(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))

def _file_signature(path):
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


class OverlayQueue(Queue):
    """
    Queue with duplicate protection:
    - A path is pending at most once at a time (waiting or in progress), even if the
      folder watcher and the rescan report the same file.
    - If a file is still in the input folder after processing (e.g. archiving
      failed), it is not queued again while it is unchanged:
      never after success, after an error no earlier than FAILED_RETRY_SECONDS.
    """

    def __init__(self):
        super().__init__()
        self._guard = threading.Lock()
        self._active = set()
        self._leftovers = {}  # path -> (signature, earliest retry)

    def put(self, item, block=True, timeout=None) -> bool:
        key = _path_key(item)
        with self._guard:
            if key in self._active:
                return False
            leftover = self._leftovers.get(key)
            if leftover is not None:
                signature, retry_at = leftover
                if _file_signature(item) == signature and time.monotonic() < retry_at:
                    return False
                del self._leftovers[key]
            self._active.add(key)
        super().put(item, block, timeout)
        return True

    def finish(self, item, success: bool) -> bool:
        """Call after processing. Returns True if the file is still in the input folder."""
        key = _path_key(item)
        signature = _file_signature(item)
        with self._guard:
            self._active.discard(key)
            if signature is None:
                return False
            retry_at = float("inf") if success else time.monotonic() + FAILED_RETRY_SECONDS
            self._leftovers[key] = (signature, retry_at)
            return True


class OverlayHandler(FileSystemEventHandler):
    def __init__(self, queue: Queue):
        self.queue = queue

    def dispatch(self, event):
        # An error here would end the folder watcher's thread.
        try:
            super().dispatch(event)
        except Exception:
            logger.exception(f"Error while handling a file event: {event!r}")

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() in ALLOWED_EXTS:
            self.queue.put(path)

    def on_moved(self, event):
        # Renaming in the input folder (e.g. upload under a temporary name) - missed in v1.4.0.
        if event.is_directory:
            return
        path = Path(event.dest_path)
        if path.suffix.lower() in ALLOWED_EXTS and _path_key(path.parent) == _path_key(SCHNEIDEN_DIR):
            self.queue.put(path)

def worker_loop(queue: Queue, stop_flag: list):
    while not stop_flag[0]:
        try:
            path = queue.get(timeout=0.5)
        except Empty:
            continue
        success = False
        try:
            success = bool(process_overlay(path))
        except Exception:
            # process_overlay catches everything itself - but the worker must never die.
            logger.exception(f"Unexpected error while processing {path}")
        finally:
            try:
                if isinstance(queue, OverlayQueue) and queue.finish(path, success):
                    if success:
                        logger.warning(f"Still in input folder after successful processing: {path.name} | "
                                       f"will not be processed again, please remove it manually.")
                    else:
                        logger.warning(f"Still in input folder after an error: {path.name} | "
                                       f"next attempt in {FAILED_RETRY_SECONDS // 60} minutes at the earliest.")
            finally:
                queue.task_done()

def process_existing(queue: Queue) -> int:
    """Queues matching files in the input folder (oldest first). Returns the number of newly queued files."""
    files = []
    for p in SCHNEIDEN_DIR.iterdir():
        try:
            if p.is_file() and p.suffix.lower() in ALLOWED_EXTS:
                files.append((p.stat().st_mtime, p))
        except OSError:
            continue  # vanished in the meantime
    files.sort(key=lambda entry: entry[0])
    added = 0
    for _, p in files:
        if queue.put(p) is not False:
            added += 1
    return added

def ensure_dirs():
    for d in [SCHNEIDEN_DIR, ATMO_DIR, FERTIG_DIR, FEHLER_DIR, ARCHIV_DIR]:
        d.mkdir(parents=True, exist_ok=True)

def wait_for_dirs(retry_seconds: float = None):
    """
    Creates or checks the folders. If a network drive is still missing (e.g. right after logon,
    while the network drives are being connected), it waits instead of crashing.
    """
    retry_seconds = STARTUP_RETRY_SECONDS if retry_seconds is None else retry_seconds
    started = time.monotonic()
    last_report = None
    while True:
        try:
            ensure_dirs()
        except OSError as e:
            now = time.monotonic()
            if last_report is None or now - last_report >= ERROR_REPORT_SECONDS:
                logger.warning(f"Folders not reachable yet ({e}) | retrying every {retry_seconds:g}s")
                last_report = now
            time.sleep(retry_seconds)
            continue
        if last_report is not None:
            logger.info(f"Folders reachable (after waiting {time.monotonic() - started:.0f}s).")
        return


def _observer_healthy(observer) -> bool:
    # If only the emitter thread (which watches the folder) dies, the observer keeps living.
    return (observer.is_alive() and bool(observer.emitters)
            and all(emitter.is_alive() for emitter in observer.emitters))


class MixerService:
    """
    Keeps the folder watcher and the worker alive: health check with restart, regular
    rescan of the input folder (in case the watcher does not report a file) and heartbeat.
    """

    def __init__(self):
        self.queue = OverlayQueue()
        self.stop_flag = [False]
        self.observer = None
        self.worker = None
        self._last_report = {}
        now = time.monotonic()
        self.last_rescan = now
        self.last_heartbeat = now

    def _report(self, key) -> bool:
        """Log recurring errors at most every ERROR_REPORT_SECONDS."""
        now = time.monotonic()
        last = self._last_report.get(key)
        if last is not None and now - last < ERROR_REPORT_SECONDS:
            return False
        self._last_report[key] = now
        return True

    def start(self):
        # Watch first, then read existing files - so nothing slips through in between.
        self.ensure_observer()
        added = self.rescan(log_found=False)
        logger.info(f"Files in input folder at startup: {added}")
        self.ensure_worker()

    def ensure_worker(self):
        if self.worker is not None and self.worker.is_alive():
            return
        if self.worker is not None:
            logger.error("Worker thread had stopped | restarting.")
        self.worker = Thread(target=worker_loop, args=(self.queue, self.stop_flag),
                             name="overlay-worker", daemon=True)
        self.worker.start()

    def ensure_observer(self):
        if self.observer is not None and _observer_healthy(self.observer):
            return
        if self.observer is not None:
            logger.error("Folder watcher had stopped | restarting.")
            self._stop_observer()
        observer = Observer()
        try:
            observer.schedule(OverlayHandler(self.queue), str(SCHNEIDEN_DIR), recursive=False)
            observer.start()
        except Exception as e:
            try:
                observer.stop()
            except Exception:
                pass
            if self._report("observer"):
                logger.error(f"Could not start folder watcher ({SCHNEIDEN_DIR}): {e}")
            return
        self.observer = observer

    def _stop_observer(self):
        try:
            self.observer.stop()
            self.observer.join(timeout=5)
        except Exception:
            pass
        self.observer = None

    def rescan(self, log_found: bool = True) -> int:
        try:
            added = process_existing(self.queue)
        except OSError as e:
            if self._report("rescan"):
                logger.warning(f"Could not read input folder ({SCHNEIDEN_DIR}): {e}")
            return 0
        if added and log_found:
            logger.warning(f"Rescan: queued {added} file(s) not reported by the folder watcher "
                           f"(or retry after an error).")
        return added

    def tick(self, now: float = None):
        now = time.monotonic() if now is None else now
        self.ensure_worker()
        self.ensure_observer()
        if now - self.last_rescan >= RESCAN_SECONDS:
            self.last_rescan = now
            self.rescan()
        if now - self.last_heartbeat >= HEARTBEAT_SECONDS:
            self.last_heartbeat = now
            logger.info(f"Heartbeat: running | version {VERSION} | queue: {self.queue.qsize()}")

    def stop(self):
        self.stop_flag[0] = True
        if self.observer is not None:
            self._stop_observer()
        if self.worker is not None:
            self.worker.join(timeout=5)


def _watchdog_version() -> str:
    try:
        from importlib.metadata import version
        return version("watchdog")
    except Exception:
        return "?"

def main() -> int:
    # Read the configuration first: it also contains the log file.
    config, config_error = {}, None
    try:
        config = load_config(CONFIG_FILE)
        apply_config(config)
    except ConfigError as e:
        config_error = e

    setup_logging()
    disable_quick_edit()
    install_thread_excepthook()

    if config_error is not None:
        logger.error(f"Configuration error in {CONFIG_FILE}: {config_error} | exiting.")
        return 1

    lock = acquire_single_instance_lock()
    if lock is None:
        logger.error(f"Overlay mixer is already running (lock file {LOCK_FILE}) | exiting this instance.")
        return 1

    write_pid()
    logger.info(f"=== Overlay Mixer Watchdog started | version {VERSION} ===")
    logger.info(f"Base: {BASE_DIR}")
    if CONFIG_FILE.is_file():
        logger.info(f"Configuration: {CONFIG_FILE} ({len(config)} settings)")
    else:
        logger.warning(f"Configuration: no file found ({CONFIG_FILE}) | using built-in defaults")
    logger.info(f"Input: {SCHNEIDEN_DIR} | Atmo: {ATMO_DIR} | Output: {FERTIG_DIR} | Error: {FEHLER_DIR} | Archive: {ARCHIV_DIR}")
    logger.info(f"Python {platform.python_version()} | watchdog {_watchdog_version()} | Log: {LOCAL_LOG_FILE} + {LOG_FILE}")
    log_id3_media_type_setting()
    log_id3_text_setting()
    log_loudness_setting()

    service = None
    try:
        wait_for_dirs()
        service = MixerService()
        service.start()
        while True:
            time.sleep(1)
            try:
                service.tick()
            except Exception:
                if service._report("tick"):
                    logger.exception("Error in health check")
    except KeyboardInterrupt:
        logger.info("Stopped by KeyboardInterrupt.")
    finally:
        if service is not None:
            service.stop()
        try:
            if PID_FILE.exists():
                PID_FILE.unlink()
        except Exception:
            pass
        release_single_instance_lock(lock)
        logger.info("=== Overlay Mixer Watchdog stopped ===")
    return 0

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
