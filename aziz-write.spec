# PyInstaller build: `pyinstaller aziz-write.spec` makes dist\aziz-write\aziz-write.exe.
#
# The exe looks next to itself for prompts.yaml, cuda\ and the log. It writes
# prompts.yaml from its built-in prompts on first use and offers to fetch the
# CUDA libraries and the speech model at first start, so none of those are
# bundled. The workflow (.github/workflows/build-windows.yml) zips the folder.
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], [
    "pystray._win32", "pynput.keyboard._win32", "pynput.mouse._win32",
]
# Packages whose DLLs and data files PyInstaller does not find on its own:
# ctranslate2's own DLLs, faster-whisper's VAD model, tokenizers, onnxruntime
# (runs the VAD), av (FFmpeg, imported by faster-whisper).
for package in ("ctranslate2", "faster_whisper", "tokenizers", "huggingface_hub", "onnxruntime", "av"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["aziz_write.py"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="aziz-write",
    console=False,  # a tray app: no console window, like pythonw
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="aziz-write")
