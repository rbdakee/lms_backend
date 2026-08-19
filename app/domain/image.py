"""Картинка ли это — по байтам, а не по имени.

Имя файла присылает клиент, и «печать.png», надетое на договор, проходило бы
проверку насквозь: настройки сохранили бы docx, а сломалась бы генерация PDF —
потом, у выдающего сертификат админа, и молча. Поэтому формат опознаётся
по началу объекта.

Библиотеки ради этого не заводим: сигнатур пять, а `imghdr` из stdlib удалён
в 3.13.
"""

# Сигнатура — первые байты файла. WEBP и SVG живут ниже отдельно: у первого
# метка формата стоит за длиной, у второго сигнатуры нет вовсе.
SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# Сколько байт нужно, чтобы решить: хватает на любую сигнатуру и на пролог
# XML с пробелами и BOM перед корневым тегом svg.
HEAD_SIZE = 512


def image_mime(head: bytes) -> str | None:
    """Тип картинки по её началу; None — это не картинка."""
    for signature, mime in SIGNATURES:
        if head.startswith(signature):
            return mime
    # RIFF....WEBP: между меткой контейнера и меткой формата лежит длина
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if _looks_like_svg(head):
        return "image/svg+xml"
    return None


def _looks_like_svg(head: bytes) -> bool:
    """У SVG сигнатуры нет: это текст, который после пробелов начинается
    прологом XML или сразу корневым тегом."""
    text = head.decode("utf-8", "ignore").lstrip("\ufeff").lstrip()
    return text.startswith("<?xml") or text.startswith("<svg")
