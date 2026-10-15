"""Aziz!Write!: push-to-talk dictation. Hold Win+Q, speak, release. Lives in the tray.

The recording is transcribed on the GPU with faster-whisper, cleaned up by
Claude Haiku through Claude Code (prompt.txt), and typed into the WezTerm pane
that had focus when the key went down, so switching windows while it processes
does not misplace the text.
"""
import ctypes
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / "aziz-write.log"
WEZTERM = r"C:\Program Files\WezTerm\wezterm.exe"
WEZTERM_PROCESS = "wezterm-gui.exe"
MODEL = "large-v3-turbo"
LANGUAGE = "en"
SAMPLE_RATE = 16000
VK_Q = 0x51
PROMPT_PATH = HERE / "prompt.txt"
CLAUDE = Path.home() / ".local" / "bin" / "claude.exe"
CLEANUP_MODEL = "haiku"
CLEANUP_TIMEOUT_S = 30
NO_WINDOW = subprocess.CREATE_NO_WINDOW
PYTHONW = HERE / ".venv" / "Scripts" / "pythonw.exe"
STARTUP_LINK = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup" / "Aziz!Write!.lnk")
SINGLE_INSTANCE_MUTEX = "Local\\aziz-write"
# What you dictate stays out of the log unless this is set, for debugging the
# cleanup prompt: the log is a plain file that would otherwise keep everything.
LOG_TEXT = os.environ.get("AZIZ_WRITE_LOG_TEXT") == "1"

log = logging.getLogger("aziz-write")


def described(text: str) -> str:
    """Text for the log: its length, plus the text itself only when LOG_TEXT."""
    return f"{len(text)} chars: {text!r}" if LOG_TEXT else f"{len(text)} chars"


def add_cuda_dll_dirs() -> None:
    """Puts the cuBLAS/cuDNN DLLs where ctranslate2 finds them.

    They come from NVIDIA's redistributable zips, unpacked into cuda/ by
    setup-cuda.ps1; NVIDIA's CDN is far faster than PyPI for these.
    """
    for bin_dir in (HERE / "cuda").glob("*/bin"):
        os.add_dll_directory(str(bin_dir))
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


add_cuda_dll_dirs()

import numpy as np  # noqa: E402
import pystray  # noqa: E402
import sounddevice as sd  # noqa: E402
from faster_whisper import BatchedInferencePipeline, WhisperModel  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from pynput import keyboard  # noqa: E402


# --- where the text goes ----------------------------------------------------

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


def focused_wezterm_pane() -> int | None:
    """The focused WezTerm pane, if WezTerm is the foreground window."""
    if foreground_process_name() != WEZTERM_PROCESS:
        return None
    clients = json.loads(wezterm_cli("list-clients", "--format", "json") or "[]")
    return clients[0]["focused_pane_id"] if clients else None


def wezterm_cli(*args: str, input: str | None = None) -> str:
    return subprocess.run(
        [WEZTERM, "cli", *args], input=input, capture_output=True, text=True,
        encoding="utf-8", check=True, creationflags=NO_WINDOW,
    ).stdout


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
    backspaces delete the wrong text. The tab's own title is restored after;
    an empty title is WezTerm's automatic one.
    """

    BACKSPACE = "\x7f"

    def __init__(self, pane_id: int | None):
        self.pane_id = pane_id
        self.marker = ""
        self.original_title = ""
        if pane_id is not None:
            panes = json.loads(wezterm_cli("list", "--format", "json"))
            self.original_title = next((p["tab_title"] for p in panes if p["pane_id"] == pane_id), "")

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
            self._cli("send-text", "--no-paste", "--pane-id", str(self.pane_id), input=keys)

    def _set_title(self, title: str) -> None:
        if self.pane_id is not None:
            self._cli("set-tab-title", "--pane-id", str(self.pane_id), title)

    def _cli(self, *args: str, input: str | None = None) -> None:
        try:
            wezterm_cli(*args, input=input)
        except subprocess.CalledProcessError as e:  # the pane may be gone
            log.warning("wezterm cli %s failed: %s", args[0], e.stderr.strip())


# --- cleanup ------------------------------------------------------------------

class Cleanup:
    """Cleans a transcript with Claude Haiku through Claude Code.

    Starting claude.exe costs about as much as Haiku's answer, so one process
    is kept started and waiting on stream-json input, for the current prompt;
    each cleanup uses it once and starts the next. prompt.txt is reread on
    every cleanup, so edits apply to the next dictation after.
    """

    def __init__(self):
        self.workdir = tempfile.mkdtemp(prefix="aziz-write-claude-")
        self.lock = threading.Lock()
        self.prompt = None
        self.proc = None
        self.prepare()

    def prepare(self) -> None:
        """Starts a process for the current prompt unless one is ready."""
        prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
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


# --- recording and transcription -------------------------------------------

class Dictation:
    def __init__(self, model: BatchedInferencePipeline, cleanup: Cleanup, on_state):
        self.model = model
        self.cleanup = cleanup
        self.on_state = on_state  # called with "recording", "processing" or "idle"
        self.lock = threading.Lock()
        self.stream = None
        self.chunks = []
        self.processing = 0
        self.status = PaneStatus(None)

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
            self.status = PaneStatus(focused_wezterm_pane())
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
                log.info("no WezTerm pane was focused; text not inserted")
        except Exception:
            log.exception("dictation failed")
        finally:
            status.finish(text)
            with self.lock:
                self.processing -= 1
            self._report_state()


# --- hotkey ------------------------------------------------------------------

WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_MASK = 0xE8  # unassigned; tapped so a lone Win release doesn't open Start


def tap_mask_key() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_MASK, 0, 0, 0)
    user32.keybd_event(VK_MASK, 0, 2, 0)  # KEYEVENTF_KEYUP


class PushToTalk:
    """Win+Q held: start on Q down while a Win key is held, stop on either up.

    Runs in the keyboard hook's event filter so the Q presses can be swallowed;
    otherwise the focused app gets them (Explorer's own Win+Q is disabled).
    """

    def __init__(self, dictation: Dictation):
        self.dictation = dictation
        # Start/stop run off the hook: Windows drops a slow low-level hook, and
        # start() asks WezTerm for the focused pane. One worker keeps them in order.
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.listener = None
        self.win_down = False
        self.active = False

    def event_filter(self, msg, data) -> bool:
        down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
        if data.vkCode in (VK_LWIN, VK_RWIN):
            self.win_down = down
            if not down and self.active:
                self.active = False
                self.worker.submit(self.dictation.stop)
            return True
        if data.vkCode != VK_Q:
            return True
        if down and (self.win_down or self.active):
            if not self.active:  # Q's key repeat arrives as more downs
                self.active = True
                tap_mask_key()
                self.worker.submit(self.dictation.start)
            self.listener.suppress_event()
        elif not down and self.active:
            self.active = False
            self.worker.submit(self.dictation.stop)
            self.listener.suppress_event()
        return True

    def start(self) -> None:
        self.listener = keyboard.Listener(win32_event_filter=self.event_filter)
        self.listener.start()


# --- tray ------------------------------------------------------------------------

TRAY_STATES = {
    "loading": ((140, 140, 140), "Aziz!Write!: loading the speech model…"),
    "idle": ((52, 120, 246), "Aziz!Write!: hold Win+Q to dictate"),
    "recording": ((229, 57, 53), "Aziz!Write!: recording…"),
    "processing": ((245, 166, 35), "Aziz! Write!…"),
    "error": ((120, 0, 0), "Aziz!Write!: failed to start; see the log"),
}


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


def set_start_at_login(enabled: bool) -> None:
    """Adds or removes a Startup-folder shortcut running the app with pythonw."""
    if not enabled:
        STARTUP_LINK.unlink(missing_ok=True)
        return
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AZIZ_LINK);"
        "$s.TargetPath = $env:AZIZ_PYTHONW; $s.Arguments = '\"' + $env:AZIZ_SCRIPT + '\"';"
        "$s.WorkingDirectory = $env:AZIZ_DIR; $s.Description = 'Aziz!Write! push-to-talk dictation'; $s.Save()"
    )
    env = dict(os.environ, AZIZ_LINK=str(STARTUP_LINK), AZIZ_PYTHONW=str(PYTHONW),
               AZIZ_SCRIPT=str(HERE / "aziz_write.py"), AZIZ_DIR=str(HERE))
    subprocess.run(["powershell", "-NoProfile", "-Command", script],
                   env=env, check=True, creationflags=NO_WINDOW)


class Tray:
    def __init__(self, on_quit):
        title = TRAY_STATES["loading"][1]
        self.icons = {state: mic_icon(rgb) for state, (rgb, _) in TRAY_STATES.items()}
        self.icon = pystray.Icon("aziz-write", self.icons["loading"], title, menu=pystray.Menu(
            pystray.MenuItem("Start at login", self._toggle_login, checked=lambda _: starts_at_login()),
            pystray.MenuItem("Edit prompt", lambda: os.startfile(PROMPT_PATH)),
            pystray.MenuItem("Open log", lambda: os.startfile(LOG_PATH)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        ))
        self.on_quit = on_quit

    def set_state(self, state: str) -> None:
        self.icon.icon = self.icons[state]
        self.icon.title = TRAY_STATES[state][1]

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
    if sys.stdout is not None:  # None under pythonw
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)
    if already_running():
        log.info("already running; exiting")
        return

    cleanup = None

    def quit_app() -> None:
        if cleanup is not None:
            cleanup.close()
        log.info("quit")
        os._exit(0)  # the keyboard hook and worker threads are not daemonic

    tray = Tray(on_quit=quit_app)

    def load_and_listen(icon) -> None:
        nonlocal cleanup
        icon.visible = True
        try:
            t = time.monotonic()
            model = BatchedInferencePipeline(WhisperModel(MODEL, device="cuda", compute_type="float16"))
            cleanup = Cleanup()
            PushToTalk(Dictation(model, cleanup, tray.set_state)).start()
            log.info("model %s loaded in %.1fs; hold Win+Q to dictate", MODEL, time.monotonic() - t)
            tray.set_state("idle")
        except Exception:
            log.exception("startup failed")
            tray.set_state("error")

    tray.run(setup=lambda icon: threading.Thread(target=load_and_listen, args=(icon,), daemon=True).start())


if __name__ == "__main__":
    main()
