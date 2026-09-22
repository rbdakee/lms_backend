"""«Жоспар» — помощник учителя (TEACHER_AI_BRIEF). Правила без фреймворков:
виды материалов, статусы, повторы и поиск кода цели обучения в тексте.

В коде и базе продукт называется `lesson_ai`: бренд живёт в текстах
интерфейса, и его переименование не должно стоить миграции (раздел 10).
"""

import re
from collections.abc import Iterable, Iterator

# Семь видов материала, состав утверждён владельцем 20.09.2026 (раздел 4).
# `kind` — одно имя во всей цепочке: колонка, поле JSON, тип на фронте.
MATERIAL_KINDS = (
    "lesson_plan",
    "presentation",
    "tasks",
    "homework",
    "assessment",
    "game",
    "answer_key",
)

# В каком формате материал скачивается файлом. `.pptx` у презентации и
# `.docx` у остальных пяти появятся с сессией L5 — вместе с их видами.
FILE_FORMATS = {"lesson_plan": "docx"}

KIT_STATUSES = ("generating", "ready", "partial", "failed")
MATERIAL_STATUSES = ("queued", "generating", "ready", "failed")
JOB_STATUSES = ("queued", "running", "done", "failed")
# Откуда взялось содержимое блока. Первая генерация, блок, переписанный моделью
# по просьбе учителя, ручная правка и откат к прежней версии (раздел 4.1, правило 3).
VERSION_SOURCES = ("generated", "regenerated", "edited", "restored")

# Попыток на задание три. Паузы между ними растут: провайдер, отказавший
# по потолку токенов в минуту, через секунду откажет снова (раздел 7).
RETRY_DELAYS_SEC = (30, 120)
MAX_ATTEMPTS = len(RETRY_DELAYS_SEC) + 1


class JobError(Exception):
    """Задание не выполнено. `code` уходит учителю на экран материала,
    `retryable` решает, будет ли ещё попытка.

    Текст исключения наружу не идёт: в нём бывает тема урока и пожелания
    учителя, а экрану хватает кода. Токены — если модель успела ответить:
    кривой ответ стоит тех же денег, что и ровный.
    """

    def __init__(self, code: str, *, retryable: bool, tokens_in: int = 0, tokens_out: int = 0):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


class BlockOutputInvalidError(Exception):
    """Модель переписала один блок не по форме или вписала в него код цели —
    и так на всех попытках. Учителю это то же, что «модель не ответила»:
    помогает ещё одна попытка."""


# Что видит учитель у упавшего материала. Код — для ветвления на фронте,
# текст — готовая строка, как у всех ошибок API.
JOB_ERROR_MESSAGES = {
    "ai_failed": "Модель не ответила",
    "model_output_invalid": "Модель вернула документ не по форме",
    "interrupted": "Генерация прервалась",
    "program_not_published": "Программа снята с публикации — генерация по ней закрыта",
    "internal_error": "Внутренняя ошибка сервера",
}


def retry_delay(attempts: int) -> int | None:
    """Через сколько секунд повторить после `attempts` неудачных попыток;
    None — попытки кончились."""
    if attempts >= MAX_ATTEMPTS:
        return None
    return RETRY_DELAYS_SEC[attempts - 1]


def kit_status(material_statuses: Iterable[str]) -> str:
    """Статус комплекта — сводка по его материалам, а не отдельное состояние.

    Пока хоть один материал ждёт или собирается — комплект собирается.
    Дальше три исхода: готово всё, готова часть, не готово ничего. Часть —
    отдельное значение, потому что один сбойный вид не должен выглядеть
    в списке уроков как пропавший комплект (раздел 7).
    """
    statuses = list(material_statuses)
    if not statuses or any(s in ("queued", "generating") for s in statuses):
        return "generating"
    ready = sum(1 for s in statuses if s == "ready")
    if ready == len(statuses):
        return "ready"
    return "partial" if ready else "failed"


# Код цели обучения: класс, раздел, подраздел, номер — `2.1.2.6`. Четыре части,
# поэтому ни дата `16.09.2022`, ни номер раздела `1.2` под него не попадают.
_CODE = re.compile(r"(?<![\d.])\d{1,2}\.\d{1,2}\.\d{1,2}\.\d{1,2}(?![\d.]*\d)")


def strip_objective_codes(text: str) -> str:
    """Текст без кодов целей — то, что уходит модели.

    Модели код не дают вовсе (раздел 4.1, правило 2): ей неоткуда его
    списать, и в документ его кладёт сервер из справочника. Учитель мог
    вписать код в тему руками, а в формулировке цели он мог остаться от
    склейки ячеек при разборе приказа.
    """
    stripped = _CODE.sub("", text)
    # Код посреди фразы оставляет двойной пробел или пробел перед знаком
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    stripped = re.sub(r" ([,.;:)])", r"\1", stripped)
    return stripped.strip()


def objective_codes_in(value) -> list[str]:
    """Все коды целей в ответе модели — во всех строках на любой глубине.

    Непустой список значит, что модель код сочинила: ответ отбивается
    и генерируется заново.
    """
    return [match for text in _strings(value) for match in _CODE.findall(text)]


def _strings(value) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
