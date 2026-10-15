# dictate

Push-to-talk dictation for Windows, built for talking to Claude Code in WezTerm.

Hold **Win+Q**, speak, release. A few seconds later the text appears in the
WezTerm pane you were in when you pressed the key, cleaned up: no "um"s,
self-corrections applied ("54, no wait, 53" becomes "53"), spoken paths and
code written as characters, lists and steps formatted as lists.

## Why

Claude Code's built-in dictation types your speech word for word, fillers and
false starts included. Off-the-shelf dictation apps fix that, but on Windows
they transcribe on the CPU (slow on long dictations) and insert text by
pasting into whatever window has focus, so switching windows while it works
sends the text to the wrong place. This app transcribes on the GPU, cleans up
with Claude Haiku, and types into the exact pane you started in.

## How it works

1. A keyboard hook catches Win+Q while held and swallows the keys.
2. The microphone is recorded at 16 kHz while the key is down.
3. [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
   (`large-v3-turbo`) transcribes it on the NVIDIA GPU, in about a second.
4. Claude Haiku cleans up the transcript through Claude Code (`claude.exe -p`),
   using the rules in `prompt.txt`. One process is kept started and waiting,
   so this takes about a second too.
5. `wezterm cli send-text` types the result into the original pane. It is
   typed, not pasted, so Claude Code shows it in full; line breaks go in as
   newlines that don't submit.

While it works, the target pane shows a marker at the cursor (🎤 recording…,
⏳ transcribing…, ✨ cleaning up…) and the tab's title says the same. The tray
icon changes color: blue ready, red recording, amber processing.

## Setup

Needs Windows with an NVIDIA GPU, Python 3.10+, WezTerm, and Claude Code
installed on Windows and logged in (`claude` → `/login`).

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File setup-cuda.ps1   # cuBLAS + cuDNN from NVIDIA
.venv\Scripts\pythonw.exe dictate.py
```

The first start downloads the Whisper model (about 1.6 GB). Then use the tray
icon's menu to turn on **Start at login**.

Windows opens Search on Win+Q. Free it for this app (then restart Explorer or
sign out and back in):

```powershell
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' DisabledHotkeys Q
```

## Files

- `dictate.py`: the app.
- `prompt.txt`: the cleanup rules given to Haiku. Edits apply to the next
  dictation; open it from the tray menu.
- `setup-cuda.ps1`: fetches the CUDA libraries into `cuda\`. NVIDIA's CDN is
  much faster than PyPI for these.
- `dictate.log`: what happened on each dictation, with timings.

## Limits

- Text only goes into WezTerm. Dictating with another app focused transcribes
  but inserts nothing yet.
- Don't type in the target pane while a dictation is processing: the marker is
  removed with backspaces.
- English only (`LANGUAGE` in `dictate.py`).
