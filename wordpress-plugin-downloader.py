import os
import shutil
import zipfile
import argparse
import requests
from datetime import datetime
from tqdm import tqdm
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

CACHE_FILENAME = "plugins_cache.json"

def get_plugins(page=1, per_page=100):
    url = (
        f"https://api.wordpress.org/plugins/info/1.2/"
        f"?action=query_plugins&request[page]={page}&request[per_page]={per_page}"
    )
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"❌ Lỗi tải trang {page}: {e}")
        return None


def download_and_extract_plugin(plugin, download_dir, verbose=False, min_installs=0, max_installs=None):
    slug = plugin["slug"]
    installs = plugin.get("active_installs", 0)

    if installs < min_installs:
        raise ValueError(f"{installs} installs < min_installs={min_installs}")
    if max_installs is not None and installs > max_installs:
        raise ValueError(f"{installs} installs > max_installs={max_installs}")

    download_link = plugin.get("download_link")
    plugin_path = Path(download_dir) / "plugins" / slug
    plugin_path = plugin_path.resolve()

    # Chuyển sang long path nếu trên Windows và độ dài vượt quá 240 ký tự
    if os.name == 'nt' and len(str(plugin_path)) >= 240:
        plugin_path = Path(rf"\\?\{plugin_path}")

    if plugin_path.exists():
        if verbose:
            print(f"📁 Xóa thư mục cũ: {plugin_path}")
        shutil.rmtree(plugin_path, ignore_errors=True)

    if verbose:
        print(f"⬇️ Tải plugin: {slug}")

    try:
        response = requests.get(download_link)
        response.raise_for_status()

        with zipfile.ZipFile(BytesIO(response.content)) as z:
            for member in z.infolist():
                # Loại bỏ đường dẫn độc hại
                member_filename = Path(member.filename)
                if ".." in member_filename.parts or member_filename.is_absolute():
                    continue

                dest_file_path = plugin_path.joinpath(*member_filename.parts)
                dest_file_path = dest_file_path.resolve()

                # Nếu Windows và path dài → chuyển sang UNC path
                if os.name == 'nt' and len(str(dest_file_path)) >= 240:
                    dest_file_path = Path(rf"\\?\{dest_file_path}")

                dest_dir = dest_file_path.parent
                dest_dir.mkdir(parents=True, exist_ok=True)

                if not member.is_dir():
                    with z.open(member) as src, open(dest_file_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)

    except requests.RequestException as e:
        print(f"❌ Không thể tải '{slug}': {e}")
    except zipfile.BadZipFile:
        print(f"❌ File zip lỗi hoặc hỏng: '{slug}'")
    except OSError as e:
        print(f"❌ Lỗi hệ thống khi giải nén '{slug}': {e}")

def download_all_plugins(download_dir, verbose=False, min_installs=0, max_installs=None, max_pages=None, use_cache=False):
    os.makedirs(os.path.join(download_dir, "plugins"), exist_ok=True)
    cache_path = os.path.join(download_dir, CACHE_FILENAME)

    valid_plugins = []

    if use_cache and os.path.exists(cache_path):
        if verbose:
            print(f"♻️ Đọc dữ liệu plugin từ cache: {cache_path}")
        with open(cache_path, "r", encoding="utf-8") as f:
            valid_plugins = json.load(f)
    else:
        first_page = get_plugins(page=1, per_page=100)
        if not first_page or "info" not in first_page:
            print("❌ Không thể lấy thông tin plugin.")
            return

        total_pages = first_page["info"]["pages"]
        if max_pages is not None:
            total_pages = min(total_pages, max_pages)

        pages = list(range(1, total_pages + 1))

        def process_page(page):
            try:
                data = get_plugins(page=page, per_page=100)
                result = []
                for plugin in data.get("plugins", []):
                    installs = plugin.get("active_installs", 0)
                    if installs < min_installs:
                        continue
                    if max_installs is not None and installs > max_installs:
                        continue

                    try:
                        last_updated = plugin.get("last_updated", "")
                        updated_dt = datetime.strptime(last_updated, "%Y-%m-%d %I:%M%p %Z")
                        if updated_dt.year < datetime.now().year - 2:
                            continue
                    except Exception:
                        continue

                    result.append(plugin)
                return result
            except Exception as e:
                if verbose:
                    print(f"⛔ Lỗi khi xử lý trang {page}: {e}")
                return []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(process_page, page): page for page in pages}
            for future in tqdm(as_completed(futures), total=len(futures), desc="🔎 Lọc plugin"):
                valid_plugins.extend(future.result())

        # Ghi cache
        if verbose:
            print(f"💾 Ghi cache plugin vào: {cache_path}")
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(valid_plugins, f, ensure_ascii=False, indent=2)

    if not valid_plugins:
        print("⚠️ Không tìm thấy plugin nào thỏa điều kiện.")
        return

    for plugin in tqdm(valid_plugins, desc="⬇️ Đang tải plugin"):
        try:
            download_and_extract_plugin(
                plugin, download_dir, verbose=verbose,
                min_installs=min_installs, max_installs=max_installs
            )
        except ValueError as e:
            if verbose:
                print(f"⛔ Bỏ qua '{plugin['slug']}': {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tải plugin WordPress từ API chính thức.")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Thực hiện tải plugin (xóa thư mục cũ nếu đã tồn tại)"
    )
    parser.add_argument(
        "--download-dir",
        type=str,
        default=".",
        help="Thư mục để lưu plugin (mặc định: thư mục hiện tại)"
    )
    parser.add_argument(
        "--min-installs",
        type=int,
        default=0,
        help="Số lượt cài đặt tối thiểu để chấp nhận plugin"
    )
    parser.add_argument(
        "--max-installs",
        type=int,
        default=None,
        help="Số lượt cài đặt tối đa để chấp nhận plugin"
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Số trang tối đa cần quét từ API (mỗi trang ~100 plugin)"
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        help="Dùng dữ liệu cache để tránh tải lại plugin từ API"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="In chi tiết quá trình thực thi"
    )

    args = parser.parse_args()

    if args.download:
        download_all_plugins(
            download_dir=args.download_dir,
            verbose=args.verbose,
            min_installs=args.min_installs,
            max_installs=args.max_installs,
            max_pages=args.max_pages,
            use_cache=args.use_cache,
        )
