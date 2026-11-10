# Quản lý nhóm & kênh Telegram

Ứng dụng chạy trên máy của bạn, giao diện là cửa sổ Tkinter, dùng Telethon để làm việc với tài khoản Telegram của bạn:

- Xem toàn bộ nhóm và kênh bạn đang tham gia. Tìm theo tên, `@username`, link `t.me` hoặc ID.
- Tìm xem một hoặc nhiều tài khoản (tối đa 5) đang ở những nhóm, kênh nào, và họ giữ vai trò gì.
- Rời nhiều nhóm và kênh cùng lúc. Có thể gửi một tin nhắn trước khi rời, và có thể xóa lịch sử chat khỏi tài khoản của bạn.
- Xem các bot trong danh sách trò chuyện, chặn bot, và có thể xóa lịch sử chat với bot.
- Đăng nhập bằng mã xác thực hoặc bằng mã QR. Lần sau mở lại app sẽ tự đăng nhập.

## Chạy trên Windows (không cần cài Python)

1. Tải file `TelegramManager-<phiên bản>.exe` của bản release, ví dụ `TelegramManager-0.1.2.exe`. Đây là một file duy nhất, không cần thư mục đi kèm.
2. Chạy file đó. Lần mở đầu có thể mất vài giây để giải nén vào thư mục tạm. Cửa sổ ứng dụng hiện ra, và biểu tượng TelegramManager nằm trong khay hệ thống (góc phải thanh tác vụ). Nếu không thấy biểu tượng, bấm mũi tên ^ trên khay hệ thống.
3. Nút X của cửa sổ chỉ ẩn cửa sổ, ứng dụng vẫn chạy (ví dụ đang rời nhóm). Để mở lại, bấm biểu tượng trong khay rồi chọn **Mở ứng dụng**. Để thoát, chọn **Đóng ứng dụng** và xác nhận. Nếu đang rời nhóm, thao tác đó sẽ bị dừng lại.
4. Nếu mở ứng dụng lần hai, bạn sẽ thấy thông báo rằng ứng dụng đã đang chạy.

Windows có thể cảnh báo SmartScreen hoặc antivirus với file tự build. Đó là vì file chưa được ký số. Hãy chắc chắn file do chính bạn build từ mã nguồn này.

## Chuẩn bị tài khoản

Cần API ID và API Hash miễn phí:

1. Vào https://my.telegram.org và đăng nhập bằng số điện thoại Telegram của bạn.
2. Mở mục **API development tools** và tạo một ứng dụng (tên và mô tả tùy ý).
3. Sao chép **App api_id** và **App api_hash** vào form đăng nhập của app.

Đăng nhập bằng QR chỉ cần API ID và API Hash, không cần nhập số điện thoại: số của tài khoản được lấy từ mã vừa quét. Đăng nhập bằng mã xác thực thì cần thêm số điện thoại. Nếu để trống số điện thoại rồi bấm Tiếp tục, ứng dụng sẽ chuyển sang đăng nhập bằng QR.

## Dữ liệu được lưu ở đâu

Tất cả nằm cạnh file exe (hoặc trong thư mục chạy app khi chạy từ mã nguồn):

- `account.json`: số điện thoại và tên file session. Không nhạy cảm.
- `session_<số điện thoại>.session`: phiên đăng nhập, cùng API ID và API Hash (SQLite). **Nhạy cảm**: ai có file này có thể truy cập tài khoản của bạn. Đừng gửi cho người khác và đừng đưa lên git.
- `TelegramManager.log`: nhật ký của lần mở gần nhất, dùng khi cần tìm lỗi.

Nút **Đăng xuất** trong app sẽ thu hồi phiên trên Telegram và xóa thông tin đã lưu trên máy. Ứng dụng chỉ kết nối tới máy chủ Telegram.

## Rời nhóm và kênh

- Mỗi đợt rời tối đa **9 mục**, sau đó nghỉ **130 giây** rồi làm đợt tiếp theo. Thời gian nghỉ đổi được bằng biến môi trường `LEAVE_PAUSE_SECONDS`.
- Khi Telegram yêu cầu chờ N giây, app chờ đúng N giây rồi tự rời tiếp các mục bị giới hạn. Mỗi mục được thử tối đa 50 lần.
- Tin nhắn tùy chọn chỉ gửi một lần cho mỗi nhóm, kể cả khi thử lại.
- **Rời và xóa lịch sử** chỉ xóa lịch sử khỏi tài khoản của bạn, không xóa tin nhắn của người khác.
- Nhóm đã rời từ trước được báo là hoàn tất, nên bấm rời lại vẫn an toàn.
- Nhóm do bạn tạo có thể không rời được. Khi đó bạn cần chuyển quyền chủ nhóm trước.
- Rời nhóm **không hoàn tác được**. Đừng thao tác gì trong lúc đang chạy. Nếu đóng ứng dụng giữa chừng, các mục đã rời vẫn đã rời trên Telegram. Phần còn lại giữ lựa chọn để bạn thử lại.

## Chặn bot

- Tab **Bot Telegram** liệt kê các bot có trong danh sách trò chuyện của bạn, tức là các bot bạn đã nhắn tin. Bot chưa từng có hội thoại thì không hiện ra.
- **Chặn bot, giữ lịch sử** chỉ chặn: bot không gửi tin nhắn cho bạn nữa, lịch sử chat vẫn còn. **Chặn và xóa lịch sử chat** thêm xóa lịch sử khỏi tài khoản của bạn, không xóa tin nhắn của người khác.
- Chặn bot không xóa bot. Có thể bỏ chặn sau này trong Cài đặt Telegram, phần người bị chặn.
- Các bot được chặn lần lượt, từng bot một. Nếu Telegram yêu cầu chờ, ứng dụng tự chờ đúng số giây đó rồi chặn tiếp.

## Chạy từ mã nguồn

Yêu cầu Python 3.13 trở lên và [uv](https://docs.astral.sh/uv/). Trên Linux hoặc WSL cần có màn hình đồ họa (WSLg đã đủ).

```bash
uv sync
uv run python launcher.py
```

Trên Windows, chạy từ mã nguồn bằng một môi trường riêng để không ghi đè `.venv` của WSL:

```bat
py -3.14 -m venv build\venv
build\venv\Scripts\python -m pip install telethon pystray segno
build\venv\Scripts\python launcher.py
```

Khi chạy từ mã nguồn trên Linux hoặc WSL không có khay hệ thống, đóng cửa sổ là thoát ứng dụng.

## Đóng gói lại file .exe (Windows)

PyInstaller không đóng gói chéo hệ điều hành, nên hãy build trên Windows:

```bat
py -3.14 -m venv build\venv
build\venv\Scripts\python -m pip install telethon pystray segno pyinstaller
build\venv\Scripts\python build_exe.py
```

Kết quả là `dist\TelegramManager-<phiên bản>.exe`, một file duy nhất, không có cửa sổ console. Thêm `--onedir` để ra thư mục `dist\TelegramManager\` thay vì một file. Thêm `--console` để giữ cửa sổ console khi cần xem nhật ký lúc gỡ lỗi.

Sau khi build, kiểm tra nhanh bằng:

```bat
dist\TelegramManager-0.1.2.exe --selftest
echo %ERRORLEVEL%
```

Mã thoát `0` nghĩa là các thành phần đã được đóng gói đủ. Nếu không phải `0`, chi tiết lỗi nằm trong `TelegramManager-selftest.log` cạnh file exe.

Cấu hình đóng gói nằm trong `TelegramManager.spec`. File này bỏ các phần không dùng (Streamlit, pandas, pyarrow, test của thư viện, IDLE, turtle). Thư mục `build\` và `dist\` đã nằm trong `.gitignore`.

Tiến trình con khi rời nhóm được chạy lại từ chính file exe, nên không cần cài Python hay giải nén lại.

## Cấu trúc mã nguồn

- `core.py`: logic Telegram (đăng nhập, lưu phiên, rời nhóm theo đợt, quét thành viên, chặn bot, QR). Không phụ thuộc giao diện.
- `gui.py`: giao diện Tkinter. Chỉ hiển thị và gọi `core.py`, thao tác chờ mạng chạy ở luồng nền.
- `launcher.py`: điểm vào. Trên Windows hiện biểu tượng khay hệ thống, chỉ cho chạy một bản, ghi nhật ký, và chạy tiến trình con khi có `--tg-worker`.
- `tg_workers.py`: tiến trình con làm việc với Telegram khi rời nhóm và quét thành viên.
- `build_exe.py`, `TelegramManager.spec`: đóng gói bằng PyInstaller.
- `assets/`: icon của ứng dụng. `telegram.png` dùng cho cửa sổ và khay, `telegram.ico` dùng cho file exe.
- `pyproject.toml`, `uv.lock`, `requirements.txt`: phụ thuộc (telethon, pystray, segno).

## Trạng thái kiểm thử

- Logic (`core.py`) đã kiểm thử bằng client giả: chia đợt, tin nhắn chỉ gửi một lần, xóa lịch sử, ngắt giữa chừng, thử lại khi bị giới hạn, quét thành viên, QR, chặn bot, đăng nhập lại và kiểm tra thông tin đăng nhập.
- Giao diện đã kiểm thử bằng client giả: đăng nhập bằng QR (không cần số điện thoại) và bằng mã xác thực, khôi phục phiên, danh sách, tìm kiếm, chọn, hộp xác nhận rời nhóm, tab Bot Telegram (chặn bot và xóa lịch sử), quét thành viên và đăng xuất. Chạy trên WSL (cửa sổ thật và cửa sổ ẩn) và trên Windows (dashboard).
- **Đã chạy với tài khoản Telegram thật và thành công.** Menu khay hệ thống (Mở/Đóng ứng dụng) và hộp xác nhận đóng ứng dụng chưa được kiểm thử tự động trên Windows. Khi rời một danh sách lớn, nên thử với 2–3 nhóm trước.
- Repo chưa có bộ test tự động.

## Icon

Icon ứng dụng lấy từ Flaticon: https://www.flaticon.com/free-icon/telegram_7301275

## Giấy phép

Dự án được phát hành theo **GNU General Public License phiên bản 3 (GPLv3)**. Toàn văn giấy phép nằm trong file [LICENSE](LICENSE).

- Bản quyền © 2026 ksi283.
- Bạn được chạy, nghiên cứu, chỉnh sửa và phân phối lại phần mềm theo các điều khoản của GPLv3.
- Khi phân phối lại, kể cả bản đã build thành file exe, phải kèm giấy phép và cung cấp mã nguồn tương ứng. Mã nguồn: https://github.com/ksi283/mtgc-telethon
- Phần mềm không đi kèm bất kỳ bảo đảm nào.
- Icon trong `assets/` lấy từ Flaticon nên vẫn theo điều khoản của Flaticon, không thuộc GPLv3 của dự án.
