"""Logic Telegram của ứng dụng, không phụ thuộc giao diện.

Gồm đăng nhập và lưu phiên, rời nhóm/kênh theo đợt, quét thành viên và tìm kiếm. Giao diện (gui.py)
chỉ gọi các hàm ở đây rồi hiển thị kết quả. Thao tác với Telegram chạy trên một event loop nền dùng chung;
mỗi tiến trình con (tg_workers.py) có một kết nối riêng để rời nhóm và quét thành viên song song.
"""
import asyncio
import json
import math
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from contextlib import closing
from datetime import datetime, timezone

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
FROZEN = getattr(sys, "frozen", False)  # True khi chạy từ file .exe đã đóng gói
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # tiến trình con không mở cửa sổ console trên Windows

ACCOUNT_FILE = "account.json"  # thông tin cơ bản (không nhạy cảm)
QR_SESSION = "session_qr"  # phiên tạm của đăng nhập QR, đổi thành file theo số điện thoại sau khi quét xong
QR_SESSION_FILE = QR_SESSION + ".session"
QR_TIMEOUT = 300  # giây chờ quét QR trước khi hủy

FILTER_TYPES = {"Tất cả": None, "Nhóm": "Group", "Kênh": "Channel"}
MODE_ANY = "Có ít nhất một tài khoản"
MODE_ALL = "Có tất cả tài khoản"
MAX_PEOPLE = 5  # số tài khoản tối đa trong một lần quét
MAX_MESSAGE_LEN = 4096  # giới hạn độ dài một tin nhắn của Telegram


def _env_int(name, default):
    """Đọc số nguyên không âm từ biến môi trường. Giá trị sai thì dùng mặc định thay vì làm app không chạy"""
    try:
        return max(0, int(os.environ[name]))
    except (KeyError, ValueError):
        return default


LEAVE_WORKERS = 3  # số tiến trình con rời nhóm/kênh cùng lúc, mỗi tiến trình một kết nối Telegram
LEAVE_BATCH = 9  # số nhóm/kênh rời trong một đợt
LEAVE_PAUSE = _env_int("LEAVE_PAUSE_SECONDS", 130)  # giây nghỉ giữa hai đợt (đặt biến môi trường để thử nhanh)
LEAVE_MAX_ATTEMPTS = 50  # số lần thử tối đa cho một mục khi Telegram liên tục yêu cầu chờ
FLOOD_MARGIN = 2  # giây cộng thêm vào thời gian Telegram yêu cầu chờ, để chắc chắn đã hết giới hạn
SCAN_WORKERS = 4  # số tiến trình con kiểm tra thành viên cùng lúc
CONNECT_TIMEOUT = 120  # giây tối đa chờ một tiến trình con kết nối được tới Telegram
JOB_TIMEOUT = 600  # giây tối đa chờ một việc trong tiến trình con, quá thì coi như tiến trình con bị treo


# Event loop nền dùng chung. Telethon gắn client vào event loop lúc connect nên mọi thao tác sau đó
# phải chạy trên đúng loop này. asyncio.run() tạo loop mới mỗi lần nên không dùng được
_loop = None
_loop_lock = threading.Lock()


def _event_loop():
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True).start()
        return _loop


def submit(coro):
    """Đặt coroutine lên event loop nền, trả về concurrent.futures.Future để theo dõi kết quả"""
    return asyncio.run_coroutine_threadsafe(coro, _event_loop())


def run_async(coro):
    """Chạy coroutine trên event loop nền và chờ kết quả. Gọi từ luồng nền, không gọi từ luồng giao diện"""
    return submit(coro).result()


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
    """Một kết nối Telegram dùng chung cho cả ứng dụng. Mọi phương thức async chạy trên event loop nền"""

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
                "id": entity.id,
                "peer": peer,  # định danh để tiến trình con gọi Telegram, lấy sẵn từ danh sách hội thoại
                "title": entity.title,
                "type": item_type,
                "participants": getattr(entity, "participants_count", None),
                "username": getattr(entity, "username", "") or "",
                "date": dialog.date.astimezone().strftime("%d/%m/%Y %H:%M") if dialog.date else "",
            })

        return sorted(groups_channels, key=lambda x: x["title"].lower())


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


_workers = set()  # tiến trình con đang chạy, để khi đóng ứng dụng có thể dừng hết


def kill_workers():
    """Dừng mọi tiến trình con đang chạy, dùng khi đóng ứng dụng"""
    for proc in list(_workers):
        try:
            proc.kill()
        except OSError:
            pass


def run_in_workers(manager, kind, jobs, workers, results, on_progress=None, halt=None):
    """Chạy các việc trong nhiều tiến trình con. Mỗi tiến trình con là một tiến trình Python riêng,
    có một kết nối Telegram riêng. Kết quả ghi vào results theo đúng thứ tự của jobs.
    Khi halt được đặt, không lấy thêm việc mới nhưng các việc đang chạy vẫn được chờ xong và ghi kết quả.
    on_progress(done, total) được gọi định kỳ từ luồng gọi"""
    client = manager.client
    config = {
        "client": f"{type(client).__module__}:{type(client).__qualname__}",
        "session": session_snapshot(client),
        "api_id": client.api_id,
        "api_hash": client.api_hash,
    }
    # UTF-8 cho đường ống để tiếng Việt không bị sai trên hệ điều hành dùng bảng mã khác
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if FROZEN:
        # Đã đóng gói thì sys.executable là chính file .exe, nên tiến trình con chạy lại nó với --tg-worker
        cmd = [sys.executable, "--tg-worker"]
    else:
        # PYTHONPATH giữ nguyên đường dẫn của tiến trình chính để tiến trình con import được cùng các module
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        cmd = [sys.executable, os.path.join(REPO_DIR, "tg_workers.py")]
    pending = queue.Queue()
    for i, job in enumerate(jobs):
        pending.put((i, {"kind": kind, **job}))
    stop = threading.Event()  # đặt khi xong việc, để các luồng không lấy thêm việc nữa
    failures = []  # lỗi lúc khởi động tiến trình con, ví dụ không kết nối được tới Telegram

    def taking_jobs():
        return not (stop.is_set() or (halt is not None and halt.is_set()))

    def serve():
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                env=env, text=True, encoding="utf-8", creationflags=NO_WINDOW,
            )
        except OSError as e:
            failures.append(_lost(f"Không chạy được tiến trình con: {e}"))
            return
        _workers.add(proc)
        try:
            status, reply = _ask(proc, json.dumps(config, ensure_ascii=False), CONNECT_TIMEOUT)
            ready = _parse_reply(reply) if status == "ok" else {}
            if not ready.get("ready"):
                failures.append(ready or _lost("Không khởi động được tiến trình con. Xem file TelegramManager.log để biết chi tiết."))
                return
            while taking_jobs():
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
            _workers.discard(proc)
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()

    threads = [threading.Thread(target=serve, daemon=True) for _ in range(min(workers, len(jobs)))]
    try:
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            if on_progress is not None:
                on_progress(sum(r is not None for r in results), len(jobs))
            time.sleep(0.2)
    finally:
        stop.set()
        for t in threads:
            t.join()
        # Việc không chạy được vì tiến trình con không khởi động thì hiện đúng lỗi đó, không hiện thông báo chung
        fallback = failures[0] if failures else _UNFINISHED
        for i, r in enumerate(results):
            if r is None:
                results[i] = fallback


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


def session_name(phone):
    """Tên session của Telethon. Telethon tự thêm đuôi .session khi tạo file"""
    return f"session_{phone}"


def mask_phone(phone):
    """Che bớt số điện thoại khi hiển thị, ví dụ +84•••••7025"""
    if len(phone) <= 7:
        return phone
    return phone[:3] + "•" * (len(phone) - 7) + phone[-4:]


# --- Đăng nhập QR ---

async def qr_begin(manager, api_id, api_hash):
    """Tạo client tạm, kết nối và lấy mã QR đăng nhập. Xóa phiên tạm còn sót lại (ví dụ app bị tắt giữa chừng) để không dùng lại khóa cũ"""
    await manager.disconnect()
    if os.path.exists(QR_SESSION_FILE):
        os.remove(QR_SESSION_FILE)
    manager.client = TelegramClient(QR_SESSION, api_id, api_hash)
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


def start_qr(manager, api_id, api_hash):
    """Bắt đầu đăng nhập QR (chặn tới khi có mã). Không cần số điện thoại vì số của tài khoản lấy từ mã vừa quét"""
    return run_async(qr_begin(manager, int(api_id), api_hash.strip()))


def wait_qr(login):
    """Đợi quét mã ở nền. Trả về future: kết quả là tài khoản đã quét, hoặc ném lỗi (QRTimeout, SessionPasswordNeededError...)"""
    return submit(qr_wait_loop(login))


def cancel_qr(manager, future, temp_file):
    """Hủy đăng nhập QR, ngắt kết nối và xóa file session tạm"""
    if future is not None:
        future.cancel()
    run_async(manager.disconnect())
    if temp_file and os.path.exists(temp_file):
        os.remove(temp_file)


def promote_qr_session(manager, api_id, api_hash, temp_file):
    """Chuyển phiên QR sang file đặt theo số điện thoại của tài khoản vừa quét. Trả về số điện thoại đó"""
    phone = normalize_phone(run_async(current_user(manager)).phone)
    run_async(manager.disconnect())  # đóng file session trước khi đổi tên (Windows không đổi tên được file đang mở)
    final = session_name(phone) + ".session"
    if os.path.exists(final):
        os.replace(final, final + ".bak")  # giữ lại bản cũ, không xóa
    os.replace(temp_file, final)
    run_async(manager.create_client(api_id, api_hash, phone))
    return phone


# --- Lưu thông tin đăng nhập trên máy ---

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


def save_login(manager, phone, api_id, api_hash):
    """Lưu thông tin đăng nhập để lần sau tự vào được: phiên, API ID/Hash trong SQLite, số trong account.json"""
    session_file = session_name(phone) + ".session"
    run_async(manager.save_session())
    save_credentials(session_file, phone, api_id, api_hash)
    save_account(phone, session_file)


def restore_session(manager):
    """Tự đăng nhập bằng thông tin đã lưu, không gửi mã. Trả về dict: authorized, phone, api_id, api_hash, notice.
    phone và api_id/api_hash dùng để điền sẵn form khi cần nhập lại"""
    account = load_account()
    if account is None:
        return {"authorized": False, "phone": "", "api_id": None, "api_hash": "", "notice": None}
    phone = account["phone"]
    credentials = load_credentials(account["session_file"], phone)
    if credentials is None:
        return {"authorized": False, "phone": phone, "api_id": None, "api_hash": "",
                "notice": "Thiếu thông tin API đã lưu. Nhập API ID và API Hash để tiếp tục."}
    known = {"phone": phone, "api_id": credentials["api_id"], "api_hash": credentials["api_hash"]}
    try:
        run_async(manager.create_client(credentials["api_id"], credentials["api_hash"], phone))
        authorized = run_async(manager.is_authorized())
    except Exception as e:
        return {**known, "authorized": False, "notice": f"Không khôi phục được phiên đăng nhập: {describe_error(e)}"}
    if not authorized:
        run_async(manager.disconnect())
        return {**known, "authorized": False,
                "notice": "Phiên đăng nhập không còn hiệu lực. Bấm Tiếp tục để nhận mã xác thực mới."}
    return {**known, "authorized": True, "notice": None}


def sign_out(manager):
    """Thu hồi phiên trên Telegram nếu được, rồi xóa file session và thông tin đã lưu trên máy.
    Trả về False nếu không thu hồi được phiên trên Telegram"""
    revoked = False
    client = manager.client
    if client is not None and client.is_connected():
        try:
            revoked = run_async(client.log_out())
        except Exception:
            revoked = False
    run_async(manager.disconnect())
    forget_account()
    return revoked


def validate_login(api_id, api_hash, phone=None):
    """Kiểm tra form đăng nhập, trả về danh sách lỗi (rỗng nếu hợp lệ). Đăng nhập QR không cần số điện thoại nên không truyền phone"""
    errors = []
    if not api_id.strip().isdecimal():
        errors.append("API ID chỉ gồm chữ số, ví dụ 1234567.")
    if not api_hash.strip():
        errors.append("Vui lòng nhập API Hash.")
    if phone is not None:
        digits = phone[1:]
        if not (phone.startswith("+") and digits.isdigit() and 8 <= len(digits) <= 15):
            errors.append("Số điện thoại phải bắt đầu bằng dấu + và mã quốc gia, ví dụ +84912345678.")
    return errors


def start_login(manager, api_id, api_hash, phone):
    """Kết nối Telegram và gửi mã nếu cần. Trả về True nếu đã đăng nhập sẵn (không cần mã)"""
    run_async(manager.create_client(int(api_id), api_hash.strip(), phone))
    return run_async(manager.login(phone))


# --- Nhóm và kênh ---

def add_labels(groups):
    """Thêm nhãn hiển thị. Tên trùng nhau thì thêm @username hoặc ID để người dùng phân biệt được các mục"""
    name_count = Counter(g["title"].strip().lower() for g in groups)
    for g in groups:
        g["label"] = g["title"]
        if name_count[g["title"].strip().lower()] > 1:
            ident = f"@{g['username']}" if g["username"] else f"ID {g['id']}"
            g["label"] = f"{g['title']} · {ident}"
    return sorted(groups, key=lambda x: (x["title"].lower(), x["id"]))


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


def scan_people(manager, groups, names, on_progress=None):
    """Tìm các nhóm, kênh mà những tài khoản này đang tham gia. Mỗi nhóm/kênh kiểm tra trong một tiến trình con"""
    client = manager.client
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
        run_in_workers(manager, "scan", jobs, SCAN_WORKERS, raw, on_progress=on_progress)
        for g, r in zip(groups, raw):
            if not r.get("ok"):
                unchecked.append({"label": g["label"], "reason": remote_error_text(r)})
            else:
                # JSON đổi khóa số thành chuỗi: đổi lại thành số để so khớp với ID người dùng
                roles = {int(uid): role for uid, role in r["roles"].items()}
                if roles:
                    rows.append({"id": g["id"], "peer": g["peer"], "label": g["label"], "type": g["type"],
                                 "type_label": "Kênh" if g["type"] == "Channel" else "Nhóm", "roles": roles})
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


def role_label(role):
    """Vai trò trong bảng kết quả quét, '—' nếu tài khoản không tham gia"""
    return role or "—"


# --- Rời nhóm/kênh ---

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


def _wait_until(deadline, reason, on_status, halt):
    """Chờ tới thời điểm deadline (theo time.monotonic), mỗi giây báo lại số giây còn lại. Dừng sớm nếu halt được đặt"""
    while not halt.is_set():
        left = deadline - time.monotonic()
        if left <= 0:
            return
        on_status(f"{reason} Còn {math.ceil(left)} giây.", None)
        halt.wait(min(1.0, left))


def leave_items(manager, items, delete_chat, text, posted, on_status, on_record, halt):
    """Rời nhóm/kênh theo đợt, mỗi đợt tối đa LEAVE_BATCH mục, mỗi mục chạy trong một tiến trình con.
    Khi Telegram yêu cầu chờ, tự chờ đúng số giây đó rồi thử lại các mục bị giới hạn, cho tới khi rời hết
    hoặc gặp lỗi không thể thử lại. Tin nhắn chỉ gửi một lần cho mỗi nhóm.

    posted: tập id nhóm đã nhận tin nhắn ở lần trước. on_status(text, fraction) báo tiến trình (fraction 0..1 hoặc None).
    on_record(results, posted) được gọi sau mỗi đợt và khi kết thúc để giao diện cập nhật.
    halt: khi được đặt, không bắt đầu đợt mới. Trả về kết quả theo đúng thứ tự của items"""
    posted = set(posted)
    pending = deque(items)
    tries = {item["id"]: 0 for item in items}
    final = {}  # id -> kết quả cuối cùng
    batch, raw = [], []  # đợt đang chạy, để ghi nhận kết quả nếu bị ngắt giữa chừng
    flood_until = 0.0  # thời điểm (time.monotonic) hết thời gian Telegram yêu cầu chờ
    prev_end = None  # thời điểm xong đợt trước

    def ordered():
        return [final[item["id"]] for item in items if item["id"] in final]

    try:
        while pending and not halt.is_set():
            batch = [pending.popleft() for _ in range(min(LEAVE_BATCH, len(pending)))]
            raw = [None] * len(batch)
            deadline, reason = flood_until, "Telegram yêu cầu chờ, ứng dụng sẽ tự rời tiếp sau khi hết thời gian."
            if prev_end is not None and any(tries[item["id"]] == 0 for item in batch):
                if prev_end + LEAVE_PAUSE > deadline:
                    deadline, reason = prev_end + LEAVE_PAUSE, "Nghỉ giữa các đợt để tránh bị Telegram giới hạn."
            _wait_until(deadline, reason, on_status, halt)
            if halt.is_set():
                break
            on_status(f"Đang rời {len(batch)} mục. Đã xong {len(final)}/{len(items)}. "
                      "Đừng thao tác gì trong lúc chạy.", len(final) / len(items))
            jobs = [{"peer": item["peer"], "delete": delete_chat,
                     "text": None if item["id"] in posted else text} for item in batch]

            def progress(done, total):
                on_status(f"Đang rời nhóm, kênh ({done}/{total}).", (len(final) + done) / len(items))

            run_in_workers(manager, "leave", jobs, LEAVE_WORKERS, raw, on_progress=progress, halt=halt)
            for item, r in zip(batch, raw):
                tries[item["id"]] += 1
                if r.get("sent"):
                    posted.add(item["id"])  # tin nhắn đã đi rồi, các lần thử sau không gửi lại
                if is_wait_error(r) and tries[item["id"]] < LEAVE_MAX_ATTEMPTS:
                    flood_until = max(flood_until, time.monotonic() + int(r["seconds"]) + FLOOD_MARGIN)
                    pending.append(item)  # thử lại sau khi hết thời gian Telegram yêu cầu chờ
                    continue
                final[item["id"]] = _final_result(item, r, text, item["id"] in posted, delete_chat, tries[item["id"]])
            on_record(ordered(), posted)
            batch, raw = [], []
            prev_end = time.monotonic()
    finally:
        # Bị ngắt (ví dụ người dùng đóng ứng dụng) thì vẫn ghi nhận kết quả của đợt đang chạy.
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
        on_record(ordered(), posted)
    return ordered()
