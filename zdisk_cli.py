import asyncio
import os
import sys
import json
import argparse
import logging
import shlex
from datetime import datetime
from pathlib import Path
import qrcode
from zdisk_client import ZDiskClient

# Configure logging to be less verbose by default
logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
logger = logging.getLogger("zdisk_cli")

# Try to load settings from current working directory first, fallback to script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.exists("zdisk_settings.json") or not os.access(SCRIPT_DIR, os.W_OK):
    SETTINGS_FILE = os.path.abspath("zdisk_settings.json")
    CACHE_DIR = os.path.abspath("cache")
else:
    SETTINGS_FILE = os.path.join(SCRIPT_DIR, "zdisk_settings.json")
    CACHE_DIR = os.path.join(SCRIPT_DIR, "cache")

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {"target_chat_id": 0, "passwords": {}}

def save_settings(settings):
    # Ensure directory exists
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
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
            work_dir=CACHE_DIR,
            target_chat_id=self.settings.get("target_chat_id"),
            loop=self.loop
        )

    async def ensure_auth(self):
        from pymax.session.store import SessionStore
        store = SessionStore(CACHE_DIR, "session.db")
        session = await store.load_session()
        await store.close()
        
        if not session or not session.token:
            print("Сессия не найдена. Требуется вход.")
            await self.login()
        else:
            await self.client.start()
            try:
                await asyncio.wait_for(self.client.auth_future, timeout=30)
            except asyncio.TimeoutError:
                print("Ошибка: Превышено время ожидания авторизации. Попробуйте войти снова.")
                await self.login()

    async def run_shell(self):
        print("\n=== ZDisk Interactive Shell ===")
        print("Команды: ls [path], cd [path], upload [file] [path], download [file] [dest], rm [file], mkdir [path], trash, config --chat ID, exit")
        current_path = "/"
        
        if self.settings.get("target_chat_id") == 0:
            print("Предупреждение: target_chat_id не задан. Используйте 'config --chat ID'.")
        else:
            try:
                await self.ensure_auth()
            except Exception as e:
                print(f"Ошибка авторизации: {e}")

        while True:
            try:
                prompt = f"zdisk:{current_path}> "
                line = await self.loop.run_in_executor(None, input, prompt)
                if not line.strip():
                    continue
                
                parts = shlex.split(line)
                cmd = parts[0].lower()
                args = parts[1:]
                
                if cmd in ["exit", "quit", "bye"]:
                    break
                elif cmd == "ls":
                    path = args[0] if args else current_path
                    if not path.startswith("/"):
                        path = os.path.join(current_path, path).replace("\\", "/")
                    await self.list_files(path)
                elif cmd == "cd":
                    new_path = args[0] if args else "/"
                    if new_path == "..":
                        if current_path != "/":
                            current_path = "/".join(current_path.strip("/").split("/")[:-1])
                            if not current_path.startswith("/"): current_path = "/" + current_path
                    elif new_path.startswith("/"):
                        current_path = new_path
                    else:
                        current_path = os.path.join(current_path, new_path).replace("\\", "/")
                    
                    if not current_path.startswith("/"): current_path = "/" + current_path
                    if len(current_path) > 1: current_path = current_path.rstrip("/")
                elif cmd == "upload":
                    if len(args) < 1:
                        print("Использование: upload <file> [target_path]")
                        continue
                    file_path = args[0]
                    target = args[1] if len(args) > 1 else current_path
                    if not target.startswith("/"):
                        target = os.path.join(current_path, target).replace("\\", "/")
                    await self.upload(file_path, target)
                elif cmd == "download":
                    if len(args) < 1:
                        print("Использование: download <file> [dest_dir]")
                        continue
                    file_name = args[0]
                    dest = args[1] if len(args) > 1 else "."
                    await self.download(file_name, dest)
                elif cmd == "rm":
                    if len(args) < 1:
                        print("Использование: rm <file> [--permanent]")
                        continue
                    perm = "--permanent" in args or "--permament" in args
                    other_args = [a for a in args if a not in ["--permanent", "--permament"]]
                    if not other_args:
                        print("Использование: rm <file> [--permanent]")
                        continue
                    fname = other_args[0]
                    await self.delete(fname, perm)
                elif cmd == "mkdir":
                    if len(args) < 1:
                        print("Использование: mkdir <path>")
                        continue
                    path = args[0]
                    if not path.startswith("/"):
                        path = os.path.join(current_path, path).replace("\\", "/")
                    await self.mkdir(path)
                elif cmd == "trash":
                    if "--list" in args:
                        await self.show_trash()
                    elif "--restore" in args:
                        idx = args.index("--restore")
                        if idx + 1 < len(args):
                            try:
                                await self.restore_trash(int(args[idx+1]))
                            except ValueError:
                                print("Ошибка: Индекс должен быть числом.")
                        else:
                            print("Использование: trash --restore <index>")
                    elif "--clear" in args:
                        idx = args.index("--clear")
                        val = None
                        if idx + 1 < len(args):
                            try:
                                val = int(args[idx+1])
                            except ValueError:
                                pass
                        await self.clear_trash(val)
                    else:
                        await self.show_trash()
                elif cmd == "config":
                    if "--chat" in args:
                        idx = args.index("--chat")
                        if idx + 1 < len(args):
                            try:
                                new_chat = int(args[idx+1])
                                self.settings['target_chat_id'] = new_chat
                                save_settings(self.settings)
                                self.client.target_chat_id = new_chat
                                print(f"ID чата изменен на {new_chat}")
                                if self.client.is_authorized is False:
                                    await self.ensure_auth()
                            except ValueError:
                                print("Ошибка: ID чата должен быть числом.")
                    else:
                        print(f"Текущий ID чата: {self.settings.get('target_chat_id')}")
                elif cmd == "help":
                    print("Доступные команды: ls, cd, upload, download, rm, mkdir, trash, config, exit")
                else:
                    print(f"Неизвестная команда: {cmd}")
                    
            except KeyboardInterrupt:
                print("\nИспользуйте 'exit' для выхода")
            except Exception as e:
                print(f"Ошибка: {e}")

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
        trash_metadata = await self.client.load_trash_metadata()
        trash_ids = {mid for t in trash_metadata for mid in t.get('msg_ids', [])}
        
        target = None
        for f in files:
            full = (f['path'].strip("/") + "/" + f['name']).strip("/")
            if full == filename.strip("/") or f['name'] == filename:
                if f['msg_id'] not in trash_ids:
                    target = f
                    break
        
        if not target:
            print(f"Ошибка: Файл '{filename}' не найден или уже в корзине.")
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
            await self.client.clear_all_trash()
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
    rm_parser.add_argument("--permanent", "--permament", action="store_true", dest="permanent", help="Удалить окончательно (мимо корзины)")

    # MKDIR
    mkdir_parser = subparsers.add_parser("mkdir", help="Создать папку")
    mkdir_parser.add_argument("path", help="Путь к новой папке")

    # Trash
    trash_parser = subparsers.add_parser("trash", help="Управление корзиной")
    trash_parser.add_argument("--list", action="store_true", help="Показать содержимое")
    trash_parser.add_argument("--restore", type=int, help="Восстановить по индексу")
    trash_parser.add_argument("--clear", type=int, nargs="?", const=-1, help="Очистить (индекс или все)")

    # Shell
    subparsers.add_parser("shell", help="Запустить интерактивный режим")

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

    cli = ZDiskCLI(settings)
    await cli.init_client()

    if not args.command or args.command == "shell":
        await cli.run_shell()
        return

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
