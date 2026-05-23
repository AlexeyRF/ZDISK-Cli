# ZDisk CLI

ZDisk - это инструмент командной строки для облачного хранения файлов в национальном мессенджере MAX (МАКС).

## Основные возможности

- **Шифрование:** Возможность защиты файлов паролем перед загрузкой (AES-256).
- **Поддержка больших файлов:** Автоматическое разбиение файлов на части (до 2 ГБ) для обхода ограничений и докачка при потере соединения.
- **Управление папками:** Создание виртуальной структуры папок.
- **Корзина:** Система удаления файлов с возможностью восстановления или окончательной очистки.
- **Вход по QR:** Безопасная авторизация через мобильное приложение.

## Установка

1. Установите зависимости (требуется Python 3.10+):
   ```bash
   pip install cryptography aiohttp maxapi-python qrcode
   ```

## Использование

1. **Настройка:**
   ```bash
   python zdisk_cli.py config --chat 0
   ```
   чат 0 отвечает за избранное
2. **Вход:**
   ```bash
   python zdisk_cli.py login
   ```
3. **Список файлов:**
   ```bash
   python zdisk_cli.py ls
   python zdisk_cli.py ls /documents
   ```
4. **Загрузка файла:**
   ```bash
   python zdisk_cli.py upload my_file.txt /backup
   python zdisk_cli.py upload private.docx / -p my_password
   ```
5. **Скачивание файла:**
   ```bash
   python zdisk_cli.py download my_file.txt ./downloads
   ```
6. **Удаление:**
   ```bash
   python zdisk_cli.py rm my_file.txt
   python zdisk_cli.py rm old_file.txt --permanent
   ```
7. **Корзина:**
   ```bash
   python zdisk_cli.py trash --list
   python zdisk_cli.py trash --restore 0
   python zdisk_cli.py trash --clear
   ```

## Структура проекта

- `zdisk_cli.py` - Главный файл приложения.
- `zdisk_client.py` - Клиент для взаимодействия с API мессенджера.
- `zdisk_crypto.py` - Модуль для шифрования и дешифрования файлов.
- `zdisk_files.py` - Логика подготовки файлов к загрузке (разбиение/сборка).
- `file_splitter.py` / `file_assembler.py` - Инструменты для работы с частями файлов.

## Post-Scriptum

Дожидайтесь явного подтверждения от программы, при скачивании больших файлов уведомление может появиться с задержкой.