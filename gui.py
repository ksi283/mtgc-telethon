"""Giao diện Tkinter của ứng dụng. Chỉ hiển thị và gọi core.py.

Mọi thao tác chờ mạng chạy ở luồng nền. Kết quả và tiến trình đưa về luồng giao diện qua hàng đợi (App.post),
vì Tkinter chỉ an toàn khi gọi từ đúng luồng của nó. Giao diện không dùng emoji vì Tk trên Windows không hiển
thị đủ ký tự ngoài mặt phẳng BMP.

Màu sắc và kiểu chữ tập trung ở PALETTE và App._apply_style, giống một file CSS: đổi một giá trị là đổi cả giao diện.
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
PALETTE = {
    "bg": "#f3f5f8",  # nền cửa sổ
    "surface": "#ffffff",  # thẻ, ô nhập, bảng, ô văn bản
    "surface_alt": "#e9edf2",  # tiêu đề bảng, thanh trạng thái, tab chưa chọn
    "border": "#d3dae3",
    "text": "#1f2328",
    "muted": "#5b6573",
    "accent": "#1b78b2",  # nút chính, liên kết, viền ô đang nhập
    "accent_hover": "#166495",
    "accent_pressed": "#12527b",
    "accent_soft": "#d4e8f5",  # dòng đang chọn
    "danger": "#b3261e",  # thao tác xóa, không hoàn tác được
    "danger_hover": "#9a1f18",
    "danger_pressed": "#7f1a14",
    "success": "#1e5e2b",
    "warn": "#6b4a00",
    "error": "#8c1d18",
    "info": "#174e7a",
}
MESSAGE_COLORS = {"info": PALETTE["info"], "error": PALETTE["error"], "success": PALETTE["success"], "warn": PALETTE["warn"]}
MESSAGE_BG = {"info": "#e7f1fa", "error": "#fdecea", "success": "#e8f5ec", "warn": "#fff4db"}
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


def required_label(parent, text, card=False):
    """Nhãn của mục bắt buộc: chữ thường, theo sau là dấu * màu đỏ"""
    row = ttk.Frame(parent, style="CardBody.TFrame" if card else "TFrame")
    label_style = "Card.TLabel" if card else "TLabel"
    ttk.Label(row, text=text, style=label_style).pack(side="left")
    ttk.Label(row, text=" *", foreground=PALETTE["danger"], style=label_style).pack(side="left")
    return row


def style_text(widget):
    """Ô văn bản kiểu Tk theo cùng bảng màu với ô nhập ttk: nền trắng, viền mỏng, khi nhập có viền xanh"""
    widget.configure(
        background=PALETTE["surface"], foreground=PALETTE["text"], insertbackground=PALETTE["text"],
        selectbackground=PALETTE["accent_soft"], selectforeground=PALETTE["text"],
        relief="flat", borderwidth=0, highlightthickness=1, highlightbackground=PALETTE["border"],
        highlightcolor=PALETTE["accent"], padx=8, pady=6,
    )


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
        self.bots = None  # bot trong danh sách trò chuyện, tải cùng nhóm và kênh; None = chưa tải
        self.bot_selected = set()  # id bot đã chọn ở tab Bot Telegram
        self.bot_query = ""  # từ khóa đã áp dụng ở tab Bot Telegram (đổi khi nhấn Enter)
        self.last_result_kind = "leave"  # "leave" hoặc "block": kết quả đang hiện là của thao tác nào
        self.bulk_name = ""  # tên thao tác đang chạy, dùng khi hỏi xác nhận đóng ứng dụng
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
        base.configure(size=10)  # cỡ 10 dễ đọc hơn mặc định 9 trên Windows
        self.body_font = base
        self.heading_font = base.copy()
        self.heading_font.configure(size=15, weight="bold")
        self.bold_font = base.copy()
        self.bold_font.configure(weight="bold")
        self.small_font = base.copy()
        self.small_font.configure(size=9)
        self.code_font = base.copy()
        self.code_font.configure(size=18, weight="bold")

    def _apply_style(self, root):
        """Kiểu chung cho mọi widget. Theme clam cho phép đổi màu nền, viền và nút, còn theme mặc định của Windows bỏ qua các tùy chọn này"""
        p = PALETTE
        style = ttk.Style(root)
        style.theme_use("clam")
        root.configure(background=p["bg"])
        style.configure(".", background=p["bg"], foreground=p["text"], font=self.body_font, bordercolor=p["border"],
                        lightcolor=p["surface"], darkcolor=p["border"], troughcolor=p["surface_alt"],
                        focuscolor=p["accent"], insertcolor=p["text"])
        # Khung, nhãn: thẻ (Card) có nền trắng và viền, các nhãn đặt trong thẻ dùng kiểu Card.*
        style.configure("TFrame", background=p["bg"])
        style.configure("CardBody.TFrame", background=p["surface"])
        style.configure("Card.TFrame", background=p["surface"], bordercolor=p["border"], relief="solid", borderwidth=1)
        style.configure("TLabel", background=p["bg"], foreground=p["text"])
        style.configure("Card.TLabel", background=p["surface"], foreground=p["text"])
        style.configure("Heading.TLabel", font=self.heading_font)
        style.configure("Card.Heading.TLabel", font=self.heading_font, background=p["surface"])
        style.configure("Muted.TLabel", foreground=p["muted"])
        style.configure("Card.Muted.TLabel", foreground=p["muted"], background=p["surface"])
        style.configure("Hint.TLabel", font=self.small_font, foreground=p["muted"])
        style.configure("Stat.TLabel", font=self.bold_font)
        style.configure("Warn.TLabel", foreground=p["warn"])
        style.configure("Danger.TLabel", foreground=p["danger"])
        style.configure("Banner.TLabel", padding=(14, 9), background=p["bg"])
        style.configure("Status.TLabel", background=p["surface_alt"], foreground=p["muted"], padding=(20, 6))
        style.configure("TSeparator", background=p["border"])
        # Nút: mặc định trắng viền xám, nút chính xanh, nút xóa đỏ, liên kết chỉ có chữ
        style.configure("TButton", padding=(14, 7), background=p["surface"], foreground=p["text"], bordercolor=p["border"],
                        lightcolor=p["border"], darkcolor=p["border"])
        style.map("TButton",
                  background=[("disabled", p["surface_alt"]), ("pressed", "#dde3eb"), ("active", "#f1f4f8")],
                  foreground=[("disabled", "#9aa3ad")],
                  bordercolor=[("focus", p["accent"])])
        style.configure("Accent.TButton", font=self.bold_font, background=p["accent"], foreground="#ffffff",
                        bordercolor=p["accent"], lightcolor=p["accent"], darkcolor=p["accent"])
        style.map("Accent.TButton",
                  background=[("disabled", "#a7cde4"), ("pressed", p["accent_pressed"]), ("active", p["accent_hover"])],
                  foreground=[("disabled", "#ffffff")],
                  bordercolor=[("pressed", p["accent_pressed"]), ("active", p["accent_hover"])],
                  lightcolor=[("pressed", p["accent_pressed"]), ("active", p["accent_hover"])],
                  darkcolor=[("pressed", p["accent_pressed"]), ("active", p["accent_hover"])])
        style.configure("Danger.TButton", font=self.bold_font, background=p["danger"], foreground="#ffffff",
                        bordercolor=p["danger"], lightcolor=p["danger"], darkcolor=p["danger"])
        style.map("Danger.TButton",
                  background=[("disabled", "#e0a7a4"), ("pressed", p["danger_pressed"]), ("active", p["danger_hover"])],
                  foreground=[("disabled", "#ffffff")],
                  bordercolor=[("pressed", p["danger_pressed"]), ("active", p["danger_hover"])],
                  lightcolor=[("pressed", p["danger_pressed"]), ("active", p["danger_hover"])],
                  darkcolor=[("pressed", p["danger_pressed"]), ("active", p["danger_hover"])])
        for name, bg in (("Link.TButton", p["bg"]), ("Card.Link.TButton", p["surface"])):
            style.configure(name, background=bg, foreground=p["accent"], bordercolor=bg, lightcolor=bg, darkcolor=bg,
                            padding=(0, 2))
            style.map(name,
                      foreground=[("active", p["accent_hover"]), ("disabled", p["muted"])],
                      background=[("active", bg), ("pressed", bg)],
                      bordercolor=[("focus", p["accent"])])
        # Ô nhập, hộp đánh dấu, tab, bảng, thanh tiến trình
        style.configure("TEntry", fieldbackground=p["surface"], foreground=p["text"], bordercolor=p["border"],
                        lightcolor=p["border"], darkcolor=p["border"], padding=(8, 5), insertcolor=p["text"])
        style.map("TEntry", bordercolor=[("focus", p["accent"])], lightcolor=[("focus", p["accent"])],
                  darkcolor=[("focus", p["accent"])], fieldbackground=[("disabled", p["surface_alt"])])
        style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
        style.configure("TRadiobutton", background=p["bg"], foreground=p["text"])
        style.map("TCheckbutton", background=[("active", p["bg"])])
        style.map("TRadiobutton", background=[("active", p["bg"])])
        style.configure("TNotebook", background=p["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 8), background=p["surface_alt"], foreground=p["muted"])
        style.map("TNotebook.Tab", background=[("selected", p["bg"])], foreground=[("selected", p["text"])])
        style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], foreground=p["text"],
                        bordercolor=p["border"], rowheight=28)
        style.map("Treeview", background=[("selected", p["accent_soft"])], foreground=[("selected", p["text"])])
        style.configure("Treeview.Heading", background=p["surface_alt"], foreground=p["text"], font=self.bold_font,
                        padding=(8, 6), relief="flat")
        style.map("Treeview.Heading", background=[("active", "#dce2ea")])
        style.configure("TProgressbar", troughcolor=p["surface_alt"], background=p["accent"], bordercolor=p["surface_alt"],
                        lightcolor=p["accent"], darkcolor=p["accent"], thickness=8)
        style.configure("TScrollbar", troughcolor=p["bg"], background=p["border"], bordercolor=p["bg"], arrowcolor=p["muted"])
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"])
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["text"], font=self.bold_font)

    def _build(self, icon):
        root = self.root
        root.title(TITLE)
        root.geometry("1120x800")
        root.minsize(960, 640)
        if icon is not None:
            self.icon_image = icon
            root.iconphoto(True, icon)
        self._apply_style(root)

        header = ttk.Frame(root, padding=(20, 14, 20, 12))
        header.pack(fill="x")
        ttk.Label(header, text=TITLE, style="Heading.TLabel").pack(side="left")
        ttk.Label(header, text="Xem và rời nhiều nhóm, kênh Telegram cùng lúc.", style="Muted.TLabel").pack(
            side="left", padx=(12, 0), pady=(3, 0))
        ttk.Separator(root).pack(fill="x")

        self.msg_label = ttk.Label(root, text="", wraplength=940, justify="left", style="Banner.TLabel")
        self.msg_label.pack(fill="x", padx=20, pady=(12, 0))

        self.status_var = tk.StringVar()
        ttk.Label(root, textvariable=self.status_var, style="Status.TLabel").pack(side="bottom", fill="x")
        self.content = ttk.Frame(root, padding=(20, 12))
        self.content.pack(fill="both", expand=True)

        self.restore_frame = self._build_restore(self.content)
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
        if stage == "login":
            self._focus_login_field()

    def _focus_login_field(self):
        """Đưa con trỏ vào ô đầu tiên còn trống trên màn đăng nhập, để gõ liền không cần bấm chuột"""
        for var, entry in ((self.api_id_var, self.api_id_entry), (self.api_hash_var, self.api_hash_entry),
                           (self.phone_var, self.phone_entry)):
            if not var.get().strip():
                entry.focus_set()
                return

    def show_message(self, text, kind="info"):
        """Thông báo trên đầu màn hình, nền tô theo loại. Khi không có thông báo thì ẩn hẳn, không chiếm chỗ"""
        if text:
            self.msg_label.configure(text=text, foreground=MESSAGE_COLORS.get(kind, PALETTE["text"]),
                                     background=MESSAGE_BG.get(kind, PALETTE["surface"]))
            self.msg_label.pack(fill="x", padx=20, pady=(12, 0), before=self.content)
        else:
            self.msg_label.configure(text="")
            self.msg_label.pack_forget()

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
            question += (f"\n\nĐang {self.bulk_name}, thao tác đó sẽ bị dừng lại. "
                         "Các mục đã xong vẫn giữ nguyên trên Telegram.")
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

    # --- Màn hình khôi phục phiên ---

    def _build_restore(self, parent):
        frame = ttk.Frame(parent, padding=16)
        box = ttk.Frame(frame, style="Card.TFrame", padding=(36, 28))
        box.pack(expand=True)
        ttk.Label(box, text="Đang khôi phục phiên đăng nhập...", style="Card.TLabel").pack()
        self.restore_bar = ttk.Progressbar(box, mode="indeterminate", length=280)
        self.restore_bar.pack(pady=(14, 0))
        return frame

    # --- Màn hình đăng nhập ---

    def _build_login(self, parent):
        frame = ttk.Frame(parent, padding=16)
        box = ttk.Frame(frame, style="Card.TFrame", padding=(36, 28))
        box.pack(expand=True)  # thẻ nằm giữa khung, cả chiều ngang lẫn chiều dọc
        ttk.Label(box, text="Kết nối tài khoản Telegram", style="Card.Heading.TLabel").pack()
        ttk.Label(box, text="Cần API ID và API Hash miễn phí từ my.telegram.org.", style="Card.Muted.TLabel").pack(pady=(4, 2))
        ttk.Button(box, text="Chưa có API ID và API Hash? Xem hướng dẫn", style="Card.Link.TButton",
                   command=self.show_guide).pack()

        form = ttk.Frame(box, style="CardBody.TFrame")
        form.pack(pady=(18, 0))
        self.api_id_var = tk.StringVar()
        self.api_hash_var = tk.StringVar()
        self.phone_var = tk.StringVar()
        # API ID và API Hash luôn bắt buộc. Số điện thoại chỉ cần khi nhận mã, QR không cần nên không đánh dấu
        required_label(form, "API ID", card=True).grid(row=0, column=0, sticky="w", pady=5)
        self.api_id_entry = ttk.Entry(form, textvariable=self.api_id_var, width=34)
        self.api_id_entry.grid(row=0, column=1, sticky="w", padx=(14, 0), pady=5)
        ttk.Label(form, text="Ví dụ: 1234567", style="Card.Muted.TLabel").grid(row=0, column=2, sticky="w", padx=10)
        required_label(form, "API Hash", card=True).grid(row=1, column=0, sticky="w", pady=5)
        self.api_hash_entry = ttk.Entry(form, textvariable=self.api_hash_var, show="•", width=34)
        self.api_hash_entry.grid(row=1, column=1, sticky="w", padx=(14, 0), pady=5)
        ttk.Label(form, text="Số điện thoại", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=5)
        self.phone_entry = ttk.Entry(form, textvariable=self.phone_var, width=34)
        self.phone_entry.grid(row=2, column=1, sticky="w", padx=(14, 0), pady=5)
        ttk.Label(form, text="Ví dụ: +84912345678 (để trống nếu dùng QR)", style="Card.Muted.TLabel").grid(row=2, column=2, sticky="w", padx=10)
        # Enter đi tiếp theo thứ tự ô; ở ô số điện thoại thì Enter gửi mã
        self.api_id_entry.bind("<Return>", lambda e: self.api_hash_entry.focus_set())
        self.api_hash_entry.bind("<Return>", lambda e: self.phone_entry.focus_set())
        self.phone_entry.bind("<Return>", lambda e: self.on_continue())

        # ttk.Label(
        #     box,
        #     text="Số điện thoại chỉ cần khi nhận mã xác thực. Nhập đủ mã quốc gia, bắt đầu bằng dấu +. "
        #          "Đăng nhập bằng QR không cần số này.",
        #     style="Card.Muted.TLabel", wraplength=560, justify="center",
        # ).pack(pady=(14, 0))

        buttons = ttk.Frame(box, style="CardBody.TFrame")
        buttons.pack(pady=(20, 0))
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
        if not phone:
            # Không có số thì không nhận được mã, nên đăng nhập bằng QR (QR không cần số điện thoại)
            self.on_qr_start()
            return
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
        box = ttk.Frame(frame, style="Card.TFrame", padding=(36, 28))
        box.pack(expand=True)
        ttk.Label(box, text="Quét mã QR bằng Telegram", style="Card.Heading.TLabel").pack()
        self.qr_canvas = tk.Canvas(box, width=240, height=240, background="white", highlightthickness=1,
                                   highlightbackground=PALETTE["border"])
        self.qr_canvas.pack(pady=(14, 12))
        steps = ttk.Frame(box, style="CardBody.TFrame")
        steps.pack()
        for n, text in enumerate((
            "Mở Telegram trên điện thoại.",
            "Vào Cài đặt → Thiết bị → Liên kết thiết bị máy tính.",
            "Quét mã này. Mã tự làm mới khi hết hạn.",
        ), 1):
            ttk.Label(steps, text=f"{n}. {text}", style="Card.Muted.TLabel").pack(anchor="w", pady=2)
        ttk.Button(box, text="Hủy đăng nhập QR", command=self.on_qr_cancel).pack(pady=(18, 0))
        return frame

    # --- Mã xác thực và xác minh 2 bước ---

    def _build_code(self, parent):
        frame = ttk.Frame(parent, padding=16)
        box = ttk.Frame(frame, style="Card.TFrame", padding=(36, 28))
        box.pack(expand=True)
        ttk.Label(box, text="Nhập mã xác thực", style="Card.Heading.TLabel").pack()
        self.code_info_var = tk.StringVar()
        ttk.Label(box, textvariable=self.code_info_var, style="Card.Muted.TLabel", wraplength=520,
                  justify="center").pack(pady=(6, 18))
        self.code_var = tk.StringVar()
        self.code_var.trace_add("write", lambda *_: self._limit(self.code_var, 6))
        self.code_entry = ttk.Entry(box, textvariable=self.code_var, width=10, font=self.code_font, justify="center")
        self.code_entry.pack()
        self.code_entry.bind("<Return>", lambda e: self.on_verify_code())
        buttons = ttk.Frame(box, style="CardBody.TFrame")
        buttons.pack(pady=(20, 0))
        ttk.Button(buttons, text="Xác nhận", style="Accent.TButton", width=16, command=self.on_verify_code).pack(side="left")
        ttk.Button(buttons, text="Gửi lại mã", width=16, command=self.on_resend_code).pack(side="left", padx=8)
        ttk.Button(box, text="Đổi số điện thoại", style="Card.Link.TButton", command=self.reset_to_login).pack(pady=(14, 0))
        return frame

    def _build_twofa(self, parent):
        frame = ttk.Frame(parent, padding=16)
        box = ttk.Frame(frame, style="Card.TFrame", padding=(36, 28))
        box.pack(expand=True)
        ttk.Label(box, text="Xác minh 2 bước", style="Card.Heading.TLabel").pack()
        ttk.Label(
            box,
            text="Tài khoản này đã bật xác minh 2 bước. Nhập mật khẩu bạn tự đặt trong Telegram, không phải mã xác thực.",
            style="Card.Muted.TLabel", wraplength=520, justify="center",
        ).pack(pady=(6, 18))
        row = ttk.Frame(box, style="CardBody.TFrame")
        row.pack()
        ttk.Label(row, text="Mật khẩu 2FA", style="Card.TLabel").pack(side="left")
        self.pw_var = tk.StringVar()
        self.pw_entry = ttk.Entry(row, textvariable=self.pw_var, show="•", width=30)
        self.pw_entry.pack(side="left", padx=10)
        self.pw_entry.bind("<Return>", lambda e: self.on_submit_2fa())
        buttons = ttk.Frame(box, style="CardBody.TFrame")
        buttons.pack(pady=(20, 0))
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
        self.bots = None
        self.bot_selected = set()
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
        self.restore_bar.start(12)
        self.background(lambda: core.restore_session(self.manager), self.on_restored, self._restore_failed)

    def on_restored(self, info):
        self.busy = False
        self.restore_bar.stop()
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
        self.restore_bar.stop()
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
        ).pack(anchor="w", pady=(4, 0))
        self.stats_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.stats_var, style="Stat.TLabel").pack(anchor="w", pady=(10, 0))

        # Thông báo, tiến trình và kết quả được đặt thẳng trong khung quản lý và chèn ngay trước chú thích cuối trang,
        # nên khi ẩn đi không để lại khoảng trống (khung trung gian không tự co lại khi con bị ẩn)
        self.dash_message_var = tk.StringVar()
        self.dash_message = ttk.Label(frame, textvariable=self.dash_message_var, style="Muted.TLabel", wraplength=900, justify="left")

        self.progress_frame = ttk.Frame(frame, padding=(0, 8))
        self.progress_label_var = tk.StringVar()
        ttk.Label(self.progress_frame, textvariable=self.progress_label_var).pack(anchor="w")
        self.progress_bar = ttk.Progressbar(self.progress_frame, maximum=100, length=600)
        self.progress_bar.pack(anchor="w", pady=(4, 0))

        self.result_frame = ttk.LabelFrame(frame, text="Kết quả lần rời gần nhất", padding=10)
        self.result_summary = ttk.Label(self.result_frame, wraplength=900, justify="left")
        self.result_summary.pack(anchor="w")
        self.result_text = tk.Text(self.result_frame, height=7, wrap="word")
        style_text(self.result_text)
        self.result_text.pack(fill="x", pady=(6, 6))
        ttk.Button(self.result_frame, text="Đóng thông báo", command=self.on_close_result).pack(anchor="e")

        name, version = core.project_info()
        footer = ttk.Label(
            frame,
            text=f"{name} v{version} · © 2026 by ksi283" if version else f"{name} · © 2026 by ksi283",
            style="Hint.TLabel", wraplength=1000, justify="left",
        )
        self.footer = footer
        footer.pack(side="bottom", anchor="w", pady=(6, 0))

        self.tabs = ttk.Notebook(frame)
        tab_list = ttk.Frame(self.tabs, padding=12)
        tab_bots = ttk.Frame(self.tabs, padding=12)
        tab_people = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(tab_list, text="Nhóm & kênh")
        self.tabs.add(tab_bots, text="Bot Telegram")
        self.tabs.add(tab_people, text="Tìm theo tài khoản")
        self._build_list_tab(tab_list)
        self._build_bot_tab(tab_bots)
        self._build_people_tab(tab_people)
        self.tabs.pack(fill="both", expand=True, pady=(10, 0))
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
        filters.pack(fill="x", pady=(10, 0))
        self.kind_var = tk.StringVar(value="Tất cả")
        for kind in core.FILTER_TYPES:
            ttk.Radiobutton(filters, text=kind, value=kind, variable=self.kind_var,
                            command=self.on_filter).pack(side="left", padx=(0, 12))

        info = ttk.Frame(parent)
        info.pack(fill="x", pady=(10, 6))
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

        # Thanh hành động và ghi chú được đóng gói ở đáy trước bảng, để bảng co lại chứ không đẩy nút ra ngoài màn hình
        chosen = ttk.Frame(parent, padding=(0, 10, 0, 0))
        chosen.pack(side="bottom", fill="x", before=tree_frame)
        self.chosen_var = tk.StringVar()
        ttk.Label(chosen, textvariable=self.chosen_var).pack(anchor="w")
        actions = ttk.Frame(chosen)
        actions.pack(anchor="w", pady=(8, 0))
        # Rời giữ lịch sử là thao tác thường (nút chính xanh); xóa lịch sử là thao tác không hoàn tác (nút đỏ)
        self.leave_btn = ttk.Button(actions, text="Rời, giữ lịch sử chat", style="Accent.TButton", width=24,
                                    command=lambda: self.on_leave_selected(False))
        self.leave_btn.pack(side="left")
        self.delete_btn = ttk.Button(actions, text="Rời và xóa lịch sử chat", style="Danger.TButton", width=26,
                                     command=lambda: self.on_leave_selected(True))
        self.delete_btn.pack(side="left", padx=8)
        ttk.Label(parent, text="Rời nhóm hoặc kênh có thể khiến bạn phải được mời lại để tham gia.",
                  style="Muted.TLabel").pack(side="bottom", anchor="w", pady=(10, 0), before=chosen)

    def _build_bot_tab(self, parent):
        ttk.Label(parent, text="Bot trong danh sách trò chuyện của bạn. Chặn bot thì bot không gửi tin nhắn cho bạn nữa.",
                  style="Muted.TLabel", wraplength=900, justify="left").pack(anchor="w")
        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(10, 0))
        ttk.Label(top, text="Tìm kiếm").pack(side="left")
        self.bot_search_var = tk.StringVar()
        self.bot_search_entry = ttk.Entry(top, textvariable=self.bot_search_var, width=48)
        self.bot_search_entry.pack(side="left", padx=8)
        self.bot_search_entry.bind("<Return>", lambda e: self.on_bot_search())
        ttk.Label(top, text="Tên bot, @username, link t.me/... hoặc ID. Nhấn Enter để tìm.",
                  style="Muted.TLabel").pack(side="left")

        info = ttk.Frame(parent)
        info.pack(fill="x", pady=(10, 6))
        self.bot_visible_var = tk.StringVar()
        ttk.Label(info, textvariable=self.bot_visible_var).pack(side="left")
        self.bot_select_all_btn = ttk.Button(info, command=self.on_bot_select_all)
        self.bot_select_all_btn.pack(side="left", padx=(12, 0))
        self.bot_clear_btn = ttk.Button(info, text="Bỏ chọn tất cả", command=self.on_bot_clear_all)
        self.bot_clear_btn.pack(side="left", padx=8)

        self.bot_empty_var = tk.StringVar()
        self.bot_empty = ttk.Label(parent, textvariable=self.bot_empty_var, style="Muted.TLabel",
                                   wraplength=900, justify="left")

        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=True)
        self.bot_tree = self._make_tree(
            tree_frame,
            ("pick", "title", "username", "date"),
            ("Chọn", "Tên bot", "Username", "Tin nhắn gần nhất"),
            (60, 330, 230, 160),
        )
        self.bot_tree.bind("<Button-1>", lambda e: self.on_tree_click(e, self.bot_tree, self.toggle_bot_pick))

        # Thanh hành động và ghi chú được đóng gói ở đáy trước bảng, để bảng co lại chứ không đẩy nút ra ngoài màn hình
        chosen = ttk.Frame(parent, padding=(0, 10, 0, 0))
        chosen.pack(side="bottom", fill="x", before=tree_frame)
        self.bot_chosen_var = tk.StringVar()
        ttk.Label(chosen, textvariable=self.bot_chosen_var).pack(anchor="w")
        actions = ttk.Frame(chosen)
        actions.pack(anchor="w", pady=(8, 0))
        # Chặn giữ lịch sử là thao tác thường (nút chính xanh); xóa lịch sử là thao tác không hoàn tác (nút đỏ)
        self.block_btn = ttk.Button(actions, text="Chặn bot, giữ lịch sử", style="Accent.TButton", width=24,
                                    command=lambda: self.on_block_selected(False))
        self.block_btn.pack(side="left")
        self.block_delete_btn = ttk.Button(actions, text="Chặn và xóa lịch sử chat", style="Danger.TButton", width=26,
                                           command=lambda: self.on_block_selected(True))
        self.block_delete_btn.pack(side="left", padx=8)
        ttk.Label(parent, text="Bot bị chặn vẫn còn trên Telegram. Có thể bỏ chặn sau này trong Cài đặt Telegram, "
                               "phần người bị chặn.", style="Muted.TLabel").pack(side="bottom", anchor="w", pady=(10, 0),
                                                                              before=chosen)

    def _build_people_tab(self, parent):
        # Bố cục gọn để bảng kết quả luôn có đủ chỗ: mô tả một dòng, ô nhập và nút quét cùng hàng
        ttk.Label(
            parent,
            text=f"Nhập tối đa {core.MAX_PEOPLE} username (ví dụ: @user1, @user2), cách nhau bằng dấu phẩy hoặc xuống dòng. "
                 "Ứng dụng kiểm tra từng nhóm, kênh để xem các tài khoản này có đang tham gia không.",
            style="Muted.TLabel", wraplength=960, justify="left",
        ).pack(anchor="w")
        form = ttk.Frame(parent)
        form.pack(anchor="w", pady=(10, 0))
        self.people_text = tk.Text(form, height=2, width=56, wrap="word")
        style_text(self.people_text)
        self.people_text.pack(side="left")
        self.scan_btn = ttk.Button(form, text="Quét nhóm, kênh", style="Accent.TButton", command=self.on_scan)
        self.scan_btn.pack(side="left", padx=(12, 0), fill="y")

        self.scan_warn_holder = ttk.Frame(parent)
        self.scan_warn_holder.pack(fill="x", pady=(6, 0))

        self.scan_body = ttk.Frame(parent)
        head = ttk.Frame(self.scan_body)
        head.pack(fill="x")
        self.scan_users_var = tk.StringVar()
        ttk.Label(head, textvariable=self.scan_users_var, style="Stat.TLabel").pack(side="left")
        self.scan_mode_var = tk.StringVar(value=core.MODE_ANY)
        modes = ttk.Frame(head)
        modes.pack(side="left", padx=(28, 0))
        for mode in (core.MODE_ANY, core.MODE_ALL):
            ttk.Radiobutton(modes, text=mode, value=mode, variable=self.scan_mode_var,
                            command=self.on_scan_mode).pack(side="left", padx=(0, 12))
        self.scan_status_var = tk.StringVar()
        ttk.Label(self.scan_body, textvariable=self.scan_status_var, style="Muted.TLabel").pack(anchor="w", pady=(6, 4))
        self.scan_tree_holder = ttk.Frame(self.scan_body)
        self.scan_tree_holder.pack(fill="both", expand=True)
        self.scan_tree = None

        # Thao tác với kết quả quét: hàng trên là chọn và rời, hàng dưới là các việc phụ
        row1 = ttk.Frame(self.scan_body)
        row1.pack(side="bottom", anchor="w", pady=(8, 0), before=self.scan_tree_holder)
        self.scan_all_btn = ttk.Button(row1, text="Chọn tất cả", command=self.on_scan_select_all)
        self.scan_all_btn.pack(side="left")
        self.scan_clear_btn = ttk.Button(row1, text="Bỏ chọn", command=self.on_scan_clear_picks)
        self.scan_clear_btn.pack(side="left", padx=8)
        self.scan_leave_btn = ttk.Button(row1, style="Accent.TButton", command=lambda: self.on_leave_scan(False))
        self.scan_leave_btn.pack(side="left")
        self.scan_delete_btn = ttk.Button(row1, style="Danger.TButton", command=lambda: self.on_leave_scan(True))
        self.scan_delete_btn.pack(side="left", padx=8)

        row2 = ttk.Frame(self.scan_body)
        row2.pack(side="bottom", anchor="w", pady=(4, 0), before=row1)
        self.scan_unchecked_btn = ttk.Button(row2, style="Link.TButton", command=self.on_show_unchecked)
        self.scan_unchecked_btn.pack(side="left")
        ttk.Button(row2, text="Xóa kết quả quét", command=self.on_clear_scan).pack(side="left", padx=8)

    # --- Tải và hiển thị danh sách ---

    def load_groups(self):
        if self.groups is not None or self.busy:
            return
        self.busy = True
        self.set_status("Đang tải danh sách nhóm, kênh và bot...")
        self.refresh_dashboard()
        self.background(self.fetch_lists, self.on_groups_loaded, self.on_groups_failed)

    def fetch_lists(self):
        """Chạy ở luồng nền: lấy hội thoại, rồi gắn nhãn hiển thị cho nhóm, kênh và bot"""
        groups, bots = core.run_async(self.manager.get_dialog_lists())
        return core.add_labels(groups), core.add_labels(bots)

    def on_groups_loaded(self, lists):
        groups, bots = lists
        self.busy = False
        self.set_status("")
        self.groups = groups
        self.bots = bots
        self.selected &= {g["id"] for g in groups}
        self.bot_selected &= {b["id"] for b in bots}
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
            self.dash_message.pack(anchor="w", pady=(8, 0), before=self.footer)
            self.tabs.pack_forget()
            self.result_frame.pack_forget()
            return
        groups = self.groups
        bots = self.bots or []
        groups_count = sum(g["type"] == "Group" for g in groups)
        self.stats_var.set(f"Nhóm: {groups_count}     Kênh: {len(groups) - groups_count}     "
                           f"Tổng cộng: {len(groups)}     Bot: {len(bots)}")
        self.render_result()
        if not groups and not bots:
            self.dash_message_var.set("Tài khoản này chưa tham gia nhóm hoặc kênh nào.")
            self.dash_message.pack(anchor="w", pady=(8, 0), before=self.footer)
            self.tabs.pack_forget()
            return
        self.dash_message.pack_forget()
        self.tabs.pack(fill="both", expand=True, pady=(10, 0))
        self.refresh_list()
        self.refresh_bots()
        self.render_scan()

    def render_result(self):
        results = self.last_result
        if not results:
            self.result_frame.pack_forget()
            return
        if self.last_result_kind == "block":
            title, verb, undone = "Kết quả lần chặn gần nhất", "chặn", "chưa chặn"
        else:
            title, verb, undone = "Kết quả lần rời gần nhất", "rời", "chưa rời"
        self.result_frame.configure(text=title)
        failed = [r for r in results if not r["ok"]]
        if failed:
            summary = (f"Đã {verb} {len(results) - len(failed)}/{len(results)} mục, còn {len(failed)} mục {undone}. "
                       f"Xem lý do bên dưới. Các mục {undone} vẫn được giữ lựa chọn để bạn thử lại.")
            self.result_summary.configure(text=summary, foreground=MESSAGE_COLORS["warn"])
        else:
            self.result_summary.configure(text=f"Đã {verb} thành công {len(results)}/{len(results)} mục.",
                                          foreground=MESSAGE_COLORS["success"])
        self.result_text.configure(state="normal", height=min(max(len(results), 2), 10))
        self.result_text.delete("1.0", "end")
        for r in results:
            self.result_text.insert("end", f"{'✔' if r['ok'] else '✘'} {r['name']}: {r['message']}\n")
        self.result_text.configure(state="disabled")
        self.result_frame.pack(fill="x", pady=(10, 0), before=self.footer)

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

    # --- Bot trong danh sách trò chuyện ---

    def visible_bots(self):
        q = core.normalize_query(self.bot_query)
        return [b for b in (self.bots or []) if not q or core.matches_query(b, q)]

    def refresh_bots(self):
        if self.bots is None:
            return
        visible = self.visible_bots()
        self.bot_tree.delete(*self.bot_tree.get_children())
        for b in visible:
            self.bot_tree.insert("", "end", iid=str(b["id"]), values=(
                PICK_MARK if b["id"] in self.bot_selected else "",
                b["label"],
                f"https://t.me/{b['username']}" if b["username"] else "",
                b["date"],
            ))
        self.bot_visible_var.set(f"Hiển thị {len(visible)} / {len(self.bots)} bot")
        self.update_bot_actions(visible)

    def update_bot_actions(self, visible):
        chosen = [b for b in (self.bots or []) if b["id"] in self.bot_selected]
        self.bot_select_all_btn.configure(text=f"Chọn tất cả ({len(visible)})")
        self.bot_select_all_btn.state(["!disabled"] if visible else ["disabled"])
        self.bot_clear_btn.state(["!disabled"] if self.bot_selected else ["disabled"])
        if chosen:
            self.bot_chosen_var.set(f"Đã chọn {len(chosen)} bot. Bạn muốn làm gì với các bot này?")
        else:
            self.bot_chosen_var.set("Tích chọn các bot muốn chặn ở bảng trên, rồi chọn hành động.")
        enabled = ["!disabled"] if chosen else ["disabled"]
        self.block_btn.state(enabled)
        self.block_delete_btn.state(enabled)
        if visible:
            self.bot_empty.pack_forget()
            return
        if self.bots:
            text = "Không tìm thấy bot nào khớp. Kiểm tra lại tên, @username hoặc ID."
        else:
            text = "Chưa có bot nào trong danh sách trò chuyện của bạn."
        self.bot_empty_var.set(text)
        self.bot_empty.pack(anchor="w", pady=(4, 0), before=self.bot_tree.master)

    def toggle_bot_pick(self, bid):
        if bid in self.bot_selected:
            self.bot_selected.discard(bid)
        else:
            self.bot_selected.add(bid)
        if self.bot_tree.exists(str(bid)):
            self.bot_tree.set(str(bid), "pick", PICK_MARK if bid in self.bot_selected else "")
        self.update_bot_actions(self.visible_bots())

    def on_bot_search(self):
        self.bot_query = self.bot_search_var.get()
        self.refresh_bots()

    def on_bot_select_all(self):
        self.bot_selected |= {b["id"] for b in self.visible_bots()}
        self.refresh_bots()

    def on_bot_clear_all(self):
        self.bot_selected = set()
        self.refresh_bots()

    def on_block_selected(self, delete_history):
        items = [b for b in (self.bots or []) if b["id"] in self.bot_selected]
        self.open_block_dialog(items, delete_history)

    def open_block_dialog(self, items, delete_history):
        """Hộp xác nhận chặn bot: liệt kê bot sẽ chặn, và xác nhận thêm nếu xóa lịch sử chat"""
        if self.busy or self.leave_halt is not None or not items:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Xác nhận chặn bot")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.configure(background=PALETTE["bg"])
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        body = ttk.Frame(dlg, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Bạn sắp chặn {len(items)} bot:", style="Heading.TLabel").pack(anchor="w")
        listing = tk.Text(body, height=min(len(items), 6), width=72, wrap="word")
        style_text(listing)
        for item in items:
            handle = f" · @{item['username']}" if item["username"] else ""
            listing.insert("end", f"- {item['label']}{handle}\n")
        listing.configure(state="disabled")
        listing.pack(fill="x", pady=(6, 10))

        understood_var = tk.BooleanVar(value=False)
        confirm_btn = None

        def refresh_confirm():
            if confirm_btn is not None:
                confirm_btn.state(["!disabled"] if not delete_history or understood_var.get() else ["disabled"])

        if delete_history:
            ttk.Label(body, text="Lịch sử chat với các bot này sẽ bị xóa khỏi tài khoản của bạn.",
                      style="Danger.TLabel").pack(anchor="w")
            ttk.Checkbutton(body, text="Tôi hiểu và muốn xóa lịch sử chat", variable=understood_var,
                            command=refresh_confirm).pack(anchor="w", pady=(4, 0))
        ttk.Label(body, text="Bot bị chặn không gửi tin nhắn cho bạn nữa. Có thể bỏ chặn sau này trong Cài đặt Telegram, "
                             "phần người bị chặn.", style="Muted.TLabel", wraplength=640, justify="left"
                  ).pack(anchor="w", pady=(8, 0))
        ttk.Label(body, text="Ứng dụng chặn từng bot một. Nếu Telegram yêu cầu chờ, ứng dụng tự chờ rồi chặn tiếp. "
                             "Giữ ứng dụng mở trong lúc chạy.", style="Muted.TLabel", wraplength=640, justify="left"
                  ).pack(anchor="w", pady=(4, 10))

        def confirm():
            dlg.destroy()
            self.start_block(items, delete_history)

        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Hủy", width=12, command=dlg.destroy).pack(side="left")
        confirm_btn = ttk.Button(buttons, text="Chặn và xóa lịch sử" if delete_history else "Chặn bot",
                                 style="Danger.TButton" if delete_history else "Accent.TButton", width=22, command=confirm)
        confirm_btn.pack(side="right")
        refresh_confirm()
        try:
            dlg.grab_set()
        except tk.TclError:
            pass  # cửa sổ chưa hiện trên màn hình (ví dụ khi kiểm thử), không cần khóa đầu vào

    def start_block(self, items, delete_history):
        """Chặn bot ở luồng nền. Kết quả từng bot được đưa về giao diện khi chạy"""
        halt = threading.Event()
        self.leave_halt = halt  # dùng chung cờ dừng với việc rời nhóm, để đóng ứng dụng dừng việc đang chạy
        self.bulk_name = "chặn bot"
        self.busy = True
        self.show_message("")
        self.show_progress(True, "Đang chặn bot...")
        self.set_status("Đang chặn bot. Đừng thao tác gì trong lúc chạy.")

        def on_status(msg, fraction):
            self.post(lambda m=msg, f=fraction: self.update_progress(m, f))

        def on_record(results):
            self.post(lambda r=list(results): self.apply_block_record(r))

        def done(_results):
            self.leave_halt = None
            self.bulk_name = ""
            self.busy = False
            self.set_status("")
            self.show_progress(False)
            self.groups = None  # tải lại để danh sách khớp với Telegram
            self.load_groups()

        def fail(e):
            self.leave_halt = None
            self.bulk_name = ""
            self.show_progress(False)
            self.show_error(e)
            self.groups = None
            self.load_groups()

        self.background(
            lambda: core.block_items(self.manager, items, delete_history, on_status, on_record, halt),
            done, fail,
        )

    def apply_block_record(self, results):
        """Cập nhật lựa chọn và kết quả sau mỗi bot, kể cả khi thao tác bị ngắt"""
        done_ids = {r["id"] for r in results if r["ok"]}
        self.bot_selected -= done_ids
        self.last_result = results
        self.last_result_kind = "block"
        self.refresh_dashboard()

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
            ttk.Label(self.scan_warn_holder, text=f"@{u['name']}: {u['reason']}", style="Warn.TLabel").pack(anchor="w")
        if not scan["users"]:
            self.scan_body.pack_forget()
            return
        self.scan_users_var.set("Đang kiểm tra: " + ", ".join(f"@{u['display']}" for u in scan["users"]))
        for w in self.scan_tree_holder.winfo_children():
            w.destroy()
        cols = ["pick", "label"] + [f"u{i}" for i in range(len(scan["users"]))]
        heads = ["Chọn", "Nhóm / kênh"] + [f"@{u['display']}" for u in scan["users"]]
        # Cột tài khoản rộng vừa phải để cả 5 tài khoản vẫn nằm gọn trong cửa sổ tối thiểu
        widths = [60, 300] + [130] * len(scan["users"])
        self.scan_tree = self._make_tree(self.scan_tree_holder, cols, heads, widths, height=10)
        self.scan_tree.bind("<Button-1>", lambda e: self.on_tree_click(e, self.scan_tree, self.toggle_scan_pick))
        unchecked = len(scan["unchecked"])
        self.scan_unchecked_btn.configure(text=f"Không kiểm tra được {unchecked} mục")
        if unchecked:
            self.scan_unchecked_btn.pack(side="left")
        else:
            self.scan_unchecked_btn.pack_forget()
        self.scan_body.pack(fill="both", expand=True, pady=(10, 0))
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
        if self.scan_rows:
            self.scan_status_var.set(f"Tìm thấy {len(self.scan_rows)} nhóm, kênh. Đã chọn {len(picked)} mục.")
        else:
            self.scan_status_var.set("Không có nhóm, kênh nào khớp với lựa chọn này.")
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
            self.progress_frame.pack(fill="x", pady=(8, 0), before=self.footer)
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
        dlg.configure(background=PALETTE["bg"])
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        body = ttk.Frame(dlg, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Bạn sắp rời {len(items)} mục:", style="Heading.TLabel").pack(anchor="w")
        listing = tk.Text(body, height=min(len(items), 6), width=72, wrap="word")
        style_text(listing)
        for item in items:
            listing.insert("end", f"- {item['label']} · {KIND_LABELS.get(item['type'], item['type'])}\n")
        listing.configure(state="disabled")
        listing.pack(fill="x", pady=(6, 10))

        send_var = tk.BooleanVar(value=False)
        understood_var = tk.BooleanVar(value=False)
        confirm_btn = None

        msg_box = ttk.Frame(body)
        msg_text = tk.Text(msg_box, height=4, width=72, wrap="word")
        style_text(msg_text)
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
            ttk.Label(body, text="Lịch sử chat sẽ bị xóa khỏi tài khoản của bạn.", style="Danger.TLabel").pack(anchor="w", pady=(6, 0))
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
                                 style="Danger.TButton" if delete_chat else "Accent.TButton", width=22, command=confirm)
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
        self.bulk_name = "rời nhóm và kênh"
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
            self.bulk_name = ""
            self.busy = False
            self.set_status("")
            self.show_progress(False)
            self.groups = None
            self.load_groups()

        def fail(e):
            self.leave_halt = None
            self.bulk_name = ""
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
        self.last_result_kind = "leave"
        self.refresh_dashboard()
        self.render_scan()


def run_gui(root, icon=None):
    """Dựng giao diện trên cửa sổ gốc và trả về App. Việc gọi root.mainloop() do launcher đảm nhiệm"""
    return App(root, icon)
