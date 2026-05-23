"""
Модуль для разделения файла на части со сжатием и контролем целисностности
"""

import os
import json
import zlib
from pathlib import Path
from typing import Dict, Optional


class FileSplitter:
    """Класс для разделения файла на части со сжатием"""
    
    def __init__(self, chunk_size: int = 10 * 1024 * 1024, compress_level: int = 6):
        """
        Инициализация разделителя
        
        Args:
            chunk_size: Максимальный размер части в байтах
            compress_level: Уровень сжатия zlib (1-9, где 9 - максимальное)
        """
        self.chunk_size = chunk_size
        self.compress_level = compress_level
    
    def calculate_crc32(self, file_path: str) -> str:
        """Вычисляет CRC32 хеш файла"""
        crc = 0
        with open(file_path, 'rb') as f:
            while chunk := f.read(8192):
                crc = zlib.crc32(chunk, crc)
        return format(crc & 0xFFFFFFFF, '08x')
    
    def compress_data(self, data: bytes) -> bytes:
        """Сжимает данные с помощью zlib"""
        return zlib.compress(data, level=self.compress_level)
    
    def split_file(self, input_file: str, output_dir: Optional[str] = None) -> Dict:
        """
        Разделяет файл на части со сжатием, используя потоковую обработку.
        """
        input_path = Path(input_file)

        if not input_path.exists():
            raise FileNotFoundError(f"Файл не найден: {input_file}")

        if not input_path.is_file():
            raise IsADirectoryError(f"Путь указывает на директорию: {input_file}")

        if output_dir is None:
            output_dir = input_path.parent
        else:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

        base_name = input_path.stem
        parts_dir = output_dir / f"{base_name}_parts"
        parts_dir.mkdir(exist_ok=True)

        original_size = input_path.stat().st_size
        original_crc = self.calculate_crc32(input_file)

        parts_info = []
        part_number = 1

        # Используем zlib.compressobj для потокового сжатия
        compressor = zlib.compressobj(level=self.compress_level)
        compressed_size = 0

        # Буфер для накопления сжатых данных для одной части
        current_part_data = bytearray()

        with open(input_file, 'rb') as f:
            while True:
                chunk = f.read(1024 * 1024) # Читаем по 1МБ
                if not chunk:
                    break

                compressed_chunk = compressor.compress(chunk)
                current_part_data.extend(compressed_chunk)

                # Если накопили достаточно для части
                while len(current_part_data) >= self.chunk_size:
                    to_write = current_part_data[:self.chunk_size]
                    current_part_data = current_part_data[self.chunk_size:]

                    self._write_part(parts_dir, base_name, part_number, to_write, parts_info)
                    compressed_size += len(to_write)
                    part_number += 1

            # Завершаем сжатие
            remaining = compressor.flush()
            current_part_data.extend(remaining)

            # Записываем остатки
            while len(current_part_data) > 0:
                to_write = current_part_data[:self.chunk_size]
                current_part_data = current_part_data[self.chunk_size:]

                self._write_part(parts_dir, base_name, part_number, to_write, parts_info)
                compressed_size += len(to_write)
                part_number += 1

        total_parts = part_number - 1

        manifest = {
            'original_file': input_path.name,
            'original_size': original_size,
            'compressed_size': compressed_size,
            'total_parts': total_parts,
            'chunk_size': self.chunk_size,
            'compress_level': self.compress_level,
            'original_crc32': original_crc,
            'parts': parts_info
        }

        manifest_path = parts_dir / f"{base_name}_manifest.json"
        with open(manifest_path, 'w', encoding='utf-8') as mf:
            json.dump(manifest, mf, indent=2, ensure_ascii=False)

        return {
            'success': True,
            'parts_dir': str(parts_dir),
            'manifest_file': str(manifest_path),
            'total_parts': total_parts,
            'original_size': original_size,
            'compressed_size': compressed_size,
            'compression_ratio': original_size / compressed_size if compressed_size > 0 else 1,
            'original_crc32': original_crc
        }

    def _write_part(self, parts_dir, base_name, part_number, data, parts_info):
        part_filename = f"{base_name}.part{part_number:04d}"
        part_path = parts_dir / part_filename

        part_crc = format(zlib.crc32(data) & 0xFFFFFFFF, '08x')

        with open(part_path, 'wb') as part_file:
            part_file.write(data)

        parts_info.append({
            'part_number': part_number,
            'filename': part_filename,
            'size': len(data),
            'crc32': part_crc
        })