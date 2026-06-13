import asyncio
import os
import logging
import json
from datetime import datetime
from pathlib import Path
from pymax import WebClient, File
from pymax.protocol import Opcode
from pymax.types import FileAttachment, Message
from pymax.exceptions import PyMaxError
from zdisk_crypto import ZDiskCrypto
from zdisk_files import ZDiskFiles
import shutil

logger = logging.getLogger("zdisk_client")

# Monkeypatch LoginResponse and AuthService in pymax to make 'token' field optional
# and automatically carry over the current session token if the server response lacks one.
try:
    from pymax.types.domain.login import LoginResponse
    from pymax.api.auth.service import AuthService
    from typing import Optional

    token_field = LoginResponse.model_fields.get("token")
    if token_field:
        token_field.default = None
        token_field.annotation = Optional[str]
        LoginResponse.__annotations__["token"] = Optional[str]
        LoginResponse.model_rebuild(force=True)

    original_login = AuthService.login
    async def patched_login(self, *args, **kwargs):
        response = await original_login(self, *args, **kwargs)
        if response and response.token is None and self.app.session is not None:
            response.token = self.app.session.token
        return response
    AuthService.login = patched_login
except Exception as e:
    logger.warning(f"Failed to patch pymax Login: {e}")


class MyQrHandler:
    def __init__(self, callback):
        self.callback = callback

    async def show_qr(self, qr_url: str) -> None:
        if self.callback:
            self.callback(qr_url)

class ZDiskClient:
    def __init__(self, work_dir: str = "cache", target_chat_id: int = 0, loop=None):
        self.work_dir = work_dir
        self.target_chat_id = target_chat_id
        self.loop = loop or asyncio.get_event_loop()
        self.client = WebClient(
            session_name="session.db",
            work_dir=work_dir,
            qr_provider=MyQrHandler(self._custom_print_qr)
        )
        self.crypto = ZDiskCrypto()
        self.files = ZDiskFiles()
        
        self.is_authorized = False
        self.auth_future = self.loop.create_future()

        # Callbacks for UI
        self.on_ready = None # function()
        self.on_qr_received = None # function(link)
        
        self.client.on_start()(self._on_client_start)

    def _custom_print_qr(self, link: str):
        if self.on_qr_received:
            self.on_qr_received(link)
        else:
            # Fallback to console if no callback
            import qrcode
            qr = qrcode.QRCode()
            qr.add_data(link)
            qr.print_ascii()

    async def _on_client_start(self, client=None):
        self.is_authorized = True
        if self.on_ready:
            await self.on_ready()
        if not self.auth_future.done():
            self.auth_future.set_result(True)

    async def start(self):
        """Starts the client. If not authorized, it will trigger login."""
        try:
            # Return the task so it can be monitored if needed
            return self.loop.create_task(self.client.start())
        except Exception as e:
            logger.exception("Error starting client")
            raise

    async def request_code(self):
        raise NotImplementedError("SMS code auth is deprecated/unused in QR flow")

    async def get_qr_data(self):
        raise NotImplementedError("Custom QR flow is deprecated, use automatic start flow")

    async def poll_qr_status(self, track_id: str):
        raise NotImplementedError("Custom QR flow is deprecated, use automatic start flow")

    async def complete_qr_login(self, track_id: str):
        raise NotImplementedError("Custom QR flow is deprecated, use automatic start flow")

    async def submit_code(self, temp_token: str, code: str):
        raise NotImplementedError("SMS code auth is deprecated/unused in QR flow")

    async def _with_retry(self, coro_func, *args, **kwargs):
        """Executes a coroutine function with retries on websocket disconnection."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return await coro_func(*args, **kwargs)
            except (PyMaxError, ConnectionError, asyncio.TimeoutError) as e:
                logger.warning(f"Connection error (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    # Give automatic reconnection some time
                    await asyncio.sleep(2)
                else:
                    raise
        return None

    async def fetch_files(self, limit: int = 100, from_time: int | None = None):
        """Fetches files from the target chat with pagination support. Filters out PART messages."""
        history = await self._with_retry(self.client.fetch_history, 
                                       chat_id=self.target_chat_id, 
                                       backward=limit,
                                       from_time=from_time)
        if not history:
            return []
        
        file_messages = []
        # Сначала соберем все манифесты и части для быстрого поиска
        manifests = {} # name -> msg
        parts_map = {} # name -> [msgs]
        
        for msg in history:
            if not msg.text or not msg.attaches: continue
            
            if msg.text.startswith("MANIFEST:"):
                name = msg.text[9:].strip("/").split("/")[-1]
                manifests[name] = msg
            elif msg.text.startswith("PART:"):
                # PART:path/name:index
                parts = msg.text[5:].split(":")
                if len(parts) >= 2:
                    name = parts[0].strip("/").split("/")[-1]
                    if name not in parts_map: parts_map[name] = []
                    parts_map[name].append(msg)

        for msg in history:
            if msg.attaches:
                for attach in msg.attaches:
                    if isinstance(attach, FileAttachment):
                        name = attach.name
                        path = ""
                        is_part = False
                        is_manifest = False
                        display_size = attach.size
                        
                        if msg.text:
                            if msg.text.startswith("FILE:"):
                                full_name = msg.text[5:]
                            elif msg.text.startswith("MANIFEST:"):
                                is_manifest = True
                                full_name = msg.text[9:]
                                # Для манифеста мы не знаем размер сразу без скачивания JSON,
                                # но можем пометить его для UI. 
                                # Позже мы попробуем оценить размер по частям.
                                name_key = full_name.strip("/").split("/")[-1]
                                if name_key in parts_map:
                                    display_size = sum(m.attaches[0].size for m in parts_map[name_key])
                            elif msg.text.startswith("PART:"):
                                is_part = True
                                full_name = None
                            else:
                                full_name = None
                            
                            if full_name:
                                full_name = full_name.strip("/")
                                if "/" in full_name:
                                    path = "/".join(full_name.split("/")[:-1])
                                    name = full_name.split("/")[-1]
                                else:
                                    path = ""
                                    name = full_name

                        if is_part:
                            continue

                        file_messages.append({
                            'msg_id': msg.id,
                            'name': name,
                            'path': path,
                            'size': display_size,
                            'file_id': attach.file_id,
                            'time': msg.time,
                            'is_manifest': is_manifest
                        })
        return file_messages

    async def delete_file(self, msg_id: int):
        """Deletes a file (including manifest and all parts) from the target chat."""
        history = await self._with_retry(self.client.fetch_history, chat_id=self.target_chat_id, backward=100)
        message_ids = [msg_id]
        if history:
            manifest_msg = None
            for m in history:
                if m.id == msg_id:
                    manifest_msg = m
                    break
            if manifest_msg and manifest_msg.text:
                if manifest_msg.text.startswith("MANIFEST:"):
                    full_name = manifest_msg.text[9:]
                    part_prefix = f"PART:{full_name}:"
                    for m in history:
                        if m.text and m.text.startswith(part_prefix):
                            message_ids.append(m.id)
        return await self._with_retry(self.client.delete_message, chat_id=self.target_chat_id, message_ids=message_ids, for_me=False)

    async def send_message(self, text: str, chat_id: int, attachments=None):
        """Sends a message bypassing the markdown formatter to preserve underscores and technical chars."""
        from pymax.protocol.enums import Opcode
        from pymax.api.response import require_payload_model
        from pymax.types.domain import Message
        from pymax.api.messages.payloads import SendMessagePayload, SendMessagePayloadMessage

        cid = self.client._app.api.messages._next_cid()
        
        attaches = []
        if attachments:
            attaches = await self.client._app.api.messages._upload_attachments(attachments)

        frame = SendMessagePayload(
            chat_id=chat_id,
            message=SendMessagePayloadMessage(
                text=text,
                cid=cid,
                elements=[],
                attaches=attaches,
            ),
            notify=True
        )
        
        response = await self._with_retry(self.client._app.invoke, Opcode.MSG_SEND, frame.to_payload())
        message = require_payload_model(response, Message).bind(self.client._app.api.messages)
        return message

    async def load_trash_metadata(self) -> list:
        """Loads trash metadata from the chat history."""
        # Find the latest message starting with "TRASH_METADATA:"
        history = await self._with_retry(self.client.fetch_history, chat_id=self.target_chat_id, backward=100)
        if not history: return []
        
        # Сортируем историю по времени (от новых к старым), если API возвращает в другом порядке
        sorted_history = sorted(history, key=lambda m: m.time if hasattr(m, 'time') else 0, reverse=True)
        
        for msg in sorted_history:
            if msg.text and msg.text.startswith("TRASH_METADATA:"):
                try:
                    data = json.loads(msg.text[15:])
                    if isinstance(data, list):
                        # Нормализуем ключи для совместимости с поврежденными сообщениями (без подчеркиваний)
                        normalized_data = []
                        for item in data:
                            normalized_item = {
                                'name': item.get('name', ''),
                                'path': item.get('path', ''),
                                'deleted_at': item.get('deleted_at', item.get('deletedat', 0.0)),
                                'msg_ids': item.get('msg_ids', item.get('msgids', []))
                            }
                            normalized_data.append(normalized_item)
                        return normalized_data
                except:
                    continue
        return []

    async def save_trash_metadata(self, metadata: list):
        """Saves trash metadata as a message and deletes the old ones."""
        # Find old trash metadata messages
        history = await self._with_retry(self.client.fetch_history, chat_id=self.target_chat_id, backward=100)
        old_metadata_msg_ids = []
        if history:
            for msg in history:
                if msg.text and msg.text.startswith("TRASH_METADATA:"):
                    old_metadata_msg_ids.append(msg.id)
        
        # Send new metadata message
        await self.send_message(
            text=f"TRASH_METADATA:{json.dumps(metadata)}",
            chat_id=self.target_chat_id
        )
        
        # Delete old metadata messages
        if old_metadata_msg_ids:
            try:
                await self._with_retry(
                    self.client.delete_message,
                    chat_id=self.target_chat_id,
                    message_ids=old_metadata_msg_ids,
                    for_me=False
                )
            except Exception as e:
                logger.error(f"Failed to delete old trash metadata messages: {e}")

    async def move_to_trash(self, name: str, path: str, msg_ids: list):
        """Adds items to trash metadata, automatically including all parts for split files."""
        history = await self._with_retry(self.client.fetch_history, chat_id=self.target_chat_id, backward=100)
        resolved_msg_ids = list(msg_ids)
        if history:
            for mid in msg_ids:
                manifest_msg = None
                for m in history:
                    if m.id == mid:
                        manifest_msg = m
                        break
                if manifest_msg and manifest_msg.text and manifest_msg.text.startswith("MANIFEST:"):
                    full_name = manifest_msg.text[9:]
                    part_prefix = f"PART:{full_name}:"
                    for m in history:
                        if m.text and m.text.startswith(part_prefix) and m.id not in resolved_msg_ids:
                            resolved_msg_ids.append(m.id)

        metadata = await self.load_trash_metadata()
        existing_msg_ids = {mid for m in metadata for mid in m.get('msg_ids', [])}
        new_msg_ids = [mid for mid in resolved_msg_ids if mid not in existing_msg_ids]
        if not new_msg_ids:
            return
            
        metadata.append({
            'name': name,
            'path': path,
            'deleted_at': datetime.now().timestamp(),
            'msg_ids': new_msg_ids
        })
        await self.save_trash_metadata(metadata)

    async def restore_from_trash(self, item_data: dict):
        """Removes items from trash metadata."""
        metadata = await self.load_trash_metadata()
        new_metadata = [m for m in metadata if not (m['deleted_at'] == item_data['deleted_at'] and m['name'] == item_data['name'])]
        await self.save_trash_metadata(new_metadata)

    async def permanent_delete_trash(self, item_data: dict):
        """Deletes messages and removes from trash metadata."""
        if item_data.get('msg_ids'):
            await self._with_retry(self.client.delete_message, 
                                   chat_id=self.target_chat_id, 
                                   message_ids=item_data['msg_ids'], 
                                   for_me=False)
        await self.restore_from_trash(item_data)

    async def clear_all_trash(self):
        """Permanently deletes all items in the trash and empties it."""
        metadata = await self.load_trash_metadata()
        if not metadata:
            return
        all_msg_ids = []
        for item in metadata:
            if item.get('msg_ids'):
                all_msg_ids.extend(item['msg_ids'])
        if all_msg_ids:
            try:
                await self._with_retry(self.client.delete_message, 
                                       chat_id=self.target_chat_id, 
                                       message_ids=all_msg_ids, 
                                       for_me=False)
            except Exception as e:
                logger.error(f"Failed to bulk delete trash messages: {e}")
        await self.save_trash_metadata([])

    async def cleanup_trash(self):
        """Deletes items older than 30 days from trash."""
        metadata = await self.load_trash_metadata()
        now = datetime.now().timestamp()
        limit = 30 * 24 * 60 * 60
        to_delete = [m for m in metadata if now - m['deleted_at'] > limit]
        if not to_delete: return
        
        for item in to_delete:
            if item.get('msg_ids'):
                try:
                    await self.client.delete_message(chat_id=self.target_chat_id, message_ids=item['msg_ids'], for_me=False)
                except: pass
        
        new_metadata = [m for m in metadata if now - m['deleted_at'] <= limit]
        await self.save_trash_metadata(new_metadata)

    async def edit_message(self, chat_id: int, message_id: int, text: str):
        """Edits an existing message's text."""
        return await self._with_retry(self.client.edit_message, chat_id=chat_id, message_id=message_id, text=text)

    async def rename_file(self, msg_id: int, path: str, new_name: str):
        """Renames a file by editing the message text."""
        full_path = f"{path.strip('/')}/{new_name}" if path else new_name
        new_text = f"FILE:{full_path}"
        return await self.edit_message(chat_id=self.target_chat_id, message_id=msg_id, text=new_text)

    async def move_file(self, msg_id: int, new_path: str, name: str):
        """Moves a file by editing the message text."""
        full_path = f"{new_path.strip('/')}/{name}" if new_path else name
        new_text = f"FILE:{full_path}"
        return await self.edit_message(chat_id=self.target_chat_id, message_id=msg_id, text=new_text)

    async def create_folder(self, target_path: str):
        """Creates a folder by uploading a dummy .keeper file."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8', dir=str(self.files.temp_dir)) as f:
            f.write("keep_folder")
            temp_path = f.name
        
        try:
            # Upload as .keeper
            await self.send_message(
                text=f"FILE:{target_path.strip('/')}/.keeper",
                chat_id=self.target_chat_id,
                attachments=[File(path=temp_path)]
            )
        finally:
            self.files.cleanup(temp_path)

    async def upload_file(self, file_path: str, password: str = None, progress_callback=None, target_path: str = ""):
        """Uploads a file, handles encryption and splitting. target_path can be like '/docs'."""
        original_filename = os.path.basename(file_path)
        full_name_with_path = f"{target_path.strip('/')}/{original_filename}" if target_path else original_filename
        if not full_name_with_path.startswith("/") and target_path:
             full_name_with_path = "/" + full_name_with_path
        
        async def do_upload():
            current_path = file_path
            temp_files = []

            # 1. Encryption
            if password:
                if progress_callback:
                    progress_callback(0, 1, "Шифрование файла...")
                enc_path = file_path + ".enc"
                await self.loop.run_in_executor(None, self.crypto.encrypt_file, file_path, enc_path, password)
                current_path = enc_path
                temp_files.append(enc_path)

            # 2. Rename to strip extension (bypass max filters)
            if progress_callback:
                progress_callback(0, 1, "Подготовка к загрузке...")
            import shutil
            import tempfile
            stripped_dir = await self.loop.run_in_executor(None, tempfile.mkdtemp, None, None, str(self.files.temp_dir))
            stripped_path = os.path.join(stripped_dir, "blob_upload")
            await self.loop.run_in_executor(None, shutil.copy2, current_path, stripped_path)
            current_path = stripped_path
            temp_files.append(stripped_dir)

            # 3. Splitting
            if progress_callback:
                progress_callback(0, 1, "Разделение файла...")
            prep = await self.loop.run_in_executor(None, self.files.prepare_upload, current_path)
            
            try:
                if prep['is_split']:
                    parts_dir = Path(prep['parts_dir'])
                    manifest_file = prep['manifest_file']
                    
                    # Send manifest first
                    msg = await self.send_message(
                        text=f"MANIFEST:{full_name_with_path}",
                        chat_id=self.target_chat_id,
                        attachments=[File(path=manifest_file)]
                    )
                    # Ждем, пока сообщение действительно будет отправлено (id появится)
                    while not msg.id:
                        await asyncio.sleep(0.5)
                    
                    # Send parts
                    parts = sorted(list(parts_dir.glob("*.part*")))
                    for i, part in enumerate(parts):
                        if progress_callback:
                            progress_callback(i + 1, len(parts), f"Отправка части {i+1}/{len(parts)}")
                        
                        part_msg = await self.send_message(
                            text=f"PART:{full_name_with_path}:{i+1}",
                            chat_id=self.target_chat_id,
                            attachments=[File(path=str(part))]
                        )
                        # Важно дождаться подтверждения отправки каждой части
                        while not part_msg.id:
                            await asyncio.sleep(0.5)
                        
                        # Небольшая пауза между частями для стабильности
                        await asyncio.sleep(0.2)
                    
                    temp_files.append(prep['parts_dir'])
                else:
                    # Normal upload
                    if progress_callback:
                        progress_callback(0, 1, "Загрузка файла...")
                    msg = await self.send_message(
                        text=f"FILE:{full_name_with_path}",
                        chat_id=self.target_chat_id,
                        attachments=[File(path=current_path)]
                    )
                    while not msg.id:
                        await asyncio.sleep(0.5)
            finally:
                # Cleanup
                for f in temp_files:
                    await self.loop.run_in_executor(None, self.files.cleanup, f)

        try:
            await do_upload()
        except Exception as e:
            logger.error(f"Upload error: {e}. Checking if file was actually uploaded...")
            # Verification: check history for the file
            files = await self.fetch_files(limit=10)
            target_name = (original_filename + (".enc" if password else ""))
            if any(f['name'] == target_name or f['name'] == original_filename for f in files):
                logger.info("File found in history, assuming upload success despite error.")
                return
            raise

    async def _download_url_to_file(self, url: str, dest_path: str, name: str, progress_callback=None, start_offset: int = 0, total_expected_size: int = 0):
        """Downloads a URL to a file with resume support and retries."""
        import aiohttp
        max_retries = 5
        attempt = 0
        
        while attempt < max_retries:
            try:
                # Проверяем, сколько уже скачано
                downloaded = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
                
                headers = {}
                if downloaded > 0:
                    headers['Range'] = f'bytes={downloaded}-'
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=None, sock_read=300)) as response:
                        if response.status == 200:
                            # Сервер отдал файл целиком (или не поддерживает Range)
                            mode = 'wb'
                            downloaded = 0
                        elif response.status == 206:
                            # Сервер отдал часть файла
                            mode = 'ab'
                        elif response.status == 416:
                            # Запрошенный диапазон невыполним (возможно файл уже скачан)
                            return
                        else:
                            raise Exception(f"Download failed with status {response.status}")

                        total_size = int(response.headers.get('Content-Length', 0)) + downloaded
                        if total_expected_size:
                             total_size = total_expected_size

                        with open(dest_path, mode) as f:
                            async for chunk in response.content.iter_chunked(1024*1024):
                                f.write(chunk)
                                downloaded += len(chunk)
                                if progress_callback and total_size:
                                    progress_callback(downloaded, total_size, f"Скачивание {name}...")
                
                # Если дошли сюда, значит скачивание завершено успешно
                return

            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
                attempt += 1
                logger.warning(f"Ошибка при скачивании {name} (попытка {attempt}/{max_retries}): {e}")
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(2 ** attempt) # Экспоненциальная задержка

    async def download_file(self, msg_id: int, file_id: int, name: str, password: str = None, progress_callback=None, is_manifest: bool = False, original_name: str = ""):
        """Downloads a file, handles assembly and decryption with resume support."""
        # 1. Get download URL
        file_req = await self._with_retry(self.client.get_file_by_id, self.target_chat_id, msg_id, file_id)
        if not file_req or not file_req.url:
            raise Exception("Could not get download URL")

        # 2. Download
        dest_path = name
        if is_manifest:
            import tempfile
            # Для манифеста докачка не очень важна, но используем общую логику
            temp_manifest = tempfile.NamedTemporaryFile(suffix="_manifest.json", delete=False, dir=str(self.files.temp_dir))
            dest_path = temp_manifest.name
            temp_manifest.close()

        # Используем вспомогательный метод с докачкой
        await self._download_url_to_file(file_req.url, dest_path, "манифеста" if is_manifest else name, progress_callback)

        # 3. Handle split/encrypted
        if is_manifest:
            return await self._handle_manifest_download(dest_path, name, password, progress_callback, original_name)
        elif name.endswith(".enc") and password:
            if progress_callback:
                progress_callback(0, 1, "Расшифровка файла...")
            dec_path = name[:-4]
            success = await self.loop.run_in_executor(None, self.crypto.decrypt_file, dest_path, dec_path, password)
            if success:
                os.remove(dest_path)
                return dec_path
            else:
                raise Exception("Decryption failed. Wrong password?")
        
        return dest_path

    async def _handle_manifest_download(self, manifest_path: str, save_name: str, password: str, progress_callback, original_name_in_chat: str = ""):
        """Downloads all parts with resume support and assembles the file."""
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
        
        search_name = original_name_in_chat or manifest['original_file']
        parts_info = manifest['parts']
        total_parts = len(parts_info)
        
        # Используем постоянную временную директорию на основе хеша манифеста для докачки частей
        import hashlib
        manifest_hash = hashlib.md5(search_name.encode()).hexdigest()
        temp_dir = self.files.temp_dir / f"parts_{manifest_hash}"
        parts_dir = temp_dir / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        
        # Копируем манифест
        shutil.copy2(manifest_path, parts_dir / os.path.basename(manifest_path))
        
        try:
            history = await self._with_retry(self.client.fetch_history, chat_id=self.target_chat_id, backward=1000)
            
            parts_found = {} 
            for msg in history:
                if msg.text and msg.text.startswith(f"PART:"):
                    text_parts = msg.text[5:].split(":")
                    if len(text_parts) >= 2:
                        name_in_msg = text_parts[0].strip("/").split("/")[-1]
                        if name_in_msg == search_name:
                            try:
                                part_idx = int(text_parts[1])
                                parts_found[part_idx] = msg
                            except ValueError: continue

            for part_info in parts_info:
                idx = part_info['part_number']
                if idx not in parts_found:
                    raise Exception(f"Часть {idx} не найдена")
                
                msg = parts_found[idx]
                attach = msg.attaches[0]
                part_dest = parts_dir / part_info['filename']
                
                if progress_callback:
                    progress_callback(idx, total_parts, f"Скачивание части {idx}/{total_parts}...")
                
                # Получаем свежую ссылку для каждой части
                file_req = await self._with_retry(self.client.get_file_by_id, self.target_chat_id, msg.id, attach.file_id)
                
                # Скачиваем с поддержкой докачки
                await self._download_url_to_file(file_req.url, str(part_dest), f"части {idx}", progress_callback)

            if progress_callback:
                progress_callback(0, 1, "Сборка файла...")
            
            output_path = Path(save_name)
            assembled_path = await self.loop.run_in_executor(
                None, self.files.assemble, 
                str(parts_dir / os.path.basename(manifest_path)), str(output_path)
            )
            
            if assembled_path.endswith(".enc") and password:
                if progress_callback:
                    progress_callback(0, 1, "Расшифровка...")
                dec_path = assembled_path[:-4]
                success = await self.loop.run_in_executor(None, self.crypto.decrypt_file, assembled_path, dec_path, password)
                if success:
                    os.remove(assembled_path)
                    assembled_path = dec_path

            if os.path.exists(manifest_path):
                os.remove(manifest_path)
                
            # Очищаем временные файлы только после успешной сборки
            shutil.rmtree(temp_dir)
            return assembled_path

        except Exception as e:
            logger.error(f"Ошибка при обработке манифеста: {e}")
            raise
