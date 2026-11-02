"""Điểm vào của ứng dụng.

Trên Windows, không tham số: hiện giao diện Tk và biểu tượng khay hệ thống với hai lựa chọn "Mở ứng dụng"
(hiện cửa sổ) và "Đóng ứng dụng" (hỏi xác nhận rồi thoát). Nút X của cửa sổ chỉ ẩn cửa sổ, ứng dụng vẫn chạy.
Nơi khác (ví dụ WSL khi chạy từ mã nguồn): chỉ có giao diện, không có khay hệ thống.

Tham số nội bộ:
    --tg-worker   tiến trình con rời nhóm và quét thành viên (không mở giao diện)
    --selftest    nạp mọi thành phần rồi thoát, mã thoát 0 nghĩa là bản đóng gói đủ thư viện

Khi đã đóng gói, sys.executable là chính file .exe, nên tiến trình con được chạy lại file này với --tg-worker.
"""
import ctypes
import os
import sys
import traceback

FROZEN = getattr(sys, "frozen", False)
IS_WINDOWS = sys.platform == "win32"
APP_TITLE = "TelegramManager"
LOG_NAME = "TelegramManager.log"
ERROR_ALREADY_EXISTS = 183
MB_OK, MB_ICONINFORMATION, MB_ICONERROR = 0x0, 0x40, 0x10
MB_TOPMOST = 0x40000
_instance_lock = None  # giữ mutex suốt đời tiến trình, để lần chạy sau biết ứng dụng đang chạy


def bundle_dir():
    """Tài nguyên đi kèm (icon): thư mục giải nén khi đã đóng gói, không thì thư mục của mã nguồn"""
    return sys._MEIPASS if FROZEN else os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """Dữ liệu người dùng (account.json, file session, nhật ký): cạnh file exe khi đã đóng gói, không thì thư mục hiện tại"""
    return os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.getcwd()


def message_box(text, icon=MB_ICONINFORMATION):
    """Hộp thoại của Windows, dùng khi chưa có cửa sổ giao diện (ví dụ đã có một bản đang chạy)"""
    ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, MB_OK | icon | MB_TOPMOST)


def first_instance():
    """True nếu chưa có ứng dụng nào đang chạy trong phiên Windows này"""
    global _instance_lock
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    _instance_lock = k32.CreateMutexW(None, False, "Local\\TelegramManagerSingleInstance")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def open_log():
    """Ghi stdout và stderr vào nhật ký của lần chạy này. Bản cửa sổ không có console nên cần file này"""
    log = open(os.path.join(data_dir(), LOG_NAME), "w", encoding="utf-8", buffering=1)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    sys.stdout = sys.stderr = log
    return log


def report(text):
    """Báo lỗi cho người dùng: hộp thoại trên Windows, còn lại in ra terminal"""
    if IS_WINDOWS:
        message_box(text, MB_ICONERROR)
    else:
        print(text, file=sys.stderr)


def start_tray(app):
    """Biểu tượng khay hệ thống. run_detached chạy trên luồng chính, dùng chung vòng lặp thông báo với Tk"""
    import pystray
    from PIL import Image

    image = Image.open(os.path.join(bundle_dir(), "assets", "telegram.png"))
    icon = pystray.Icon(APP_TITLE, image, APP_TITLE, menu=pystray.Menu(
        pystray.MenuItem("Mở ứng dụng", lambda icon_, item: app.post(app.show_window), default=True),
        pystray.MenuItem("Đóng ứng dụng", lambda icon_, item: app.post(app.request_quit)),
    ))
    app.tray_active = True
    app.on_exit = icon.stop
    icon.run_detached()
    return icon


def selftest():
    """Nạp mọi thành phần của ứng dụng rồi thoát, không hiện cửa sổ và không đọc tài khoản đã lưu.
    Lỗi thì ghi chi tiết vào TelegramManager-selftest.log cạnh file exe"""
    try:
        import tkinter

        import PIL.Image  # noqa: F401
        import segno  # noqa: F401
        import telethon  # noqa: F401
        if IS_WINDOWS:
            import pystray._win32  # noqa: F401
        import core  # noqa: F401
        import gui
        import tg_workers  # noqa: F401

        root = tkinter.Tk()
        root.withdraw()
        app = gui.App(root, restore=False)
        # Chân cửa sổ phải hiện phiên bản, nên pyproject.toml phải được đóng gói kèm
        _, version = core.project_info()
        if not version or version not in app.footer.cget("text"):
            raise RuntimeError(f"Chân cửa sổ không hiện phiên bản: {app.footer.cget('text')!r}")
        app.close()
        return 0
    except Exception:
        with open(os.path.join(data_dir(), "TelegramManager-selftest.log"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        return 1


def main():
    args = sys.argv[1:]
    if "--tg-worker" in args:
        import tg_workers  # import muộn: tiến trình con không cần giao diện

        tg_workers.main()
        return 0
    if "--selftest" in args:
        return selftest()
    if FROZEN:
        os.chdir(data_dir())
    if IS_WINDOWS and not first_instance():
        message_box("TelegramManager đang chạy. Bấm biểu tượng trong khay hệ thống để mở lại ứng dụng.")
        return 0
    if FROZEN:
        open_log()  # chỉ mở nhật ký sau khi biết đây là bản duy nhất, để không ghi đè nhật ký của bản đang chạy

    import tkinter

    import gui

    try:
        root = tkinter.Tk()
    except tkinter.TclError as e:
        report(f"Không mở được giao diện: {e}")
        return 1
    icon = tkinter.PhotoImage(file=os.path.join(bundle_dir(), "assets", "telegram.png"))
    app = gui.App(root, icon)
    if IS_WINDOWS:
        start_tray(app)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
