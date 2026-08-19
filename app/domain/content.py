"""Содержимое, которое пишет админ: разметка урока и задания, ссылка на видео.

Форма зафиксирована в CONTRACT.md, раздел «Сессия 7а»: `{"html": "…"}`.
Чистит разметку сервер, а не редактор на клиенте: то, что вернулось
из PATCH, и лежит в базе — иначе автор не увидит, что его разметку
почистили, и будет чинить одно и то же во второй раз.
"""

import re
from html import unescape

import nh3

# Белый список из DESIGN_BRIEF, 5.18: жирный, курсив, заголовки, списки,
# цитата, ссылка, таблица. Больше визуальный редактор урока не умеет,
# и всё, что приходит сверх этого, — след вставки из Word.
ALLOWED_TAGS = {
    "p", "br", "h2", "h3", "b", "strong", "i", "em", "u", "s",
    "ul", "ol", "li", "blockquote", "a",
    "table", "thead", "tbody", "tr", "th", "td",
}

ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
}

# javascript: и data: в href — это скрипт на экране каждого учителя.
ALLOWED_SCHEMES = {"http", "https", "mailto"}

# Пробелы, которых не видит str.strip(): для Python это обычные символы,
# а на экране — такая же пустота, как и всё остальное поле.
_INVISIBLE = str.maketrans("", "", "\u200b\u200c\u200d\ufeff")

# Все обычные написания ссылки на YouTube. Других хостингов пока нет
# (DESIGN_BRIEF, 5.18), и длительность вводится руками: по чужой ссылке
# она ненадёжна.
_YOUTUBE_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^&]*&)*v=|embed/|shorts/|live/|v/))"
    r"([\w-]{11})"
)


def sanitize_html(raw: str) -> str:
    """Разметка по белому списку. Лишнее вырезается молча, без ошибки:
    отбивать сохранение урока из-за пришедшего `<span>` бессмысленно —
    человек всё равно не знает, откуда тот взялся."""
    return nh3.clean(
        raw,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_SCHEMES,
        link_rel="noopener noreferrer",
    )


def is_blank_html(html: str) -> bool:
    """Пустое содержимое. Редактор на пустом поле присылает не пустую строку,
    а `<p></p>`, `<p><br></p>` или `<p>&nbsp;</p>`, и сохранять такой урок
    как заполненный — значит соврать в чек-листе готовности курса.

    Сущности раскодируются до проверки: `nh3` отдаёт неразрывный пробел
    именно сущностью, и без этого `&nbsp;` осталось бы текстом из шести
    непробельных символов.
    """
    return not unescape(nh3.clean(html, tags=set())).translate(_INVISIBLE).strip()


def youtube_id(url: str) -> str | None:
    """Идентификатор видео или None, если на ссылку YouTube не похоже."""
    found = _YOUTUBE_RE.search(url)
    return found.group(1) if found else None


def normalize_youtube(url: str) -> str | None:
    """Ссылка в одном написании: иначе одно и то же видео лежит в базе
    четырьмя разными строками, и найти его правкой нельзя."""
    video = youtube_id(url)
    return f"https://www.youtube.com/watch?v={video}" if video else None
