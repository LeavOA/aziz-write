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

## GPU transcription

Transcription runs on the NVIDIA GPU through faster-whisper, which needs two
NVIDIA libraries on top of the normal graphics driver: **cuBLAS** (CUDA 12)
and **cuDNN 9**. The full CUDA Toolkit is not needed.

**Get them from NVIDIA, not PyPI.** They are also published on PyPI as
`nvidia-cublas-cu12` and `nvidia-cudnn-cu12`, but those wheels are about
1.3 GB together and PyPI and its mirrors served them at 0.1–0.6 MB/s when
this was set up: over an hour. NVIDIA's own redistributable CDN served the
same libraries at several MB/s, about three minutes. `setup-cuda.ps1` does
this:

1. downloads the cuBLAS and cuDNN zips from
   `developer.download.nvidia.com/compute/.../redist/` into `cuda-download\`,
2. checks each against the SHA-256 that NVIDIA publishes for it,
3. unpacks them into `cuda\`.

`dictate.py` adds every `cuda\*\bin` folder to the DLL search path at startup,
so nothing is installed system-wide. To move to newer versions, change the
paths and hashes in `setup-cuda.ps1`; NVIDIA lists them in
`redistrib_<version>.json` next to the zips.

**The speech model** (`large-v3-turbo`, about 1.6 GB) is downloaded from
Hugging Face by faster-whisper on the first start and cached in
`%USERPROFILE%\.cache\huggingface`. It stays loaded in GPU memory while the
app runs, about 1.6 GB of VRAM. Expect about a second to transcribe 30–80
seconds of speech on a laptop RTX 2000.

To check the GPU is usable before starting the app:

```powershell
.venv\Scripts\python -c "import dictate, ctranslate2; print(ctranslate2.get_cuda_device_count())"
```

It should print `1` (or more).

## Freeing Win+Q from Windows Search

Windows binds Win+Q to Search. Until that binding is removed, Win+Q opens
Search instead of (or as well as) dictating. Explorer reads a per-user list of
Win+letter shortcuts to leave alone, `DisabledHotkeys`; add Q to it:

```powershell
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' DisabledHotkeys Q
Stop-Process -Name explorer -Force   # Explorer restarts itself and rereads it
```

If `DisabledHotkeys` already lists other letters, append Q instead of
replacing them. Search is still one keypress away: press Win and start
typing.

To give Win+Q back to Search, remove the value and restart Explorer:

```powershell
Remove-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' DisabledHotkeys
Stop-Process -Name explorer -Force
```

## Files

- `dictate.py`: the app.
- `prompt.txt`: the cleanup rules given to Haiku. Edits apply to the next
  dictation; open it from the tray menu.
- `setup-cuda.ps1`: fetches the CUDA libraries into `cuda\` (see GPU
  transcription).
- `dictate.log`: what happened on each dictation, with timings.

## Limits

- Text only goes into WezTerm. Dictating with another app focused transcribes
  but inserts nothing yet.
- Don't type in the target pane while a dictation is processing: the marker is
  removed with backspaces.
- English only (`LANGUAGE` in `dictate.py`).
