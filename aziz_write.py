"""Aziz!Write!: push-to-talk dictation. Hold Win+Q, speak, release. Lives in the tray.

The recording is transcribed on the GPU with faster-whisper, cleaned up by
Claude Haiku through Claude Code (prompt.txt), and typed into the WezTerm pane
that had focus when the key went down, so switching windows while it processes
does not misplace the text.
"""
import ctypes
import fnmatch
import functools
import gc
import hashlib
import json
import logging
import os
import platform
import shutil
import site
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import winreg
import zipfile
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

VERSION = "0.2.0"  # a release is the tag v<VERSION>; the workflow checks they match
REPO = "LeavOA/aziz-write"
ISSUES_URL = f"https://github.com/{REPO}/issues/new"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
FROZEN = getattr(sys, "frozen", False)  # compiled with PyInstaller (aziz-write.spec)
# The app's folder. prompt.txt, the log and cuda\ are looked for here: next to
# the exe when compiled, else next to this file.
HERE = (Path(sys.executable) if FROZEN else Path(__file__)).resolve().parent
LOG_PATH = HERE / "aziz-write.log"
MODEL = "large-v3-turbo"
LANGUAGE = "en"
SAMPLE_RATE = 16000
VK_Q = 0x51
VK_ESCAPE = 0x1B
PROMPTS_PATH = HERE / "prompts.yaml"  # the cleanup prompts; made from BUILTIN_PROMPTS when missing
CLAUDE = Path.home() / ".local" / "bin" / "claude.exe"
CLEANUP_MODEL = "haiku"
CLEANUP_TIMEOUT_S = 30
NO_WINDOW = subprocess.CREATE_NO_WINDOW
PYTHONW = HERE / ".venv" / "Scripts" / "pythonw.exe"
START_MENU = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
STARTUP_LINK = START_MENU / "Startup" / "Aziz!Write!.lnk"
START_MENU_LINK = START_MENU / "Aziz!Write!.lnk"
# Where the exe installs itself: per user, no administrator needed, and the
# app can write next to itself there (prompts, log, CUDA libraries, updates).
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Programs" / "Aziz!Write!"
# Per-user settings: the shortcut, the agent and terminal, where the CUDA
# libraries and the model were pointed to, whether start-at-login was offered,
# and the system shortcuts turned off to make room for ours (see Conflict).
SETTINGS_PATH = Path(os.environ["APPDATA"]) / "Aziz!Write!" / "settings.json"
EXPLORER_ADVANCED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
SINGLE_INSTANCE_MUTEX = "Local\\aziz-write"
# What you dictate stays out of the log unless this is set, for debugging the
# cleanup prompt: the log is a plain file that would otherwise keep everything.
LOG_TEXT = os.environ.get("AZIZ_WRITE_LOG_TEXT") == "1"

# The NVIDIA libraries transcription needs on top of the graphics driver:
# cuBLAS (CUDA 12) and cuDNN 9, from NVIDIA's redistributable CDN, which is far
# faster than PyPI's wheels of the same. Unpacked into cuda\<archive>\bin. To
# move to newer versions, change the paths and hashes; NVIDIA lists them in
# redistrib_<version>.json next to the zips.
CUDA_DIR = HERE / "cuda"
CUDA_DOWNLOAD_DIR = HERE / "cuda-download"
CUDA_BASE_URL = "https://developer.download.nvidia.com/compute/"
CUDA_ARCHIVES = (
    ("cudnn/redist/cudnn/windows-x86_64/cudnn-windows-x86_64-9.14.0.64_cuda12-archive.zip",
     "27db1cf50f830a1335de991cf1edde43afb4fc65c365dbf013245f1364ec2b12"),
    ("cuda/redist/libcublas/windows-x86_64/libcublas-windows-x86_64-12.9.1.4-archive.zip",
     "d534d98b0b453a98914dbf3adf47d7e84b55037abf02f87466439e1dcef581ed"),
)
CUDA_DLLS = ("cublas64_12.dll", "cudnn64_9.dll")  # one of each must be found
MODEL_FILES = ("model.bin", "config.json")  # what a folder holding the model has
# What faster-whisper fetches of a model's repository (its download_model does the same).
MODEL_FILE_PATTERNS = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*")

log = logging.getLogger("aziz-write")


def described(text: str) -> str:
    """Text for the log: its length, plus the text itself only when LOG_TEXT."""
    return f"{len(text)} chars: {text!r}" if LOG_TEXT else f"{len(text)} chars"


# Hugging Face's chunked Xet downloader hung at 0 bytes fetching the model on
# a Windows 11 machine, on two networks; plain HTTPS from its CDN works. Read
# when huggingface_hub is imported, so it must be set before faster_whisper.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
if sys.stderr is None:  # pythonw and the windowed exe: tqdm would write progress bars to nowhere and fail
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import numpy as np  # noqa: E402
import pystray  # noqa: E402
import sounddevice as sd  # noqa: E402
import yaml  # noqa: E402
from faster_whisper import BatchedInferencePipeline, WhisperModel, download_model  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from pynput import keyboard  # noqa: E402


# --- settings and small Windows helpers --------------------------------------

_settings_lock = threading.Lock()


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_settings(**changes) -> dict:
    with _settings_lock:
        settings = load_settings()
        settings.update(changes)
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return settings


def powershell(script: str, **env: str) -> str:
    """Runs a PowerShell snippet without a window; env values go in as AZIZ_* variables."""
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, **{f"AZIZ_{k.upper()}": v for k, v in env.items()}},
        capture_output=True, text=True, encoding="utf-8", check=True, creationflags=NO_WINDOW,
    ).stdout


MB_YESNO, MB_ICONQUESTION, MB_ICONWARNING = 0x4, 0x20, 0x30
MB_SETFOREGROUND, MB_TOPMOST, IDYES = 0x10000, 0x40000, 6


def ask(text: str) -> bool:
    """A Yes/No box on top of everything; True for Yes."""
    flags = MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND | MB_TOPMOST
    return ctypes.windll.user32.MessageBoxW(None, text, "Aziz!Write!", flags) == IDYES


def tell(text: str) -> None:
    flags = MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
    ctypes.windll.user32.MessageBoxW(None, text, "Aziz!Write!", flags)


# --- the CUDA libraries and the model ------------------------------------------

def add_dll_dirs(dirs: list[Path]) -> None:
    """Puts the cuBLAS/cuDNN DLLs where ctranslate2 finds them when it opens a
    CUDA model (they are loaded then, not when it is imported)."""
    for d in dirs:
        if str(d) not in os.environ["PATH"].split(os.pathsep):
            os.add_dll_directory(str(d))
            os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


def dirs_with_cuda_dlls(candidates: list[Path]) -> tuple[list[Path], list[str]]:
    """Of the candidate folders, the first holding each of CUDA_DLLS, and the DLLs none has."""
    found, missing = [], []
    for dll in CUDA_DLLS:
        d = next((d for d in candidates if (d / dll).is_file()), None)
        if d is None:
            missing.append(dll)
        elif d not in found:
            found.append(d)
    return found, missing


def cuda_dll_dirs(root: Path) -> tuple[list[Path], list[str]]:
    """The folders under root holding CUDA_DLLS: root itself, its bin, or a
    subfolder's bin, as NVIDIA's zips unpack."""
    return dirs_with_cuda_dlls([d for d in (root, root / "bin", *root.glob("*/bin")) if d.is_dir()])


def installed_cuda_dirs() -> tuple[list[Path], list[str]]:
    """The CUDA libraries already on the machine: on the PATH, in the CUDA
    Toolkit, in a cuDNN install, or in PyPI's nvidia wheels of this Python."""
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    candidates = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    candidates += [Path(v) / "bin" for k, v in os.environ.items() if k.upper().startswith("CUDA_PATH")]
    candidates += sorted((program_files / "NVIDIA GPU Computing Toolkit" / "CUDA").glob("v12.*/bin"), reverse=True)
    candidates += sorted((program_files / "NVIDIA" / "CUDNN").glob("v9.*/bin/12*"), reverse=True)
    candidates += sorted((program_files / "NVIDIA" / "CUDNN").glob("v9.*/bin"), reverse=True)
    if not FROZEN:
        candidates += [Path(s) / "nvidia" / lib / "bin" for s in site.getsitepackages() for lib in ("cublas", "cudnn")]
    return dirs_with_cuda_dlls([d for d in candidates if d.is_dir()])


def remote_size(url: str) -> int | None:
    """Content-Length of a URL (a HEAD request), or None if it cannot be asked."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=10) as response:
            return int(response.headers.get("Content-Length") or 0) or None
    except Exception as e:
        log.info("could not get the size of %s: %s", url, e)
        return None


def gb(size: int | None) -> str:
    return f"about {size / 2**30:.1f} GB" if size else "size unknown"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, path: Path, on_progress) -> None:
    """Fetches url to path by way of a .part file; on_progress gets the fraction done."""
    part = path.with_suffix(path.suffix + ".part")
    with urllib.request.urlopen(url) as response, part.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        got = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            got += len(chunk)
            on_progress(got / total if total else 0)
    part.replace(path)


def install_cuda(on_progress) -> None:
    """Downloads the CUDA libraries, checks them against NVIDIA's SHA-256, and
    unpacks them into cuda\\. A zip already downloaded and intact is kept.
    on_progress gets a line of text."""
    CUDA_DOWNLOAD_DIR.mkdir(exist_ok=True)
    CUDA_DIR.mkdir(exist_ok=True)
    for n, (path, sha256) in enumerate(CUDA_ARCHIVES, 1):
        zip_path = CUDA_DOWNLOAD_DIR / path.rsplit("/", 1)[1]
        name = zip_path.name.split("-windows")[0]
        if not (zip_path.exists() and sha256_of(zip_path) == sha256):
            log.info("downloading %s", zip_path.name)
            download(CUDA_BASE_URL + path, zip_path,
                     lambda f: on_progress(f"downloading {name} ({n} of {len(CUDA_ARCHIVES)}): {f:.0%}"))
            if sha256_of(zip_path) != sha256:
                zip_path.unlink()
                raise RuntimeError(f"{zip_path.name} does not match NVIDIA's published checksum")
        on_progress(f"unpacking {name}…")
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(CUDA_DIR)
        log.info("unpacked %s into %s", zip_path.name, CUDA_DIR)


def model_in(folder: Path) -> Path | None:
    """The model in folder: the model's files themselves, or a Hugging Face
    cache (or its hub folder) holding them."""
    if all((folder / f).exists() for f in MODEL_FILES):
        return folder
    for cache in (folder, folder / "hub"):
        try:
            return Path(download_model(MODEL, local_files_only=True, cache_dir=str(cache)))
        except Exception:
            continue
    return None


def model_repo() -> str:
    """The Hugging Face repository faster-whisper gets MODEL from (MODEL itself if it is one)."""
    from faster_whisper import utils as fw_utils
    return getattr(fw_utils, "_MODELS", {}).get(MODEL, MODEL)


def model_download_size() -> int | None:
    """Bytes a download of the model fetches, asked of Hugging Face; None if it cannot be."""
    from huggingface_hub import HfApi
    try:
        info = HfApi().model_info(model_repo(), files_metadata=True)
    except Exception as e:
        log.info("could not get the size of the model %s: %s", model_repo(), e)
        return None
    return sum(f.size or 0 for f in info.siblings
               if any(fnmatch.fnmatch(f.rfilename, p) for p in MODEL_FILE_PATTERNS))


def download_model_with_progress(on_progress, total: int | None) -> None:
    """faster-whisper's download of the model, with the byte progress reported
    against total (from model_download_size), or as bytes so far without it."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import tqdm as hf_tqdm

    class Progress(hf_tqdm):
        # Bars in bytes (per file, or one for all) and one counting files (n=1
        # each). Their totals grow as files are discovered, hence the total
        # from outside. A disabled bar (no console, as in the exe) keeps few
        # attributes, hence the getattrs and the count kept here.
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.got = kwargs.get("initial") or 0

        def update(self, n=1):
            super().update(n)
            self.got += n
            if n > 100 and getattr(self, "desc", "Downloading").startswith("Downloading"):
                done = f"{self.got / 2**20:,.0f} MB"
                on_progress(f"downloading the model: {done} of {total / 2**20:,.0f} MB ({self.got / total:.0%})"
                            if total else f"downloading the model: {done}")

    log.info("downloading the model %s", model_repo())
    snapshot_download(model_repo(), tqdm_class=Progress, allow_patterns=list(MODEL_FILE_PATTERNS))


class Need:
    """Something the app needs on disk. The setup window shows it until it is
    found: already there, downloaded, or pointed at."""

    title = ""
    source = ""  # where Download gets it

    def __init__(self):
        self.status = ""  # progress or failure text for the window
        self.busy = False
        self.found = None  # where it is: a path, or text naming several
        self.size = None  # bytes a download fetches, once looked up (ask_size)
        self.size_lookup = None

    @property
    def about(self) -> str:
        """For the window: what Download fetches, and how much."""
        looking = self.size_lookup is not None and self.size_lookup.is_alive()
        return f"Download: {'asking how much…' if looking else gb(self.size)}, {self.source}."

    def ask_size(self) -> None:
        """Looks up the download's size, in the background, once."""
        if self.size_lookup is not None:
            return

        def look_up():
            self.size = self.download_size()
            log.info("a download of the %s is %s", self.title, gb(self.size))

        self.size_lookup = threading.Thread(target=look_up, daemon=True)
        self.size_lookup.start()

    def download_size(self) -> int | None:
        raise NotImplementedError

    def locate(self):
        raise NotImplementedError

    def download(self, on_progress) -> None:
        raise NotImplementedError

    def accept(self, folder: Path) -> str | None:
        """Remembers folder as the place to look, or returns why it cannot be."""
        raise NotImplementedError


class CudaNeed(Need):
    title = "CUDA libraries (cuBLAS 12 and cuDNN 9)"
    source = "from NVIDIA, unpacked into the app's cuda folder"

    def locate(self):
        """A folder chosen before, the app's own cuda folder, or wherever the
        machine already has the libraries."""
        for root in (load_settings().get("cuda_dir"), CUDA_DIR):
            if root and not cuda_dll_dirs(Path(root))[1]:
                add_dll_dirs(cuda_dll_dirs(Path(root))[0])
                self.found = Path(root)
                return self.found
        dirs, missing = installed_cuda_dirs()
        if not missing:
            add_dll_dirs(dirs)
            self.found = "; ".join(str(d) for d in dirs)
            log.info("using the CUDA libraries already installed in %s", self.found)
            return self.found
        return None

    def download_size(self) -> int | None:
        sizes = [remote_size(CUDA_BASE_URL + path) for path, _ in CUDA_ARCHIVES]
        return sum(sizes) if all(sizes) else None

    def download(self, on_progress) -> None:
        install_cuda(on_progress)

    def accept(self, folder: Path) -> str | None:
        missing = cuda_dll_dirs(folder)[1]
        if missing:
            return (f"No {' or '.join(missing)} in {folder}: looked in it, in its bin folder, "
                    "and in its subfolders' bin folders.")
        update_settings(cuda_dir=str(folder))
        return None


class ModelNeed(Need):
    title = f"Speech model ({MODEL})"
    source = "from Hugging Face, into its cache in your profile"

    def locate(self):
        folder = load_settings().get("model_dir")
        self.found = model_in(Path(folder)) if folder else None
        if self.found is None:
            try:
                self.found = Path(download_model(MODEL, local_files_only=True))
            except Exception:
                return None
        return self.found

    def download_size(self) -> int | None:
        return model_download_size()

    def download(self, on_progress) -> None:
        download_model_with_progress(on_progress, self.size)

    def accept(self, folder: Path) -> str | None:
        if model_in(folder) is None:
            return (f"No {MODEL} model in {folder}: it has neither the model's files "
                    "(model.bin, config.json, …) nor a Hugging Face cache holding them.")
        update_settings(model_dir=str(folder))
        return None


def tk_window(func):
    """For a function that makes and runs a Tk window in the calling thread.

    Tk objects must be freed by the thread that made them, or Tcl aborts the
    process ("Tcl_AsyncDelete: async handler deleted by the wrong thread"), and
    the window's callbacks leave cycles that only the garbage collector frees:
    so it is run here, in this thread, before the thread moves on.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        finally:
            gc.collect()
    return wrapper


@tk_window
def setup_window(needs: list[Need]) -> bool:
    """Shows what is missing, with Download and Choose folder buttons for each.
    True once everything is found; False if the window is closed first."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"Aziz!Write! {VERSION} setup")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=16)
    frame.grid()
    ttk.Label(frame, wraplength=560, text=(
        "Aziz!Write! needs these to transcribe. Download them, or point it at copies you already have."
    )).grid(column=0, row=0, columnspan=3, sticky="w", pady=(0, 12))
    done = [False]
    for need in needs:
        need.ask_size()

    def start_download(need: Need) -> None:
        need.busy = True
        need.status = "starting the download…"

        def run():
            try:
                need.download(lambda text: setattr(need, "status", text))
                if need.locate() is None:
                    need.status = "downloaded, but still not found; see the log"
            except Exception as e:
                log.exception("downloading the %s failed", need.title)
                need.status = f"download failed: {e}"
            finally:
                need.busy = False

        threading.Thread(target=run, daemon=True).start()

    def choose(need: Need) -> None:
        folder = filedialog.askdirectory(parent=root, title=f"Folder holding the {need.title}")
        if folder:
            error = need.accept(Path(folder))
            if error:
                messagebox.showwarning("Aziz!Write!", error, parent=root)
            else:
                need.locate()

    rows = []
    for i, need in enumerate(needs):
        r = 1 + 2 * i
        ttk.Label(frame, text=need.title, font=("Segoe UI", 10, "bold")).grid(column=0, row=r, sticky="w")
        dl = ttk.Button(frame, text="Download", command=lambda n=need: start_download(n))
        dl.grid(column=1, row=r, padx=(16, 4))
        ch = ttk.Button(frame, text="Choose folder…", command=lambda n=need: choose(n))
        ch.grid(column=2, row=r)
        status = ttk.Label(frame, wraplength=560, foreground="gray")
        status.grid(column=0, row=r + 1, columnspan=3, sticky="w", pady=(2, 10))
        rows.append((need, dl, ch, status))
    ttk.Button(frame, text="Quit", command=root.destroy).grid(column=2, row=1 + 2 * len(needs), sticky="e")

    def poll():
        for need, dl, ch, status in rows:
            if need.found is not None:
                text, color = f"Found: {need.found}", "green"
            elif need.busy:
                text, color = need.status, "black"
            else:
                text, color = f"Not found. {need.about}" + (f"\n{need.status}" if need.status else ""), "gray"
            status.config(text=text, foreground=color)
            for button in (dl, ch):
                button.state(["disabled"] if need.busy or need.found is not None else ["!disabled"])
        if all(n.found is not None for n in needs):
            done[0] = True
            root.after(700, root.destroy)
            return
        root.after(100, poll)

    root.after(100, poll)
    root.eval("tk::PlaceWindow . center")
    root.mainloop()
    return done[0]


# --- the terminal the text goes into ---------------------------------------------

def foreground_process_name() -> str:
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return Path(buf.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


class WezTerm:
    """WezTerm, through `wezterm cli`. Another terminal would implement these
    same methods and be added to TERMINALS.

    A pane id names where the text goes; it stays valid while the user moves
    to other windows. Without WEZTERM_UNIX_SOCKET (set only in WezTerm's own
    shells, not at login), `wezterm cli` finds the GUI's gui-sock-<pid> socket
    in the runtime dir but connects to it by bare name, so it must run from
    that folder.
    """

    name = "WezTerm"
    exe = Path(r"C:\Program Files\WezTerm\wezterm.exe")
    process = "wezterm-gui.exe"
    runtime_dir = Path.home() / ".local" / "share" / "wezterm"

    def check(self) -> str | None:
        return None if self.exe.exists() else f"WezTerm not found at {self.exe}"

    def focused_pane(self) -> int | None:
        """The focused pane, if the terminal is the foreground window."""
        if foreground_process_name() != self.process:
            return None
        clients = json.loads(self._cli("list-clients", "--format", "json") or "[]")
        return clients[0]["focused_pane_id"] if clients else None

    def pane_title(self, pane_id: int) -> str:
        panes = json.loads(self._cli("list", "--format", "json"))
        return next((p["tab_title"] for p in panes if p["pane_id"] == pane_id), "")

    def send_text(self, pane_id: int, keys: str) -> None:
        """Types keys into the pane, as if from the keyboard (not a paste)."""
        self._cli("send-text", "--no-paste", "--pane-id", str(pane_id), input=keys)

    def set_title(self, pane_id: int, title: str) -> None:
        """Sets the title of the pane's tab; empty is the terminal's own title."""
        self._cli("set-tab-title", "--pane-id", str(pane_id), title)

    def _cli(self, *args: str, input: str | None = None) -> str:
        return subprocess.run(
            [str(self.exe), "cli", *args], input=input, capture_output=True, text=True,
            encoding="utf-8", check=True, creationflags=NO_WINDOW, cwd=self.runtime_dir,
        ).stdout


TERMINALS = {"wezterm": WezTerm}  # settings "terminal": the key


def chosen_terminal():
    return TERMINALS.get(load_settings().get("terminal"), WezTerm)()


def as_typed(text: str) -> str:
    # Typed, not pasted, so Claude Code shows it in full. Line breaks go as
    # ESC+CR, which WezTerm's config also sends for SHIFT+Enter: a newline that
    # does not submit.
    return text.replace("\r\n", "\n").strip("\n").replace("\n", "\x1b\r")


class PaneStatus:
    """Shows dictation progress in the target pane: as a marker typed at the
    cursor, and as the title of the pane's tab (visible when the tab is in the
    background).

    The marker is removed with one backspace per character, so its characters
    are each one grapheme, and typing into the pane meanwhile would make the
    backspaces delete the wrong text. The tab's own title is restored after.
    """

    BACKSPACE = "\x7f"

    def __init__(self, terminal, pane_id: int | None):
        self.terminal = terminal
        self.pane_id = pane_id
        self.marker = ""
        self.original_title = ""
        if pane_id is not None:
            self.original_title = terminal.pane_title(pane_id)

    def show(self, status: str) -> None:
        self._replace_marker(status)
        self._set_title(status)

    def finish(self, text: str = "") -> None:
        """Replaces the marker with text (possibly none) and restores the title."""
        self._replace_marker(as_typed(text), is_marker=False)
        self._set_title(self.original_title)

    def _replace_marker(self, new: str, is_marker: bool = True) -> None:
        if self.pane_id is None:
            return
        keys = self.BACKSPACE * len(self.marker) + new
        self.marker = new if is_marker else ""
        if keys:
            self._try(self.terminal.send_text, self.pane_id, keys)

    def _set_title(self, title: str) -> None:
        if self.pane_id is not None:
            self._try(self.terminal.set_title, self.pane_id, title)

    def _try(self, action, *args) -> None:
        try:
            action(*args)
        except subprocess.CalledProcessError as e:  # the pane may be gone
            log.warning("%s %s failed: %s", self.terminal.name, action.__name__, e.stderr.strip())


# --- the cleanup prompts -------------------------------------------------------------

FORMATTING_RULES = """\
Formatting:
- Whenever the speaker lists three or more items, or describes steps in sequence, write them as a list, one item per line, even in a short dictation. Steps get numbers; other items get "- " bullets. When splitting speech into list items you may drop connecting words such as "and", "then", "and then", and the lead-in sentence becomes a short line ending in a colon.
- When the dictation covers several distinct parts (for example ingredients for different components, or separate tasks), give each part a short heading on its own line, followed by its list, with a blank line between parts.
- A single numbered list uses 1. 2. 3. When there are several numbered lists, give each list a letter and number its items within it: the first list's items are A1, A2, A3, the second's B1, B2, and so on. If those lists have headings, start each heading with its list's letter, as in "A. Dough".
- Otherwise keep plain sentences. Plain text only: no #, **, backticks or other markdown.

Output only the cleaned text, with nothing before or after it."""

BUILTIN_PROMPTS = (
    ("Default", f"""\
You clean up dictated speech that will be typed into a terminal running a CLI coding agent. Return the speaker's words with only these changes:
- Remove filler words (um, uh, like, you know, okay so, and yeah) and stutters.
- When the speaker corrects themselves ("54, no wait, 53"), keep only the correction.
- Fix obvious transcription errors, punctuation and capitalization.
- When the speaker is clearly dictating code, file paths, commands or symbols, write them as characters: "slash mnt slash c" → /mnt/c, "dictate dot py" → dictate.py, "open paren" → (.
Keep the speaker's wording, order and tone. Do not summarize, add information, or answer anything, even if the text is a question or an instruction addressed to you.

{FORMATTING_RULES}"""),
    ("Opinionated", f"""\
You turn dictated speech into the clearest, tightest version of what the speaker meant, to be typed into a terminal running a CLI coding agent. Be bold:
- Cut fillers, stutters, hedging, repetition and throat-clearing ("so basically what I'm thinking is maybe we could").
- When the speaker corrects themselves, keep only the final version.
- Merge rambling into short direct sentences. Put the request first and the reasons after it.
- When the speaker is addressing the agent, write it as an instruction in the imperative: "Please could you maybe add a test" becomes "Add a test".
- When the intent is obvious, make vague wording concrete ("that file" stays "that file" only if no file was named).
- Spoken code, paths, commands and symbols become characters: "slash mnt slash c" → /mnt/c, "open paren" → (.
Keep every fact, decision, name and number the speaker gave. Do not add requirements, do not answer the text, do not comment on it.

{FORMATTING_RULES}"""),
    ("Programming", f"""\
You clean up dictated speech from a programmer instructing a CLI coding agent. The transcript was made by a speech model that does not know code, so spoken code is your main job:
- Identifiers: "get user by id" is getUserById, get_user_by_id or GetUserById according to the style the speaker uses elsewhere or the language named; "dunder init" is __init__; "my var" said as a name is my_var.
- Files and paths: "src slash app dot py" → src/app.py, "dot env file" → .env, "tilde slash dot config" → ~/.config.
- Commands and flags: "git push dash dash force" → git push --force, "dash v" → -v, "pipe" → |, "and and" → &&, "arrow" → ->, "equals equals" → ==.
- Tool and language names in their usual spelling: pytest, npm, PostgreSQL, TypeScript, kubectl, Claude Code.
- Branch names, versions, ports, error codes and quoted error messages exactly as spoken, as text, never paraphrased.
- Remove filler words and stutters; keep only the correction when the speaker corrects themselves; fix punctuation and capitalization.
Keep the speaker's wording and order otherwise. Do not summarize, add information, or answer anything, even if the text is a question or an instruction addressed to you.

{FORMATTING_RULES}"""),
    ("Light touch", """\
You clean up dictated speech as little as possible. Return the speaker's words with only these changes:
- Remove filler words (um, uh, like, you know) and stutters.
- When the speaker corrects themselves ("54, no wait, 53"), keep only the correction.
- Fix punctuation and capitalization.
Change nothing else: keep the wording, order, sentence structure and tone, do not make lists or headings, and do not answer or comment on the text. Plain text only. Output only the cleaned text, with nothing before or after it."""),
)


def builtin_prompts() -> list[dict]:
    return [{"name": name, "text": text} for name, text in BUILTIN_PROMPTS]


def save_prompts(prompts: list[dict]) -> None:
    """Writes prompts.yaml, each text as a readable block."""
    class Dumper(yaml.SafeDumper):
        pass

    def block_text(dumper, text):
        style = "|" if "\n" in text else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", text, style=style)

    Dumper.add_representer(str, block_text)
    clean = [{"name": str(p["name"]).strip(),
              "text": "\n".join(line.rstrip() for line in str(p["text"]).strip().splitlines())}
             for p in prompts]
    PROMPTS_PATH.write_text(
        "# Aziz!Write!'s cleanup prompts. Edit here or in the tray menu's Prompt window;\n"
        "# the one chosen there (settings.json, \"prompt\") is given to the agent.\n"
        + yaml.dump({"prompts": clean}, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=10000),
        encoding="utf-8")


def load_prompts() -> list[dict]:
    """The prompts in prompts.yaml, written from the built-ins first if missing or empty."""
    prompts = []
    if PROMPTS_PATH.exists():
        try:
            data = yaml.safe_load(PROMPTS_PATH.read_text(encoding="utf-8")) or {}
            prompts = [p for p in data.get("prompts", [])
                       if isinstance(p, dict) and str(p.get("name", "")).strip() and str(p.get("text", "")).strip()]
        except yaml.YAMLError:
            log.exception("%s is not valid YAML; using the built-in prompts", PROMPTS_PATH)
            return builtin_prompts()
    if not prompts:
        prompts = builtin_prompts()
        save_prompts(prompts)
        log.info("wrote the built-in prompts to %s", PROMPTS_PATH)
    return prompts


def current_prompt_name() -> str:
    prompts = load_prompts()
    name = load_settings().get("prompt")
    return name if any(p["name"] == name for p in prompts) else prompts[0]["name"]


def current_prompt() -> str:
    """The chosen prompt's text, reread so edits apply to the next dictation."""
    prompts = load_prompts()
    name = current_prompt_name()
    return next(p["text"] for p in prompts if p["name"] == name).strip()


@tk_window
def prompts_window(on_changed) -> None:
    """Picks, edits, adds, renames and deletes prompts; saves to prompts.yaml."""
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk

    prompts = load_prompts()
    chosen = [current_prompt_name()]
    root = tk.Tk()
    root.title("Aziz!Write! prompts")
    root.attributes("-topmost", True)
    frame = ttk.Frame(root, padding=12)
    frame.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frame.columnconfigure(1, weight=1)
    frame.rowconfigure(1, weight=1)

    ttk.Label(frame, text="Prompts").grid(column=0, row=0, sticky="w")
    status = ttk.Label(frame, foreground="gray")
    status.grid(column=1, row=0, sticky="w", padx=(12, 0))
    names = tk.Listbox(frame, exportselection=False, width=24, height=14)
    names.grid(column=0, row=1, sticky="nsw", pady=(4, 8))
    editor = tk.Text(frame, wrap="word", width=90, height=24, font=("Segoe UI", 10), undo=True)
    editor.grid(column=1, row=1, sticky="nsew", padx=(12, 0), pady=(4, 8))
    left = ttk.Frame(frame)
    left.grid(column=0, row=2, sticky="w")
    right = ttk.Frame(frame)
    right.grid(column=1, row=2, sticky="e")
    shown = [None]  # index of the prompt in the editor

    def refresh_names() -> None:
        names.delete(0, "end")
        for p in prompts:
            names.insert("end", p["name"] + ("  (in use)" if p["name"] == chosen[0] else ""))
        status.config(text=f"In use: {chosen[0]}. Edits apply to the next dictation after Save.")

    def selected() -> int | None:
        picked = names.curselection()
        return picked[0] if picked else None

    def editor_text() -> str:
        return editor.get("1.0", "end").strip()

    def unsaved() -> bool:
        return shown[0] is not None and editor_text() != prompts[shown[0]]["text"].strip()

    def show(index: int) -> None:
        shown[0] = index
        editor.delete("1.0", "end")
        editor.insert("1.0", prompts[index]["text"])
        editor.edit_reset()
        names.selection_clear(0, "end")
        names.selection_set(index)
        names.see(index)

    def save() -> bool:
        if shown[0] is None:
            return True
        if not editor_text():
            messagebox.showwarning("Aziz!Write!", "A prompt cannot be empty.", parent=root)
            return False
        prompts[shown[0]]["text"] = editor_text()
        save_prompts(prompts)
        log.info("saved the prompt %s", prompts[shown[0]]["name"])
        return True

    def on_select(_event=None) -> None:
        index = selected()
        if index is None or index == shown[0]:
            return
        if unsaved() and messagebox.askyesno("Aziz!Write!", f"Save the changes to {prompts[shown[0]]['name']}?",
                                             parent=root) and not save():
            names.selection_clear(0, "end")
            names.selection_set(shown[0])
            return
        show(index)

    def ask_name(title: str, initial: str = "") -> str | None:
        while True:
            name = simpledialog.askstring("Aziz!Write!", title, initialvalue=initial, parent=root)
            if name is None:
                return None
            name = name.strip()
            if name and not any(p["name"] == name for p in prompts if p["name"] != initial):
                return name
            messagebox.showwarning("Aziz!Write!", "Give the prompt a name no other prompt has.", parent=root)

    def new() -> None:
        name = ask_name("Name for the new prompt (it starts as a copy of the shown one):")
        if name is None:
            return
        prompts.append({"name": name, "text": editor_text() or prompts[shown[0] or 0]["text"]})
        save_prompts(prompts)
        refresh_names()
        show(len(prompts) - 1)

    def rename() -> None:
        index = shown[0]
        if index is None:
            return
        old = prompts[index]["name"]
        name = ask_name(f"New name for {old}:", old)
        if name is None or name == old:
            return
        prompts[index]["name"] = name
        if chosen[0] == old:
            chosen[0] = name
            update_settings(prompt=name)
            on_changed()
        save_prompts(prompts)
        refresh_names()
        names.selection_set(index)

    def delete() -> None:
        index = shown[0]
        if index is None:
            return
        if len(prompts) == 1:
            messagebox.showwarning("Aziz!Write!", "The last prompt cannot be deleted.", parent=root)
            return
        if not messagebox.askyesno("Aziz!Write!", f"Delete the prompt {prompts[index]['name']}?", parent=root):
            return
        gone = prompts.pop(index)
        if chosen[0] == gone["name"]:
            chosen[0] = prompts[0]["name"]
            update_settings(prompt=chosen[0])
            on_changed()
        save_prompts(prompts)
        shown[0] = None
        refresh_names()
        show(min(index, len(prompts) - 1))

    def use() -> None:
        if shown[0] is None or not save():
            return
        chosen[0] = prompts[shown[0]]["name"]
        update_settings(prompt=chosen[0])
        log.info("prompt in use: %s", chosen[0])
        refresh_names()
        on_changed()

    def restore_builtins() -> None:
        missing = [p for p in builtin_prompts() if not any(q["name"] == p["name"] for q in prompts)]
        if not missing:
            messagebox.showinfo("Aziz!Write!", "All the built-in prompts are here, perhaps edited. "
                                "Rename one to get a fresh copy of it.", parent=root)
            return
        prompts.extend(missing)
        save_prompts(prompts)
        refresh_names()

    def close() -> None:
        if unsaved() and messagebox.askyesno("Aziz!Write!", f"Save the changes to {prompts[shown[0]]['name']}?",
                                             parent=root) and not save():
            return
        root.destroy()

    names.bind("<<ListboxSelect>>", on_select)
    ttk.Button(left, text="New…", command=new).pack(side="left")
    ttk.Button(left, text="Rename…", command=rename).pack(side="left", padx=(4, 0))
    ttk.Button(left, text="Delete", command=delete).pack(side="left", padx=(4, 0))
    ttk.Button(right, text="Restore built-ins", command=restore_builtins).pack(side="left")
    ttk.Button(right, text="Open prompts.yaml", command=lambda: os.startfile(PROMPTS_PATH)).pack(side="left", padx=(4, 0))
    ttk.Button(right, text="Save", command=save).pack(side="left", padx=(12, 0))
    ttk.Button(right, text="Use this prompt", command=use).pack(side="left", padx=(4, 0))
    ttk.Button(right, text="Close", command=close).pack(side="left", padx=(4, 0))
    root.protocol("WM_DELETE_WINDOW", close)
    refresh_names()
    show(next(i for i, p in enumerate(prompts) if p["name"] == chosen[0]))
    root.eval("tk::PlaceWindow . center")
    root.mainloop()


# --- cleanup agents ---------------------------------------------------------------

class ClaudeCode:
    """Cleans a transcript with Claude Haiku through Claude Code.

    Starting claude.exe costs about as much as Haiku's answer, so one process
    is kept started and waiting on stream-json input, for the current prompt;
    each cleanup uses it once and starts the next. The prompt is reread on
    every cleanup, so edits apply to the next dictation after.
    """

    name = "Claude Code"

    def __init__(self):
        self.workdir = tempfile.mkdtemp(prefix="aziz-write-claude-")
        self.lock = threading.Lock()
        self.prompt = None
        self.proc = None

    def check(self) -> str | None:
        return None if CLAUDE.exists() else f"Claude Code not found at {CLAUDE}"

    def start(self) -> None:
        self.prepare()

    def prepare(self) -> None:
        """Starts a process for the current prompt unless one is ready."""
        prompt = current_prompt()
        with self.lock:
            if self.proc is not None and self.proc.poll() is None and self.prompt == prompt:
                return
            self.close()
            self.prompt = prompt
            self.proc = subprocess.Popen(
                [str(CLAUDE), "-p", "--input-format", "stream-json",
                 "--output-format", "stream-json", "--verbose",
                 "--model", CLEANUP_MODEL, "--system-prompt", prompt,
                 "--tools", "", "--no-session-persistence", "--strict-mcp-config",
                 "--setting-sources", "", "--disable-slash-commands"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", cwd=self.workdir, creationflags=NO_WINDOW,
            )

    def clean(self, text: str) -> str:
        self.prepare()
        with self.lock:
            proc, self.proc = self.proc, None
        try:
            return self._ask(proc, text)
        finally:
            proc.kill()
            proc.wait()
            threading.Thread(target=self.prepare, daemon=True).start()

    def _ask(self, proc: subprocess.Popen, text: str) -> str:
        timer = threading.Timer(CLEANUP_TIMEOUT_S, proc.kill)
        timer.start()
        try:
            message = {"type": "user", "message": {"role": "user", "content": text}}
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            for line in proc.stdout:
                event = json.loads(line)
                if event.get("type") == "result":
                    if event.get("is_error"):
                        raise RuntimeError(event.get("result") or event.get("subtype"))
                    return (event.get("result") or "").strip()
            raise RuntimeError("claude exited without a result")
        finally:
            timer.cancel()

    def close(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None


class CommandAgent:
    """Any other agent with a command line: `agent_command` in settings.json,
    a list of arguments in which {prompt} becomes the cleanup prompt. It gets
    the transcript on stdin and its stdout is the cleaned text. For example
    ["gemini", "-m", "gemini-2.5-flash", "-p", "{prompt}"].
    """

    name = "custom command (agent_command in settings.json)"

    def __init__(self, command: list):
        self.command = [str(arg) for arg in command]

    def check(self) -> str | None:
        if not self.command:
            return "no agent_command in settings.json"
        if shutil.which(self.command[0]) is None:
            return f"{self.command[0]} not found"
        return None

    def start(self) -> None:
        pass

    def clean(self, text: str) -> str:
        prompt = current_prompt()
        result = subprocess.run(
            [arg.replace("{prompt}", prompt) for arg in self.command], input=text,
            capture_output=True, text=True, encoding="utf-8", timeout=CLEANUP_TIMEOUT_S, creationflags=NO_WINDOW,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[-300:] or f"exit code {result.returncode}")
        return result.stdout.strip()

    def close(self) -> None:
        pass


AGENTS = {  # settings "agent": the key
    "claude": ("Claude Code", lambda settings: ClaudeCode()),
    "command": ("Custom command", lambda settings: CommandAgent(settings.get("agent_command", []))),
}


def chosen_agent(settings: dict | None = None) -> tuple[str, object]:
    settings = load_settings() if settings is None else settings
    key = settings.get("agent") if settings.get("agent") in AGENTS else "claude"
    return key, AGENTS[key][1](settings)


def preflight(terminal, agent) -> None:
    """Raises with a plain reason for the common ways startup fails, for the tray tooltip."""
    import ctranslate2
    for problem in (terminal.check(), agent.check()):
        if problem:
            raise RuntimeError(problem)
    if ctranslate2.get_cuda_device_count() == 0:
        raise RuntimeError("no CUDA-capable NVIDIA GPU found")


# --- recording and transcription -------------------------------------------

class Dictation:
    def __init__(self, model: BatchedInferencePipeline, cleanup, terminal, on_state):
        self.model = model
        self.cleanup = cleanup  # an agent: clean(text) -> text
        self.terminal = terminal
        self.on_state = on_state  # called with "recording", "processing" or "idle"
        self.lock = threading.Lock()
        self.stream = None
        self.chunks = []
        self.processing = 0
        self.status = PaneStatus(terminal, None)

    def _report_state(self) -> None:
        with self.lock:
            state = "recording" if self.stream else "processing" if self.processing else "idle"
        self.on_state(state)

    def start(self) -> None:
        with self.lock:
            if self.stream is not None:
                return
            self.chunks = []
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                callback=lambda data, *_: self.chunks.append(data.copy()),
            )
            self.stream.start()
            self.on_state("recording")
            self.status = PaneStatus(self.terminal, self.terminal.focused_pane())
            self.status.show("🎤 recording…")
        log.info("recording (pane %s)", self.status.pane_id)

    def stop(self) -> None:
        with self.lock:
            if self.stream is None:
                return
            self.stream.stop()
            self.stream.close()
            self.stream = None
            audio = np.concatenate(self.chunks)[:, 0] if self.chunks else np.zeros(0, np.float32)
            status = self.status
            self.processing += 1
        self._report_state()
        status.show("⏳ transcribing…")
        threading.Thread(target=self.finish, args=(audio, status), daemon=True).start()

    def finish(self, audio: np.ndarray, status: PaneStatus) -> None:
        text = ""
        try:
            seconds = len(audio) / SAMPLE_RATE
            if seconds < 0.3:
                log.info("too short (%.2fs), ignored", seconds)
                return
            t = time.monotonic()
            segments, _ = self.model.transcribe(audio, language=LANGUAGE, beam_size=1, batch_size=8)
            text = " ".join(s.text.strip() for s in segments).strip()
            log.info("transcribed %.1fs of audio in %.2fs, %s", seconds, time.monotonic() - t, described(text))
            if text:
                status.show("✨ cleaning up…")
                t = time.monotonic()
                try:
                    text = self.cleanup.clean(text)
                    log.info("cleaned up in %.2fs, %s", time.monotonic() - t, described(text))
                except Exception:
                    log.exception("cleanup failed; inserting the raw transcript")
            if text and status.pane_id is None:
                log.info("no %s pane was focused; text not inserted", self.terminal.name)
        except Exception:
            log.exception("dictation failed")
        finally:
            status.finish(text)
            with self.lock:
                self.processing -= 1
            self._report_state()


# --- the shortcut ---------------------------------------------------------------

MODIFIER_VKS = {0x5B: "win", 0x5C: "win", 0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
                0x12: "alt", 0xA4: "alt", 0xA5: "alt", 0x10: "shift", 0xA0: "shift", 0xA1: "shift"}
MODIFIER_ORDER = ("win", "ctrl", "alt", "shift")
MODIFIER_LABELS = {"win": "Win", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift"}
# Keys whose name needs the extended flag, or GetKeyNameText calls them "Num 7" etc.
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}


def key_name(vk: int) -> str:
    """The key's name as the keyboard layout shows it: Q, F5, Space, Page Up."""
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    user32 = ctypes.windll.user32
    scan = user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
    lparam = (scan << 16) | ((1 << 24) if vk in EXTENDED_VKS else 0)
    buf = ctypes.create_unicode_buffer(64)
    if scan and user32.GetKeyNameTextW(lparam, buf, len(buf)):
        return buf.value
    return f"key {vk:#04x}"


@dataclass(frozen=True)
class Hotkey:
    """Modifiers plus a key, like Win+Q. The key starts and stops the recording
    while the modifiers are held."""

    modifiers: frozenset
    vk: int

    def __str__(self) -> str:
        mods = [MODIFIER_LABELS[m] for m in MODIFIER_ORDER if m in self.modifiers]
        return "+".join(mods + [key_name(self.vk)])

    def to_json(self) -> dict:
        return {"modifiers": sorted(self.modifiers), "vk": self.vk}

    @classmethod
    def from_json(cls, data: dict) -> "Hotkey":
        return cls(frozenset(data["modifiers"]), int(data["vk"]))


DEFAULT_HOTKEY = Hotkey(frozenset({"win"}), VK_Q)


def saved_hotkey() -> Hotkey:
    try:
        return Hotkey.from_json(load_settings()["hotkey"])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_HOTKEY


# --- hotkey ------------------------------------------------------------------

WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
VK_MASK = 0xE8  # unassigned; tapped so a lone Win (or Alt) release doesn't open Start (or a menu)


def tap_mask_key() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_MASK, 0, 0, 0)
    user32.keybd_event(VK_MASK, 0, 2, 0)  # KEYEVENTF_KEYUP


class Capture:
    """Reads the next shortcut typed, for the picker: the last non-modifier key
    pressed with modifiers held, once every key is released. Esc cancels."""

    def __init__(self):
        self.keys_down = set()
        self.candidate = None
        self.result = None
        self.done = threading.Event()

    def key(self, vk: int, down: bool, modifiers: frozenset) -> None:
        if down and vk == VK_ESCAPE:
            self.done.set()
            return
        (self.keys_down.add if down else self.keys_down.discard)(vk)
        if down and vk not in MODIFIER_VKS and modifiers:
            self.candidate = Hotkey(modifiers, vk)
        if not self.keys_down and self.candidate is not None:
            self.result = self.candidate
            self.done.set()

    def shown(self, modifiers: frozenset) -> str:
        if self.candidate is not None:
            return str(self.candidate)
        return "+".join(MODIFIER_LABELS[m] for m in MODIFIER_ORDER if m in modifiers)


class PushToTalk:
    """The shortcut held: start on the key down while its modifiers are held,
    stop when the key or a modifier goes up.

    Runs in the keyboard hook's event filter so the key presses can be
    swallowed; otherwise the focused app gets them. A Capture set in `capture`
    gets every key instead, all swallowed, while the picker is open.
    """

    def __init__(self, dictation: Dictation, hotkey: Hotkey):
        self.dictation = dictation
        self.hotkey = hotkey
        # Start/stop run off the hook: Windows drops a slow low-level hook, and
        # start() asks the terminal for the focused pane. One worker keeps them in order.
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.listener = None
        self.held = set()  # modifier vk codes currently down
        self.capture = None
        self.active = False

    def held_modifiers(self) -> frozenset:
        return frozenset(MODIFIER_VKS[vk] for vk in self.held)

    def event_filter(self, msg, data) -> bool:
        down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
        vk = data.vkCode
        if vk == VK_MASK:
            return True
        if vk in MODIFIER_VKS:
            (self.held.add if down else self.held.discard)(vk)
        capture = self.capture
        if capture is not None:
            capture.key(vk, down, self.held_modifiers())
            self.listener.suppress_event()
        if vk in MODIFIER_VKS:
            if not down and self.active and MODIFIER_VKS[vk] in self.hotkey.modifiers:
                self.active = False
                self._run(self.dictation.stop)
            return True
        if vk != self.hotkey.vk:
            return True
        if down and (self.active or self.hotkey.modifiers <= self.held_modifiers()):
            if not self.active:  # the key's repeat arrives as more downs
                self.active = True
                if self.hotkey.modifiers & {"win", "alt"}:
                    tap_mask_key()
                self._run(self.dictation.start)
            self.listener.suppress_event()
        elif not down and self.active:
            self.active = False
            self._run(self.dictation.stop)
            self.listener.suppress_event()
        return True

    def _run(self, action) -> None:
        """Queues action on the worker; the executor would swallow its errors."""
        def logged():
            try:
                action()
            except Exception:
                log.exception("%s failed", action.__name__)
        self.worker.submit(logged)

    def start(self) -> None:
        self.listener = keyboard.Listener(win32_event_filter=self.event_filter)
        self.listener.start()


# --- shortcuts already taken ----------------------------------------------------

# Windows' own Win+letter shortcuts. Explorer leaves alone the letters listed
# in its DisabledHotkeys value, except L (locking is a policy, not a hotkey).
WINDOWS_WIN_KEYS = {
    "A": "Quick Settings", "B": "the notification area", "C": "Copilot",
    "D": "showing the desktop", "E": "File Explorer", "F": "Feedback Hub",
    "G": "Game Bar", "H": "voice typing", "I": "Settings", "K": "Cast",
    "L": "locking the PC", "M": "minimizing all windows", "N": "the notification center",
    "P": "Project", "Q": "Search", "R": "Run", "S": "Search", "T": "the taskbar",
    "U": "accessibility settings", "V": "clipboard history", "W": "Widgets",
    "X": "the Quick Link menu", "Z": "Snap layouts",
}
SHORTCUT_FOLDERS = (r'@("$env:APPDATA\Microsoft\Windows\Start Menu", "$env:ProgramData\Microsoft\Windows\Start Menu", '
                    r'[Environment]::GetFolderPath("Desktop"), "$env:PUBLIC\Desktop")')
# Modifier and key spellings in a shortcut file's Hotkey property (WScript.Shell).
WSH_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "win": "win", "ext": "win"}
WSH_KEYS = {"escape": "esc", "return": "enter", "prior": "page up", "next": "page down",
            "back": "backspace", "snapshot": "print screen"}


@dataclass
class Conflict:
    """Something else using a shortcut, and how to turn that off, if we can.

    `free` turns it off and returns a record for the settings' `freed` list, so
    `restore_freed` can turn it back on when the shortcut is no longer ours.
    """

    description: str
    free: callable = None
    free_prompt: str = ""


def disabled_hotkeys() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, EXPLORER_ADVANCED) as key:
            return winreg.QueryValueEx(key, "DisabledHotkeys")[0]
    except OSError:
        return ""


def set_disabled_hotkeys(letters: str) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, EXPLORER_ADVANCED) as key:
        if letters:
            winreg.SetValueEx(key, "DisabledHotkeys", 0, winreg.REG_SZ, letters)
        else:
            try:
                winreg.DeleteValue(key, "DisabledHotkeys")
            except FileNotFoundError:
                pass
    restart_explorer()


def restart_explorer() -> None:
    """Explorer reads DisabledHotkeys and shortcut files' hotkeys when it starts."""
    log.info("restarting Explorer")
    powershell("Stop-Process -Name explorer -Force; Start-Sleep 2; "
               "if (-not (Get-Process explorer -ErrorAction SilentlyContinue)) { Start-Process explorer }")


def free_windows_hotkey(letter: str, hotkey: Hotkey) -> dict:
    set_disabled_hotkeys(disabled_hotkeys() + letter)
    return {"kind": "windows", "letter": letter, "hotkey": hotkey.to_json(), "name": str(hotkey)}


def shortcut_files_with_hotkeys() -> list[dict]:
    """Every .lnk in the Start Menus and desktops that has a shortcut key: {path, hotkey}."""
    lines = powershell(
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $sh = New-Object -ComObject WScript.Shell; "
        f"Get-ChildItem {SHORTCUT_FOLDERS} -Recurse -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object "
        "{ $s = $sh.CreateShortcut($_.FullName); if ($s.Hotkey) { @{path=$_.FullName; hotkey=$s.Hotkey} | ConvertTo-Json -Compress } }"
    )
    return [json.loads(line) for line in lines.splitlines() if line.strip()]


def wsh_hotkey_matches(wsh: str, hotkey: Hotkey) -> bool:
    parts = [p.strip().lower() for p in wsh.split("+")]
    modifiers = frozenset(WSH_MODIFIERS[p] for p in parts[:-1] if p in WSH_MODIFIERS)
    key = WSH_KEYS.get(parts[-1], parts[-1])
    return modifiers == hotkey.modifiers and key == key_name(hotkey.vk).lower()


def set_shortcut_file_hotkey(path: str, hotkey: str) -> None:
    powershell("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AZIZ_PATH); "
               "$s.Hotkey = $env:AZIZ_HOTKEY; $s.Save()", path=path, hotkey=hotkey)
    restart_explorer()


def free_shortcut_file(path: str, wsh: str, hotkey: Hotkey) -> dict:
    set_shortcut_file_hotkey(path, "")
    return {"kind": "shortcut", "path": path, "shortcut_hotkey": wsh, "hotkey": hotkey.to_json(), "name": str(hotkey)}


def registered_elsewhere(hotkey: Hotkey) -> bool:
    """Whether a running program has the combination as a global hotkey (RegisterHotKey).

    Windows says only that it is taken, not by whom.
    """
    mod = {"alt": 1, "ctrl": 2, "shift": 4, "win": 8}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if user32.RegisterHotKey(None, 1, sum(mod[m] for m in hotkey.modifiers) | 0x4000, hotkey.vk):
        user32.UnregisterHotKey(None, 1)
        return False
    return ctypes.get_last_error() == 1409  # ERROR_HOTKEY_ALREADY_REGISTERED


def find_conflict(hotkey: Hotkey) -> Conflict | None:
    """What already uses the shortcut, if anything: Windows itself, a shortcut
    file's key, or a program's global hotkey."""
    letter = key_name(hotkey.vk)
    if hotkey.modifiers == {"win"} and letter in WINDOWS_WIN_KEYS:
        if letter in disabled_hotkeys():
            return None  # already turned off for this account
        what = WINDOWS_WIN_KEYS[letter]
        if letter == "L":
            return Conflict(f"Windows uses {hotkey} for {what}, which cannot be turned off per user.")
        return Conflict(
            f"Windows uses {hotkey} for {what}.",
            free=lambda: free_windows_hotkey(letter, hotkey),
            free_prompt=(f"Turn {hotkey} off for your Windows account so Aziz!Write! can have it? "
                         "Explorer restarts to apply this, which closes File Explorer windows."),
        )
    for link in shortcut_files_with_hotkeys():
        if wsh_hotkey_matches(link["hotkey"], hotkey):
            path = link["path"]
            shared = path.lower().startswith((os.environ["ProgramData"].lower(), os.environ["PUBLIC"].lower()))
            if shared:
                return Conflict(f"{hotkey} opens the shortcut file {path}. "
                                "It is shared by all users, so only an administrator can change it.")
            return Conflict(
                f"{hotkey} opens the shortcut file {path}.",
                free=lambda: free_shortcut_file(path, link["hotkey"], hotkey),
                free_prompt=(f"Remove {hotkey} from that shortcut file so Aziz!Write! can have it? "
                             "Explorer restarts to apply this, which closes File Explorer windows."),
            )
    if registered_elsewhere(hotkey):
        return Conflict(f"A running program has registered {hotkey} as a global shortcut; "
                        "Windows does not say which one.")
    return None


def describe_freed(record: dict) -> str:
    if record["kind"] == "windows":
        return f"Windows' own {record['name']} ({WINDOWS_WIN_KEYS.get(record['letter'], 'a Windows feature')})"
    return f"the shortcut key {record['shortcut_hotkey']} of {record['path']}"


def restore_freed(record: dict) -> None:
    if record["kind"] == "windows":
        set_disabled_hotkeys(disabled_hotkeys().replace(record["letter"], ""))
    else:
        set_shortcut_file_hotkey(record["path"], record["shortcut_hotkey"])


def remember_freed(record: dict) -> None:
    with _settings_lock:
        freed = load_settings().get("freed", [])
    update_settings(freed=freed + [record])
    log.info("turned off %s for %s", describe_freed(record), record["name"])


def offer_restore(hotkey: Hotkey | None = None) -> None:
    """System shortcuts turned off for a hotkey (or, with None, for any) can
    come back once it is not ours."""
    for record in load_settings().get("freed", []):
        if hotkey is not None and Hotkey.from_json(record["hotkey"]) != hotkey:
            continue
        what = describe_freed(record)
        if not ask(f"{what[0].upper()}{what[1:]} was turned off so Aziz!Write! could use {record['name']}, "
                   "which it no longer does.\n\nTurn it back on? "
                   "Explorer restarts to apply this, which closes File Explorer windows."):
            continue
        try:
            restore_freed(record)
        except Exception:
            log.exception("could not restore %s", what)
            tell(f"Could not turn {what} back on; see the log.")
            continue
        with _settings_lock:
            freed = [r for r in load_settings().get("freed", []) if r != record]
        update_settings(freed=freed)
        log.info("restored %s", what)


def free_for(conflict: Conflict, hotkey: Hotkey, current: Hotkey | None) -> bool:
    """Asks about the conflict and, if agreed, turns the other shortcut off (when
    possible). True when the hotkey can be used."""
    keeps = f"\n\nNo keeps {current}." if current else ""
    if conflict.free is None:
        return ask(f"{conflict.description}\n\nUse {hotkey} anyway? Aziz!Write! sees the keys first "
                   f"while it runs, so the other shortcut stops working meanwhile.{keeps}")
    if not ask(f"{conflict.description}\n\n{conflict.free_prompt}{keeps}"):
        return False
    try:
        remember_freed(conflict.free())
    except Exception:
        log.exception("could not free %s", hotkey)
        tell(f"Could not turn the other {hotkey} off; see the log.")
        return False
    return True


# --- the shortcut picker --------------------------------------------------------

@tk_window
def pick_hotkey(ptt: PushToTalk) -> Hotkey | None:
    """A window that reads the next shortcut typed, through the keyboard hook."""
    import tkinter as tk
    capture = Capture()
    root = tk.Tk()
    root.title("Aziz!Write! shortcut")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, justify="left", padx=24, pady=16, text=(
        "Press the new push-to-talk shortcut, then release the keys.\n"
        f"It needs Win, Ctrl, Alt or Shift plus a key. Now: {ptt.hotkey}. Esc cancels."
    )).pack()
    shown = tk.Label(root, font=("Segoe UI", 18), pady=16)
    shown.pack()
    root.eval("tk::PlaceWindow . center")

    def poll():
        if capture.done.is_set():
            root.destroy()
            return
        shown.config(text=capture.shown(ptt.held_modifiers()) or "…")
        root.after(50, poll)

    ptt.capture = capture
    try:
        root.after(50, poll)
        root.mainloop()
    finally:
        ptt.capture = None
    return capture.result


def change_hotkey(ptt: PushToTalk, on_changed) -> None:
    if ptt.capture is not None:
        return  # the picker is already open
    new = pick_hotkey(ptt)
    if new is None or new == ptt.hotkey:
        return
    conflict = find_conflict(new)
    if conflict is not None and not free_for(conflict, new, ptt.hotkey):
        return
    old, ptt.hotkey = ptt.hotkey, new
    update_settings(hotkey=new.to_json())
    log.info("shortcut changed from %s to %s", old, new)
    on_changed()
    offer_restore(old)


# --- tray ------------------------------------------------------------------------

TRAY_STATES = {
    "loading": ((140, 140, 140), "Aziz!Write! {version}: starting…"),
    "idle": ((52, 120, 246), "Aziz!Write! {version}: hold {hotkey} to dictate"),
    "recording": ((229, 57, 53), "Aziz!Write!: recording…"),
    "processing": ((245, 166, 35), "Aziz! Write!…"),
    "error": ((120, 0, 0), "Aziz!Write!: {error}"),
}
TOOLTIP_MAX = 127  # Shell_NotifyIcon's limit


def mic_icon(rgb: tuple) -> Image.Image:
    """A white microphone on a circle in the state's color.

    Drawn at 4x and scaled down, so the curves stay smooth at tray size.
    """
    k = 4
    image = Image.new("RGBA", (64 * k, 64 * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    white = (255, 255, 255, 255)
    d.ellipse((1 * k, 1 * k, 63 * k, 63 * k), fill=rgb + (255,))
    d.rounded_rectangle((24 * k, 11 * k, 40 * k, 37 * k), radius=8 * k, fill=white)  # capsule
    d.arc((17 * k, 19 * k, 47 * k, 45 * k), start=0, end=180, fill=white, width=4 * k)  # holder
    d.line((32 * k, 45 * k, 32 * k, 52 * k), fill=white, width=4 * k)  # stem
    d.line((24 * k, 53 * k, 40 * k, 53 * k), fill=white, width=4 * k)  # base
    return image.resize((64, 64), Image.LANCZOS)


def starts_at_login() -> bool:
    return STARTUP_LINK.exists()


def startup_command() -> tuple[str, str]:
    """What the Startup shortcut runs: the exe itself, or the script with the venv's pythonw."""
    if FROZEN:
        return str(Path(sys.executable).resolve()), ""
    return str(PYTHONW), f'"{HERE / "aziz_write.py"}"'


def set_start_at_login(enabled: bool) -> None:
    """Adds or removes a Startup-folder shortcut that runs the app."""
    if not enabled:
        STARTUP_LINK.unlink(missing_ok=True)
        return
    make_shortcut(STARTUP_LINK, *startup_command())


def installed() -> bool:
    """Whether this is the exe in INSTALL_DIR (from source: never)."""
    return FROZEN and HERE.resolve() == INSTALL_DIR.resolve()


def make_shortcut(link: Path, target: str, args: str = "") -> None:
    powershell(
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AZIZ_LINK);"
        "$s.TargetPath = $env:AZIZ_TARGET; $s.Arguments = $env:AZIZ_ARGS;"
        "$s.WorkingDirectory = $env:AZIZ_DIR; $s.Description = 'Aziz!Write! push-to-talk dictation'; $s.Save()",
        link=str(link), target=target, args=args, dir=str(Path(target).parent),
    )


def after_exit(script: str, **env: str) -> None:
    """Runs a PowerShell snippet once this process has exited, with AZIZ_* env values.

    Children outlive their parent on Windows; a hidden console is enough.
    (DETACHED_PROCESS, which gives none, stops PowerShell from running at all.)
    """
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; {script}"],
        env={**os.environ, **{f"AZIZ_{k.upper()}": v for k, v in env.items()}},
        creationflags=NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )


def install_to_programs(on_quit) -> None:
    """Copies the app's folder to INSTALL_DIR, adds a Start Menu entry, points
    an existing start-at-login shortcut there, and restarts from there."""
    log.info("installing into %s", INSTALL_DIR)
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    for item in HERE.iterdir():
        if item.name in ("aziz-write.log", "cuda-download"):
            continue
        target = INSTALL_DIR / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    exe = str(INSTALL_DIR / Path(sys.executable).name)
    make_shortcut(START_MENU_LINK, exe)
    if starts_at_login():
        make_shortcut(STARTUP_LINK, exe)
    after_exit("Start-Process -FilePath $env:AZIZ_EXE -WorkingDirectory $env:AZIZ_DIR", exe=exe, dir=str(INSTALL_DIR))
    log.info("installed; restarting from %s", INSTALL_DIR)
    on_quit()


def offer_install(on_quit) -> bool:
    """When the exe runs from somewhere else (an unzipped folder, Downloads),
    offers to install. True when it is installing and about to restart."""
    if not FROZEN or installed():
        return False
    if not ask(f"Install Aziz!Write! into {INSTALL_DIR} and add it to the Start Menu?\n\n"
               "It then runs from there, and this folder can be deleted. "
               "No keeps running from here (the tray menu can install later)."):
        return False
    try:
        install_to_programs(on_quit)
    except Exception as e:
        log.exception("installing failed")
        tell(f"Could not install: {e}")
        return False
    return True


def first_launch(hotkey: Hotkey, on_quit) -> None:
    """On the first launch: offers to install, to start at login, and to free
    the shortcut if Windows or something else has it."""
    if load_settings().get("asked_start_at_login"):
        return
    if offer_install(on_quit):
        return  # the installed copy asks the rest
    update_settings(asked_start_at_login=True)
    try:
        if not starts_at_login() and ask(
            "Start Aziz!Write! automatically when you sign in to Windows?\n\n"
            "You can change this later in the tray icon's menu (Start at login)."
        ):
            set_start_at_login(True)
        conflict = find_conflict(hotkey)
        if conflict is not None and conflict.free is not None:
            free_for(conflict, hotkey, None)
    except Exception:
        log.exception("first-launch setup failed")


def report_problem(hotkey_text: str) -> None:
    """Opens a new GitHub issue with the version, Windows version and the end
    of the log filled in. The log has timings only, unless AZIZ_WRITE_LOG_TEXT was set."""
    try:
        tail = "".join(LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-30:])[-3000:]
    except OSError:
        tail = "(no log)"
    body = (f"**Aziz!Write!:** {VERSION} ({'exe' if FROZEN else 'from source'})\n"
            f"**Windows:** {platform.platform()}\n**Shortcut:** {hotkey_text}\n\n"
            "**What happened, and what you expected:**\n\n\n\n"
            f"**Log, last lines:**\n```\n{tail}\n```\n")
    webbrowser.open(f"{ISSUES_URL}?{urllib.parse.urlencode({'body': body})}")


# --- updates ----------------------------------------------------------------------

UPDATE_SCRIPT = r"""
# Written by Aziz!Write!: waits for the running app to exit, puts the new
# version's files in place, and starts it again.
param([int]$AppPid, [string]$Zip, [string]$Here, [string]$Exe)
$ErrorActionPreference = 'Stop'
try {
    Wait-Process -Id $AppPid -ErrorAction SilentlyContinue
    $new = Join-Path (Split-Path $Zip) 'new'
    Expand-Archive -Path $Zip -DestinationPath $new -Force
    if (Test-Path (Join-Path $Here '_internal')) { Remove-Item (Join-Path $Here '_internal') -Recurse -Force }
    foreach ($item in Get-ChildItem $new) {
        if ($item.Name -eq 'prompts.yaml' -and (Test-Path (Join-Path $Here 'prompts.yaml'))) { continue }  # keep the user's edits
        Copy-Item $item.FullName -Destination $Here -Recurse -Force
    }
    Start-Process -FilePath $Exe -WorkingDirectory $Here
    Remove-Item (Split-Path $Zip) -Recurse -Force
} catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("Updating Aziz!Write! failed: $_`n`nThe new version is unpacked in $new; copy it over $Here by hand, then start the app again.", 'Aziz!Write!') | Out-Null
}
"""


def version_tuple(text: str) -> tuple:
    return tuple(int(part) for part in text.strip().lstrip("v").split(".") if part.isdigit())


def latest_release() -> tuple[str, str] | None:
    """The newest release's version and zip URL, if it is newer than this one."""
    request = urllib.request.Request(LATEST_RELEASE_URL, headers={"User-Agent": f"aziz-write/{VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            release = json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # no release yet
        raise
    version = release.get("tag_name", "")
    if version_tuple(version) <= version_tuple(VERSION):
        return None
    zip_url = next((a["browser_download_url"] for a in release.get("assets", [])
                    if a["name"].endswith("-windows.zip")), None)
    if zip_url is None:
        raise RuntimeError(f"release {version} has no Windows zip attached")
    return version.lstrip("v"), zip_url


def install_release(zip_url: str, on_progress, on_quit) -> None:
    """Downloads the release and hands over to UPDATE_SCRIPT, which replaces the
    files once this process has quit. The log, cuda\\ and prompts.yaml stay."""
    workdir = Path(tempfile.mkdtemp(prefix="aziz-write-update-"))
    zip_path = workdir / "update.zip"
    download(zip_url, zip_path, lambda f: on_progress(f"downloading the update: {f:.0%}"))
    script = workdir / "update.ps1"
    script.write_text(UPDATE_SCRIPT, encoding="utf-8")
    subprocess.Popen(  # the script itself waits for this process to exit (see after_exit on the flags)
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-AppPid", str(os.getpid()), "-Zip", str(zip_path), "-Here", str(HERE), "-Exe", sys.executable],
        creationflags=NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    log.info("update downloaded; quitting for %s to install it", script)
    on_quit()


def git(*args: str) -> str:
    if shutil.which("git") is None:
        raise RuntimeError("git is not installed")
    result = subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True,
                            encoding="utf-8", creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout


def source_update() -> str | None:
    """For a source checkout: the new commits upstream, or None when up to date."""
    git("fetch", "--quiet")
    if git("rev-list", "--count", "HEAD..@{u}").strip() == "0":
        return None
    return git("log", "--oneline", "--no-decorate", "HEAD..@{u}")


def install_source_update(on_progress, on_quit) -> None:
    """Fast-forwards the checkout, reinstalls the requirements, and restarts."""
    on_progress("pulling the update…")
    git("pull", "--ff-only", "--quiet")
    on_progress("installing the requirements…")
    python = PYTHONW.with_name("python.exe")
    result = subprocess.run([str(python), "-m", "pip", "install", "-q", "-r", str(HERE / "requirements.txt")],
                            capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f"pip install failed: {result.stderr.strip()[-500:]}")
    after_exit("Start-Process -FilePath $env:AZIZ_PYTHONW -ArgumentList ('\"' + $env:AZIZ_SCRIPT + '\"') "
               "-WorkingDirectory $env:AZIZ_DIR",
               pythonw=str(PYTHONW), script=str(HERE / "aziz_write.py"), dir=str(HERE))
    log.info("updated; restarting")
    on_quit()


def check_for_updates(on_progress, on_quit) -> None:
    """The menu's Check for updates: a release for the exe, git for a checkout."""
    try:
        if FROZEN:
            release = latest_release()
            if release is None:
                tell(f"Aziz!Write! {VERSION} is the newest release.")
                return
            version, zip_url = release
            if ask(f"Aziz!Write! {version} is available (this is {VERSION}).\n\n"
                   "Download it and restart into it? Your prompts, settings, log and CUDA libraries stay."):
                install_release(zip_url, on_progress, on_quit)
        else:
            commits = source_update()
            if commits is None:
                tell(f"This checkout of Aziz!Write! is up to date with its upstream branch.")
                return
            if ask(f"New commits upstream:\n\n{commits}\nPull them, reinstall the requirements and restart?"):
                install_source_update(on_progress, on_quit)
    except Exception as e:
        log.exception("update check failed")
        tell(f"Could not update: {e}")


def downloaded_model_dir() -> Path | None:
    """The model's folder in the Hugging Face cache (models--…), if it is there."""
    try:
        snapshot = Path(download_model(MODEL, local_files_only=True))
    except Exception:
        return None
    return snapshot.parents[1] if snapshot.parents[1].name.startswith("models--") else None


def disable_and_clean_up(on_quit) -> None:
    """Undoes what the app set up on this account, then quits. The app's own
    folder is left for the user to delete."""
    folder = (f"The installed app, {HERE}, is removed too." if installed()
              else f"The app's folder, {HERE}, stays; delete it yourself afterwards.")
    if not ask("Disable Aziz!Write! and clean up?\n\nThis turns off start at login, offers to turn "
               "back on any Windows shortcuts it turned off, removes its settings, and quits. "
               f"It then asks whether to delete the downloaded CUDA libraries and speech model.\n\n{folder}"):
        return
    try:
        set_start_at_login(False)
        START_MENU_LINK.unlink(missing_ok=True)
        offer_restore()
        cuda_dirs = [d for d in (CUDA_DIR, CUDA_DOWNLOAD_DIR) if d.exists()]
        if cuda_dirs and ask("Delete the CUDA libraries the app downloaded (about 2.5 GB)?\n\n"
                             + "\n".join(str(d) for d in cuda_dirs)):
            for d in cuda_dirs:
                shutil.rmtree(d)
                log.info("deleted %s", d)
        model_dir = downloaded_model_dir()
        if model_dir is not None and ask(f"Delete the speech model from the Hugging Face cache (about 1.6 GB)?\n\n"
                                         f"{model_dir}\n\nOther programs using faster-whisper may share it."):
            shutil.rmtree(model_dir)
            log.info("deleted %s", model_dir)
        SETTINGS_PATH.unlink(missing_ok=True)
        try:
            SETTINGS_PATH.parent.rmdir()
        except OSError:
            pass
        log.info("disabled and cleaned up")
    except Exception as e:
        log.exception("clean-up failed")
        tell(f"Clean-up did not finish: {e}. See the log.")
        return
    if installed():
        after_exit("Remove-Item -LiteralPath $env:AZIZ_DIR -Recurse -Force", dir=str(INSTALL_DIR))
        tell("Aziz!Write! is disabled and will remove itself now.")
    else:
        tell(f"Aziz!Write! is disabled and will quit now. To remove it, delete {HERE}.")
    on_quit()


class Tray:
    def __init__(self, on_quit, on_set_hotkey, on_select_agent, hotkey_text):
        self.hotkey_text = hotkey_text  # the current shortcut's name, for the menu and tooltip
        self.state = "loading"
        self.error = ""
        self.prompts_open = threading.Lock()  # held while the prompts window is open
        self.icons = {state: mic_icon(rgb) for state, (rgb, _) in TRAY_STATES.items()}
        ready = lambda _: self.state not in ("loading", "error")  # noqa: E731

        def agent_item(key: str, name: str) -> pystray.MenuItem:
            return pystray.MenuItem(name, lambda: on_select_agent(key), radio=True,
                                    checked=lambda _: chosen_agent()[0] == key)

        agents = pystray.Menu(*(agent_item(key, name) for key, (name, _) in AGENTS.items()))
        self.icon = pystray.Icon("aziz-write", self.icons["loading"], self._title(), menu=pystray.Menu(
            pystray.MenuItem(lambda _: f"Shortcut: {hotkey_text()}…", on_set_hotkey, enabled=ready),
            pystray.MenuItem(lambda _: f"Agent: {AGENTS[chosen_agent()[0]][0]}", agents, enabled=ready),
            pystray.MenuItem(lambda _: f"Prompt: {current_prompt_name()}…", self._prompts),
            pystray.MenuItem("Start at login", self._toggle_login, checked=lambda _: starts_at_login()),
            pystray.MenuItem("Install into Programs…", self._install, visible=lambda _: FROZEN and not installed()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open log", lambda: os.startfile(LOG_PATH)),
            pystray.MenuItem("Report a problem…", lambda: report_problem(hotkey_text())),
            pystray.MenuItem(f"Version {VERSION}", None, enabled=False),
            pystray.MenuItem("Check for updates…", self._check_for_updates),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Disable and clean up…", self._clean_up),
            pystray.MenuItem("Quit", self._quit),
        ))
        self.on_quit = on_quit

    def _clean_up(self) -> None:
        threading.Thread(target=disable_and_clean_up, args=(self._quit,), daemon=True).start()

    def _install(self) -> None:
        threading.Thread(target=offer_install, args=(self._quit,), daemon=True).start()

    def _prompts(self) -> None:
        if self.prompts_open.locked():
            return
        def run():
            with self.prompts_open:
                prompts_window(self.refresh)
        threading.Thread(target=run, daemon=True).start()

    def _check_for_updates(self) -> None:
        def run():
            check_for_updates(self.set_progress, self._quit)
            self.refresh()  # the tooltip, if the check ended without quitting
        threading.Thread(target=run, daemon=True).start()

    def _title(self) -> str:
        text = TRAY_STATES[self.state][1].format(version=VERSION, hotkey=self.hotkey_text(), error=self.error)
        return text[:TOOLTIP_MAX]

    def set_state(self, state: str) -> None:
        self.state = state
        self.icon.icon = self.icons[state]
        self.icon.title = self._title()

    def set_error(self, reason: str) -> None:
        """Red icon, the reason in the tooltip; the menu's Open log has the rest."""
        self.error = f"{reason.splitlines()[0] if reason else 'failed to start'} (see Open log in the menu)"
        self.set_state("error")

    def set_progress(self, text: str) -> None:
        """While starting: what is being fetched or loaded, in the tooltip."""
        self.icon.title = f"Aziz!Write! {VERSION}: {text}"[:TOOLTIP_MAX]

    def refresh(self) -> None:
        """After a setting changed: the menu entries and tooltip name it."""
        self.icon.title = self._title()
        self.icon.update_menu()

    def run(self, setup) -> None:
        self.icon.run(setup=setup)

    def _toggle_login(self) -> None:
        try:
            set_start_at_login(not starts_at_login())
        except subprocess.CalledProcessError:
            log.exception("could not change start at login")

    def _quit(self) -> None:
        self.icon.stop()
        self.on_quit()


def already_running() -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)  # held until exit
    return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def main() -> None:
    handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    if sys.stdout is not None:  # None under pythonw and in the windowed exe
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)
    for name in ("httpx", "httpx2"):  # Hugging Face's client logs every request
        logging.getLogger(name).setLevel(logging.WARNING)
    if already_running():
        log.info("already running; exiting")
        return
    log.info("Aziz!Write! %s starting (%s)", VERSION, "exe" if FROZEN else "from source")

    dictation = None
    ptt = None
    hotkey = saved_hotkey()

    def quit_app() -> None:
        if dictation is not None:
            dictation.cleanup.close()
        log.info("quit")
        os._exit(0)  # the keyboard hook and worker threads are not daemonic

    def set_hotkey() -> None:
        if ptt is not None:
            threading.Thread(target=change_hotkey, args=(ptt, tray.refresh), daemon=True).start()

    def select_agent(key: str) -> None:
        """Switches the cleanup agent, keeping the old one if the new one cannot run."""
        if dictation is None or key == chosen_agent()[0]:
            return
        name, agent = AGENTS[key][0], AGENTS[key][1](load_settings())
        problem = agent.check()
        if problem:
            tell(f"Cannot use {name}: {problem}.")
            tray.refresh()
            return
        try:
            agent.start()
        except Exception as e:
            log.exception("could not start the agent %s", name)
            tell(f"Cannot use {name}: {e}")
            tray.refresh()
            return
        old, dictation.cleanup = dictation.cleanup, agent
        old.close()
        update_settings(agent=key)
        log.info("agent changed to %s", name)
        tray.refresh()

    tray = Tray(on_quit=quit_app, on_set_hotkey=set_hotkey, on_select_agent=select_agent,
                hotkey_text=lambda: str(ptt.hotkey if ptt is not None else hotkey))

    def load_and_listen(icon) -> None:
        nonlocal dictation, ptt
        icon.visible = True
        threading.Thread(target=first_launch, args=(hotkey, quit_app), daemon=True).start()
        try:
            cuda, model_files = CudaNeed(), ModelNeed()
            missing = [need for need in (cuda, model_files) if need.locate() is None]
            if missing:
                log.info("setup needed: %s", ", ".join(need.title for need in missing))
                tray.set_progress("setup needed; see the setup window")
                if not setup_window(missing):
                    log.info("setup window closed; quitting")
                    quit_app()
            terminal = chosen_terminal()
            agent_key, agent = chosen_agent()
            preflight(terminal, agent)
            t = time.monotonic()
            tray.set_progress("loading the speech model…")
            model = BatchedInferencePipeline(
                WhisperModel(str(model_files.found), device="cuda", compute_type="float16"))
            agent.start()
            dictation = Dictation(model, agent, terminal, tray.set_state)
            ptt = PushToTalk(dictation, hotkey)
            ptt.start()
            log.info("model %s loaded in %.1fs from %s; %s cleans up, text goes to %s; hold %s to dictate",
                     MODEL, time.monotonic() - t, model_files.found, agent.name, terminal.name, hotkey)
            tray.set_state("idle")
        except Exception as e:
            log.exception("startup failed")
            tray.set_error(str(e))

    tray.run(setup=lambda icon: threading.Thread(target=load_and_listen, args=(icon,), daemon=True).start())


if __name__ == "__main__":
    main()
