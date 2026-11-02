"""Giao diện Tkinter của ứng dụng. Chỉ hiển thị và gọi core.py.

Mọi thao tác chờ mạng chạy ở luồng nền. Kết quả và tiến trình đưa về luồng giao diện qua hàng đợi (App.post),
vì Tkinter chỉ an toàn khi gọi từ đúng luồng của nó. Giao diện không dùng emoji vì Tk trên Windows không hiển
thị đủ ký tự ngoài mặt phẳng BMP.
"""
import queue
import threading
import tkinter as tk
import traceback
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import segno
from telethon.errors import SessionPasswordNeededError

import core

TITLE = "Quản lý nhóm & kênh Telegram"
PICK_MARK = "✔"
KIND_LABELS = {"Group": "Nhóm", "Channel": "Kênh"}
MESSAGE_COLORS = {"info": "#1f4e79", "error": "#b00020", "success": "#1e7e34", "warn": "#8a5a00"}
STAGE_FRAMES = {
    "restoring": "restore_frame",
    "login": "login_frame",
    "code": "code_frame",
    "2fa": "twofa_frame",
    "qr": "qr_frame",
    "dashboard": "dash_frame",
}


def draw_qr(canvas, text, cell=7, border=3):
    """Vẽ mã QR lên canvas bằng các ô vuông, không cần ảnh trung gian"""
    matrix = segno.make(text, error="m").matrix
    size = (len(matrix) + 2 * border) * cell
    canvas.delete("all")
    canvas.configure(width=size, height=size)
    canvas.create_rectangle(0, 0, size, size, fill="white", outline="")
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                x0, y0 = (x + border) * cell, (y + border) * cell
                canvas.create_rectangle(x0, y0, x0 + cell, y0 + cell, fill="black", outline="")


def fmt_number(n):
    """Số có dấu chấm ngăn cách hàng nghìn theo cách viết tiếng Việt, ví dụ 54.000"""
    return "" if n is None else f"{n:,}".replace(",", ".")


def required_label(parent, text):
    """Nhãn của mục bắt buộc: chữ thường, theo sau là dấu * màu đỏ"""
    row = ttk.Frame(parent)
    ttk.Label(row, text=text).pack(side="left")
    ttk.Label(row, text=" *", foreground=MESSAGE_COLORS["error"]).pack(side="left")
    return row


class App:
    """Cửa sổ chính. Mỗi màn hình (khôi phục, đăng nhập, mã, 2 bước, QR, quản lý) là một khung, chỉ một khung hiện cùng lúc"""

    def __init__(self, root, icon=None, restore=True):
        self.root = root
        self.manager = core.TelegramManager()
        self.inbox = queue.Queue()  # việc từ luồng nền hoặc khay hệ thống, chạy trên luồng giao diện
        self.on_exit = None  # launcher gán hàm dừng biểu tượng khay khi đóng ứng dụng
        self.tray_active = False  # có biểu tượng khay: đóng cửa sổ chỉ ẩn đi, ứng dụng vẫn chạy
        self.stage = None
        self.busy = False
        self.phone = ""
        self.api_id = None
        self.api_hash = ""
        self.groups = None  # None = chưa tải
        self.selected = set()  # id nhóm/kênh đã chọn ở tab nhóm & kênh
        self.posted = set()  # nhóm đã nhận tin nhắn khi rời nhưng chưa rời được, lần sau không gửi lại
        self.last_result = None
        self.scan = None  # kết quả quét theo tài khoản
        self.scan_rows = []  # các dòng đang hiển thị trong bảng kết quả quét
        self.scan_picks = set()
        self.qr = None  # {"login", "future", "url"} khi đang chờ quét
        self.qr_temp = None  # file session tạm của phiên QR, chuyển sang file chính sau khi xác nhận
        self.leave_halt = None  # Event của lần rời đang chạy, None nếu không chạy
        self.query = ""  # từ khóa đã áp dụng ở tab nhóm & kênh (đổi khi nhấn Enter)
        self._pump_id = None
        self._qr_poll_id = None  # lịch kiểm tra QR mỗi giây, hủy khi đóng cửa sổ
        self._build_fonts()
        self._build(icon)
        self._pump_id = self.root.after(100, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_window)
        if restore:
            self.start_restore()

    # --- Luồng và hàng đợi ---

    def post(self, fn):
        """Đưa một hàm vào hàng đợi để chạy trên luồng giao diện. Dùng được từ luồng bất kỳ"""
        self.inbox.put(fn)

    def _pump(self):
        while True:
            try:
                fn = self.inbox.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                traceback.print_exc()
        self._pump_id = self.root.after(100, self._pump)

    def background(self, work, done, fail=None):
        """Chạy work() ở luồng nền. Khi xong gọi done(kết quả), lỗi thì gọi fail(lỗi), cả hai trên luồng giao diện"""
        def run():
            try:
                result = work()
            except Exception as e:
                self.post(lambda err=e: (fail or self.show_error)(err))
            else:
                self.post(lambda res=result: done(res))

        threading.Thread(target=run, daemon=True).start()

    # --- Giao diện chung ---

    def _build_fonts(self):
        base = tkfont.nametofont("TkDefaultFont")
        self.heading_font = base.copy()
        self.heading_font.configure(size=base.cget("size") + 4, weight="bold")
        self.bold_font = base.copy()
        self.bold_font.configure(weight="bold")

    def _build(self, icon):
        root = self.root
        root.title(TITLE)
        root.geometry("1120x760")
        root.minsize(960, 640)
        if icon is not None:
            self.icon_image = icon
            root.iconphoto(True, icon)
        style = ttk.Style(root)
        style.configure("Heading.TLabel", font=self.heading_font)
        style.configure("Accent.TButton", font=self.bold_font)
        style.configure("Muted.TLabel", foreground="#555555")

        header = ttk.Frame(root, padding=(16, 12, 16, 0))
        header.pack(fill="x")
        ttk.Label(header, text=TITLE, style="Heading.TLabel").pack(side="left")
        ttk.Label(header, text="Xem và rời nhiều nhóm, kênh Telegram cùng lúc.", style="Muted.TLabel").pack(side="left", padx=(12, 0))

        self.msg_label = ttk.Label(root, text="", wraplength=940, justify="left")
        self.msg_label.pack(fill="x", padx=16, pady=(8, 0))

        self.status_var = tk.StringVar()
        ttk.Label(root, textvariable=self.status_var, style="Muted.TLabel", padding=(16, 4)).pack(side="bottom", fill="x")
        self.content = ttk.Frame(root, padding=(16, 8))
        self.content.pack(fill="both", expand=True)

        self.restore_frame = ttk.Frame(self.content, padding=16)
        ttk.Label(self.restore_frame, text="Đang khôi phục phiên đăng nhập...").pack(anchor="w")
        self.login_frame = self._build_login(self.content)
        self.code_frame = self._build_code(self.content)
        self.twofa_frame = self._build_twofa(self.content)
        self.qr_frame = self._build_qr(self.content)
        self.dash_frame = self._build_dashboard(self.content)

    def show_stage(self, stage):
        self.stage = stage
        for name in set(STAGE_FRAMES.values()):
            getattr(self, name).pack_forget()
        getattr(self, STAGE_FRAMES[stage]).pack(fill="both", expand=True)
        # Màn đăng nhập căn giữa, nên thông báo lỗi trên đầu màn này cũng căn giữa; các màn khác vẫn căn trái
        self.msg_label.configure(justify="center" if stage == "login" else "left")

    def show_message(self, text, kind="info"):
        self.msg_label.configure(text=text, foreground=MESSAGE_COLORS.get(kind, ""))

    def set_status(self, text):
        self.status_var.set(text or "")

    def show_error(self, e):
        self.busy = False
        self.set_status("")
        self.show_message(core.describe_error(e), "error")

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close_window(self):
        if self.tray_active:
            self.root.withdraw()
        else:
            self.request_quit()

    def request_quit(self):
        """Hỏi xác nhận rồi đóng ứng dụng. Nếu đang rời nhóm thì thao tác đó bị dừng lại"""
        self.show_window()
        question = "Bạn có chắc muốn đóng TelegramManager?"
        if self.leave_halt is not None:
            question += "\n\nĐang rời nhóm, thao tác đó sẽ bị dừng lại. Các mục đã rời vẫn đã rời trên Telegram."
        if not messagebox.askyesno("Đóng ứng dụng", question, parent=self.root):
            return
        if self.leave_halt is not None:
            self.leave_halt.set()
        core.kill_workers()
        if self.on_exit is not None:
            self.on_exit()
        self.close()

    def close(self):
        """Dừng các lịch đang chờ (nhận việc từ luồng nền, kiểm tra QR) rồi hủy cửa sổ. Dùng khi thoát và trong kiểm thử"""
        if self._pump_id is not None:
            self.root.after_cancel(self._pump_id)
            self._pump_id = None
        if self._qr_poll_id is not None:
            self.root.after_cancel(self._qr_poll_id)
            self._qr_poll_id = None
        self.root.destroy()

    def show_guide(self):
        messagebox.showinfo(
            "Lấy API ID và API Hash",
            "1. Mở https://my.telegram.org và đăng nhập bằng số điện thoại của bạn.\n"
            "2. Chọn API development tools và tạo một ứng dụng (tên và mô tả tùy ý).\n"
            "3. Sao chép App api_id và App api_hash vào form đăng nhập của ứng dụng.",
            parent=self.root,
        )

    # --- Màn hình đăng nhập ---

    def _build_login(self, parent):
        frame = ttk.Frame(parent, padding=16)
        box = ttk.Frame(frame)
        box.pack(expand=True)  # khối nội dung nằm giữa khung, cả chiều ngang lẫn chiều dọc
        ttk.Label(box, text="Kết nối tài khoản Telegram", style="Heading.TLabel").pack()
        ttk.Label(box, text="Cần API ID và API Hash miễn phí từ my.telegram.org.", style="Muted.TLabel").pack(pady=(2, 8))
        ttk.Button(box, text="Chưa có API ID và API Hash? Xem hướng dẫn", command=self.show_guide).pack()

        form = ttk.Frame(box)
        form.pack(pady=(16, 0))
        self.api_id_var = tk.StringVar()
        self.api_hash_var = tk.StringVar()
        self.phone_var = tk.StringVar()
        # API ID và API Hash luôn bắt buộc. Số điện thoại chỉ cần khi nhận mã, QR không cần nên không đánh dấu
        required_label(form, "API ID").grid(row=0, column=0, sticky="w", pady=4)
        self.api_id_entry = ttk.Entry(form, textvariable=self.api_id_var, width=36)
        self.api_id_entry.grid(row=0, column=1, sticky="w", padx=(12, 0), pady=4)
        ttk.Label(form, text="Ví dụ: 1234567", style="Muted.TLabel").grid(row=0, column=2, sticky="w", padx=8)
        required_label(form, "API Hash").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.api_hash_var, show="•", width=36).grid(row=1, column=1, sticky="w", padx=(12, 0), pady=4)
        ttk.Label(form, text="Số điện thoại").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.phone_var, width=36).grid(row=2, column=1, sticky="w", padx=(12, 0), pady=4)
        ttk.Label(form, text="Ví dụ: +84912345678", style="Muted.TLabel").grid(row=2, column=2, sticky="w", padx=8)
        ttk.Label(
            box,
            text="Số điện thoại chỉ cần khi nhận mã xác thực. Nhập đủ mã quốc gia, bắt đầu bằng dấu +. "
                 "Đăng nhập bằng QR không cần số này.",
            style="Muted.TLabel", wraplength=720, justify="center",
        ).pack(pady=(4, 0))

        buttons = ttk.Frame(box)
        buttons.pack(pady=(16, 0))
        ttk.Button(buttons, text="Tiếp tục", style="Accent.TButton", width=18, command=self.on_continue).pack(side="left")
        ttk.Button(buttons, text="Đăng nhập bằng QR", width=22, command=self.on_qr_start).pack(side="left", padx=12)
        return frame

    def fill_login_form(self, info):
        """Điền sẵn số điện thoại và API ID/Hash đã lưu, để người dùng chỉ cần bấm Tiếp tục"""
        if info.get("phone"):
            self.phone_var.set(info["phone"])
        if info.get("api_id") is not None:
            self.api_id_var.set(str(info["api_id"]))
            self.api_hash_var.set(info["api_hash"])

    def on_continue(self):
        if self.busy:
            return
        phone = self.phone_var.get().strip().replace(" ", "")
        api_id, api_hash = self.api_id_var.get(), self.api_hash_var.get()
        errors = core.validate_login(api_id, api_hash, phone)
        if errors:
            self.show_message("\n".join(errors), "error")
            return
        self.show_message("")
        self.busy = True
        self.set_status("Đang kết nối và gửi mã...")

        def done(authorized):
            self.busy = False
            self.set_status("")
            self.api_id = int(api_id)
            self.api_hash = api_hash.strip()
            self.phone = phone
            if authorized:
                self.complete_login()
            else:
                self.show_code()

        self.background(lambda: core.start_login(self.manager, api_id, api_hash, phone), done)

    def on_qr_start(self):
        if self.busy:
            return
        api_id, api_hash = self.api_id_var.get(), self.api_hash_var.get()
        errors = core.validate_login(api_id, api_hash)
        if errors:
            self.show_message("\n".join(errors), "error")
            return
        self.show_message("")
        self.busy = True
        self.set_status("Đang tạo mã QR...")

        def done(login):
            self.busy = False
            self.set_status("")
            self.api_id = int(api_id)
            self.api_hash = api_hash.strip()
            self.qr_temp = core.QR_SESSION_FILE
            self.qr = {"login": login, "future": core.wait_qr(login), "url": None}
            self.show_stage("qr")
            self.refresh_qr()
            self._qr_poll_id = self.root.after(1000, self.poll_qr)

        self.background(lambda: core.start_qr(self.manager, api_id, api_hash), done)

    def poll_qr(self):
        """Kiểm tra mỗi giây: đổi mã khi mã cũ hết hạn, và xử lý khi đã quét hoặc hết thời gian"""
        qr = self.qr
        if qr is None:
            return
        future = qr["future"]
        if not future.done():
            self.refresh_qr()
            self._qr_poll_id = self.root.after(1000, self.poll_qr)
            return
        self.qr = None
        try:
            future.result()
        except SessionPasswordNeededError:
            self.show_twofa()
            return
        except Exception as e:
            text = f"Đăng nhập QR không thành công: {core.describe_error(e)}"
            self.cancel_qr_async(done=lambda _, msg=text: self._login_failed(msg))
            return
        self.complete_login(promote=True)

    def _login_failed(self, text):
        self.busy = False
        self.set_status("")
        self.show_stage("login")
        self.show_message(text, "error")

    def refresh_qr(self):
        qr = self.qr
        if qr is None:
            return
        url = qr["login"].url
        if url != qr["url"]:
            qr["url"] = url
            draw_qr(self.qr_canvas, url)

    def cancel_qr_async(self, done=None):
        """Hủy đăng nhập QR ở nền: ngắt kết nối và xóa file session tạm"""
        qr = self.qr
        self.qr = None
        temp = self.qr_temp
        self.qr_temp = None
        future = qr["future"] if qr else None
        self.background(lambda: core.cancel_qr(self.manager, future, temp), done or (lambda _: None))

    def on_qr_cancel(self):
        self.busy = True
        self.set_status("Đang hủy đăng nhập QR...")

        def done(_):
            self.busy = False
            self.set_status("")
            self.reset_to_login()

        self.cancel_qr_async(done=done)

    def _build_qr(self, parent):
        frame = ttk.Frame(parent, padding=16)
        ttk.Label(frame, text="Quét mã QR bằng Telegram", style="Heading.TLabel").pack(anchor="w")
        self.qr_canvas = tk.Canvas(frame, width=240, height=240, background="white", highlightthickness=1,
                                   highlightbackground="#cccccc")
        self.qr_canvas.pack(anchor="w", pady=(12, 8))
        ttk.Label(
            frame,
            text="1. Mở Telegram trên điện thoại. 2. Vào Cài đặt → Thiết bị → Liên kết thiết bị máy tính. "
                 "3. Quét mã này. Mã tự làm mới khi hết hạn.",
            style="Muted.TLabel", wraplength=720, justify="left",
        ).pack(anchor="w")
        ttk.Button(frame, text="Hủy đăng nhập QR", command=self.on_qr_cancel).pack(anchor="w", pady=(12, 0))
        return frame

    # --- Mã xác thực và xác minh 2 bước ---

    def _build_code(self, parent):
        frame = ttk.Frame(parent, padding=16)
        ttk.Label(frame, text="Nhập mã xác thực", style="Heading.TLabel").pack(anchor="w")
        self.code_info_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.code_info_var, wraplength=720, justify="left").pack(anchor="w", pady=(4, 12))
        row = ttk.Frame(frame)
        row.pack(anchor="w")
        ttk.Label(row, text="Mã xác thực").pack(side="left")
        self.code_var = tk.StringVar()
        self.code_var.trace_add("write", lambda *_: self._limit(self.code_var, 6))
        self.code_entry = ttk.Entry(row, textvariable=self.code_var, width=14)
        self.code_entry.pack(side="left", padx=8)
        self.code_entry.bind("<Return>", lambda e: self.on_verify_code())
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w", pady=(12, 0))
        ttk.Button(buttons, text="Xác nhận", style="Accent.TButton", width=16, command=self.on_verify_code).pack(side="left")
        ttk.Button(buttons, text="Gửi lại mã", width=16, command=self.on_resend_code).pack(side="left", padx=8)
        ttk.Button(buttons, text="Đổi số điện thoại", width=18, command=self.reset_to_login).pack(side="left")
        return frame

    def _build_twofa(self, parent):
        frame = ttk.Frame(parent, padding=16)
        ttk.Label(frame, text="Xác minh 2 bước", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="Tài khoản này đã bật xác minh 2 bước. Nhập mật khẩu bạn tự đặt trong Telegram, không phải mã xác thực.",
            wraplength=720, justify="left",
        ).pack(anchor="w", pady=(4, 12))
        row = ttk.Frame(frame)
        row.pack(anchor="w")
        ttk.Label(row, text="Mật khẩu 2FA").pack(side="left")
        self.pw_var = tk.StringVar()
        self.pw_entry = ttk.Entry(row, textvariable=self.pw_var, show="•", width=30)
        self.pw_entry.pack(side="left", padx=8)
        self.pw_entry.bind("<Return>", lambda e: self.on_submit_2fa())
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w", pady=(12, 0))
        ttk.Button(buttons, text="Đăng nhập", style="Accent.TButton", width=16, command=self.on_submit_2fa).pack(side="left")
        ttk.Button(buttons, text="Quay lại từ đầu", width=18, command=self.reset_to_login).pack(side="left", padx=8)
        return frame

    @staticmethod
    def _limit(var, n):
        value = var.get()
        if len(value) > n:
            var.set(value[:n])

    def show_code(self):
        self.code_var.set("")
        self.code_info_var.set(
            f"Telegram đã gửi mã tới {core.mask_phone(self.phone)}. "
            "Mã thường hiện trong ứng dụng Telegram trên điện thoại hoặc qua SMS."
        )
        self.show_stage("code")
        self.code_entry.focus_set()

    def show_twofa(self):
        self.pw_var.set("")
        self.show_stage("2fa")
        self.pw_entry.focus_set()

    def on_verify_code(self):
        if self.busy:
            return
        code = self.code_var.get().strip()
        if not code.isdigit():
            self.show_message("Mã xác thực chỉ gồm chữ số.", "error")
            return
        self.busy = True
        self.set_status("Đang xác thực...")

        def done(needs_2fa):
            self.busy = False
            self.set_status("")
            if needs_2fa:
                self.show_twofa()
            else:
                self.complete_login()

        self.background(lambda: core.run_async(self.manager.verify_code(self.phone, code)), done)

    def on_resend_code(self):
        if self.busy:
            return
        self.busy = True
        self.set_status("Đang gửi lại mã...")

        def done(_):
            self.busy = False
            self.set_status("")
            self.show_message("Đã gửi lại mã. Kiểm tra Telegram hoặc tin nhắn SMS.", "success")

        self.background(lambda: core.run_async(self.manager.send_code(self.phone)), done)

    def on_submit_2fa(self):
        if self.busy:
            return
        password = self.pw_var.get()
        if not password:
            self.show_message("Vui lòng nhập mật khẩu 2FA.", "error")
            return
        self.busy = True
        self.set_status("Đang đăng nhập...")
        self.background(lambda: core.run_async(self.manager.verify_2fa(password)),
                        lambda _: self.complete_login(promote=self.qr_temp is not None))

    def complete_login(self, promote=False):
        """Lưu thông tin đăng nhập rồi vào màn quản lý. Với đăng nhập QR, chuyển phiên sang file theo số của tài khoản vừa quét"""
        self.busy = True
        self.set_status("Đang lưu thông tin đăng nhập...")
        temp = self.qr_temp if promote else None

        def work():
            if temp:
                phone = core.promote_qr_session(self.manager, self.api_id, self.api_hash, temp)
            else:
                phone = self.phone
            core.save_login(self.manager, phone, self.api_id, self.api_hash)
            return phone

        def done(phone):
            self.busy = False
            self.set_status("")
            self.phone = phone
            self.qr_temp = None
            self.enter_dashboard()

        def fail(e):
            self.qr_temp = None
            self._login_failed(core.describe_error(e))

        self.background(work, done, fail)

    def enter_dashboard(self):
        self.show_message("")
        self.account_var.set(f"Đã kết nối · {core.mask_phone(self.phone)}")
        self.show_stage("dashboard")
        self.refresh_dashboard()
        self.load_groups()

    def reset_to_login(self):
        """Ngắt kết nối, hủy QR nếu đang chờ, xóa dữ liệu của phiên cũ trên màn hình và quay về form đăng nhập"""
        qr_active = self.qr is not None or self.qr_temp is not None
        self.phone = ""
        self.groups = None
        self.selected = set()
        self.posted = set()
        self.last_result = None
        self.scan = None
        self.scan_picks = set()
        self.show_message("")
        self.show_stage("login")
        if qr_active:
            self.cancel_qr_async()
        else:
            self.background(lambda: core.run_async(self.manager.disconnect()), lambda _: None)

    # --- Khôi phục phiên khi mở ứng dụng ---

    def start_restore(self):
        self.busy = True
        self.show_stage("restoring")
        self.background(lambda: core.restore_session(self.manager), self.on_restored, self._restore_failed)

    def on_restored(self, info):
        self.busy = False
        if info["authorized"]:
            self.api_id = info["api_id"]
            self.api_hash = info["api_hash"]
            self.phone = info["phone"]
            self.enter_dashboard()
            return
        self.fill_login_form(info)
        self.show_stage("login")
        if info["notice"]:
            self.show_message(info["notice"], "info")

    def _restore_failed(self, e):
        self.busy = False
        self.show_stage("login")
        self.show_message(f"Không khôi phục được phiên đăng nhập: {core.describe_error(e)}", "error")

    # --- Màn hình quản lý ---

    def _build_dashboard(self, parent):
        frame = ttk.Frame(parent)
        top = ttk.Frame(frame)
        top.pack(fill="x")
        self.account_var = tk.StringVar()
        ttk.Label(top, textvariable=self.account_var, style="Heading.TLabel").pack(side="left")
        ttk.Button(top, text="Đăng xuất", command=self.on_sign_out).pack(side="right")
        ttk.Button(top, text="Tải lại danh sách", command=self.on_reload).pack(side="right", padx=8)
        ttk.Label(
            frame,
            text="Lần sau mở app sẽ tự đăng nhập, không cần mã xác thực. Đăng xuất sẽ thu hồi phiên và xóa thông tin đã lưu trên máy.",
            style="Muted.TLabel", wraplength=900, justify="left",
        ).pack(anchor="w", pady=(2, 0))
        self.stats_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.stats_var).pack(anchor="w", pady=(6, 0))

        info = ttk.Frame(frame)
        info.pack(fill="x")
        self.dash_message_var = tk.StringVar()
        self.dash_message = ttk.Label(info, textvariable=self.dash_message_var, style="Muted.TLabel", wraplength=900, justify="left")

        self.progress_frame = ttk.Frame(info, padding=(0, 8))
        self.progress_label_var = tk.StringVar()
        ttk.Label(self.progress_frame, textvariable=self.progress_label_var).pack(anchor="w")
        self.progress_bar = ttk.Progressbar(self.progress_frame, maximum=100, length=600)
        self.progress_bar.pack(anchor="w", pady=(4, 0))

        self.result_frame = ttk.LabelFrame(info, text="Kết quả lần rời gần nhất", padding=8)
        self.result_summary = ttk.Label(self.result_frame, wraplength=900, justify="left")
        self.result_summary.pack(anchor="w")
        self.result_text = tk.Text(self.result_frame, height=7, wrap="word", relief="flat", background=self.root.cget("background"))
        self.result_text.pack(fill="x", pady=(4, 4))
        ttk.Button(self.result_frame, text="Đóng thông báo", command=self.on_close_result).pack(anchor="e")

        footer = ttk.Label(
            frame,
            text="Quyền riêng tư: thông tin cơ bản nằm trong account.json. Phiên đăng nhập và API ID/Hash nằm trong file "
                 "session_<số điện thoại>.session (SQLite) ngay trên máy này. Ứng dụng chỉ kết nối tới máy chủ Telegram.",
            style="Muted.TLabel", wraplength=900, justify="left",
        )
        footer.pack(side="bottom", anchor="w", pady=(6, 0))

        self.tabs = ttk.Notebook(frame)
        tab_list = ttk.Frame(self.tabs, padding=8)
        tab_people = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab_list, text="Nhóm & kênh")
        self.tabs.add(tab_people, text="Tìm theo tài khoản")
        self._build_list_tab(tab_list)
        self._build_people_tab(tab_people)
        self.tabs.pack(fill="both", expand=True, pady=(8, 0))
        return frame

    def _make_tree(self, parent, cols, heads, widths, height=12):
        tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="none", height=height)
        for col, head, width in zip(cols, heads, widths):
            tree.heading(col, text=head)
            tree.column(col, width=width, minwidth=40, stretch=(col in ("title", "label")),
                        anchor="center" if col in ("pick", "members") else "w")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    @staticmethod
    def on_tree_click(event, tree, toggle):
        """Bấm vào cột Chọn của một dòng thì đổi trạng thái chọn của dòng đó"""
        if tree.identify_region(event.x, event.y) != "cell" or tree.identify_column(event.x) != "#1":
            return
        iid = tree.identify_row(event.y)
        if iid:
            toggle(int(iid))

    def _build_list_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Label(top, text="Tìm kiếm").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(top, textvariable=self.search_var, width=48)
        self.search_entry.pack(side="left", padx=8)
        self.search_entry.bind("<Return>", lambda e: self.on_search())
        ttk.Label(top, text="Tên, @username, link t.me/... hoặc ID. Nhấn Enter để tìm.", style="Muted.TLabel").pack(side="left")

        filters = ttk.Frame(parent)
        filters.pack(fill="x", pady=(8, 0))
        self.kind_var = tk.StringVar(value="Tất cả")
        for kind in core.FILTER_TYPES:
            ttk.Radiobutton(filters, text=kind, value=kind, variable=self.kind_var,
                            command=self.on_filter).pack(side="left", padx=(0, 12))

        info = ttk.Frame(parent)
        info.pack(fill="x", pady=(8, 4))
        self.visible_var = tk.StringVar()
        ttk.Label(info, textvariable=self.visible_var).pack(side="left")
        self.select_all_btn = ttk.Button(info, command=self.on_select_all)
        self.select_all_btn.pack(side="left", padx=(12, 0))
        self.clear_btn = ttk.Button(info, text="Bỏ chọn tất cả", command=self.on_clear_all)
        self.clear_btn.pack(side="left", padx=8)

        self.list_empty_var = tk.StringVar()
        self.list_empty = ttk.Label(parent, textvariable=self.list_empty_var, style="Muted.TLabel", wraplength=900, justify="left")

        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=True)
        self.tree = self._make_tree(
            tree_frame,
            ("pick", "title", "kind", "members", "username", "date"),
            ("Chọn", "Tên nhóm / kênh", "Loại", "Thành viên", "Username", "Tin nhắn gần nhất"),
            (60, 330, 80, 90, 170, 140),
        )
        self.tree.bind("<Button-1>", lambda e: self.on_tree_click(e, self.tree, self.toggle_group_pick))

        chosen = ttk.Frame(parent, padding=(0, 8, 0, 0))
        chosen.pack(fill="x")
        self.chosen_var = tk.StringVar()
        ttk.Label(chosen, textvariable=self.chosen_var).pack(anchor="w")
        actions = ttk.Frame(chosen)
        actions.pack(anchor="w", pady=(6, 0))
        self.leave_btn = ttk.Button(actions, text="Rời, giữ lịch sử chat", width=24, command=lambda: self.on_leave_selected(False))
        self.leave_btn.pack(side="left")
        self.delete_btn = ttk.Button(actions, text="Rời và xóa lịch sử chat", style="Accent.TButton", width=26,
                                     command=lambda: self.on_leave_selected(True))
        self.delete_btn.pack(side="left", padx=8)
        ttk.Label(parent, text="Rời nhóm hoặc kênh có thể khiến bạn phải được mời lại để tham gia.",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

    def _build_people_tab(self, parent):
        ttk.Label(
            parent,
            text=f"Nhập tối đa {core.MAX_PEOPLE} username, cách nhau bằng dấu phẩy hoặc xuống dòng. "
                 "Ứng dụng kiểm tra từng nhóm, kênh để xem các tài khoản này có đang tham gia không.",
            style="Muted.TLabel", wraplength=900, justify="left",
        ).pack(anchor="w")
        ttk.Label(parent, text="Username (ví dụ: @user1, @user2)").pack(anchor="w", pady=(8, 2))
        self.people_text = tk.Text(parent, height=3, width=60, wrap="word")
        self.people_text.pack(anchor="w")
        self.scan_btn = ttk.Button(parent, text="Quét nhóm, kênh", style="Accent.TButton", width=20, command=self.on_scan)
        self.scan_btn.pack(anchor="w", pady=(6, 0))

        self.scan_warn_holder = ttk.Frame(parent)
        self.scan_warn_holder.pack(fill="x", pady=(6, 0))

        self.scan_body = ttk.Frame(parent)
        self.scan_users_var = tk.StringVar()
        ttk.Label(self.scan_body, textvariable=self.scan_users_var).pack(anchor="w")
        self.scan_mode_var = tk.StringVar(value=core.MODE_ANY)
        modes = ttk.Frame(self.scan_body)
        modes.pack(anchor="w", pady=(4, 4))
        for mode in (core.MODE_ANY, core.MODE_ALL):
            ttk.Radiobutton(modes, text=mode, value=mode, variable=self.scan_mode_var,
                            command=self.on_scan_mode).pack(side="left", padx=(0, 12))
        self.scan_tree_holder = ttk.Frame(self.scan_body)
        self.scan_tree_holder.pack(fill="both", expand=True)
        self.scan_tree = None
        self.scan_status_var = tk.StringVar()
        ttk.Label(self.scan_body, textvariable=self.scan_status_var).pack(anchor="w", pady=(6, 0))
        self.scan_empty_var = tk.StringVar()
        ttk.Label(self.scan_body, textvariable=self.scan_empty_var, style="Muted.TLabel").pack(anchor="w")

        row1 = ttk.Frame(self.scan_body)
        row1.pack(anchor="w", pady=(6, 0))
        self.scan_all_btn = ttk.Button(row1, text="Chọn tất cả", width=14, command=self.on_scan_select_all)
        self.scan_all_btn.pack(side="left")
        self.scan_clear_btn = ttk.Button(row1, text="Bỏ chọn", width=14, command=self.on_scan_clear_picks)
        self.scan_clear_btn.pack(side="left", padx=8)
        self.scan_leave_btn = ttk.Button(row1, width=26, command=lambda: self.on_leave_scan(False))
        self.scan_leave_btn.pack(side="left")
        self.scan_delete_btn = ttk.Button(row1, style="Accent.TButton", width=30, command=lambda: self.on_leave_scan(True))
        self.scan_delete_btn.pack(side="left", padx=8)

        row2 = ttk.Frame(self.scan_body)
        row2.pack(anchor="w", pady=(6, 0))
        self.scan_unchecked_btn = ttk.Button(row2, command=self.on_show_unchecked)
        self.scan_unchecked_btn.pack(side="left")
        ttk.Button(row2, text="Xóa kết quả quét", width=18, command=self.on_clear_scan).pack(side="left", padx=8)

    # --- Tải và hiển thị danh sách ---

    def load_groups(self):
        if self.groups is not None or self.busy:
            return
        self.busy = True
        self.set_status("Đang tải danh sách nhóm và kênh...")
        self.refresh_dashboard()
        self.background(
            lambda: core.add_labels(core.run_async(self.manager.get_groups_and_channels())),
            self.on_groups_loaded,
            self.on_groups_failed,
        )

    def on_groups_loaded(self, groups):
        self.busy = False
        self.set_status("")
        self.groups = groups
        self.selected &= {g["id"] for g in groups}
        self.refresh_dashboard()

    def on_groups_failed(self, e):
        self.busy = False
        self.set_status("")
        self.show_message(core.describe_error(e), "error")
        self.refresh_dashboard()

    def on_reload(self):
        if self.busy:
            return
        self.groups = None
        self.load_groups()

    def on_sign_out(self):
        if self.busy:
            return
        self.busy = True
        self.set_status("Đang đăng xuất...")

        def done(revoked):
            self.busy = False
            self.set_status("")
            self.reset_to_login()
            if not revoked:
                self.show_message("Chưa thu hồi được phiên trên Telegram. Hãy vào Cài đặt → Thiết bị trên Telegram để kết thúc phiên này.", "warn")

        self.background(lambda: core.sign_out(self.manager), done, self.show_error)

    def refresh_dashboard(self):
        if self.stage != "dashboard":
            return
        if self.groups is None:
            self.stats_var.set("")
            self.dash_message_var.set("Đang tải..." if self.busy else "Chưa có danh sách nhóm và kênh. Bấm Tải lại danh sách.")
            self.dash_message.pack(anchor="w", pady=(8, 0))
            self.tabs.pack_forget()
            self.result_frame.pack_forget()
            return
        groups = self.groups
        groups_count = sum(g["type"] == "Group" for g in groups)
        self.stats_var.set(f"Nhóm: {groups_count}     Kênh: {len(groups) - groups_count}     Tổng cộng: {len(groups)}")
        self.render_result()
        if not groups:
            self.dash_message_var.set("Tài khoản này chưa tham gia nhóm hoặc kênh nào.")
            self.dash_message.pack(anchor="w", pady=(8, 0))
            self.tabs.pack_forget()
            return
        self.dash_message.pack_forget()
        self.tabs.pack(fill="both", expand=True, pady=(8, 0))
        self.refresh_list()
        self.render_scan()

    def render_result(self):
        results = self.last_result
        if not results:
            self.result_frame.pack_forget()
            return
        failed = [r for r in results if not r["ok"]]
        if failed:
            summary = (f"Đã rời {len(results) - len(failed)}/{len(results)} mục, còn {len(failed)} mục chưa rời. "
                       "Xem lý do bên dưới. Các mục chưa rời vẫn được giữ lựa chọn để bạn thử lại.")
            self.result_summary.configure(text=summary, foreground=MESSAGE_COLORS["warn"])
        else:
            self.result_summary.configure(text=f"Đã rời thành công {len(results)}/{len(results)} mục.",
                                          foreground=MESSAGE_COLORS["success"])
        self.result_text.configure(state="normal", height=min(max(len(results), 2), 10))
        self.result_text.delete("1.0", "end")
        for r in results:
            self.result_text.insert("end", f"{'✔' if r['ok'] else '✘'} {r['name']}: {r['message']}\n")
        self.result_text.configure(state="disabled")
        self.result_frame.pack(fill="x", pady=(8, 0))

    def on_close_result(self):
        self.last_result = None
        self.refresh_dashboard()

    def visible_groups(self):
        return core.filter_groups(self.groups or [], self.query, self.kind_var.get())

    def refresh_list(self):
        if self.groups is None:
            return
        visible = self.visible_groups()
        self.tree.delete(*self.tree.get_children())
        for g in visible:
            self.tree.insert("", "end", iid=str(g["id"]), values=(
                PICK_MARK if g["id"] in self.selected else "",
                g["label"],
                KIND_LABELS.get(g["type"], g["type"]),
                fmt_number(g["participants"]),
                f"https://t.me/{g['username']}" if g["username"] else "",
                g["date"],
            ))
        self.visible_var.set(f"Hiển thị {len(visible)} / {len(self.groups)} nhóm, kênh")
        self.update_list_actions(visible)

    def update_list_actions(self, visible):
        chosen = [g for g in (self.groups or []) if g["id"] in self.selected]
        self.select_all_btn.configure(text=f"Chọn tất cả ({len(visible)})")
        self.select_all_btn.state(["!disabled"] if visible else ["disabled"])
        self.clear_btn.state(["!disabled"] if self.selected else ["disabled"])
        if chosen:
            self.chosen_var.set(f"Đã chọn {len(chosen)} mục. Bạn muốn làm gì với các mục này?")
        else:
            self.chosen_var.set("Tích chọn các nhóm hoặc kênh muốn rời ở bảng trên, rồi chọn hành động.")
        enabled = ["!disabled"] if chosen else ["disabled"]
        self.leave_btn.state(enabled)
        self.delete_btn.state(enabled)
        if visible:
            self.list_empty.pack_forget()
            return
        kind = self.kind_var.get()
        q = core.normalize_query(self.query)
        if not q:
            text = f"Không có nhóm hoặc kênh nào trong mục {kind}."
        elif core.filter_groups(self.groups, self.query, "Tất cả"):
            text = f"Không có kết quả trong mục {kind}. Chọn “Tất cả” để xem kết quả khác."
        else:
            text = "Không tìm thấy nhóm hoặc kênh nào khớp. Kiểm tra lại tên, @username hoặc ID."
            if core.is_username(q):
                text += " Muốn tìm nhóm, kênh mà một hay nhiều tài khoản đang tham gia? Mở tab “Tìm theo tài khoản”."
        self.list_empty_var.set(text)
        self.list_empty.pack(anchor="w", pady=(4, 0), before=self.tree.master)

    def toggle_group_pick(self, gid):
        if gid in self.selected:
            self.selected.discard(gid)
        else:
            self.selected.add(gid)
        if self.tree.exists(str(gid)):
            self.tree.set(str(gid), "pick", PICK_MARK if gid in self.selected else "")
        self.update_list_actions(self.visible_groups())

    def on_search(self):
        self.query = self.search_var.get()
        self.refresh_list()

    def on_filter(self):
        self.refresh_list()

    def on_select_all(self):
        self.selected |= {g["id"] for g in self.visible_groups()}
        self.refresh_list()

    def on_clear_all(self):
        self.selected = set()
        self.refresh_list()

    def on_leave_selected(self, delete_chat):
        items = [g for g in (self.groups or []) if g["id"] in self.selected]
        self.open_leave_dialog(items, delete_chat)

    # --- Quét thành viên ---

    def on_scan(self):
        if self.busy:
            return
        names, invalid = core.parse_usernames(self.people_text.get("1.0", "end"))
        if invalid:
            self.show_message("Không phải username hợp lệ: " + ", ".join(invalid), "error")
            return
        if not names:
            self.show_message("Nhập ít nhất một username.", "error")
            return
        if len(names) > core.MAX_PEOPLE:
            self.show_message(f"Mỗi lần chỉ quét tối đa {core.MAX_PEOPLE} tài khoản.", "error")
            return
        if self.groups is None:
            self.show_message("Đang tải danh sách nhóm và kênh. Thử lại sau ít giây.", "warn")
            return
        self.show_message("")
        self.busy = True
        self.set_status("Đang kiểm tra nhóm, kênh...")
        self.show_progress(True, "Đang kiểm tra nhóm, kênh...")
        groups = list(self.groups)

        def on_progress(done, total):
            self.post(lambda d=done, t=total: self.update_progress(f"Đang kiểm tra nhóm, kênh ({d}/{t}).", d / t if t else 1))

        def done(result):
            self.busy = False
            self.set_status("")
            self.show_progress(False)
            self.scan = result
            self.scan_picks = set()
            self.scan_mode_var.set(core.MODE_ANY)
            self.render_scan()

        def fail(e):
            self.show_progress(False)
            self.show_error(e)

        self.background(lambda: core.scan_people(self.manager, groups, names, on_progress), done, fail)

    def render_scan(self):
        for w in self.scan_warn_holder.winfo_children():
            w.destroy()
        scan = self.scan
        if scan is None:
            self.scan_body.pack_forget()
            return
        for u in scan["unresolved"]:
            ttk.Label(self.scan_warn_holder, text=f"@{u['name']}: {u['reason']}", foreground=MESSAGE_COLORS["warn"]).pack(anchor="w")
        if not scan["users"]:
            self.scan_body.pack_forget()
            return
        self.scan_users_var.set("Đang kiểm tra: " + ", ".join(f"@{u['display']}" for u in scan["users"]))
        for w in self.scan_tree_holder.winfo_children():
            w.destroy()
        cols = ["pick", "label"] + [f"u{i}" for i in range(len(scan["users"]))]
        heads = ["Chọn", "Nhóm / kênh"] + [f"@{u['display']}" for u in scan["users"]]
        widths = [60, 340] + [150] * len(scan["users"])
        self.scan_tree = self._make_tree(self.scan_tree_holder, cols, heads, widths, height=10)
        self.scan_tree.bind("<Button-1>", lambda e: self.on_tree_click(e, self.scan_tree, self.toggle_scan_pick))
        unchecked = len(scan["unchecked"])
        self.scan_unchecked_btn.configure(text=f"Không kiểm tra được {unchecked} mục")
        if unchecked:
            self.scan_unchecked_btn.pack(side="left")
        else:
            self.scan_unchecked_btn.pack_forget()
        self.scan_body.pack(fill="both", expand=True, pady=(8, 0))
        self.refresh_scan()

    def refresh_scan(self):
        scan = self.scan
        if scan is None or self.scan_tree is None:
            return
        users = scan["users"]
        self.scan_rows = [g for g in scan["groups"] if core.matches_mode(g, users, self.scan_mode_var.get())]
        self.scan_tree.delete(*self.scan_tree.get_children())
        for g in self.scan_rows:
            values = [PICK_MARK if g["id"] in self.scan_picks else "", g["label"]]
            values += [core.role_label(g["roles"].get(u["id"])) for u in users]
            self.scan_tree.insert("", "end", iid=str(g["id"]), values=values)
        picked = [g for g in self.scan_rows if g["id"] in self.scan_picks]
        self.scan_status_var.set(f"Tìm thấy {len(self.scan_rows)} nhóm, kênh. Đã chọn {len(picked)} mục.")
        self.scan_empty_var.set("" if self.scan_rows else "Không có nhóm, kênh nào khớp với lựa chọn này.")
        self.scan_all_btn.state(["!disabled"] if self.scan_rows else ["disabled"])
        self.scan_clear_btn.state(["!disabled"] if picked else ["disabled"])
        self.scan_leave_btn.configure(text=f"Rời {len(picked)} mục (giữ lịch sử)")
        self.scan_delete_btn.configure(text=f"Rời và xóa lịch sử {len(picked)} mục")
        enabled = ["!disabled"] if picked else ["disabled"]
        self.scan_leave_btn.state(enabled)
        self.scan_delete_btn.state(enabled)

    def toggle_scan_pick(self, gid):
        if gid in self.scan_picks:
            self.scan_picks.discard(gid)
        else:
            self.scan_picks.add(gid)
        if self.scan_tree is not None and self.scan_tree.exists(str(gid)):
            self.scan_tree.set(str(gid), "pick", PICK_MARK if gid in self.scan_picks else "")
        self.refresh_scan()

    def on_scan_mode(self):
        self.refresh_scan()

    def on_scan_select_all(self):
        self.scan_picks |= {g["id"] for g in self.scan_rows}
        self.refresh_scan()

    def on_scan_clear_picks(self):
        self.scan_picks -= {g["id"] for g in self.scan_rows}
        self.refresh_scan()

    def on_leave_scan(self, delete_chat):
        items = [g for g in self.scan_rows if g["id"] in self.scan_picks]
        self.open_leave_dialog(items, delete_chat)

    def on_show_unchecked(self):
        lines = [f"- {u['label']}: {u['reason']}" for u in self.scan["unchecked"]]
        messagebox.showinfo("Không kiểm tra được", "\n".join(lines), parent=self.root)

    def on_clear_scan(self):
        self.scan = None
        self.scan_picks = set()
        self.render_scan()

    # --- Tiến trình và kết quả khi rời ---

    def show_progress(self, on, text=""):
        if on:
            self.progress_label_var.set(text)
            self.progress_bar.configure(value=0)
            self.progress_frame.pack(fill="x", pady=(8, 0))
        else:
            self.progress_frame.pack_forget()

    def update_progress(self, text, fraction):
        self.progress_label_var.set(text)
        if fraction is not None:
            self.progress_bar.configure(value=max(0, min(100, fraction * 100)))

    def open_leave_dialog(self, items, delete_chat):
        """Hộp xác nhận rời nhóm/kênh: liệt kê mục sẽ rời, tùy chọn gửi tin nhắn, và xác nhận thêm nếu xóa lịch sử"""
        if self.busy or self.leave_halt is not None or not items:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Xác nhận rời nhóm / kênh")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        body = ttk.Frame(dlg, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Bạn sắp rời {len(items)} mục:", style="Heading.TLabel").pack(anchor="w")
        listing = tk.Text(body, height=min(len(items), 6), width=72, wrap="word")
        for item in items:
            listing.insert("end", f"- {item['label']} · {KIND_LABELS.get(item['type'], item['type'])}\n")
        listing.configure(state="disabled")
        listing.pack(fill="x", pady=(4, 8))

        send_var = tk.BooleanVar(value=False)
        understood_var = tk.BooleanVar(value=False)
        confirm_btn = None

        msg_box = ttk.Frame(body)
        msg_text = tk.Text(msg_box, height=4, width=72, wrap="word")
        msg_text.pack(fill="x")
        caption_var = tk.StringVar()
        preview_var = tk.StringVar()
        ttk.Label(msg_box, textvariable=caption_var).pack(anchor="w", pady=(4, 0))
        ttk.Label(msg_box, textvariable=preview_var, wraplength=640, justify="left", style="Muted.TLabel").pack(anchor="w")

        def valid():
            if send_var.get():
                text = msg_text.get("1.0", "end").strip()
                if not text:
                    caption_var.set("Nhập nội dung tin nhắn, hoặc bỏ tích gửi tin nhắn.")
                    preview_var.set("")
                    return False
                if len(text) > core.MAX_MESSAGE_LEN:
                    caption_var.set(f"Tin nhắn dài tối đa {core.MAX_MESSAGE_LEN} ký tự (hiện {len(text)}).")
                    preview_var.set("")
                    return False
                caption_var.set(f"Tin nhắn sẽ gửi tới {len(items)} mục:")
                preview_var.set(text)
            return not delete_chat or understood_var.get()

        def refresh_confirm():
            if confirm_btn is not None:
                confirm_btn.state(["!disabled"] if valid() else ["disabled"])

        def toggle_message():
            if send_var.get():
                msg_box.pack(fill="x", pady=(0, 8), before=box_after)
            else:
                msg_box.pack_forget()
            refresh_confirm()

        ttk.Checkbutton(body, text="Gửi tin nhắn trước khi rời (tùy chọn)", variable=send_var,
                        command=toggle_message).pack(anchor="w")
        msg_text.bind("<KeyRelease>", lambda e: refresh_confirm())
        box_after = ttk.Frame(body)  # neo để khung tin nhắn luôn nằm đúng vị trí
        box_after.pack(fill="x")

        if delete_chat:
            ttk.Label(body, text="Lịch sử chat sẽ bị xóa khỏi tài khoản của bạn.", foreground=MESSAGE_COLORS["warn"]).pack(anchor="w", pady=(6, 0))
            ttk.Checkbutton(body, text="Tôi hiểu và muốn xóa lịch sử chat", variable=understood_var,
                            command=refresh_confirm).pack(anchor="w")
        ttk.Label(body, text="Sau khi rời, bạn có thể cần được mời lại để tham gia.", style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        ttk.Label(
            body,
            text=f"Rời theo đợt tối đa {core.LEAVE_BATCH} mục, nghỉ {core.LEAVE_PAUSE} giây giữa các đợt. "
                 "Nếu Telegram yêu cầu chờ, ứng dụng tự chờ đúng số giây đó rồi rời tiếp cho tới khi xong. "
                 "Giữ ứng dụng mở trong lúc chạy. Đóng ứng dụng sẽ dừng việc đang chạy.",
            style="Muted.TLabel", wraplength=640, justify="left",
        ).pack(anchor="w", pady=(4, 10))

        def confirm():
            text = msg_text.get("1.0", "end").strip() if send_var.get() else None
            dlg.destroy()
            self.start_leave(items, delete_chat, text)

        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Hủy", width=12, command=dlg.destroy).pack(side="left")
        confirm_btn = ttk.Button(buttons, text="Rời và xóa lịch sử" if delete_chat else "Rời nhóm / kênh",
                                 style="Accent.TButton", width=22, command=confirm)
        confirm_btn.pack(side="right")
        refresh_confirm()
        self.leave_dialog = dlg
        try:
            dlg.grab_set()
        except tk.TclError:
            pass  # cửa sổ chưa hiện trên màn hình (ví dụ khi kiểm thử), không cần khóa đầu vào

    def start_leave(self, items, delete_chat, text):
        """Chạy việc rời nhóm ở luồng nền. Tiến trình và kết quả từng đợt được đưa về giao diện khi chạy"""
        halt = threading.Event()
        self.leave_halt = halt
        self.busy = True
        self.show_message("")
        self.show_progress(True, "Đang rời nhóm, kênh...")
        self.set_status("Đang rời nhóm, kênh. Đừng thao tác gì trong lúc chạy.")
        posted = set(self.posted)

        def on_status(msg, fraction):
            self.post(lambda m=msg, f=fraction: self.update_progress(m, f))

        def on_record(results, posted_ids):
            self.post(lambda r=list(results), p=set(posted_ids): self.apply_leave_record(r, p))

        def done(_results):
            self.leave_halt = None
            self.busy = False
            self.set_status("")
            self.show_progress(False)
            self.groups = None
            self.load_groups()

        def fail(e):
            self.leave_halt = None
            self.show_progress(False)
            self.show_error(e)
            self.groups = None
            self.load_groups()

        self.background(
            lambda: core.leave_items(self.manager, items, delete_chat, text, posted, on_status, on_record, halt),
            done, fail,
        )

    def apply_leave_record(self, results, posted):
        """Cập nhật lựa chọn, danh sách và kết quả sau mỗi đợt, kể cả khi thao tác bị ngắt"""
        done_ids = {r["id"] for r in results if r["ok"]}
        self.selected -= done_ids
        self.scan_picks -= done_ids
        self.posted = {i for i in posted if i not in done_ids}
        if self.groups is not None:
            self.groups = [g for g in self.groups if g["id"] not in done_ids]
        if self.scan is not None:
            self.scan["groups"] = [g for g in self.scan["groups"] if g["id"] not in done_ids]
        self.last_result = results
        self.refresh_dashboard()
        self.render_scan()


def run_gui(root, icon=None):
    """Dựng giao diện trên cửa sổ gốc và trả về App. Việc gọi root.mainloop() do launcher đảm nhiệm"""
    return App(root, icon)
