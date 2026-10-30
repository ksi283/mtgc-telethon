"""Điểm vào của bản đóng gói: chạy file này, hoặc TelegramManager.exe sau khi đóng gói, để mở ứng dụng.

Khi đã đóng gói, sys.executable là chính file .exe. Tiến trình con để rời nhóm và quét thành viên
được chạy lại file này với đối số --tg-worker, nên nhánh đó được kiểm tra trước khi mở giao diện.
"""
import os
import sys


def main():
    if "--tg-worker" in sys.argv:
        import tg_workers  # import muộn: tiến trình con không cần Streamlit

        tg_workers.main()
        return

    if getattr(sys, "frozen", False):
        # account.json và file session nằm cạnh file .exe, không nằm trong thư mục tạm của PyInstaller
        os.chdir(os.path.dirname(os.path.abspath(sys.executable)))
        app_dir = sys._MEIPASS
    else:
        app_dir = os.path.dirname(os.path.abspath(__file__))

    from streamlit.web import cli as stcli

    sys.argv = [
        "streamlit", "run", os.path.join(app_dir, "telegram_manager.py"),
        "--global.developmentMode=false",  # bản đóng gói không phải cài đặt phát triển, và chế độ này không cho đặt cổng
        "--server.address=localhost",
        "--server.fileWatcherType=none",
        "--browser.gatherUsageStats=false",
    ]
    if "STREAMLIT_SERVER_PORT" not in os.environ:
        sys.argv.append(f"--server.port={pick_port()}")
    sys.exit(stcli.main())


def port_free(port):
    """True nếu cổng trống trên mọi địa chỉ localhost (IPv4 và IPv6), giống các địa chỉ Streamlit sẽ lắng nghe"""
    import socket

    for family, _, _, _, addr in socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM):
        with socket.socket(family, socket.SOCK_STREAM) as s:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: không cho app khác dùng chung cổng với mình
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                s.bind(addr)
            except OSError:
                return False
    return True


def pick_port(preferred=8501):
    """Cổng 8501 nếu còn trống; nếu không (ví dụ đang chạy bản dev) thì để hệ điều hành chọn một cổng trống"""
    import socket

    if port_free(preferred):
        return preferred
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    main()
