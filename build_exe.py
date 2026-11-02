"""Đóng gói ứng dụng thành file chạy được, qua cấu hình TelegramManager.spec.

    python build_exe.py            bản release nhẹ: một file dist/TelegramManager-<phiên bản>.exe, không có cửa sổ console
    python build_exe.py --onedir   thư mục dist/TelegramManager thay vì một file
    python build_exe.py --console  giữ cửa sổ console để xem nhật ký khi gỡ lỗi

PyInstaller không đóng gói chéo hệ điều hành, nên chạy file này trên đúng hệ điều hành cần dùng,
bằng môi trường Python đã cài các thư viện của dự án và pyinstaller.
"""
import os
import sys

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    args = sys.argv[1:]
    os.environ["TELEGRAMMANAGER_ONEDIR"] = "1" if "--onedir" in args else "0"
    os.environ["TELEGRAMMANAGER_CONSOLE"] = "1" if "--console" in args else "0"
    os.chdir(ROOT)
    PyInstaller.__main__.run([
        os.path.join(ROOT, "TelegramManager.spec"),
        "--noconfirm",
        "--clean",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build", "pyinstaller"),
    ])


if __name__ == "__main__":
    main()
