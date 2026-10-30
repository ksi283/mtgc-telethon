"""Đóng gói ứng dụng thành file chạy được.

    python build_exe.py            bản release: một file dist/TelegramManager-<phiên bản>.exe (trên Windows)
    python build_exe.py --onedir   thư mục dist/TelegramManager, dễ kiểm tra các file bên trong hơn

PyInstaller không đóng gói chéo hệ điều hành, nên chạy file này trên đúng hệ điều hành cần dùng,
bằng môi trường Python đã cài các thư viện của dự án và pyinstaller.
"""
import os
import sys
import tomllib

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.abspath(__file__))
SEP = os.pathsep  # ";" trên Windows, ":" trên Linux và macOS


def app_version():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)["project"]["version"]


def main():
    onedir = "--onedir" in sys.argv[1:]
    name = "TelegramManager" if onedir else f"TelegramManager-{app_version()}"
    os.chdir(ROOT)
    PyInstaller.__main__.run([
        "launcher.py",
        "--name", name,
        "--onedir" if onedir else "--onefile",
        "--console",
        "--noconfirm",
        "--clean",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build", "pyinstaller"),
        "--specpath", os.path.join(ROOT, "build"),
        "--paths", ROOT,
        # telegram_manager.py được Streamlit chạy từ file, nên đưa vào dạng dữ liệu; các thư viện nó dùng được thêm vào.
        # Đường dẫn tuyệt đối: PyInstaller tính đường dẫn tương đối theo thư mục chứa file .spec
        "--add-data", f"{os.path.join(ROOT, 'telegram_manager.py')}{SEP}.",
        "--add-data", f"{os.path.join(ROOT, 'vendor')}{SEP}vendor",
        "--collect-all", "streamlit",
        "--collect-all", "pandas",
        "--collect-all", "pyarrow",
        "--collect-all", "telethon",
        "--copy-metadata", "streamlit",
    ])


if __name__ == "__main__":
    main()
