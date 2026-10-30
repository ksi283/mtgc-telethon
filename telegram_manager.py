import asyncio
import json
import math
import os
import re
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from contextlib import closing
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import Channel, Chat, User

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

def _env_int(name, default):
    """Đọc số nguyên không âm từ biến môi trường. Giá trị sai thì dùng mặc định thay vì làm app không chạy"""
    try:
        return max(0, int(os.environ[name]))
    except (KeyError, ValueError):
        return default

# Cấu hình trang
st.set_page_config(
    page_title="Quản lý nhóm & kênh Telegram",
    page_icon="📱",
    layout="wide"
)

STEP_LABELS = ["Thông tin", "Xác thực", "Quản lý"]
STEP_BY_STAGE = {"login": 0, "code": 1, "2fa": 1, "dashboard": 2}
FILTER_TYPES = {"Tất cả": None, "Nhóm": "Group", "Kênh": "Channel"}
MODE_ANY = "Có ít nhất một tài khoản"
MODE_ALL = "Có tất cả tài khoản"
MAX_PEOPLE = 5  # số tài khoản tối đa trong một lần quét
LEAVE_WORKERS = 3  # số tiến trình con rời nhóm/kênh cùng lúc, mỗi tiến trình một kết nối Telegram
LEAVE_BATCH = 9  # số nhóm/kênh rời trong một đợt
LEAVE_PAUSE = _env_int("LEAVE_PAUSE_SECONDS", 130)  # giây nghỉ giữa hai đợt (đặt biến môi trường để thử nhanh)
LEAVE_MAX_ATTEMPTS = 50  # số lần thử tối đa cho một mục khi Telegram liên tục yêu cầu chờ
FLOOD_MARGIN = 2  # giây cộng thêm vào thời gian Telegram yêu cầu chờ, để chắc chắn đã hết giới hạn
SCAN_WORKERS = 4  # số tiến trình con kiểm tra thành viên cùng lúc
CONNECT_TIMEOUT = 120  # giây tối đa chờ một tiến trình con kết nối được tới Telegram
JOB_TIMEOUT = 600  # giây tối đa chờ một việc trong tiến trình con, quá thì coi như tiến trình con bị treo
MAX_MESSAGE_LEN = 4096  # giới hạn độ dài một tin nhắn của Telegram
QR_TIMEOUT = 300  # giây chờ quét QR trước khi hủy
QR_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "qrcode-generator.js")
ACCOUNT_FILE = "account.json"  # thông tin cơ bản (không nhạy cảm), đã thêm vào .gitignore

@st.cache_resource
def get_async_loop():
    """Event loop chạy nền, dùng chung cho mọi lần Streamlit rerun.

    Telethon gắn client vào event loop lúc connect, nên mọi thao tác sau đó
    phải chạy trên đúng loop này. asyncio.run() tạo loop mới mỗi lần nên không dùng được.
    """
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop

def run_async(coro):
    """Chạy coroutine trên event loop nền và chờ kết quả"""
    return asyncio.run_coroutine_threadsafe(coro, get_async_loop()).result()

class QRTimeout(Exception):
    """Hết thời gian chờ quét mã QR"""


def describe_error(e):
    """Đổi lỗi kỹ thuật của Telethon thành thông báo dễ hiểu"""
    if isinstance(e, PhoneNumberInvalidError):
        return "Số điện thoại không hợp lệ. Nhập đủ mã quốc gia, ví dụ +84912345678."
    if isinstance(e, ApiIdInvalidError):
        return "API ID hoặc API Hash không đúng. Kiểm tra lại trên my.telegram.org."
    if isinstance(e, FloodWaitError):
        return f"Telegram tạm giới hạn yêu cầu. Vui lòng thử lại sau khoảng {e.seconds} giây."
    if isinstance(e, PhoneCodeExpiredError):
        return 'Mã xác thực đã hết hạn. Bấm "Gửi lại mã" để nhận mã mới.'
    if isinstance(e, PhoneCodeInvalidError):
        return "Mã xác thực không đúng. Kiểm tra lại và nhập lại."
    if isinstance(e, PasswordHashInvalidError):
        return "Mật khẩu 2FA không đúng."
    if isinstance(e, ChatAdminRequiredError):
        return "Telegram không cho xem danh sách thành viên của mục này."
    if isinstance(e, ChatWriteForbiddenError):
        return "Bạn không có quyền gửi tin nhắn trong mục này."
    if isinstance(e, QRTimeout):
        return str(e)
    if isinstance(e, OSError):
        return "Không kết nối được tới Telegram. Kiểm tra mạng rồi thử lại."
    return f"Đã xảy ra lỗi: {e}"

class TelegramManager:
    def __init__(self):
        self.client = None

    async def create_client(self, api_id, api_hash, phone):
        """Tạo và kết nối Telegram client"""
        await self.disconnect()
        self.client = TelegramClient(session_name(phone), api_id, api_hash)
        await self.client.connect()
        return self.client

    async def disconnect(self):
        """Ngắt kết nối Telegram nếu đang kết nối"""
        if self.client is not None and self.client.is_connected():
            await self.client.disconnect()

    async def login(self, phone):
        """Đăng nhập vào Telegram. Trả về True nếu đã đăng nhập sẵn, False nếu cần nhập mã"""
        if not await self.client.is_user_authorized():
            await self.send_code(phone)
            return False
        return True

    async def is_authorized(self):
        """Kiểm tra phiên đăng nhập hiện tại còn hiệu lực không"""
        return await self.client.is_user_authorized()

    async def save_session(self):
        """Ghi hẳn thay đổi xuống file SQLite. Telethon giữ transaction mở đến khi save"""
        self.client.session.save()

    async def send_code(self, phone):
        """Gửi (hoặc gửi lại) mã xác thực tới số điện thoại"""
        await self.client.send_code_request(phone)

    async def verify_code(self, phone, code):
        """Xác thực bằng mã. Trả về True nếu tài khoản cần nhập thêm mật khẩu 2FA"""
        try:
            await self.client.sign_in(phone, code)
            return False
        except SessionPasswordNeededError:
            return True

    async def verify_2fa(self, password):
        """Xác thực bằng mật khẩu 2FA"""
        await self.client.sign_in(password=password)

    async def get_groups_and_channels(self):
        """Lấy danh sách nhóm và kênh đang tham gia. Bỏ qua mục đã rời, và bản cũ của nhóm đã chuyển thành siêu nhóm"""
        dialogs = await self.client.get_dialogs()
        groups_channels = []

        for dialog in dialogs:
            entity = dialog.entity
            if not isinstance(entity, (Channel, Chat)) or entity.left:
                continue
            if isinstance(entity, Chat):
                if entity.deactivated or entity.migrated_to:
                    continue  # nhóm thường đã chuyển thành siêu nhóm, mục siêu nhóm mới là mục cần rời
                peer, item_type = ["chat", entity.id, None], "Group"
            else:
                peer = ["channel", entity.id, entity.access_hash or 0]
                item_type = "Channel" if entity.broadcast else "Group"

            groups_channels.append({
                'id': entity.id,
                'peer': peer,  # định danh để tiến trình con gọi Telegram, lấy sẵn từ danh sách hội thoại
                'title': entity.title,
                'type': item_type,
                'participants': getattr(entity, 'participants_count', None),
                'username': getattr(entity, 'username', '') or '',
                'date': dialog.date.astimezone().strftime('%d/%m/%Y %H:%M') if dialog.date else ''
            })

        return sorted(groups_channels, key=lambda x: x['title'].lower())

@st.cache_resource
def get_manager():
    """TelegramManager dùng chung cho mọi phiên trình duyệt, tải lại trang không tạo thêm kết nối"""
    return TelegramManager()

async def resolve_username(client, name):
    """Tìm đối tượng theo @username: có thể là người dùng, nhóm hoặc kênh"""
    return await client.get_entity(name)

def session_snapshot(client):
    """Dữ liệu để tiến trình con dựng lại cùng phiên Telegram. Chỉ giữ trong bộ nhớ, không ghi file"""
    s = client.session
    return {
        "dc_id": s.dc_id,
        "server": s.server_address,
        "port": s.port,
        "auth_key": s.auth_key.key.hex() if s.auth_key else None,
    }

def user_spec(user):
    """Định danh của một tài khoản để tiến trình con gọi được Telegram"""
    return (user.id, user.access_hash or 0)

_UNFINISHED = {"ok": False, "sent": False, "error": "WorkerError", "text": "Tiến trình con chưa chạy việc này. Hãy thử lại."}

def _lost(text):
    """Kết quả khi không biết việc đã làm tới đâu, ví dụ tiến trình con dừng giữa chừng"""
    return {"ok": False, "sent": False, "error": "WorkerError", "text": text}

def _parse_reply(reply):
    """Đọc một dòng trả lời của tiến trình con. Dòng không đọc được thì trả về {} để người gọi tự xử lý"""
    try:
        return json.loads(reply)
    except ValueError:
        return {}

def _ask(proc, line, timeout):
    """Gửi một dòng cho tiến trình con rồi chờ dòng trả lời. Quá timeout giây thì dừng tiến trình con.
    Trả về (trạng thái, dòng trả lời). Trạng thái: 'ok', 'not_sent' (việc chưa tới được tiến trình con),
    'eof' (tiến trình con đã thoát), 'timeout' (quá giờ và đã dừng tiến trình con)"""
    stuck = threading.Event()

    def stop_worker():
        stuck.set()
        proc.kill()

    timer = threading.Timer(timeout, stop_worker)
    timer.start()
    try:
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            return "not_sent", ""
        reply = proc.stdout.readline()
    except (OSError, ValueError):
        reply = ""
    finally:
        timer.cancel()
    if reply:
        return "ok", reply
    return ("timeout" if stuck.is_set() else "eof"), ""

def run_in_workers(kind, jobs, workers, label, results):
    """Chạy các việc trong nhiều tiến trình con. Mỗi tiến trình con là một tiến trình Python riêng,
    có một kết nối Telegram riêng. Kết quả ghi vào results theo đúng thứ tự của jobs. Nếu bị ngắt giữa chừng
    (ví dụ người dùng tải lại trang) thì các việc đang chạy vẫn được chờ xong và ghi kết quả trước khi thoát"""
    client = get_manager().client
    config = {
        "client": f"{type(client).__module__}:{type(client).__qualname__}",
        "session": session_snapshot(client),
        "api_id": client.api_id,
        "api_hash": client.api_hash,
    }
    # PYTHONPATH giữ nguyên đường dẫn của tiến trình chính để tiến trình con import được cùng các module.
    # UTF-8 cho đường ống để tiếng Việt không bị sai trên hệ điều hành dùng bảng mã khác
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p),
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    script = os.path.join(REPO_DIR, "tg_workers.py")
    pending = queue.Queue()
    for i, job in enumerate(jobs):
        pending.put((i, {"kind": kind, **job}))
    stop = threading.Event()
    failures = []  # lỗi lúc khởi động tiến trình con, ví dụ không kết nối được tới Telegram

    def serve():
        try:
            proc = subprocess.Popen(
                [sys.executable, script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                env=env, text=True, encoding="utf-8",
            )
        except OSError as e:
            failures.append(_lost(f"Không chạy được tiến trình con: {e}"))
            return
        try:
            status, reply = _ask(proc, json.dumps(config, ensure_ascii=False), CONNECT_TIMEOUT)
            ready = _parse_reply(reply) if status == "ok" else {}
            if not ready.get("ready"):
                failures.append(ready or _lost("Không khởi động được tiến trình con. Xem cửa sổ terminal để biết chi tiết."))
                return
            while not stop.is_set():
                try:
                    i, job = pending.get_nowait()
                except queue.Empty:
                    return
                status, reply = _ask(proc, json.dumps(job, ensure_ascii=False), JOB_TIMEOUT)
                if status == "not_sent":  # việc chưa tới được tiến trình con: trả lại hàng đợi cho tiến trình khác
                    pending.put((i, job))
                    return
                if status == "ok":
                    results[i] = _parse_reply(reply) or _lost(
                        "Tiến trình con trả lời không hợp lệ, chưa rõ kết quả. Kiểm tra trong Telegram rồi thử lại.")
                    continue
                if status == "timeout":
                    results[i] = _lost(f"Quá {JOB_TIMEOUT // 60} phút không có phản hồi từ Telegram, "
                                       "chưa rõ kết quả. Kiểm tra trong Telegram rồi thử lại.")
                else:
                    results[i] = _lost("Tiến trình con dừng giữa chừng, chưa rõ kết quả. "
                                       "Kiểm tra trong Telegram rồi thử lại.")
                return  # tiến trình con đã hỏng: thoát, việc còn lại do tiến trình khác làm
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()

    threads = [threading.Thread(target=serve, daemon=True) for _ in range(min(workers, len(jobs)))]
    slot = st.empty()
    bar = slot.progress(0.0, text=label)
    try:
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            done = sum(r is not None for r in results)
            bar.progress(done / len(jobs), text=f"{label} ({done}/{len(jobs)})")
            time.sleep(0.2)
    finally:
        stop.set()  # không lấy thêm việc mới, việc đang chạy vẫn xong và ghi kết quả
        for t in threads:
            t.join()
        # Việc không chạy được vì tiến trình con không khởi động thì hiện đúng lỗi đó, không hiện thông báo chung
        fallback = failures[0] if failures else _UNFINISHED
        for i, r in enumerate(results):
            if r is None:
                results[i] = fallback
    slot.empty()

REMOTE_MESSAGES = {
    "ChatAdminRequiredError": "Telegram không cho xem danh sách thành viên của mục này.",
    "ChatWriteForbiddenError": "Bạn không có quyền gửi tin nhắn trong mục này.",
}

def remote_error_text(r):
    """Thông báo tiếng Việt cho lỗi xảy ra trong tiến trình con"""
    name = r.get("error", "")
    if name == "FloodWaitError":
        return f"Telegram tạm giới hạn yêu cầu. Vui lòng thử lại sau khoảng {r.get('seconds')} giây."
    if name == "WorkerError":
        return r.get("text", "")
    if name in REMOTE_MESSAGES:
        return REMOTE_MESSAGES[name]
    return f"Đã xảy ra lỗi: {r.get('text', '')}"

def normalize_phone(phone):
    """Số điện thoại dạng +84..., bỏ mọi ký tự không phải chữ số"""
    return "+" + re.sub(r"\D", "", phone or "")

def qr_session_name(phone):
    """Tên session tạm dùng cho lần đăng nhập QR của số này"""
    return f"session_qr_{normalize_phone(phone)}"

async def qr_begin(manager, api_id, api_hash, phone):
    """Tạo client tạm, kết nối và lấy mã QR đăng nhập"""
    await manager.disconnect()
    manager.client = TelegramClient(qr_session_name(phone), api_id, api_hash)
    await manager.client.connect()
    return await manager.client.qr_login()

async def qr_wait_loop(login):
    """Đợi người dùng quét mã. Mã hết hạn thì tạo mã mới và đợi tiếp, quá QR_TIMEOUT giây thì dừng"""
    deadline = time.monotonic() + QR_TIMEOUT
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise QRTimeout("Hết thời gian quét mã QR.")
        seconds = (login.expires - datetime.now(timezone.utc)).total_seconds()
        try:
            return await login.wait(timeout=max(min(seconds, left), 1))
        except asyncio.TimeoutError:
            await login.recreate()

async def current_user(manager):
    """Tài khoản đang đăng nhập trên client hiện tại"""
    return await manager.client.get_me()

async def logout_client(manager):
    """Thu hồi phiên tạm vừa tạo nhưng thuộc tài khoản khác. Trả về False nếu Telegram không thu hồi được"""
    return await manager.client.log_out()

def qr_html(url):
    """Trang HTML vẽ mã QR bằng thư viện đi kèm trong repo, không gửi token ra ngoài"""
    with open(QR_LIB, encoding="utf-8") as f:
        lib = f.read()
    return (
        '<div id="qr" style="display:flex;justify-content:center"></div>'
        f"<script>{lib}</script>"
        "<script>"
        "var qr = qrcode(0, 'M');"
        f"qr.addData({json.dumps(url)});"
        "qr.make();"
        "document.getElementById('qr').innerHTML = qr.createSvgTag({cellSize: 6, margin: 2});"
        "</script>"
    )

def start_qr_login(api_id, api_hash, phone):
    """Bắt đầu đăng nhập QR. Trả về thông báo lỗi, hoặc None nếu đã sẵn sàng hiển thị mã"""
    try:
        login = run_async(qr_begin(get_manager(), int(api_id), api_hash.strip(), phone))
    except Exception as e:
        return describe_error(e)
    st.session_state.api_id = int(api_id)
    st.session_state.api_hash = api_hash.strip()
    st.session_state.phone = phone
    st.session_state.qr_temp = qr_session_name(phone) + ".session"
    st.session_state.qr = {
        "login": login,
        "future": asyncio.run_coroutine_threadsafe(qr_wait_loop(login), get_async_loop()),
    }
    return None

def cancel_qr_login():
    """Hủy đăng nhập QR và xóa file session tạm"""
    qr = st.session_state.qr
    if qr:
        qr["future"].cancel()
    run_async(get_manager().disconnect())
    tmp = st.session_state.qr_temp
    if tmp and os.path.exists(tmp):
        os.remove(tmp)
    st.session_state.qr = None
    st.session_state.qr_temp = None

def promote_qr_session():
    """Chuyển phiên QR từ file tạm sang file của số đã nhập. Trả về lỗi nếu quét mã của tài khoản khác"""
    manager = get_manager()
    tmp = st.session_state.qr_temp
    phone = st.session_state.phone
    me = run_async(current_user(manager))
    if normalize_phone(me.phone) != phone:
        revoked = run_async(logout_client(manager))  # thu hồi phiên tạm, không để lại cho tài khoản khác
        run_async(manager.disconnect())  # đóng file session trước khi xóa (Windows không xóa được file đang mở)
        if os.path.exists(tmp):
            os.remove(tmp)
        st.session_state.qr_temp = None
        message = f"Mã QR này thuộc tài khoản {normalize_phone(me.phone)}. Hãy nhập đúng số điện thoại của tài khoản đó."
        if not revoked:
            message += " Không thu hồi được phiên đó trên Telegram, hãy vào Cài đặt → Thiết bị để kết thúc phiên."
        return message
    run_async(manager.disconnect())
    final = session_name(phone) + ".session"
    if os.path.exists(final):
        os.replace(final, final + ".bak")  # giữ lại bản cũ, không xóa
    os.replace(tmp, final)
    run_async(manager.create_client(st.session_state.api_id, st.session_state.api_hash, phone))
    st.session_state.qr_temp = None
    return None

def init_state():
    """Khởi tạo session state với giá trị mặc định"""
    defaults = {
        "stage": "login",          # login -> code -> 2fa -> dashboard
        "phone": "",
        "groups": None,            # None = chưa tải
        "selected_ids": set(),
        "posted_ids": set(),       # nhóm đã nhận tin nhắn khi rời nhưng chưa rời được, lần sau không gửi lại
        "table_version": 0,        # đổi giá trị để reset bảng chọn
        "last_result": None,
        "scan": None,              # kết quả quét theo tài khoản
        "scan_version": 0,         # đổi mỗi lần quét để reset ô chọn
        "qr": None,                # đăng nhập QR đang chờ quét: {"login", "future"}
        "qr_temp": None,           # file session tạm của phiên QR, chuyển sang file chính sau khi xác nhận
        "leave_nonce": 0,          # đếm số lần mở hộp xác nhận rời nhóm, để mỗi lần mở có ô nhập mới
        "login_notice": None,      # thông báo hiện ở màn hình đăng nhập
        "restored": False,         # đã thử khôi phục phiên trong lần tải trang này
        "api_id": None,            # thông tin đăng nhập đang dùng
        "api_hash": "",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    if not st.session_state.restored:
        st.session_state.restored = True
        restore_session()

def session_name(phone):
    """Tên session của Telethon. Telethon tự thêm đuôi .session khi tạo file"""
    return f"session_{phone}"

def load_account():
    """Đọc thông tin cơ bản đã lưu, hoặc None nếu chưa có hoặc file bị hỏng"""
    try:
        with open(ACCOUNT_FILE, encoding="utf-8") as f:
            account = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(account, dict):
        return None
    return account if {"phone", "session_file"} <= account.keys() else None

def save_account(phone, session_file):
    """Lưu thông tin cơ bản và tên file session. Phần nhạy cảm nằm trong chính file session"""
    with open(ACCOUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"phone": phone, "session_file": session_file}, f, ensure_ascii=False, indent=2)

def save_credentials(session_file, phone, api_id, api_hash):
    """Lưu API ID/Hash vào bảng riêng trong file session (SQLite). Telethon không dùng bảng này"""
    with closing(sqlite3.connect(session_file)) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS app_credentials ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), phone TEXT NOT NULL, "
            "api_id INTEGER NOT NULL, api_hash TEXT NOT NULL)"
        )
        con.execute(
            "INSERT OR REPLACE INTO app_credentials (id, phone, api_id, api_hash) VALUES (1, ?, ?, ?)",
            (phone, api_id, api_hash),
        )
        con.commit()

def load_credentials(session_file, phone):
    """Đọc API ID/Hash đã lưu. Chỉ trả về khi số điện thoại trong SQLite khớp với file JSON"""
    if not os.path.exists(session_file):
        return None
    try:
        with closing(sqlite3.connect(session_file)) as con:
            row = con.execute("SELECT phone, api_id, api_hash FROM app_credentials WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] != phone:
        return None
    return {"api_id": row[1], "api_hash": row[2]}

def forget_account():
    """Xóa thông tin đã lưu trên máy: file session (chứa khóa đăng nhập và API ID/Hash) và thông tin cơ bản"""
    account = load_account()
    if account is not None:
        for path in (account["session_file"], account["session_file"] + "-journal"):
            if os.path.exists(path):
                os.remove(path)
    if os.path.exists(ACCOUNT_FILE):
        os.remove(ACCOUNT_FILE)

def sign_out():
    """Đăng xuất: thu hồi phiên trên Telegram nếu được, rồi xóa file session và thông tin đã lưu trên máy"""
    revoked = False
    client = get_manager().client
    if client is not None and client.is_connected():
        try:
            revoked = run_async(client.log_out())
        except Exception:
            revoked = False
    reset_to_login()
    forget_account()
    if not revoked:
        st.session_state.login_notice = (
            "Chưa thu hồi được phiên trên Telegram. Hãy vào Cài đặt → Thiết bị trên Telegram để kết thúc phiên này."
        )

def enter_dashboard():
    """Lưu thông tin đăng nhập rồi chuyển sang màn hình quản lý. Trả về lỗi nếu không chuyển được"""
    phone = st.session_state.phone
    if st.session_state.qr_temp:
        error = promote_qr_session()
        if error:
            return error
    session_file = session_name(phone) + ".session"
    run_async(get_manager().save_session())
    save_credentials(session_file, phone, st.session_state.api_id, st.session_state.api_hash)
    save_account(phone, session_file)
    st.session_state.stage = "dashboard"
    return None

def restore_session():
    """Tự đăng nhập bằng thông tin đã lưu. Không gửi mã."""
    account = load_account()
    if account is None:
        return
    phone = account["phone"]
    st.session_state.login_phone = phone
    credentials = load_credentials(account["session_file"], phone)
    if credentials is None:
        st.session_state.login_notice = "Thiếu thông tin API đã lưu. Nhập API ID và API Hash để tiếp tục."
        return
    st.session_state.login_api_id = str(credentials["api_id"])
    st.session_state.login_api_hash = credentials["api_hash"]

    manager = get_manager()
    try:
        with st.spinner("Đang khôi phục phiên đăng nhập..."):
            run_async(manager.create_client(credentials["api_id"], credentials["api_hash"], phone))
            authorized = run_async(manager.is_authorized())
    except Exception as e:
        st.session_state.login_notice = f"Không khôi phục được phiên đăng nhập: {describe_error(e)}"
        return

    if authorized:
        st.session_state.phone = phone
        st.session_state.api_id = credentials["api_id"]
        st.session_state.api_hash = credentials["api_hash"]
        st.session_state.stage = "dashboard"
    else:
        run_async(manager.disconnect())
        st.session_state.login_notice = "Phiên đăng nhập không còn hiệu lực. Bấm Tiếp tục để nhận mã xác thực mới."

def mask_phone(phone):
    """Che bớt số điện thoại khi hiển thị, ví dụ +84•••••7025"""
    if len(phone) <= 7:
        return phone
    return phone[:3] + "•" * (len(phone) - 7) + phone[-4:]

def validate_login(api_id, api_hash, phone):
    """Kiểm tra form đăng nhập, trả về danh sách lỗi (rỗng nếu hợp lệ)"""
    errors = []
    if not api_id.strip().isdigit():
        errors.append("API ID chỉ gồm chữ số, ví dụ 1234567.")
    if not api_hash.strip():
        errors.append("Vui lòng nhập API Hash.")
    digits = phone[1:]
    if not (phone.startswith("+") and digits.isdigit() and 8 <= len(digits) <= 15):
        errors.append("Số điện thoại phải bắt đầu bằng dấu + và mã quốc gia, ví dụ +84912345678.")
    return errors

def start_login(api_id, api_hash, phone):
    """Kết nối Telegram và gửi mã nếu cần. Trả về thông báo lỗi, hoặc None nếu thành công"""
    manager = get_manager()
    try:
        run_async(manager.create_client(int(api_id), api_hash.strip(), phone))
        authorized = run_async(manager.login(phone))
    except Exception as e:
        return describe_error(e)
    st.session_state.api_id = int(api_id)
    st.session_state.api_hash = api_hash.strip()
    st.session_state.phone = phone
    if authorized:
        enter_dashboard()
    else:
        st.session_state.stage = "code"
    return None

def reset_to_login():
    """Ngắt kết nối và đưa về màn hình nhập thông tin"""
    if st.session_state.qr or st.session_state.qr_temp:
        cancel_qr_login()
    else:
        run_async(get_manager().disconnect())
    st.session_state.stage = "login"
    st.session_state.phone = ""
    st.session_state.groups = None
    st.session_state.selected_ids = set()
    st.session_state.last_result = None
    st.session_state.login_notice = None
    st.session_state.scan = None
    st.session_state.posted_ids = set()

def add_labels(groups):
    """Thêm nhãn hiển thị. Tên trùng nhau thì thêm @username hoặc ID để người dùng phân biệt được các mục"""
    name_count = Counter(g['title'].strip().lower() for g in groups)
    for g in groups:
        g['label'] = g['title']
        if name_count[g['title'].strip().lower()] > 1:
            ident = f"@{g['username']}" if g['username'] else f"ID {g['id']}"
            g['label'] = f"{g['title']} · {ident}"
    return sorted(groups, key=lambda x: (x['title'].lower(), x['id']))

def normalize_query(query):
    """Chuẩn hóa từ khóa: bỏ link t.me, dấu @ và khoảng trắng thừa"""
    q = query.strip().lower()
    q = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/", "", q)
    return q.strip("/").lstrip("@").strip()

def matches_query(g, q):
    """'ID 1234' chỉ khớp ID. Số thuần khớp ID, và cũng khớp tên hoặc username có chứa số đó. Chữ khớp theo tên hoặc username"""
    id_match = re.fullmatch(r"id\s*(\d+)", q)
    if id_match:
        return str(g["id"]) == id_match.group(1)
    if q.isdigit() and str(g["id"]) == q:
        return True
    return q in g["title"].lower() or (bool(g["username"]) and q in g["username"].lower())

def filter_groups(groups, query, kind):
    """Lọc theo loại, rồi theo từ khóa"""
    q = normalize_query(query)
    wanted = FILTER_TYPES[kind]
    return [
        g for g in groups
        if (wanted is None or g["type"] == wanted)
        and (not q or matches_query(g, q))
    ]

def is_username(name):
    """Chuỗi có dạng username Telegram: 5-32 ký tự, bắt đầu bằng chữ, gồm chữ, số và dấu gạch dưới"""
    return re.fullmatch(r"[a-z][a-z0-9_]{4,31}", name) is not None

def parse_usernames(text):
    """Tách danh sách username (cách nhau bằng dấu phẩy, khoảng trắng hoặc xuống dòng). Trả về (hợp lệ, không hợp lệ)"""
    names, invalid = [], []
    for raw in re.split(r"[,;\s]+", text):
        name = normalize_query(raw) if raw else ""
        if not name:
            continue
        if not is_username(name):
            invalid.append(raw)
        elif name not in names:
            names.append(name)
    return names, invalid

def scan_people(groups, names):
    """Tìm các nhóm, kênh mà những tài khoản này đang tham gia. Mỗi nhóm/kênh kiểm tra trong một tiến trình con"""
    client = get_manager().client
    users, unresolved = [], []
    for name in names:
        try:
            target = run_async(resolve_username(client, name))
        except ValueError:
            unresolved.append({"name": name, "reason": "Không tìm thấy trên Telegram."})
            continue
        except Exception as e:
            unresolved.append({"name": name, "reason": describe_error(e)})
            continue
        if isinstance(target, User):
            users.append(target)
        else:
            unresolved.append({"name": name, "reason": "Đây là nhóm hoặc kênh, không phải tài khoản người dùng."})

    rows, unchecked = [], []
    if users:
        jobs = [{"peer": g["peer"], "users": [user_spec(u) for u in users]} for g in groups]
        raw = [None] * len(jobs)
        run_in_workers("scan", jobs, SCAN_WORKERS, "Đang kiểm tra nhóm, kênh", raw)
        for g, r in zip(groups, raw):
            if not r.get("ok"):
                unchecked.append({"label": g["label"], "reason": remote_error_text(r)})
            else:
                # JSON đổi khóa số thành chuỗi: đổi lại thành số để so khớp với ID người dùng
                roles = {int(uid): role for uid, role in r["roles"].items()}
                if roles:
                    rows.append({"id": g["id"], "peer": g["peer"], "label": g["label"], "type": g["type"], "roles": roles})
    return {
        "users": [{"id": u.id, "display": u.username or str(u.id)} for u in users],
        "unresolved": unresolved,
        "groups": rows,
        "unchecked": unchecked,
    }

def matches_mode(row, users, mode):
    """Dòng có khớp với chế độ lọc: ít nhất một tài khoản, hoặc tất cả tài khoản"""
    found = sum(1 for u in users if u["id"] in row["roles"])
    return found >= 1 if mode == MODE_ANY else found == len(users)

def role_badge(role):
    """Nhãn vai trò trong bảng kết quả, '—' nếu tài khoản không tham gia"""
    badges = {"Chủ nhóm": "👑 Chủ nhóm", "Quản trị viên": "🛡️ Quản trị viên", "Thành viên": "👤 Thành viên"}
    return badges.get(role, "—")

def set_picks(rows, version, value):
    """Chọn hoặc bỏ chọn mọi dòng đang hiển thị (dùng làm callback của nút)"""
    for g in rows:
        st.session_state[f"pick_{version}_{g['id']}"] = value

def is_wait_error(r):
    """Telegram yêu cầu chờ rồi thử lại (FloodWaitError, SlowModeWaitError...). Đúng khi có số giây cần chờ"""
    return not r.get("ok") and r.get("error", "").endswith("WaitError") and int(r.get("seconds") or 0) > 0

def leave_error_text(r):
    """Thông báo lỗi khi rời nhóm/kênh. Giữ tên lỗi của Telegram để dễ tra cứu khi cần báo lỗi"""
    name = r.get("error", "")
    if is_wait_error(r):
        return f"Telegram tạm giới hạn yêu cầu. Vui lòng thử lại sau khoảng {r.get('seconds')} giây."
    if name == "ChatWriteForbiddenError":
        return "Bạn không có quyền gửi tin nhắn trong mục này."
    if name == "WorkerError":
        return r.get("text", "")
    return f"Đã xảy ra lỗi ({name}): {r.get('text', '')}"

def _result(item, ok, message, sent):
    return {"id": item["id"], "name": item["label"], "ok": ok, "message": message, "sent": sent}

def _success_text(r, sent_text):
    """Thông báo khi đã rời, hoặc coi như đã rời"""
    if r.get("already_out"):
        head = "Bạn không còn trong mục này, nên coi như đã rời"
    else:
        head = "Đã gửi tin nhắn, rồi rời" if sent_text else "Đã rời"
    if r.get("history_error"):
        return f"{head}, nhưng chưa xóa được lịch sử. {leave_error_text(r['history_error'])}"
    if r.get("history"):
        return f"{head} và xóa lịch sử chat."
    return f"{head}."

def _final_result(item, r, text, was_posted, delete_chat, tries, interrupted=False):
    """Kết quả cuối của một mục để hiện trên trang, sau một lần thử hoặc khi hết lượt thử.
    text là tin nhắn của lần rời này (None nếu không gửi). was_posted: tin nhắn đã gửi ở lần trước"""
    sent = bool(r.get("sent")) or was_posted
    if is_wait_error(r):
        if interrupted:
            message = f"Chưa rời, Telegram yêu cầu chờ {r['seconds']} giây. Thao tác bị ngắt, bấm rời lại để tiếp tục."
        else:
            message = f"Chưa rời, Telegram vẫn giới hạn sau {tries} lần thử. {leave_error_text(r)}"
        return _result(item, False, message, sent)
    if r.get("ok"):
        message = _success_text(r, sent and bool(text))
        if delete_chat and item["type"] == "Channel" and not r.get("already_out"):
            message += " Ứng dụng chỉ xóa lịch sử của nhóm thường, không xóa lịch sử kênh."
        return _result(item, True, message, sent)
    if r.get("error") == "WorkerError":  # chưa rõ việc đã làm tới đâu, thông báo đã nói rõ điều đó
        return _result(item, False, r.get("text", ""), sent)
    detail = leave_error_text(r)
    if text and sent:
        message = f"Đã gửi tin nhắn, nhưng không rời được. {detail}"
    elif text:
        message = f"Chưa rời, vì không gửi được tin nhắn. {detail}"
    else:
        message = detail
    return _result(item, False, message, sent)

def _interrupted_result(item, was_posted):
    return _result(item, False, "Chưa rời: thao tác bị ngắt trước khi làm. Bấm rời lại để tiếp tục.", was_posted)

def _wait_until(deadline, status, reason):
    """Chờ tới thời điểm deadline (theo time.monotonic), mỗi giây cập nhật số giây còn lại trên trang"""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        status.info(f"{reason} Còn {math.ceil(left)} giây.")
        time.sleep(min(1.0, left))

def _record_leave(results, posted):
    """Cập nhật lựa chọn, danh sách và kết quả. Gọi sau mỗi đợt và cả khi bị ngắt, để không mất kết quả đã có"""
    done_ids = {r["id"] for r in results if r["ok"]}
    st.session_state.selected_ids = st.session_state.selected_ids - done_ids
    st.session_state.posted_ids = {i for i in posted if i not in done_ids}
    if st.session_state.scan:
        # Nhóm đã rời không còn là thành viên, nên bỏ khỏi kết quả quét
        st.session_state.scan["groups"] = [g for g in st.session_state.scan["groups"] if g["id"] not in done_ids]
    st.session_state.last_result = results
    st.session_state.groups = None
    st.session_state.table_version += 1  # bảng mới, không giữ ô chọn cũ của các nhóm đã rời

def leave_items(items, delete_chat, text=None):
    """Rời nhóm/kênh theo đợt, mỗi đợt tối đa LEAVE_BATCH mục, mỗi mục chạy trong một tiến trình con.
    Khi Telegram yêu cầu chờ, tự chờ đúng số giây đó rồi thử lại các mục bị giới hạn, cho tới khi rời hết
    hoặc gặp lỗi không thể thử lại. Tin nhắn chỉ gửi một lần cho mỗi nhóm. Trả về kết quả theo đúng thứ tự của items"""
    posted = set(st.session_state.posted_ids)  # nhóm đã nhận tin nhắn ở lần trước thì không gửi lại
    pending = deque(items)
    tries = {item["id"]: 0 for item in items}
    final = {}  # id -> kết quả cuối cùng
    batch, raw = [], []  # đợt đang chạy, để ghi nhận kết quả nếu bị ngắt giữa chừng
    flood_until = 0.0  # thời điểm (time.monotonic) hết thời gian Telegram yêu cầu chờ
    prev_end = None  # thời điểm xong đợt trước
    status = st.empty()

    def ordered():
        return [final[item["id"]] for item in items if item["id"] in final]

    try:
        while pending:
            batch = [pending.popleft() for _ in range(min(LEAVE_BATCH, len(pending)))]
            raw = [None] * len(batch)
            deadline, reason = flood_until, "Telegram yêu cầu chờ, ứng dụng sẽ tự rời tiếp sau khi hết thời gian."
            if prev_end is not None and any(tries[item["id"]] == 0 for item in batch):
                if prev_end + LEAVE_PAUSE > deadline:
                    deadline, reason = prev_end + LEAVE_PAUSE, "Nghỉ giữa các đợt để tránh bị Telegram giới hạn."
            _wait_until(deadline, status, reason)
            status.info(
                f"Đang rời {len(batch)} mục. Đã xong {len(final)}/{len(items)}. "
                "Đừng bấm gì trên trang trong lúc chạy."
            )
            jobs = [{"peer": item["peer"], "delete": delete_chat,
                     "text": None if item["id"] in posted else text} for item in batch]
            run_in_workers("leave", jobs, LEAVE_WORKERS, "Đang rời nhóm, kênh", raw)
            for item, r in zip(batch, raw):
                tries[item["id"]] += 1
                if r.get("sent"):
                    posted.add(item["id"])  # tin nhắn đã đi rồi, các lần thử sau không gửi lại
                if is_wait_error(r) and tries[item["id"]] < LEAVE_MAX_ATTEMPTS:
                    flood_until = max(flood_until, time.monotonic() + int(r["seconds"]) + FLOOD_MARGIN)
                    pending.append(item)  # thử lại sau khi hết thời gian Telegram yêu cầu chờ
                    continue
                final[item["id"]] = _final_result(item, r, text, item["id"] in posted, delete_chat, tries[item["id"]])
            _record_leave(ordered(), posted)
            batch, raw = [], []
            prev_end = time.monotonic()
        status.empty()
    finally:
        # Bị ngắt (ví dụ người dùng tải lại trang) thì vẫn ghi nhận kết quả của đợt đang chạy.
        # Các mục còn lại được báo rõ là chưa rời, để người dùng biết cần bấm rời lại
        for item, r in zip(batch, raw):
            if item["id"] in final:
                continue
            if r is None:
                final[item["id"]] = _interrupted_result(item, item["id"] in posted)
            else:
                if r.get("sent"):
                    posted.add(item["id"])
                final[item["id"]] = _final_result(item, r, text, item["id"] in posted, delete_chat,
                                                  tries[item["id"]], interrupted=True)
        for item in pending:
            if item["id"] not in final:
                final[item["id"]] = _interrupted_result(item, item["id"] in posted)
        _record_leave(ordered(), posted)
    return ordered()

def open_leave_dialog(items, delete_chat):
    """Mở hộp xác nhận rời nhóm. Mỗi lần mở dùng bộ ô nhập mới, không giữ nội dung của lần trước"""
    st.session_state.leave_nonce += 1
    confirm_leave(items, delete_chat, st.session_state.leave_nonce)

@st.dialog("Xác nhận rời nhóm / kênh")
def confirm_leave(items, delete_chat, nonce):
    st.write(f"Bạn sắp rời **{len(items)}** mục:")
    for item in items:
        kind = "Kênh" if item["type"] == "Channel" else "Nhóm"
        st.markdown(f"- {item['label']} · {kind}")

    send_on = st.checkbox("Gửi tin nhắn trước khi rời (tùy chọn)", key=f"send_on_{nonce}")
    text, text_ok = "", True
    if send_on:
        text = st.text_area(
            "Nội dung tin nhắn",
            key=f"send_text_{nonce}",
            placeholder="Ví dụ: Mình rời nhóm, cảm ơn mọi người!",
        ).strip()
        if not text:
            text_ok = False
            st.caption("Nhập nội dung tin nhắn, hoặc bỏ tích gửi tin nhắn.")
        elif len(text) > MAX_MESSAGE_LEN:
            text_ok = False
            st.caption(f"Tin nhắn dài tối đa {MAX_MESSAGE_LEN} ký tự (hiện {len(text)}).")
        else:
            st.caption(f"Tin nhắn sẽ gửi tới {len(items)} mục:")
            st.markdown("> " + text.replace("\n", "\n> "))

    understood = True
    if delete_chat:
        st.warning("Lịch sử chat sẽ bị xóa khỏi tài khoản của bạn.")
        understood = st.checkbox("Tôi hiểu và muốn xóa lịch sử chat", key=f"understood_{nonce}")
    st.caption("Sau khi rời, bạn có thể cần được mời lại để tham gia.")
    st.caption(
        f"Rời theo đợt tối đa {LEAVE_BATCH} mục, nghỉ {LEAVE_PAUSE} giây giữa các đợt. "
        "Nếu Telegram yêu cầu chờ, ứng dụng tự chờ đúng số giây đó rồi rời tiếp cho tới khi xong. "
        "Giữ trang mở trong lúc chạy, tải lại trang sẽ dừng việc đang chạy."
    )

    cancel_col, confirm_col = st.columns(2)
    if cancel_col.button("Hủy", width="stretch"):
        st.rerun()
    if confirm_col.button(
        "Rời và xóa lịch sử" if delete_chat else "Rời nhóm / kênh",
        type="primary",
        disabled=not (understood and text_ok),
        width="stretch",
    ):
        leave_items(items, delete_chat, text if send_on else None)
        st.rerun()

def render_sidebar():
    with st.sidebar:
        st.markdown("### 🔐 Tài khoản")
        if st.session_state.stage == "dashboard":
            st.success("Đã kết nối")
            st.write(f"📞 {mask_phone(st.session_state.phone)}")
            if st.button("🔄 Tải lại danh sách", width="stretch"):
                st.session_state.groups = None
                st.rerun()
            if st.button("🚪 Đăng xuất", width="stretch"):
                sign_out()
                st.rerun()
            st.caption("Lần sau mở app sẽ tự đăng nhập, không cần mã xác thực. Đăng xuất sẽ xóa thông tin đã lưu trên máy.")
        else:
            st.info("Chưa đăng nhập")

        st.divider()
        st.markdown("#### 🔒 Quyền riêng tư")
        st.caption(
            "Thông tin cơ bản nằm trong `account.json`. Phiên đăng nhập và API ID/Hash "
            "nằm trong file `session_<số điện thoại>.session` (SQLite) ngay trên máy này. "
            "Ứng dụng chỉ kết nối tới máy chủ Telegram."
        )

def render_steps():
    current = STEP_BY_STAGE[st.session_state.stage]
    parts = []
    for i, label in enumerate(STEP_LABELS):
        if i < current:
            parts.append(f"✅ {label}")
        elif i == current:
            parts.append(f"**🔵 {label}**")
        else:
            parts.append(f"⚪ {label}")
    st.markdown("  →  ".join(parts))

@st.fragment(run_every=2)
def render_qr_box():
    """Hiện mã QR và kiểm tra trạng thái quét mỗi 2 giây"""
    qr = st.session_state.qr
    if qr is None:
        return
    future = qr["future"]
    if not future.done():
        st.subheader("Quét mã QR bằng Telegram")
        components.html(qr_html(qr["login"].url), height=230)
        st.caption(
            "1. Mở Telegram trên điện thoại. 2. Vào Cài đặt → Thiết bị → Liên kết thiết bị máy tính. "
            "3. Quét mã này. Mã tự làm mới khi hết hạn."
        )
        if st.button("Hủy đăng nhập QR"):
            cancel_qr_login()
            st.rerun()
        return

    st.session_state.qr = None
    try:
        future.result()
    except SessionPasswordNeededError:
        st.session_state.stage = "2fa"
        st.rerun()
    except Exception as e:
        cancel_qr_login()
        st.session_state.login_notice = f"Đăng nhập QR không thành công: {describe_error(e)}"
        st.rerun()
    error = enter_dashboard()
    if error:
        st.session_state.login_notice = error
    st.rerun()

def render_login():
    st.subheader("Kết nối tài khoản Telegram")
    st.caption("Cần API ID và API Hash miễn phí từ my.telegram.org.")
    with st.expander("Chưa có API ID và API Hash? Xem hướng dẫn"):
        st.markdown(
            "1. Mở [my.telegram.org](https://my.telegram.org) và đăng nhập bằng số điện thoại của bạn.\n"
            "2. Chọn **API development tools** và tạo một ứng dụng (tên và mô tả tùy ý).\n"
            "3. Sao chép **App api_id** và **App api_hash** vào form bên dưới."
        )

    if st.session_state.login_notice:
        st.info(st.session_state.login_notice)

    if st.session_state.qr:
        render_qr_box()
        return

    with st.form("login_form"):
        api_id = st.text_input("API ID", placeholder="Ví dụ: 1234567", key="login_api_id")
        api_hash = st.text_input("API Hash", type="password", key="login_api_hash")
        phone = st.text_input(
            "Số điện thoại",
            placeholder="+84912345678",
            help="Nhập đầy đủ mã quốc gia, bắt đầu bằng dấu +.",
            key="login_phone",
        )
        code_col, qr_col = st.columns(2)
        code_clicked = code_col.form_submit_button("Tiếp tục", type="primary", width="stretch")
        qr_clicked = qr_col.form_submit_button("📷 Đăng nhập bằng QR", width="stretch")

    if code_clicked or qr_clicked:
        st.session_state.login_notice = None
        phone = phone.strip().replace(" ", "")
        errors = validate_login(api_id, api_hash, phone)
        if errors:
            for error in errors:
                st.error(error)
            return
        if code_clicked:
            with st.spinner("Đang kết nối và gửi mã..."):
                error = start_login(api_id, api_hash, phone)
        else:
            with st.spinner("Đang tạo mã QR..."):
                error = start_qr_login(api_id, api_hash, phone)
        if error:
            st.error(error)
        else:
            st.rerun()

def render_code():
    manager = get_manager()
    phone = st.session_state.phone
    st.subheader("Nhập mã xác thực")
    st.info(
        f"Telegram đã gửi mã tới **{mask_phone(phone)}**. "
        "Mã thường hiện trong ứng dụng Telegram trên điện thoại hoặc qua SMS."
    )

    with st.form("code_form"):
        code = st.text_input("Mã xác thực", placeholder="Ví dụ: 12345", max_chars=6)
        submitted = st.form_submit_button("Xác nhận", type="primary", width="stretch")

    if submitted:
        code = code.strip()
        if not code.isdigit():
            st.error("Mã xác thực chỉ gồm chữ số.")
        else:
            try:
                with st.spinner("Đang xác thực..."):
                    needs_2fa = run_async(manager.verify_code(phone, code))
            except Exception as e:
                st.error(describe_error(e))
            else:
                if needs_2fa:
                    st.session_state.stage = "2fa"
                else:
                    enter_dashboard()
                st.rerun()

    resend_col, change_col = st.columns(2)
    if resend_col.button("Gửi lại mã", width="stretch"):
        try:
            run_async(manager.send_code(phone))
            st.toast("Đã gửi lại mã. Kiểm tra Telegram hoặc tin nhắn SMS.")
        except Exception as e:
            st.error(describe_error(e))
    if change_col.button("Đổi số điện thoại", width="stretch"):
        reset_to_login()
        st.rerun()

def render_2fa():
    st.subheader("Xác minh 2 bước")
    st.info(
        "Tài khoản này đã bật xác minh 2 bước. "
        "Nhập mật khẩu bạn tự đặt trong Telegram, không phải mã xác thực."
    )

    with st.form("2fa_form"):
        password = st.text_input("Mật khẩu 2FA", type="password")
        submitted = st.form_submit_button("Đăng nhập", type="primary", width="stretch")

    if submitted:
        if not password:
            st.error("Vui lòng nhập mật khẩu 2FA.")
        else:
            try:
                with st.spinner("Đang đăng nhập..."):
                    run_async(get_manager().verify_2fa(password))
            except Exception as e:
                st.error(describe_error(e))
            else:
                error = enter_dashboard()
                if error:
                    st.error(error)
                else:
                    st.rerun()

    if st.button("Quay lại từ đầu"):
        reset_to_login()
        st.rerun()

def render_last_result():
    results = st.session_state.last_result
    failed = [r for r in results if not r["ok"]]
    with st.container(border=True):
        if failed:
            st.warning(
                f"Đã rời {len(results) - len(failed)}/{len(results)} mục, còn {len(failed)} mục chưa rời. "
                "Xem lý do bên dưới. Các mục chưa rời vẫn được giữ lựa chọn để bạn thử lại."
            )
        else:
            st.success(f"Đã rời thành công {len(results)}/{len(results)} mục.")
        for r in results:
            st.markdown(f"{'✅' if r['ok'] else '❌'} **{r['name']}**: {r['message']}")
        if st.button("Đóng thông báo"):
            st.session_state.last_result = None
            st.rerun()

def render_list_tab(groups):
    """Tab nhóm & kênh: tìm, lọc, chọn và rời nhóm/kênh của bạn"""
    search_col, filter_col = st.columns([2, 1])
    query = search_col.text_input(
        "Tìm kiếm",
        placeholder="Tên, @username, link t.me/... hoặc ID (nhấn Enter để tìm)",
        label_visibility="collapsed",
    )
    kind = filter_col.radio(
        "Loại", list(FILTER_TYPES), horizontal=True, label_visibility="collapsed"
    )

    visible = filter_groups(groups, query, kind)
    visible_ids = {g["id"] for g in visible}
    st.caption(f"Hiển thị {len(visible)} / {len(groups)} nhóm, kênh")

    select_all_col, clear_col, _ = st.columns([2, 2, 3])
    if select_all_col.button(f"☑️ Chọn tất cả ({len(visible)})", disabled=not visible, width="stretch"):
        st.session_state.selected_ids = st.session_state.selected_ids | visible_ids
        st.session_state.table_version += 1
        st.rerun()
    if clear_col.button("Bỏ chọn tất cả", disabled=not st.session_state.selected_ids, width="stretch"):
        st.session_state.selected_ids = set()
        st.session_state.table_version += 1
        st.rerun()

    if visible:
        table = pd.DataFrame([{
            "id": g["id"],
            "Chọn": g["id"] in st.session_state.selected_ids,
            "Tên": g["label"],
            "Loại": "📢 Kênh" if g["type"] == "Channel" else "👥 Nhóm",
            "Thành viên": g["participants"],
            "Username": f"https://t.me/{g['username']}" if g["username"] else "",
            "Tin nhắn gần nhất": g["date"],
        } for g in visible])
        edited = st.data_editor(
            table,
            key=f"table_{st.session_state.table_version}_{kind}_{query}",
            column_config={
                "id": None,
                "Chọn": st.column_config.CheckboxColumn(width="small"),
                "Tên": st.column_config.TextColumn("Tên nhóm / kênh", width="large"),
                "Loại": st.column_config.TextColumn(width="small"),
                "Thành viên": st.column_config.NumberColumn(format="localized"),
                "Username": st.column_config.LinkColumn(display_text=r"https://t\.me/(.+)"),
                "Tin nhắn gần nhất": st.column_config.TextColumn(width="medium"),
            },
            disabled=["Tên", "Loại", "Thành viên", "Username", "Tin nhắn gần nhất"],
            hide_index=True,
            num_rows="fixed",
            width="stretch",
        )
        # Giữ lại lựa chọn của các mục đang bị ẩn bởi bộ lọc/tìm kiếm
        picked = {int(i) for i, checked in zip(edited["id"], edited["Chọn"]) if checked}
        st.session_state.selected_ids = (st.session_state.selected_ids - visible_ids) | picked
    else:
        if not normalize_query(query):
            st.info(f"Không có nhóm hoặc kênh nào trong mục {kind}.")
        elif filter_groups(groups, query, "Tất cả"):
            st.info(f"Không có kết quả trong mục {kind}. Chọn “Tất cả” để xem kết quả khác.")
        else:
            st.info("Không tìm thấy nhóm hoặc kênh nào khớp. Kiểm tra lại tên, @username hoặc ID.")
            if is_username(normalize_query(query)):
                st.caption("Muốn tìm nhóm, kênh mà một hay nhiều tài khoản đang tham gia? Mở tab “👤 Tìm theo tài khoản”.")

    chosen = [g for g in groups if g["id"] in st.session_state.selected_ids]
    with st.container(border=True):
        if chosen:
            st.markdown(f"**Đã chọn {len(chosen)} mục.** Bạn muốn làm gì với các mục này?")
        else:
            st.caption("Tích chọn các nhóm hoặc kênh muốn rời ở bảng trên, rồi chọn hành động.")
        leave_col, delete_col, _ = st.columns([2, 2, 3])
        if leave_col.button("🚪 Rời, giữ lịch sử chat", disabled=not chosen, width="stretch"):
            open_leave_dialog(chosen, delete_chat=False)
        if delete_col.button(
            "🗑️ Rời và xóa lịch sử chat", type="primary", disabled=not chosen, width="stretch"
        ):
            open_leave_dialog(chosen, delete_chat=True)

    st.caption("⚠️ Rời nhóm hoặc kênh có thể khiến bạn phải được mời lại để tham gia.")

def render_scan_result(scan):
    """Kết quả quét: mỗi dòng là một nhóm/kênh, mỗi cột là một tài khoản. Tích dòng nào thì rời dòng đó"""
    version = st.session_state.scan_version
    for u in scan["unresolved"]:
        st.warning(f"@{u['name']}: {u['reason']}")
    users = scan["users"]
    if not users:
        return

    st.markdown("Đang kiểm tra: " + ", ".join(f"**@{u['display']}**" for u in users))
    mode = st.radio(
        "Hiển thị", [MODE_ANY, MODE_ALL], horizontal=True,
        key=f"mode_{version}", label_visibility="collapsed",
    )
    rows = [g for g in scan["groups"] if matches_mode(g, users, mode)]
    status = st.empty()

    widths = [1, 5] + [3] * len(users)
    head = st.columns(widths, vertical_alignment="center")
    head[1].markdown("**Nhóm / kênh**")
    for col, u in zip(head[2:], users):
        col.markdown(f"**@{u['display']}**")
    if not rows:
        st.info("Không có nhóm, kênh nào khớp với lựa chọn này.")

    box = st.container(height=420) if len(rows) > 8 else st.container()
    with box:
        for g in rows:
            cols = st.columns(widths, vertical_alignment="center")
            cols[0].checkbox(
                f"Chọn {g['label']}", key=f"pick_{version}_{g['id']}", label_visibility="collapsed"
            )
            cols[1].markdown(g["label"])
            for col, u in zip(cols[2:], users):
                col.markdown(role_badge(g["roles"].get(u["id"])))

    picked = [g for g in rows if st.session_state.get(f"pick_{version}_{g['id']}")]
    status.markdown(f"Tìm thấy **{len(rows)}** nhóm, kênh. Đã chọn **{len(picked)}** mục.")

    all_col, none_col, _ = st.columns([2, 2, 4])
    all_col.button(
        "Chọn tất cả", key=f"all_{version}", disabled=not rows, width="stretch",
        on_click=set_picks, args=(rows, version, True),
    )
    none_col.button(
        "Bỏ chọn", key=f"none_{version}", disabled=not picked, width="stretch",
        on_click=set_picks, args=(rows, version, False),
    )
    leave_col, delete_col, _ = st.columns([2, 2, 3])
    if leave_col.button(
        f"🚪 Rời {len(picked)} mục (giữ lịch sử)", key=f"leave_{version}", disabled=not picked, width="stretch"
    ):
        open_leave_dialog(picked, delete_chat=False)
    if delete_col.button(
        f"🗑️ Rời và xóa lịch sử {len(picked)} mục", key=f"delete_{version}", type="primary",
        disabled=not picked, width="stretch",
    ):
        open_leave_dialog(picked, delete_chat=True)

    if scan["unchecked"]:
        with st.expander(f"Không kiểm tra được {len(scan['unchecked'])} mục"):
            for u in scan["unchecked"]:
                st.markdown(f"- {u['label']}: {u['reason']}")
    if st.button("Xóa kết quả quét", key=f"clear_{version}"):
        st.session_state.scan = None
        st.rerun()

def render_people_tab(groups):
    """Tab tìm theo tài khoản: quét nhiều username rồi chọn các nhóm, kênh để rời"""
    st.caption(
        f"Nhập tối đa {MAX_PEOPLE} username, cách nhau bằng dấu phẩy hoặc xuống dòng. "
        "Ứng dụng kiểm tra từng nhóm, kênh để xem các tài khoản này có đang tham gia không."
    )
    with st.form("people_form"):
        text = st.text_area("Username", placeholder="Ví dụ: @KienTDT, @user2", label_visibility="collapsed")
        submitted = st.form_submit_button("🔎 Quét nhóm, kênh", type="primary")

    if submitted:
        names, invalid = parse_usernames(text)
        if invalid:
            st.error("Không phải username hợp lệ: " + ", ".join(invalid))
        elif not names:
            st.error("Nhập ít nhất một username.")
        elif len(names) > MAX_PEOPLE:
            st.error(f"Mỗi lần chỉ quét tối đa {MAX_PEOPLE} tài khoản.")
        else:
            st.session_state.scan = scan_people(groups, names)
            st.session_state.scan_version += 1

    if st.session_state.scan:
        render_scan_result(st.session_state.scan)

def render_dashboard():
    if st.session_state.groups is None:
        with st.spinner("Đang tải danh sách nhóm và kênh..."):
            try:
                st.session_state.groups = add_labels(run_async(get_manager().get_groups_and_channels()))
            except Exception as e:
                st.error(describe_error(e))
                if st.button("Thử lại"):
                    st.rerun()
                return
    groups = st.session_state.groups

    c1, c2, c3 = st.columns(3)
    c1.metric("👥 Nhóm", sum(g["type"] == "Group" for g in groups))
    c2.metric("📢 Kênh", sum(g["type"] == "Channel" for g in groups))
    c3.metric("📱 Tổng cộng", len(groups))

    if st.session_state.last_result:
        render_last_result()

    if not groups:
        st.info("Tài khoản này chưa tham gia nhóm hoặc kênh nào.")
        return

    # key giữ tab đang chọn khi thêm thẻ kết quả phía trên làm thay đổi cấu trúc trang
    tab_list, tab_people = st.tabs(["📋 Nhóm & kênh", "👤 Tìm theo tài khoản"], key="main_tabs")
    with tab_list:
        render_list_tab(groups)
    with tab_people:
        render_people_tab(groups)

init_state()
st.title("📱 Quản lý nhóm & kênh Telegram")
st.caption("Xem và rời nhiều nhóm, kênh Telegram cùng lúc.")
render_sidebar()
render_steps()

stage = st.session_state.stage
if stage == "dashboard":
    render_dashboard()
else:
    _, center, _ = st.columns([1, 2, 1])
    with center:
        {"login": render_login, "code": render_code, "2fa": render_2fa}[stage]()
