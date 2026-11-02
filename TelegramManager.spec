# -*- mode: python ; coding: utf-8 -*-
"""Cấu hình PyInstaller, chạy qua build_exe.py.

Bản này dùng Tkinter nên không đóng gói Streamlit, pandas hay pyarrow. Các phần không dùng (test, IDLE, turtle,
công cụ phát triển) cũng được loại khỏi bản đóng gói để file nhẹ hơn. Sau khi build, kiểm tra bằng:
    TelegramManager-<phiên bản>.exe --selftest
Mã thoát 0 nghĩa là các thành phần đã được đóng gói đủ.
"""
import os
import tomllib

from PyInstaller.utils.hooks import collect_all

ROOT = SPECPATH
CONSOLE = os.environ.get("TELEGRAMMANAGER_CONSOLE") == "1"
ONEDIR = os.environ.get("TELEGRAMMANAGER_ONEDIR") == "1"
with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]
ICON = os.path.join(ROOT, "assets", "telegram.ico") if os.name == "nt" else None

EXCLUDES = [
    "streamlit", "pandas", "numpy", "pyarrow", "matplotlib", "PyQt5", "PySide6", "IPython", "pytest",
    "tkinter.test", "idlelib", "turtle", "unittest", "doctest", "pydoc", "lib2to3",
]

telethon_datas, telethon_binaries, telethon_hidden = collect_all("telethon")
hiddenimports = telethon_hidden + (["pystray._win32"] if os.name == "nt" else [])

a = Analysis(
    [os.path.join(ROOT, "launcher.py")],
    pathex=[ROOT],
    binaries=telethon_binaries,
    datas=telethon_datas + [(os.path.join(ROOT, "assets"), "assets")],
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)
if ONEDIR:
    exe = EXE(pyz, a.scripts, exclude_binaries=True, name="TelegramManager", console=CONSOLE,
              icon=ICON, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="TelegramManager", upx=False)
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, name=f"TelegramManager-{VERSION}", console=CONSOLE,
              icon=ICON, upx=False, runtime_tmpdir=None)
