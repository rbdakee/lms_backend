"""Презентация из шаблонов — сценарий колоды (PPTX_TEMPLATES_SPEC, раздел 2).

Когда в установке есть библиотека шаблонов (`lesson_ai/pptx_templates`),
вид `presentation` генерируется этой схемой вместо `schemas/presentation`:
слайд — не «тип и поля», а выбор заготовки (`template`) и заполнение её
слотов (`fill`). Оформление рисует дизайнер в `library.pptx`, модель —
только содержимое, поэтому текста на слайде помещается мало, и лимиты
слотов названы в каталоге шаблонов.

Строгий режим провайдера требует, чтобы у каждого объекта все поля были
обязательными, поэтому слоты передаются списком записей (`slot` + что
вписывать), а не словарём: в словаре модель обязана была бы перечислить
слоты чужих шаблонов. Формат записи один на все виды слотов — пустые
поля остаются пустыми, чьё когда — сказано в описаниях.

Титульный слайд и «Сегодня на уроке» собирает сервер, как и раньше:
тема, класс и цели урока — из комплекта, а не от модели. Блоки те же,
что у прежней схемы (`goals`, три этапа), поэтому оглавление помощника,
нумерация слайдов и правка из чата не меняются.

Старые материалы (слайды с `layout`) живут в базе вечно, и править их
помощник должен и после перехода на шаблоны. Для проверки ответа и правок
`any_form_draft()` принимает обе формы слайда; провайдеру же уходит
строго одна (`PresentationDeckDraft`): объединение форм он переносит
плохо, а новую колоду модель пишет только по шаблонам.
"""

from typing import Literal

from pydantic import BaseModel, Field, create_model, field_validator

from app.lesson_ai.images import known_or_empty
from app.lesson_ai.schemas import Strict, Text
from app.lesson_ai.schemas.presentation import Slide

__all__ = [
    "ANIMATIONS",
    "ADD_KINDS",
    "ADD_TEXT_STYLES",
    "STAGE_FIELDS",
    "DeckAnimation",
    "DeckSlide",
    "PresentationDeckDraft",
    "any_form_draft",
]

AnimationEffect = Literal["none", "appear", "fade"]
ANIMATIONS: tuple[str, ...] = AnimationEffect.__args__
ADD_KINDS: tuple[str, ...] = ("text", "image", "shape")
ADD_TEXT_STYLES: tuple[str, ...] = ("title", "body", "caption", "accent")
# Блоки слайдов — те же, что у прежней схемы презентации
STAGE_FIELDS = ("slides_beginning", "slides_middle", "slides_end")


class SlotFill(Strict):
    slot: str = Field(
        description=(
            "Имя слота шаблона — из каталога шаблонов (в правилах выше). Слот"
            " вписывается одной записью; запись по слоту, которого в шаблоне нет,"
            " пропускается с предупреждением."
        )
    )
    text: str = Field(
        default="",
        description=(
            "Текст для слота вида text или number: одна строка на детей, коротко."
            " Для number — число, слово или равенство не длиннее 12 знаков. У"
            " слотов других видов пусто."
        ),
    )
    items: list[str] = Field(
        default_factory=list,
        description=(
            "Пункты для слота вида bullets — по пункту на строку, в порядке"
            " появления. Без букв-номеров в начале: их ставит шаблон. У слотов"
            " других видов пусто."
        ),
    )
    image: str = Field(
        default="",
        description=(
            "Только слот вида image: ключ картинки из каталога готовых изображений"
            " (список ключей — в правилах выше). Картинка вписывается в рамку слота"
            " с сохранением пропорций. Ключа нет — слот останется пустым, поэтому"
            " свой ключ не выдумывать."
        ),
    )

    @field_validator("image")
    @classmethod
    def _known_image(cls, value: str) -> str:
        return known_or_empty(value)


class AddItem(Strict):
    kind: Literal["text", "image", "shape"] = Field(
        description=(
            "Что добавить поверх шаблона: text — надпись; image — картинка из"
            " каталога изображений; shape — фигура-украшение. Добавлять только"
            " то, чего шаблону не хватает: подпись, стрелка, значок."
        )
    )
    x: float = Field(description="Отступ слева в процентах от ширины слайда, 0–100.")
    y: float = Field(description="Отступ сверху в процентах от высоты слайда, 0–100.")
    w: float = Field(description="Ширина в процентах от ширины слайда, 1–100.")
    h: float = Field(description="Высота в процентах от высоты слайда, 1–100.")
    text: str = Field(
        default="", description="Текст надписи — только для kind=text; иначе пусто."
    )
    image: str = Field(
        default="",
        description="Ключ картинки из каталога готовых изображений — только для kind=image.",
    )
    style: str = Field(
        default="",
        description=(
            "Вид добавленного. Для text: title — крупный заголовок, body — обычный"
            " текст, caption — мелкая подпись, accent — выделенный текст. Для shape:"
            " circle — круг, rounded — плашка со скруглением, arrow — стрелка,"
            " star — звезда; цвет — из палитры шаблона. Для image пусто."
        ),
    )

    @field_validator("image")
    @classmethod
    def _known_image(cls, value: str) -> str:
        return known_or_empty(value)


class DeckAnimation(Strict):
    effect: AnimationEffect = Field(
        default="appear",
        description=(
            "Анимация появления: appear — по клику, fade — по клику и плавно,"
            " none — слайд показывают целиком. Смысл тот же, что у прежней"
            " схемы: открывать по клику то, что дети сначала угадывают."
        ),
    )
    order: list[str] = Field(
        default_factory=list,
        description=(
            "Что и в каком порядке открывается по клику: имена слотов шаблона,"
            " а для добавленного — add:1, add:2 по номеру записи в add. Один"
            " элемент списка — один щелчок учителя. Пусто — порядок по умолчанию"
            " из каталога шаблона; none — список не нужен."
        ),
    )


class DeckSlide(Strict):
    template: str = Field(
        description=(
            "Ключ шаблона из каталога (в правилах выше). Выбирай по смыслу и"
            " чередуй: соседние слайды — из разных шаблонов, один и тот же"
            " шаблон два раза подряд — скучно. Шаблона нет в каталоге — слайд"
            " не соберётся, поэтому ключ только из каталога."
        )
    )
    fill: list[SlotFill] = Field(
        default_factory=list,
        description=(
            "Заполнение слотов шаблона: по записи на слот, который заполняешь."
            " Обязательные слоты шаблона (обязательно — в каталоге) заполняй"
            " всегда; необязательные — по смыслу. Держись лимитов знаков и"
            " пунктов из каталога: превышение не ошибка, но текст станет мельче."
        ),
    )
    remove: list[str] = Field(
        default_factory=list,
        description=(
            "Какие украшения шаблона убрать: имена из списка removable в каталоге"
            " шаблона, как они там названы. Без нужды шаблон не перекраивай."
        ),
    )
    add: list[AddItem] = Field(
        default_factory=list,
        description=(
            "Что дорисовать поверх шаблона. Пусто почти всегда: у шаблона есть"
            " всё нужное. Дорисовывай, только когда без этого слайд непонятен."
        ),
    )
    animation: DeckAnimation = Field(
        default_factory=DeckAnimation,
        description=(
            "Анимация слайда: что открывается по клику. Открывай то, что дети"
            " сначала думают: варианты ответа, ответ, число, пункты шагов."
        ),
    )
    notes: Text = Field(
        description=(
            "Заметки для учителя: к какому шагу плана урока слайд, что учитель"
            " говорит и спрашивает, когда переключает слайд. На слайде не видны."
        )
    )


class PresentationDeckDraft(Strict):
    """Ответ модели по презентации из шаблонов. Блоки — те же, что у прежней
    схемы: титульный и «Сегодня на уроке» впереди добавляет сервер."""

    goals: list[Text] = Field(
        min_length=1,
        description=(
            "Слайд «Сегодня на уроке»: цели урока из плана словами ребёнка —"
            " «научимся…», «узнаем…». Две-три строки."
        ),
    )
    slides_beginning: list[DeckSlide] = Field(
        min_length=1,
        description=(
            "Слайды начала урока, 0–5 минут: настрой, разминка, выход на тему —"
            " ровно то, что в начале урока по плану. Один-два слайда."
        ),
    )
    slides_middle: list[DeckSlide] = Field(
        min_length=1,
        description=(
            "Слайды середины урока, 5–35 минут: по слайду на каждый шаг середины"
            " урока в плане, в том же порядке, с теми же числами и заданиями;"
            " объяснение нового — на примере, физминутка — если она есть в плане."
            " Шаблоны чередуй: проверка — вопрос с вариантами, главное число —"
            " крупное число, шаги алгоритма — шаги."
        ),
    )
    slides_end: list[DeckSlide] = Field(
        min_length=1,
        description=(
            "Слайды конца урока, 35–40 минут: итог, рефлексия, домашнее задание —"
            " как в конце урока по плану. Один-два слайда."
        ),
    )


def any_form_draft() -> type[BaseModel]:
    """Та же схема, но пункт блока — слайд-сценарий ИЛИ слайд прежней формы.

    Проверка ответа и правок принимает обе формы: старые материалы живут
    в базе вечно, и разворачивать их в сценарий при правке нельзя — они
    собираются старым путём. Провайдеру эта схема не уходит: см. докстринг
    модуля. Строится из `PresentationDeckDraft`, чтобы описания полей
    не разошлись. Слайд-сценарий первым в объединении: слайд с `template`
    и слайд с `layout` друг друга не путают — у каждого лишние поля чужой
    формы запрещены.
    """
    fields: dict = {}
    for name, field in PresentationDeckDraft.model_fields.items():
        annotation = list[DeckSlide | Slide] if name in STAGE_FIELDS else field.annotation
        fields[name] = (annotation, field)
    return create_model("PresentationDeckAnyForm", __base__=Strict, **fields)
