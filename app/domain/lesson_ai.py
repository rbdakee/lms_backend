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

# В каком формате материал скачивается файлом. Презентация — настоящий `.pptx`:
# учитель правит слайд под свой класс, а PDF не правится (раздел 5).
FILE_FORMATS = {kind: "docx" for kind in MATERIAL_KINDS} | {"presentation": "pptx"}

# Два уровня заказа вместо флага «геймификация» (раздел 4.3). Середина —
# значение по умолчанию: учитель проскочил вопрос, комплект всё равно собирается.
ENGAGEMENT_LEVELS = ("calm", "active", "full_game")
DIFFICULTY_LEVELS = ("basic", "standard", "advanced")
DEFAULT_ENGAGEMENT = "active"
DEFAULT_DIFFICULTY = "standard"

# Из каких материалов собирается `answer_key`: у каждой их задачи есть поле
# `answer`, заполненное тогда же, когда задача придумана (раздел 4.2).
ANSWER_SOURCES = ("tasks", "homework", "assessment")

# Чего ждёт задание, прежде чем его можно взять. План урока идёт первым,
# пять видов получают его на вход; ключи собираются из готовых задач
# (раздел 4.2). Ждут, пока задание-предшественник живо (`queued` или
# `running`, включая паузу между попытками); закрытое любым исходом
# отпускает зависимых — упавший план не должен оставить учителя без
# остального комплекта (раздел 7).
WAITS_FOR = {
    "presentation": ("lesson_plan",),
    "tasks": ("lesson_plan",),
    "homework": ("lesson_plan",),
    "assessment": ("lesson_plan",),
    "game": ("lesson_plan",),
    "answer_key": ANSWER_SOURCES,
}

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
    "no_sources": "Не из чего собрать ключи: задания, домашняя и проверочная не готовы",
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


def assemble_answer_key(sources: Iterable[tuple[str, Iterable[tuple[str, object]]]]) -> dict:
    """Ключи с ответами — из готовых материалов, без модели (раздел 4.2).

    `sources` — материалы в порядке видов, каждый — блоками в порядке формы.
    Группа задач в любой схеме — объект с `title` и списком `tasks`, у каждой
    задачи есть `text` и `answer`; остальное устройство схемы вида ключам
    безразлично. Поэтому новая форма задания не требует правки сборки.

    Ответ: по ключу на вид, внутри — группы в порядке документа и задачи
    с номером внутри группы, как их видит ребёнок на карточке.
    """
    key: dict[str, list[dict]] = {}
    for kind, blocks in sources:
        groups = [
            {
                # Блок, из которого группа: у заданий это уровень карточек, и без
                # него две карточки «Найди пару» разных уровней в ключах не различить
                "block": name,
                "title": group.get("title") or "",
                "answers": [
                    {"number": number, "task": task["text"], "answer": task["answer"]}
                    for number, task in enumerate(group["tasks"], start=1)
                ],
            }
            for name, value in blocks
            for group in _task_groups(value)
        ]
        if groups:
            key[kind] = groups
    return key


def _task_groups(value) -> Iterator[dict]:
    if isinstance(value, dict):
        tasks = value.get("tasks")
        if isinstance(tasks, list) and tasks and all(_is_task(task) for task in tasks):
            yield value
            return
        for item in value.values():
            yield from _task_groups(item)
    elif isinstance(value, list):
        for item in value:
            yield from _task_groups(item)


def _is_task(value) -> bool:
    return isinstance(value, dict) and "text" in value and "answer" in value
