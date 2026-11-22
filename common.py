"""Shared configuration and helpers for converter.py and check.py.

Both scripts must agree on which input file produces which output file, so
the planning logic lives here instead of being duplicated.
"""
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

# --- Configuration helpers ---

def env_str(name, default):
    return os.environ.get(name, default).strip()


def env_bool(name, default):
    return env_str(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def env_int(name, default, minimum=None, maximum=None):
    raw = env_str(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        fatal(f"{name} must be an integer, got '{raw}'.")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        fatal(f"{name} must be between {minimum} and {maximum}, got {value}.")
    return value


def env_float(name, default):
    raw = env_str(name, str(default))
    try:
        return float(raw)
    except ValueError:
        fatal(f"{name} must be a number, got '{raw}'.")


def env_choice(name, default, choices):
    value = env_str(name, default).lower()
    if value not in choices:
        fatal(f"{name} must be one of {', '.join(sorted(choices))}, got '{value}'.")
    return value


def fatal(msg):
    print(f"[FATAL] {msg}", flush=True)
    sys.exit(1)


INPUT_DIR = Path(env_str("INPUT_DIR", "/data/input"))
OUTPUT_DIR = Path(env_str("OUTPUT_DIR", "/data/output"))
MODE = env_choice("MODE", "full", {"convert", "check", "full"})

VIDEO_CONTAINER = env_str("VIDEO_CONTAINER", "mp4").lstrip(".")
IMAGE_FORMAT = env_choice("IMAGE_FORMAT", "heic", {"heic", "avif"})

EXTENSIONS_VIDEO = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.m2ts', '.mts', '.mpg', '.mpeg'}
EXTENSIONS_IMAGE = {'.jpg', '.jpeg', '.png', '.heic', '.webp', '.tiff', '.bmp', '.avif'}

# Formats that are already "modern" and are only copied, never re-encoded or reverted.
MODERN_IMAGE_SUFFIXES = {'.heic', '.avif'}

# Marker for in-progress files. Encoders write to a hidden temp file in the
# target folder, which is renamed into place only after a fully successful run,
# so an interrupted or failed job never leaves a half-written output behind.
PARTIAL_MARKER = ".partial-"

# Folders and files that are never media, even when they have a media extension:
# Synology thumbnails, macOS resource forks and Windows/macOS system folders.
IGNORED_DIRS = {"@eaDir", "#recycle", ".AppleDouble", "$RECYCLE.BIN", ".Trashes", ".Spotlight-V100"}


def file_type(path):
    suffix = path.suffix.lower()
    if suffix in EXTENSIONS_VIDEO:
        return "VIDEO"
    if suffix in EXTENSIONS_IMAGE:
        return "IMAGE"
    return None


def is_partial(name):
    return name.startswith(".") and PARTIAL_MARKER in name


def _is_relative_to(path, other):
    try:
        path.resolve().relative_to(other.resolve())
        return True
    except ValueError:
        return False


def walk_files(root, skip=None):
    """Yield every regular file below root, skipping ignored folders and, if
    given, the `skip` folder (e.g. an output folder nested inside the input)."""
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in IGNORED_DIRS and not (skip is not None and (here / d).resolve() == skip.resolve())
        )
        for f in sorted(filenames):
            if f.startswith("._"):
                continue
            yield here / f


def scan_inputs():
    """All convertible input files, in a stable order."""
    skip = OUTPUT_DIR if _is_relative_to(OUTPUT_DIR, INPUT_DIR) else None
    return [p for p in walk_files(INPUT_DIR, skip=skip) if file_type(p)]


class Plan:
    """Where each input file goes.

    `output` is the converted file. `revert` is where the original is copied
    when the conversion came out larger (it keeps its original extension so
    the file is not mislabeled). Both names are reserved per folder, so two
    inputs can never write to the same output path.
    """

    def __init__(self, inputs):
        self.items = {}      # input path -> (output path, revert path)
        self.by_output = {}  # output or revert path -> input path
        self.renamed = []    # inputs that got a non-default output name
        for path in inputs:
            self._add(path)

    def _add(self, path):
        ext = VIDEO_CONTAINER if file_type(path) == "VIDEO" else IMAGE_FORMAT
        target_dir = OUTPUT_DIR / path.parent.relative_to(INPUT_DIR)
        same_format = path.suffix.lower() == f".{ext.lower()}"

        # Default: "IMG_1.jpg" -> "IMG_1.heic". On a clash (e.g. IMG_1.jpg and
        # IMG_1.png in one folder) the later file keeps its source extension in
        # the name: "IMG_1.png.heic". A counter is the last resort.
        for n in range(1000):
            if n == 0:
                output = target_dir / f"{path.stem}.{ext}"
                revert = target_dir / path.name
            elif n == 1:
                output = target_dir / f"{path.name}.{ext}"
                revert = target_dir / path.name
            else:
                output = target_dir / f"{path.stem}.{n}.{ext}"
                revert = target_dir / f"{path.stem}.{n}{path.suffix}"
            if same_format:
                revert = output
            if not ({output, revert} & self.by_output.keys()):
                break
        else:
            raise RuntimeError(f"Could not find a free output name for {path}")

        if n:
            self.renamed.append((path, output))
        self.items[path] = (output, revert)
        self.by_output[output] = path
        self.by_output[revert] = path


class StopFlag:
    """Turns SIGTERM / SIGINT into a clean shutdown: queued jobs are skipped,
    running encoders are terminated and their partial files removed, instead
    of `docker stop` killing everything mid-write."""

    def __init__(self):
        self.event = threading.Event()
        self.signum = None
        self._running = set()
        self._lock = threading.RLock()

    def install(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle)

    def _handle(self, signum, _frame):
        if not self.event.is_set():
            print(f"\n[INFO] Received {signal.Signals(signum).name}, stopping running jobs...", flush=True)
        self.signum = signum
        self.event.set()
        with self._lock:
            for proc in list(self._running):
                try:
                    proc.terminate()
                except OSError:
                    pass

    @property
    def stopped(self):
        return self.event.is_set()

    def exit_code(self):
        return 128 + self.signum if self.signum else 0

    def run(self, cmd, capture_stdout=False):
        """subprocess.run() replacement that can be interrupted by a stop signal.
        stdin is always /dev/null so parallel ffmpeg runs never fight over the TTY."""
        with self._lock:
            if self.stopped:
                return subprocess.CompletedProcess(cmd, 255, b"", b"Cancelled by stop signal")
            proc = subprocess.Popen(
                [str(c) for c in cmd],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._running.add(proc)
        try:
            out, err = proc.communicate()
        finally:
            with self._lock:
                self._running.discard(proc)
        return subprocess.CompletedProcess(cmd, proc.returncode, out or b"", err or b"")


STOP = StopFlag()


def remove_quietly(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
