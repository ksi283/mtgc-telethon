"""Tiến trình con để rời nhóm/kênh hoặc tìm thành viên.

Mỗi tiến trình con là một tiến trình Python riêng chạy file này, không phải bản fork của Streamlit,
nên không import lại Streamlit và không dùng chung luồng hay khóa với tiến trình chính.
Mỗi tiến trình con có một kết nối Telegram riêng, dựng từ cùng auth key và DC của phiên chính,
chỉ giữ trong bộ nhớ nên không ghi vào file SQLite của phiên chính.

Giao thức (stdin/stdout, mỗi dòng một JSON, mã hóa UTF-8):
- Dòng đầu: cấu hình kết nối. Tiến trình con trả lời {"ready": true} khi đã kết nối.
- Mỗi việc: một dòng JSON, tiến trình con trả lời đúng một dòng kết quả.
- Đóng stdin là hết việc: tiến trình con ngắt kết nối và tự thoát.
"""
import asyncio
import importlib
import json
import sys
import traceback

from telethon.crypto import AuthKey
from telethon.errors import ChatAdminRequiredError, PeerIdInvalidError, UserNotParticipantError
from telethon.sessions import StringSession
from telethon.tl import functions
from telethon.tl.types import (
    ChannelParticipantAdmin,
    ChannelParticipantBanned,
    ChannelParticipantCreator,
    ChannelParticipantLeft,
    ChatParticipantAdmin,
    ChatParticipantCreator,
    InputChannel,
    InputPeerChannel,
    InputPeerChat,
    InputUser,
    InputUserSelf,
)


def _peer(spec):
    """Peer đầu vào từ định danh (kiểu, id, access_hash) mà tiến trình chính đã chuẩn bị"""
    kind, ident, access_hash = spec
    if kind == "channel":
        return InputPeerChannel(channel_id=ident, access_hash=access_hash)
    return InputPeerChat(chat_id=ident)


def _role(is_creator, is_admin):
    if is_creator:
        return "Chủ nhóm"
    if is_admin:
        return "Quản trị viên"
    return "Thành viên"


def _is_member(part):
    """Người đã rời hoặc đã bị đuổi khỏi kênh/siêu nhóm thì không còn là thành viên. Người bị hạn chế quyền
    nhưng vẫn ở trong nhóm (vẫn xem được tin nhắn) thì vẫn là thành viên"""
    if isinstance(part, ChannelParticipantLeft):
        return False
    if isinstance(part, ChannelParticipantBanned):
        return not (part.left or part.banned_rights.view_messages)
    return True


def _failure(e, sent=False):
    return {
        "ok": False,
        "sent": sent,
        "error": type(e).__name__,
        "text": str(e),
        "seconds": getattr(e, "seconds", None),
    }


async def _leave(client, job):
    peer = _peer(job["peer"])
    is_chat = isinstance(peer, InputPeerChat)
    sent = False
    if job["text"]:
        try:
            await client.send_message(peer, job["text"], parse_mode=None)
        except Exception as e:
            return _failure(e, sent=False)
        sent = True
    # Gọi trực tiếp thay vì delete_dialog: delete_dialog luôn xóa lịch sử của nhóm thường, kể cả khi người dùng
    # chọn giữ lịch sử, và lỗi ở bước xóa lịch sử làm nhóm đã rời vẫn bị báo là thất bại
    already_out = False
    try:
        if is_chat:
            # revoke_history=False: chỉ rời và xóa khỏi tài khoản của mình, không xóa tin nhắn của người khác
            await client(functions.messages.DeleteChatUserRequest(
                peer.chat_id, InputUserSelf(), revoke_history=False
            ))
        else:
            await client(functions.channels.LeaveChannelRequest(peer))
    except UserNotParticipantError:
        already_out = True
    except PeerIdInvalidError as e:
        # Nhóm thường đã chuyển thành siêu nhóm nên không còn hoạt động. Telethon cũng bỏ qua lỗi này
        if not is_chat:
            return _failure(e, sent)
        already_out = True
    except Exception as e:
        return _failure(e, sent)
    # Đã không còn trong nhóm thì vẫn xóa lịch sử nếu người dùng yêu cầu, không bỏ qua bước này
    result = {"ok": True, "sent": sent, "already_out": already_out}
    if job["delete"] and is_chat:
        try:
            await client(functions.messages.DeleteHistoryRequest(peer, 0, revoke=False))
            result["history"] = True
        except Exception as e:
            result["history_error"] = _failure(e)
    return result


async def _scan(client, job):
    kind, ident, access_hash = job["peer"]
    roles = {}
    try:
        if kind == "chat":
            # Nhóm thường: một lần gọi lấy đủ thành viên, không cần hỏi từng người
            full = await client(functions.messages.GetFullChatRequest(ident))
            participants = getattr(full.full_chat.participants, "participants", None)
            if participants is None:
                raise ChatAdminRequiredError(request=None)
            wanted = {uid for uid, _ in job["users"]}
            for p in participants:
                if p.user_id in wanted:
                    roles[p.user_id] = _role(
                        isinstance(p, ChatParticipantCreator), isinstance(p, ChatParticipantAdmin)
                    )
        else:
            channel = InputChannel(channel_id=ident, access_hash=access_hash)
            for uid, user_hash in job["users"]:
                try:
                    res = await client(
                        functions.channels.GetParticipantRequest(channel, InputUser(user_id=uid, access_hash=user_hash))
                    )
                except UserNotParticipantError:
                    continue
                part = res.participant
                if _is_member(part):
                    roles[uid] = _role(isinstance(part, ChannelParticipantCreator), isinstance(part, ChannelParticipantAdmin))
        return {"ok": True, "roles": roles}
    except Exception as e:
        return _failure(e)


async def _run(client, job):
    if job["kind"] == "leave":
        return await _leave(client, job)
    return await _scan(client, job)


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def _close(client):
    # Phải ngắt kết nối bên trong vòng lặp đang chạy. Gọi ngoài vòng lặp thì Telethon dùng vòng lặp khác và báo lỗi khi thoát
    await client.disconnect()


def main():
    # Đường ống luôn dùng UTF-8, giống tiến trình chính, không phụ thuộc bảng mã mặc định của hệ điều hành
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        config = json.loads(sys.stdin.readline())
        module_name, class_name = config["client"].split(":")
        client_cls = getattr(importlib.import_module(module_name), class_name)
        session = StringSession()
        session.set_dc(config["session"]["dc_id"], config["session"]["server"], config["session"]["port"])
        if config["session"]["auth_key"]:
            session.auth_key = AuthKey(bytes.fromhex(config["session"]["auth_key"]))
        client = client_cls(session, config["api_id"], config["api_hash"])
        loop.run_until_complete(client.connect())
    except Exception as e:
        # Gửi lỗi về tiến trình chính để hiện lên giao diện, không chỉ in ra terminal
        traceback.print_exc()
        _send({"ready": False, **_failure(e)})
        return
    _send({"ready": True})
    try:
        for line in sys.stdin:
            try:
                result = loop.run_until_complete(_run(client, json.loads(line)))
            except Exception as e:
                result = _failure(e)  # một việc lỗi không làm dừng cả tiến trình con
            _send(result)
    finally:
        loop.run_until_complete(_close(client))


if __name__ == "__main__":
    main()
