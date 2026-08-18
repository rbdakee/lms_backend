"""Подписанная ссылка на файл: путь + время истечения + подпись на серверном
секрете. Смысл — не «никто не скачает», а «пересылка ссылки не работает»
(BACKEND_NOTES, раздел 9): у ссылки короткий срок жизни, и через час она
не открывается ни у кого.

Формат выбран не свой, а тот, который умеет проверять nginx: в бою байты
раздаёт он, без похода в приложение. Локально ровно то же делает бэкенд,
чтобы разработка не требовала nginx. Подпись — base64url от бинарного md5
без выравнивающих «=»; md5 здесь не про стойкость хэша, а про формат модуля,
защищает секрет.

Соответствующий кусок конфигурации nginx:

    location /files/lesson/ {
        secure_link $arg_s,$arg_e;
        secure_link_md5 "$secure_link_expires$uri STORAGE_SECRET";
        if ($secure_link = "") { return 403; }   # подпись не сошлась
        if ($secure_link = "0") { return 403; }  # срок вышел
        alias /srv/storage/;
    }

Привязки к IP в строке подписи нет сознательно: у мобильного интернета
адрес меняется, и видео оборвалось бы посреди урока.
"""

import base64
import hashlib
import hmac


def sign(path: str, expires: int, secret: str) -> str:
    # Порядок и пробел перед секретом — из директивы secure_link_md5 выше:
    # строка должна собираться здесь и в nginx одинаково, до байта
    digest = hashlib.md5(f"{expires}{path} {secret}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def is_valid(path: str, expires: int, signature: str, secret: str, *, now: int) -> bool:
    if expires <= now:
        return False
    # Сравниваем байтами: в адресе может оказаться что угодно, а сравнение
    # строк с не-ASCII compare_digest не умеет вовсе
    return hmac.compare_digest(sign(path, expires, secret).encode(), signature.encode())
