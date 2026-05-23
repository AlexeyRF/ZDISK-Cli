"""
Модуль для сборки файла из частей с распаковкой и проверкой целостности
"""

import json
import zlib
from pathlib import Path
from typing import Dict, Optional


class FileAssembler:
    """Класс для сборки файла из частей с распаковкой"""
    
    def __init__(self):
        """Инициализация сборщика"""
        pass
    
    def calculate_crc32(self, file_path: str) -> str:
        """Вычисляет CRC32 хеш файла"""
        crc = 0
        with open(file_path, 'rb') as f:
            while chunk := f.read(8192):
                crc = zlib.crc32(chunk, crc)
        return format(crc & 0xFFFFFFFF, '08x')
    
    def decompress_data(self, data: bytes) -> bytes:
        """Распаковывает данные с помощью zlib"""
        return zlib.decompress(data)
    
    def verify_part(self, part_path: Path, expected_crc: str, expected_size: int) -> bool:
        """Проверяет целостность одной части"""
        if not part_path.exists():
            return False
        
        if part_path.stat().st_size != expected_size:
            return False
        
        with open(part_path, 'rb') as f:
            content = f.read()
            actual_crc = format(zlib.crc32(content) & 0xFFFFFFFF, '08x')
        
        return actual_crc == expected_crc
    
    def assemble_file(self, manifest_file: str, output_file: Optional[str] = None) -> Dict:
        """
        Собирает и распаковывает файл из частей, используя потоковую обработку.
        """
        manifest_path = Path(manifest_file)

        if not manifest_path.exists():
            raise FileNotFoundError(f"Манифест не найден: {manifest_file}")

        with open(manifest_path, 'r', encoding='utf-8') as mf:
            manifest = json.load(mf)

        parts_dir = manifest_path.parent
        original_filename = manifest['original_file']
        total_parts = manifest['total_parts']
        original_crc = manifest['original_crc32']
        compressed_size = manifest['compressed_size']

        if output_file is None:
            output_path = parts_dir / original_filename
        else:
            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)

        # Предварительная проверка наличия и размера частей
        missing_parts = []
        for part_info in manifest['parts']:
            part_path = parts_dir / part_info['filename']
            if not part_path.exists():
                missing_parts.append(part_info['part_number'])
            elif part_path.stat().st_size != part_info['size']:
                # Можно добавить более детальную проверку CRC здесь, но это замедлит процесс
                pass

        if missing_parts:
            raise RuntimeError(f"Отсутствуют части: {missing_parts}")

        # Используем zlib.decompressobj для потоковой распаковки
        decompressor = zlib.decompressobj()
        actual_compressed_size = 0

        try:
            with open(output_path, 'wb') as out_f:
                for part_info in manifest['parts']:
                    part_path = parts_dir / part_info['filename']

                    with open(part_path, 'rb') as p_f:
                        # Читаем часть кусками для экономии памяти
                        while True:
                            chunk = p_f.read(1024 * 1024)
                            if not chunk:
                                break

                            actual_compressed_size += len(chunk)
                            decompressed_chunk = decompressor.decompress(chunk)
                            out_f.write(decompressed_chunk)

                # Завершаем распаковку
                remaining = decompressor.flush()
                out_f.write(remaining)
        except Exception as e:
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(f"Ошибка при сборке/распаковке: {e}")

        if actual_compressed_size != compressed_size:
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(
                f"Размер собранных данных ({actual_compressed_size}) "
                f"не совпадает с ожидаемым ({compressed_size})"
            )

        assembled_crc = self.calculate_crc32(str(output_path))

        if assembled_crc != original_crc:
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(
                f"Ошибка целостности! CRC32 собранного файла ({assembled_crc}) "
                f"не совпадает с оригиналом ({original_crc})"
            )

        return {
            'success': True,
            'output_file': str(output_path),
            'original_crc32': original_crc,
            'original_size': manifest['original_size'],
            'compressed_size': compressed_size,
            'verified': True
        }