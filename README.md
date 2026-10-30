# Quản lý nhóm & kênh Telegram

Ứng dụng chạy trên máy của bạn, giao diện mở trong trình duyệt, dùng Telethon để làm việc với tài khoản Telegram của bạn:

- Xem toàn bộ nhóm và kênh bạn đang tham gia. Tìm theo tên, `@username`, link `t.me` hoặc ID.
- Tìm xem một hoặc nhiều tài khoản (tối đa 5) đang ở những nhóm, kênh nào, và họ giữ vai trò gì.
- Rời nhiều nhóm và kênh cùng lúc. Có thể gửi một tin nhắn trước khi rời, và có thể xóa lịch sử chat khỏi tài khoản của bạn.
- Đăng nhập bằng mã xác thực hoặc bằng mã QR. Lần sau mở lại app sẽ tự đăng nhập.

## Chạy trên Windows (không cần cài Python)

1. Tải file `TelegramManager-<phiên bản>.exe` của bản release, ví dụ `TelegramManager-0.1.0.exe`. Đây là một file duy nhất, không cần thư mục đi kèm.
2. Chạy file đó. Lần mở đầu có thể mất vài giây để giải nén vào thư mục tạm. Một cửa sổ console hiện ra và trình duyệt mở trang của ứng dụng.
3. Nếu trình duyệt không tự mở, hãy mở địa chỉ `http://localhost:8501` (hoặc địa chỉ khác được in trong console).
4. Giữ cửa sổ console mở khi dùng app. Đóng console là dừng app.

Ứng dụng dùng cổng 8501 nếu còn trống. Nếu cổng đang bận (ví dụ bạn đang chạy bản dev), app tự chọn một cổng khác và in địa chỉ trong console. Muốn cố định cổng, đặt biến môi trường `STREAMLIT_SERVER_PORT` trước khi chạy.

Windows có thể cảnh báo SmartScreen hoặc antivirus với file tự build. Đó là vì file chưa được ký số. Hãy chắc chắn file do chính bạn build từ mã nguồn này.

## Chuẩn bị tài khoản

Cần API ID và API Hash miễn phí:

1. Vào https://my.telegram.org và đăng nhập bằng số điện thoại Telegram của bạn.
2. Mở mục **API development tools** và tạo một ứng dụng (tên và mô tả tùy ý).
3. Sao chép **App api_id** và **App api_hash** vào form đăng nhập của app.

## Dữ liệu được lưu ở đâu

Tất cả nằm cạnh file exe (hoặc trong thư mục chạy app khi chạy từ mã nguồn):

- `account.json`: số điện thoại và tên file session. Không nhạy cảm.
- `session_<số điện thoại>.session`: phiên đăng nhập, cùng API ID và API Hash (SQLite). **Nhạy cảm**: ai có file này có thể truy cập tài khoản của bạn. Đừng gửi cho người khác và đừng đưa lên git.

Nút **Đăng xuất** trong app sẽ thu hồi phiên trên Telegram và xóa thông tin đã lưu trên máy. Ứng dụng chỉ kết nối tới máy chủ Telegram, và chỉ lắng nghe trên `localhost` của máy bạn.

## Rời nhóm và kênh

- Mỗi đợt rời tối đa **9 mục**, sau đó nghỉ **130 giây** rồi làm đợt tiếp theo. Thời gian nghỉ đổi được bằng biến môi trường `LEAVE_PAUSE_SECONDS`.
- Khi Telegram yêu cầu chờ N giây, app chờ đúng N giây rồi tự rời tiếp các mục bị giới hạn. Mỗi mục được thử tối đa 50 lần.
- Tin nhắn tùy chọn chỉ gửi một lần cho mỗi nhóm, kể cả khi thử lại.
- **Rời và xóa lịch sử** chỉ xóa lịch sử khỏi tài khoản của bạn, không xóa tin nhắn của người khác.
- Nhóm đã rời từ trước được báo là hoàn tất, nên bấm rời lại vẫn an toàn.
- Nhóm do bạn tạo có thể không rời được. Khi đó bạn cần chuyển quyền chủ nhóm trước.
- Rời nhóm **không hoàn tác được**. Đừng thao tác trên trang khi đang chạy. Nếu tải lại trang giữa chừng, các mục đã rời vẫn đã rời trên Telegram. Phần còn lại giữ lựa chọn để bạn thử lại.

## Chạy từ mã nguồn (Linux hoặc WSL)

Yêu cầu Python 3.13 và [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run streamlit run telegram_manager.py --server.address localhost
```

Trên Windows, chạy từ mã nguồn bằng một môi trường riêng để không ghi đè `.venv` của WSL:

```bat
py -3.14 -m venv build\venv
build\venv\Scripts\python -m pip install streamlit==1.66.1 telethon==1.45.0 pandas==3.1.0
build\venv\Scripts\python -m streamlit run telegram_manager.py --server.address localhost
```

## Đóng gói lại file .exe (Windows)

PyInstaller không đóng gói chéo hệ điều hành, nên hãy build trên Windows:

```bat
py -3.14 -m venv build\venv
build\venv\Scripts\python -m pip install streamlit==1.66.1 telethon==1.45.0 pandas==3.1.0 pyinstaller
build\venv\Scripts\python build_exe.py
```

Kết quả là `dist\TelegramManager-<phiên bản>.exe`, một file duy nhất. Thêm `--onedir` để ra thư mục `dist\TelegramManager\` thay vì một file, dễ kiểm tra các file bên trong hơn. Thư mục `build\` và `dist\` đã nằm trong `.gitignore`.

Tiến trình con khi rời nhóm được chạy lại từ chính file exe và dùng lại thư mục giải nén của app, nên không phải giải nén lại.

## Cấu trúc mã nguồn

- `telegram_manager.py`: giao diện Streamlit và toàn bộ logic.
- `tg_workers.py`: tiến trình con làm việc với Telegram khi rời nhóm và quét thành viên.
- `launcher.py`: điểm vào của bản đóng gói. Chọn cổng, chạy Streamlit, và chạy tiến trình con khi có `--tg-worker`.
- `build_exe.py`: đóng gói ứng dụng bằng PyInstaller.
- `vendor/qrcode-generator.js`: thư viện vẽ mã QR (giấy phép MIT).
- `pyproject.toml`, `uv.lock`: phụ thuộc của dự án.

## Trạng thái kiểm thử

- Đã kiểm thử bằng client giả: đăng nhập và giao diện, rời theo đợt, chờ khi bị giới hạn, gửi tin nhắn, xóa lịch sử, và tiến trình con.
- Bản Windows đã kiểm tra trên Windows 11 với Python 3.14: trang đăng nhập hiển thị, chọn cổng khi cổng 8501 bận, và tiến trình con hoạt động.
- **Chưa kiểm chứng với tài khoản Telegram thật.** Trước khi rời một danh sách lớn, hãy thử với 2–3 nhóm.
- Repo chưa có bộ test tự động.
