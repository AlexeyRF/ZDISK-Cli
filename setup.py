from setuptools import setup

setup(
    name="zdisk",
    version="1.0.0",
    py_modules=["zdisk_cli", "zdisk_client", "zdisk_crypto", "zdisk_files", "file_splitter", "file_assembler"],
    install_requires=[
        "cryptography",
        "aiohttp",
        "maxapi-python>=2.2.0",
        "qrcode",
    ],
    entry_points={
        "console_scripts": [
            "zdisk=zdisk_cli:run",
        ],
    },
)
