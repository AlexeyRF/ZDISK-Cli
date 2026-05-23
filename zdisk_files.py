import os
import json
from pathlib import Path
from file_splitter import FileSplitter
from file_assembler import FileAssembler
import shutil
class ZDiskFiles:
    """Wraps splitting and assembly logic for ZDisk."""
    
    # MAX_PART_SIZE = 2 * 1024 * 1024 * 1024 - 10 * 1024 * 1024 # Slightly less than 2GB
    # For testing, we might want to use a smaller size, e.g., 50MB
    MAX_PART_SIZE = 1900 * 1024 * 1024 # Approx 1.9GB to be safe within 2GB limit
    
    def __init__(self, temp_dir: str = None):
        if temp_dir is None:
            import tempfile
            self.temp_dir = Path(tempfile.gettempdir()) / "zdisk_temp"
        else:
            self.temp_dir = Path(temp_dir)
            
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.splitter = FileSplitter(chunk_size=self.MAX_PART_SIZE)
        self.assembler = FileAssembler()

    def prepare_upload(self, file_path: str) -> dict:
        """
        Splits file if necessary and returns info.
        Returns a dict with 'is_split', 'parts_dir', and 'manifest_file'.
        """
        file_size = os.path.getsize(file_path)
        if file_size > self.MAX_PART_SIZE:
            result = self.splitter.split_file(file_path, output_dir=str(self.temp_dir))
            return {
                'is_split': True,
                'parts_dir': result['parts_dir'],
                'manifest_file': result['manifest_file'],
                'total_parts': result['total_parts']
            }
        else:
            return {
                'is_split': False,
                'file_path': file_path
            }

    def assemble(self, manifest_file: str, output_path: str = None) -> str:
        """Assembles file from manifest."""
        result = self.assembler.assemble_file(manifest_file, output_file=output_path)
        return result['output_file']

    def cleanup(self, path: str):
        """Cleans up temporary files/directories."""
        try:
            p = Path(path)
            if p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass

    def clean_all(self):
        """Cleans up everything in the central temp directory."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir.mkdir(parents=True, exist_ok=True)
