# Aziz!Write!

> "Aziz! Light!" — *The Fifth Element*

Push-to-talk dictation for Windows, built for talking to Claude Code in WezTerm.

Hold **Win+Q** (or a shortcut of your choice), speak, release. A few seconds
later the text appears in the WezTerm pane you were in when you pressed the
key, cleaned up: no "um"s, self-corrections applied ("54, no wait, 53"
becomes "53"), spoken paths and code written as characters, lists and steps
formatted as lists.

## Why

Claude Code's built-in dictation types your speech word for word, fillers and
false starts included. Off-the-shelf dictation apps fix that, but on Windows
they transcribe on the CPU (slow on long dictations) and insert text by
pasting into whatever window has focus, so switching windows while it works
sends the text to the wrong place. This app transcribes on the GPU, cleans up
with Claude Haiku, and types into the exact pane you started in.

## How it works

1. A keyboard hook catches the shortcut while held and swallows the keys.
2. The microphone is recorded at 16 kHz while the key is down.
3. [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
   (`large-v3-turbo`) transcribes it on the NVIDIA GPU, in about a second.
4. An agent cleans up the transcript using the chosen prompt (see The
   prompts). By default that is Claude Haiku through Claude Code
   (`claude.exe -p`); one process is kept started and waiting, so this takes
   about a second too.
5. `wezterm cli send-text` types the result into the original pane. It is
   typed, not pasted, so Claude Code shows it in full; line breaks go in as
   newlines that don't submit.

While it works, the target pane shows a marker at the cursor (🎤 recording…,
⏳ transcribing…, ✨ cleaning up…) and the tab's title says the same. The tray
icon changes color: blue ready, red recording, amber processing. Dark red
means it failed to start: hover the icon for the reason, and **Open log** in
its menu has the details.

## Setup

Needs Windows with an NVIDIA GPU, WezTerm, and Claude Code installed on
Windows and logged in (`claude` → `/login`).

**The compiled app:** download `aziz-write-<version>-windows.zip` from the
[releases](https://github.com/LeavOA/aziz-write/releases), unzip it into a
folder of your own (it writes its log and the CUDA libraries next to the
exe), and run `aziz-write.exe`. Windows SmartScreen warns about unsigned
programs: **More info**, then **Run anyway**.

**From source** (needs Python 3.10+):

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw.exe aziz_write.py
```

Either way, a start that cannot find what transcription needs opens a setup
window listing it, with two buttons for each item:

- **CUDA libraries** (cuBLAS 12 and cuDNN 9, see GPU transcription). Before
  asking, the app looks for them where they may already be: on the `PATH`,
  in the CUDA Toolkit, in a cuDNN install, and in PyPI's `nvidia-*` wheels of
  its own Python. **Download** fetches them from NVIDIA into `cuda\` next to
  the app. **Choose folder…** points the app at copies you have elsewhere: a
  folder with the DLLs, or with `bin\` under it, or with NVIDIA's unpacked
  archives under it.
- **The speech model** (`large-v3-turbo`). **Download** fetches it from
  Hugging Face into its cache in your profile. **Choose folder…** points the
  app at a folder holding the model's files (`model.bin`, `config.json`, …)
  or at another Hugging Face cache that has it.

The sizes shown are asked of NVIDIA and Hugging Face when the window opens,
so they follow the model configured (`MODEL` in `aziz_write.py`). The window
comes back at every start where something is still missing. The chosen
folders are kept in the settings file (see Files). The first start also
asks whether to **start at login** and whether to **free Win+Q from Windows
Search** (see The shortcut); both can be changed later from the tray icon's
menu.

Before loading the model the app checks that WezTerm and the agent are
installed and that a CUDA-capable GPU is present, and names what is missing
in the tray icon's tooltip. Only one instance runs at a time, so starting it
again by hand is harmless.

## The shortcut

The default is Win+Q. To change it, pick **Shortcut: Win+Q…** in the tray
icon's menu and press the new combination: a key plus Win, Ctrl, Alt or
Shift. The key starts the recording while the modifiers are held, and
releasing any of them stops it.

If something else already uses the combination, the app says what, as far as
Windows lets it find out:

- **Windows' own Win+letter shortcuts** (Win+E for File Explorer, Win+Q and
  Win+S for Search, and so on). Explorer keeps a per-user list of Win+letter
  shortcuts to leave alone, `DisabledHotkeys` under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced`. The
  app offers to add the letter to it and restart Explorer, which rereads the
  list. Win+L (lock) cannot be turned off this way.
- **A shortcut file's shortcut key**: a `.lnk` in a Start Menu or on a
  desktop with a key set in its properties. The app offers to remove the key
  from the file (not for files shared by all users, which need an
  administrator).
- **A running program's global hotkey.** Windows only says that the
  combination is registered, not by whom. The app offers to use it anyway: it
  sees the keys first, so the other program's shortcut stops working while
  the app runs.

Every system shortcut the app turns off is recorded in its settings file
under `freed`. When you change the shortcut away from one of those, and when
you disable the app, it offers to turn the old one back on.

To free Win+Q by hand instead:

```powershell
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' DisabledHotkeys Q
Stop-Process -Name explorer -Force   # Explorer restarts itself and rereads it
```

If `DisabledHotkeys` already lists other letters, append Q instead of
replacing them. Search is still one keypress away: press Win and start
typing. To give Win+Q back to Search, remove the Q (or the whole value) and
restart Explorer again.

## Start at login

**Start at login** in the tray icon's menu puts a shortcut in the Startup
folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Aziz!Write!.lnk`)
that runs the exe, or, from source, `aziz_write.py` with the venv's
`pythonw.exe`. The same from a shell, from source:

```powershell
.venv\Scripts\python -c "import aziz_write; aziz_write.set_start_at_login(True)"
```

Together with the shortcut freed, dictation is ready from the moment you
sign in.

## The prompts

The cleanup rules the agent gets are a prompt chosen from `prompts.yaml`,
next to the app. **Prompt: Default…** in the tray icon's menu opens a window
that lists the prompts, shows the chosen one's text for editing, and has
buttons to save, use the shown prompt, add a new one (a copy of the shown one
to start from), rename, delete, and restore any built-in prompt that was
deleted. Saved edits apply to the next dictation.

The file is written from the built-in prompts when it is missing:

- **Default**: removes fillers and self-corrections, writes spoken code as
  characters, keeps the wording otherwise, and formats lists and steps.
- **Opinionated**: rewrites rambling into short direct sentences and turns
  requests into imperative instructions, keeping every fact and decision.
- **Programming**: the default plus detailed rules for spoken identifiers,
  paths, flags, operators and tool names.
- **Light touch**: fillers, corrections and punctuation only; no lists, no
  restructuring.

`prompts.yaml` is plain YAML, a list of `name` and `text`, so it can also
be edited by hand (the window's **Open prompts.yaml** button) or shared.

## The agent and the terminal

**Agent** in the tray icon's menu picks what cleans up the transcript:

- **Claude Code** (the default): Claude Haiku through `claude.exe -p`, with
  the chosen prompt as the system prompt. Needs Claude Code installed in
  `%USERPROFILE%\.local\bin` and logged in.
- **Custom command**: any other agent with a command line. Put the command
  in the settings file as `agent_command`, a list of arguments in which
  `{prompt}` is replaced by the chosen prompt's text. The agent gets the
  transcript on standard input, and what it prints is the cleaned text. For
  example, for the Gemini CLI (untested):

  ```json
  "agent_command": ["gemini", "-m", "gemini-2.5-flash", "-p", "{prompt}"]
  ```

  Each dictation runs the command once, so it is as fast as the agent's
  start-up plus its answer.

The terminal is WezTerm; the code is arranged so that another terminal that
can name a pane, type into it and set its title could be added (`TERMINALS`
in `aziz_write.py`, chosen by `terminal` in the settings file), but none is.

## GPU transcription

Transcription runs on the NVIDIA GPU through faster-whisper, which needs two
NVIDIA libraries on top of the normal graphics driver: **cuBLAS** (CUDA 12)
and **cuDNN 9**. The full CUDA Toolkit is not needed.

**The app offers them from NVIDIA, not PyPI.** They are also published on
PyPI as `nvidia-cublas-cu12` and `nvidia-cudnn-cu12`, but those wheels are
about 1.3 GB together and PyPI and its mirrors served them at 0.1–0.6 MB/s
when this was set up: over an hour. NVIDIA's own redistributable CDN served
the same libraries at several MB/s, about three minutes. The setup window's
**Download**:

1. downloads the cuBLAS and cuDNN zips from
   `developer.download.nvidia.com/compute/.../redist/` into `cuda-download\`,
2. checks each against the SHA-256 that NVIDIA publishes for it,
3. unpacks them into `cuda\`.

The app adds the `bin` folders it finds to its own DLL search path, so
nothing is installed system-wide; delete the two folders to remove them. To
move to newer versions, change `CUDA_ARCHIVES` in `aziz_write.py`; NVIDIA
lists the paths and hashes in `redistrib_<version>.json` next to the zips.

**The speech model** (`large-v3-turbo`, about 1.6 GB) comes from Hugging Face
through faster-whisper and is cached in `%USERPROFILE%\.cache\huggingface`.
The app turns off Hugging Face's Xet downloader (`HF_HUB_DISABLE_XET=1`),
which hung at 0 bytes on Windows 11, and fetches the model over plain HTTPS.
It stays loaded in GPU memory while the app runs, about 1.6 GB of VRAM.
Expect about a second to transcribe 30–80 seconds of speech on a laptop RTX
2000.

To check the GPU is usable, from source:

```powershell
.venv\Scripts\python -c "import aziz_write, ctranslate2; print(ctranslate2.get_cuda_device_count())"
```

It should print `1` (or more).

## Compile for Windows

The exe is built with [PyInstaller](https://pyinstaller.org) from
`aziz-write.spec`, which lists the packages whose DLLs and data files it
would otherwise miss. Locally:

```powershell
.venv\Scripts\pip install pyinstaller
.venv\Scripts\pyinstaller aziz-write.spec
```

That makes `dist\aziz-write\`, with `aziz-write.exe` and its libraries. The
app writes `prompts.yaml` next to the exe on first use, and the setup window
puts the CUDA libraries in the same folder.

The GitHub Actions workflow `.github/workflows/build-windows.yml` does the
same on every push to `main` and every pull request: it compiles, starts the
exe to check that nothing is missing from the bundle, and keeps
`aziz-write-<version>-windows.zip` as a workflow artifact. Pushing a tag
`v<version>` also creates a GitHub release with the zip attached. The version
is `VERSION` in `aziz_write.py`, shown in the tray tooltip and menu; the
workflow refuses a tag that does not match it.

```powershell
git tag v0.2.0
git push origin v0.2.0
```

## Updating

**Check for updates…** in the tray icon's menu:

- **The compiled app** compares its version with the newest GitHub release.
  If there is a newer one it downloads the zip and quits, and a script
  replaces the app's files once it has exited and starts it again.
  `prompts.yaml`, the log, `cuda\` and the settings are kept.
- **A source checkout** fetches its upstream branch and shows the new
  commits. If you agree it fast-forwards (`git pull --ff-only`, so local
  commits or edits that conflict stop it with a message), reinstalls
  `requirements.txt` into the venv, and restarts.

## Reporting a problem

**Report a problem…** in the tray icon's menu opens a new GitHub issue with
the version, the Windows version, the shortcut and the last lines of the log
filled in. The log has timings and lengths only, not what you said, unless
`AZIZ_WRITE_LOG_TEXT` was set (see Files); check the text before sending.

## Disabling and removing

**Disable and clean up…** in the tray icon's menu turns off start at login,
offers to turn back on each Windows shortcut the app turned off, removes the
settings file, asks whether to delete the downloaded CUDA libraries and the
speech model, and quits. Then delete the app's folder.

By hand: quit from the tray menu, delete
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Aziz!Write!.lnk`,
`%APPDATA%\Aziz!Write!`, the app's folder, and, if you want the model gone,
`%USERPROFILE%\.cache\huggingface\hub\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo`.
Give Win+Q back to Search as described under The shortcut.

## Files

- `aziz_write.py`: the app. `VERSION` is near the top, the built-in prompts
  in `BUILTIN_PROMPTS`.
- `prompts.yaml`: the cleanup prompts, written next to the app on first use
  (see The prompts). The one in use is `prompt` in the settings file.
- `aziz-write.spec`: the PyInstaller build (see Compile for Windows).
- `.github/workflows/build-windows.yml`: compiles on GitHub.
- `%APPDATA%\Aziz!Write!\settings.json`: the shortcut (`hotkey`), the agent
  (`agent`, `agent_command`), the terminal (`terminal`), folders chosen in the
  setup window (`cuda_dir`, `model_dir`), whether start-at-login was offered,
  and the system shortcuts turned off for the app (`freed`).
- `aziz-write.log`: timings and lengths for each dictation. It does not
  record what you said. To debug the cleanup prompt you can set the
  environment variable `AZIZ_WRITE_LOG_TEXT=1` before starting the app, which
  adds the raw and cleaned text of every dictation to this plain-text file.
  Turn it off and delete the log when done.

## Limits

- Text only goes into WezTerm. Dictating with another app focused transcribes
  but inserts nothing yet.
- Don't type in the target pane while a dictation is processing: the marker is
  removed with backspaces.
- The microphone is opened only while the shortcut is held, and some take a
  moment to start: about half a second for a laptop's built-in mic array.
  Wait for the 🎤 marker before speaking, or the first word is lost.
- English only (`LANGUAGE` in `aziz_write.py`).
