import asyncio
import os
import sys
import json
import argparse
import logging
from datetime import datetime
from pathlib import Path
import qrcode
from zdisk_client import ZDiskClient
from pymax.crud import Database

# Configure logging to be less verbose by default
logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
logger = logging.getLogger("zdisk_cli")

SETTINGS_FILE = "zdisk_settings.json"

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {"target_chat_id": 0, "passwords": {}}

def save_settings(settings):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)

def format_size(size_bytes):
    if size_bytes < 1024: return f"{size_bytes} B"
    elif size_bytes < 1024**2: return f"{size_bytes/1024:.2f} KB"
    elif size_bytes < 1024**3: return f"{size_bytes/1024**2:.2f} MB"
    else: return f"{size_bytes/1024**3:.2f} GB"

def progress_callback(current, total, text):
    if total > 0:
        percent = (current / total) * 100
        bar_length = 30
        filled_length = int(bar_length * current // total)
        bar = '█' * filled_length + '-' * (bar_length - filled_length)
        sys.stdout.write(f'\r{text} |{bar}| {percent:.1f}% ({format_size(current)}/{format_size(total)})')
        if current >= total:
            sys.stdout.write('\n')
    else:
        sys.stdout.write(f'\r{text}... ')
    sys.stdout.flush()

class ZDiskCLI:
    def __init__(self, settings):
        self.settings = settings
        self.client = None
        self.loop = asyncio.get_event_loop()

    async def init_client(self):
        self.client = ZDiskClient(
            target_chat_id=self.settings.get("target_chat_id"),
            loop=self.loop
        )

    async def ensure_auth(self):
        db = Database("cache")
        token = db.get_auth_token()
        
        if not token:
            print("Сессия не найдена. Требуется вход.")
            await self.login()
        else:
            await self.client.start()
            try:
                await asyncio.wait_for(self.client.auth_future, timeout=30)
            except asyncio.TimeoutError:
                print("Ошибка: Превышено время ожидания авторизации. Попробуйте войти снова.")
                await self.login()

    async def login(self):
        print("Генерация QR-кода для входа...")
        
        def display_qr(link):
            qr = qrcode.QRCode()
            qr.add_data(link)
            print("\nОтсканируйте этот QR-код в мобильном приложении Max:")
            qr.print_ascii()
            print("Ожидание подтверждения...")

        self.client.on_qr_received = display_qr
        await self.client.start()
        
        try:
            await asyncio.wait_for(self.client.auth_future, timeout=300)
            print("Вход выполнен успешно!")
        except asyncio.TimeoutError:
            print("Ошибка: Время ожидания QR-кода истекло.")
            sys.exit(1)

    async def list_files(self, path="/", search=None):
        files = await self.client.fetch_files(limit=1000)
        trash_metadata = await self.client.load_trash_metadata()
        trash_ids = {mid for t in trash_metadata for mid in t.get('msg_ids', [])}
        
        path = "/" + path.strip("/")
        if path == "/":
             path = ""
        
        filtered = []
        for f in files:
            if f['msg_id'] in trash_ids: continue
            if f['name'] == ".keeper": continue
            
            f_path = "/" + f['path'].strip("/")
            if f_path == "/": f_path = ""
            
            # Check if it's in the requested path or a subpath
            if path and not f_path.startswith(path):
                continue
            
            if search and search.lower() not in f['name'].lower():
                continue
            
            filtered.append(f)

        if not filtered:
            print("Файлы не найдены.")
            return

        print(f"{'Имя':<40} | {'Размер':<12} | {'Дата':<20} | {'Путь'}")
        print("-" * 90)
        for f in sorted(filtered, key=lambda x: x['path'] + x['name']):
            ts = f['time'] / 1000 if f['time'] > 1e11 else f['time']
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            size = format_size(f['size'])
            print(f"{f['name']:<40} | {size:<12} | {dt:<20} | {f['path']}")

    async def upload(self, file_path, target_path="/", password=None):
        if not os.path.exists(file_path):
            print(f"Ошибка: Файл '{file_path}' не найден.")
            return

        print(f"Загрузка '{file_path}' в '{target_path}'...")
        try:
            await self.client.upload_file(
                file_path=file_path,
                password=password,
                target_path=target_path,
                progress_callback=progress_callback
            )
            print("Загрузка завершена.")
        except Exception as e:
            print(f"\nОшибка при загрузке: {e}")

    async def download(self, filename, dest_dir=".", password=None):
        files = await self.client.fetch_files(limit=1000)
        target = None
        for f in files:
            if f['name'] == filename:
                target = f
                break
        
        if not target:
            # Try searching by full path
            for f in files:
                full = (f['path'].strip("/") + "/" + f['name']).strip("/")
                if full == filename.strip("/"):
                    target = f
                    break

        if not target:
            print(f"Ошибка: Файл '{filename}' не найден.")
            return

        print(f"Скачивание '{target['name']}'...")
        try:
            out_name = os.path.join(dest_dir, target['name'])
            await self.client.download_file(
                msg_id=target['msg_id'],
                file_id=target['file_id'],
                name=out_name,
                password=password,
                progress_callback=progress_callback,
                is_manifest=target.get('is_manifest', False),
                original_name=target['name']
            )
            print(f"\nСкачивание завершено: {out_name}")
        except Exception as e:
            print(f"\nОшибка при скачивании: {e}")

    async def delete(self, filename, permanent=False):
        files = await self.client.fetch_files(limit=1000)
        target = None
        for f in files:
            full = (f['path'].strip("/") + "/" + f['name']).strip("/")
            if full == filename.strip("/") or f['name'] == filename:
                target = f
                break
        
        if not target:
            print(f"Ошибка: Файл '{filename}' не найден.")
            return

        if permanent:
            print(f"Окончательное удаление '{filename}'...")
            await self.client.delete_file(target['msg_id'])
        else:
            print(f"Перемещение '{filename}' в корзину...")
            await self.client.move_to_trash(target['name'], target['path'], [target['msg_id']])
        print("Удалено.")

    async def show_trash(self):
        metadata = await self.client.load_trash_metadata()
        if not metadata:
            print("Корзина пуста.")
            return

        print(f"{'#':<3} | {'Имя':<30} | {'Путь':<20} | {'Удален'}")
        print("-" * 80)
        for i, item in enumerate(metadata):
            dt = datetime.fromtimestamp(item['deleted_at']).strftime("%Y-%m-%d %H:%M")
            print(f"{i:<3} | {item['name']:<30} | {item['path']:<20} | {dt}")

    async def restore_trash(self, index):
        metadata = await self.client.load_trash_metadata()
        if 0 <= index < len(metadata):
            item = metadata[index]
            print(f"Восстановление '{item['name']}'...")
            await self.client.restore_from_trash(item)
            print("Восстановлено.")
        else:
            print("Ошибка: Неверный индекс.")

    async def clear_trash(self, index=None):
        if index is not None:
            metadata = await self.client.load_trash_metadata()
            if 0 <= index < len(metadata):
                item = metadata[index]
                print(f"Окончательное удаление '{item['name']}'...")
                await self.client.permanent_delete_trash(item)
                print("Удалено.")
            else:
                print("Ошибка: Неверный индекс.")
        else:
            print("Очистка всей корзины...")
            metadata = await self.client.load_trash_metadata()
            for item in metadata:
                await self.client.permanent_delete_trash(item)
            print("Корзина очищена.")

    async def mkdir(self, path):
        print(f"Создание папки '{path}'...")
        await self.client.create_folder(path)
        print("Готово.")

async def main():
    parser = argparse.ArgumentParser(description="ZDisk CLI - Облачное хранилище в мессенджере MAX")
    subparsers = parser.add_subparsers(dest="command", help="Команды")

    # Login
    subparsers.add_parser("login", help="Вход в аккаунт")

    # LS
    ls_parser = subparsers.add_parser("ls", help="Список файлов")
    ls_parser.add_argument("path", nargs="?", default="/", help="Путь к папке")
    ls_parser.add_argument("-s", "--search", help="Поиск по имени")

    # Upload
    up_parser = subparsers.add_parser("upload", help="Загрузить файл")
    up_parser.add_argument("file", help="Путь к локальному файлу")
    up_parser.add_argument("path", nargs="?", default="/", help="Целевой путь в облаке")
    up_parser.add_argument("-p", "--password", help="Пароль для шифрования")

    # Download
    down_parser = subparsers.add_parser("download", help="Скачать файл")
    down_parser.add_argument("file", help="Имя файла или путь в облаке")
    down_parser.add_argument("dest", nargs="?", default=".", help="Локальная папка для сохранения")
    down_parser.add_argument("-p", "--password", help="Пароль для расшифровки")

    # RM
    rm_parser = subparsers.add_parser("rm", help="Удалить файл")
    rm_parser.add_argument("file", help="Имя файла или путь в облаке")
    rm_parser.add_argument("--permanent", action="store_true", help="Удалить окончательно (мимо корзины)")

    # MKDIR
    mkdir_parser = subparsers.add_parser("mkdir", help="Создать папку")
    mkdir_parser.add_argument("path", help="Путь к новой папке")

    # Trash
    trash_parser = subparsers.add_parser("trash", help="Управление корзиной")
    trash_parser.add_argument("--list", action="store_true", help="Показать содержимое")
    trash_parser.add_argument("--restore", type=int, help="Восстановить по индексу")
    trash_parser.add_argument("--clear", type=int, nargs="?", const=-1, help="Очистить (индекс или все)")

    # Settings
    set_parser = subparsers.add_parser("config", help="Настройки")
    set_parser.add_argument("--chat", type=int, help="ID чата для хранения")

    args = parser.parse_args()

    settings = load_settings()
    
    if args.command == "config":
        if args.chat is not None:
            settings['target_chat_id'] = args.chat
        save_settings(settings)
        print("Настройки сохранены.")
        return

    if not args.command:
        parser.print_help()
        return

    cli = ZDiskCLI(settings)
    await cli.init_client()

    if args.command == "login":
        await cli.login()
        return

    if settings.get("target_chat_id") == 0:
        print("Ошибка: Не задан target_chat_id. Используйте 'config --chat ID'.")
        return

    await cli.ensure_auth()

    if args.command == "ls":
        await cli.list_files(args.path, args.search)
    elif args.command == "upload":
        await cli.upload(args.file, args.path, args.password)
    elif args.command == "download":
        await cli.download(args.file, args.dest, args.password)
    elif args.command == "rm":
        await cli.delete(args.file, args.permanent)
    elif args.command == "mkdir":
        await cli.mkdir(args.path)
    elif args.command == "trash":
        if args.restore is not None:
            await cli.restore_trash(args.restore)
        elif args.clear is not None:
            await cli.clear_trash(None if args.clear == -1 else args.clear)
        else:
            await cli.show_trash()

def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    run()
