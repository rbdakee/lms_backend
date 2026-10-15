"""Файл, приложенный к чату «Жоспара»: узнать вид и достать текст.

Вид определяется по **сигнатуре байтов**, а не по расширению имени: браузер
присылает имя как есть, и `pasport.jpg`, оказавшийся PDF-сканом, не должен
уходить модели картинкой. Проверяются только первые байты и структура ZIP —
содержимое файла в лог не попадает нигде.

Страницы PDF считаются без новых пакетов: подсчётом объектов `/Type /Page`
(не `/Pages`) в сырых байтах. Это грубо: страницы, спрятанные провайдером
в сжатые object streams (редкий способ сохранения), не считаются, и файл
пройдёт с меньшим числом. Грубость осознанная — потолок здесь защита
счёта за токены, а не точный метр; всерьёз считать страницы можно только
PDF-библиотекой, новой зависимости ради этого заводить нельзя.

Текст `.docx` и `.pptx` достаётся самими `python-docx`/`python-pptx`:
у этих форматов текст лежит в XML открыто, и вызов модели ради того,
что уже лежит в байтах, — деньги за воздух.
"""

import io
import re
from collections.abc import Callable

from app.domain.lesson_ai import ATTACHMENT_SUMMARY_MAX

# Сигнатуры картинок: JPEG (FF D8 FF), PNG, WEBP (RIFF….WEBP).


def sniff(data: bytes) -> str | None:
    """Вид вложения по байтам: `image` | `pdf` | `docx` | `pptx` | None.

    None значит «не принимаем» — включая HEIC: браузеры на iPhone сами
    конвертируют его в JPEG при выборе файла, а присланное мимо браузера
    фото в JPEG не превратится.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image"
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return _sniff_zip(data)
    return None


def is_heic(data: bytes) -> bool:
    """HEIC и HEIF: ISO BMFF-контейнер, бренд в коробке `ftyp` (байты 4–11).
    Отдельный ответ для него — учитель понятную просьбу «сохраните как JPG»
    читает, а безымянный отказ «тип не поддерживается» — нет."""
    return data[4:8] == b"ftyp" and data[8:12] in (
        b"heic",
        b"heix",
        b"hevc",
        b"heim",
        b"heis",
        b"hevm",
        b"hevs",
        b"mif1",
        b"msf1",
    )


def _sniff_zip(data: bytes) -> str | None:
    """OOXPS — это ZIP с обязательной частью: `word/document.xml` у Word,
    `ppt/presentation.xml` у PowerPoint. Прочий ZIP (архив с картинками,
    переименованный в .docx) — не наш формат."""
    import zipfile

    try:
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    except Exception:
        # Обрезанный или переименованный файл: байты соврали о формате
        return None
    if "word/document.xml" in names:
        return "docx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    return None


# Объект страницы в PDF. `/Type /Page` с любым числом пробелов; `(?![s])`
# отсекает `/Type /Pages` — узел дерева страниц, а не саму страницу.
_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![s])")


def pdf_pages(data: bytes) -> int:
    """Сколько объектов страниц в байтах PDF — грубо, но без новых пакетов
    (см. докстринг модуля). Ноль — файл без распознанных страниц: потолок
    пусть проверяет сам счёт, а не остановка разбора."""
    return len(_PAGE_OBJECT.findall(data))


def docx_text(data: bytes) -> str:
    """Весь текст документа Word: абзацы и таблицы, в порядке документа.
    Содержимое файла в исключении не нужно никому — наружу идёт только тип."""
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ExtractError from exc
    lines = [para.text for para in document.paragraphs if para.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                lines.append("\t".join(cells))
    return "\n".join(lines)


def pptx_text(data: bytes) -> str:
    """Текст презентации по слайдам: модель и учитель читают его как доклад,
    а не мешком строк."""
    import pptx

    try:
        presentation = pptx.Presentation(io.BytesIO(data))
    except Exception as exc:
        raise ExtractError from exc
    lines = []
    for number, slide in enumerate(presentation.slides, start=1):
        texts = []
        for shape in slide.shapes:
            frame = getattr(shape, "text_frame", None)
            if frame is None:
                continue
            text = frame.text.strip()
            if text:
                texts.append(text)
        if texts:
            lines.append(f"— Слайд {number} —\n" + "\n".join(texts))
    return "\n\n".join(lines)


class ExtractError(Exception):
    """Текст из файла не достался: ZIP открылся, а содержимое битое.
    Вложение получает `failed`, а процесс — не падает."""


# Расход: docx и pptx разбираются без модели, токенов у них нет
EXTRACTORS: dict[str, Callable[[bytes], str]] = {"docx": docx_text, "pptx": pptx_text}

# Офисные форматы — MIME из стандарта; у картинок подтип виден по байтам
MIME_BY_KIND = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def image_mime(data: bytes) -> str:
    """Точный подтип картинки — по той же сигнатуре, что дала `image`:
    в `data:` URI и в колонке `mime` должно лежать то, чем файл является."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def snip(text: str, limit: int = ATTACHMENT_SUMMARY_MAX) -> str:
    """Выжимка в разумном пределе, с честной пометкой об обрезке: без неё
    модель приняла бы обрыв за конец документа и достраивала бы сама."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[Текст обрезан — файл длиннее, показано начало]"
