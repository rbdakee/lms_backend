"""ҚМЖ «Жоспара»: JSON-схема, которую видит модель, и разбор её ответа.

Схема и проверка ответа выводятся из одной pydantic-модели (L3, решение 9),
поэтому здесь проверяется, что к модели уходит именно то, что принимает строгий
режим провайдера, и что описания полей — инструкция к каждому блоку — на месте.
Главное правило — кода цели обучения модели не во что писать и не с чего
списать, а сочинённый код отбивается (раздел 4.1, правило 2; L3, решение 10):
сочинённый код проваливает проверку у завуча.

Разбор ответа проверяется на подставном ИИ без базы: `Prepared` собирается
руками, генератор в базу не ходит вовсе. Образец ответа — сокращённый настоящий
ответ модели на русский ҚМЖ из живого прогона L3 (решение 16).
"""

import copy
import json
from itertools import pairwise

import pytest

from app.domain.lesson_ai import JobError, objective_codes_in, strip_objective_codes
from app.lesson_ai.context import LessonBrief, Prepared
from app.lesson_ai.kinds import LESSON_PLAN, generators
from app.lesson_ai.schemas import strict_schema
from app.lesson_ai.schemas.lesson_plan import STAGE_MINUTES, LessonPlanDraft
from tests.conftest import FakeAi

# Порядок формы ҚМЖ (TEACHER_AI_BRIEF, раздел 4): в нём блоки лежат в документе,
# в нём их печатает выгрузка в Word
FORM_ORDER = [
    "lesson_goals",
    "assessment_criteria",
    "language_goals",
    "values",
    "cross_curricular_links",
    "prior_knowledge",
    "stage_beginning",
    "stage_middle",
    "stage_end",
    "differentiation",
    "assessment",
    "health_and_safety",
    "reflection",
]
STEP_FIELDS = ["teacher", "students", "resources"]
# Всё, что строгий режим провайдера принимает наверняка; остальное к модели не уходит
ALLOWED_KEYS = {
    "type",
    "description",
    "items",
    "enum",
    "anyOf",
    "properties",
    "required",
    "additionalProperties",
}
FORBIDDEN_KEYS = {"$ref", "$defs", "title", "minItems", "minLength"}

CODE = "2.1.2.6"
OBJECTIVE = "выполнять устно сложение и вычитание двузначных чисел без перехода через десяток"

BRIEF = LessonBrief(
    lang="ru",
    grade=2,
    subject="Математика",
    section="Числа и величины",
    subsection="Операции над числами",
    objectives=(OBJECTIVE,),
    topic="Сложение и вычитание двузначных чисел",
    class_size=24,
    notes="",
)
PREPARED = Prepared(
    brief=BRIEF,
    section={"title": "Числа и величины", "subsection": "Операции над числами"},
    objectives=[{"code": CODE, "text": OBJECTIVE, "marker": ""}],
)

DRAFT = {
    "lesson_goals": [
        "Учащиеся научатся устно складывать двузначные числа без перехода через десяток.",
        "Учащиеся смогут объяснять ход вычислений и обосновывать свои действия.",
    ],
    "assessment_criteria": [
        "Учащийся правильно определяет разряды в двузначных числах.",
        "Учащийся объясняет устно способ решения задачи.",
    ],
    "language_goals": {
        "vocabulary": ["двузначное число", "десятки", "единицы", "разряд"],
        "phrases": [
            "В числе 46 — 4 десятка и 6 единиц.",
            "Я складываю десятки и единицы по отдельности.",
        ],
    },
    "values": "Взаимоуважение при совместном выполнении заданий через работу в парах.",
    "cross_curricular_links": "Познание мира: подсчёт предметов в окружающей среде.",
    "prior_knowledge": "Учащиеся умеют складывать и вычитать однозначные числа.",
    "stage_beginning": [
        {
            "teacher": "Записывает на доске 34, 52, 70 и просит назвать десятки и единицы.",
            "students": "Называют десятки и единицы в заданных числах.",
            "resources": ["доска"],
        }
    ],
    "stage_middle": [
        {
            "teacher": "Показывает пример 41 + 25: сначала десятки 4 + 2, потом единицы 1 + 5.",
            "students": "Вместе с учителем проговаривают разрядный состав.",
            "resources": ["доска", "счётные палочки"],
        },
        {
            "teacher": "Даёт в парах примеры 24 + 13, 67 – 24, 85 – 31.",
            "students": "Разбирают разряды и проговаривают действия друг другу.",
            "resources": ["тетрадь"],
        },
    ],
    "stage_end": [
        {
            "teacher": "Просит оценить работу фишками. Домашнее задание: 36 + 23, 81 – 40.",
            "students": "Показывают свою фишку, записывают домашнее задание.",
            "resources": ["фишки для самооценки", "тетрадь"],
        }
    ],
    "differentiation": "Тем, кому трудно, — счётные палочки; сильным — объяснить соседу.",
    "assessment": "Уточняющие вопросы в парах, самооценка цветными фишками в конце.",
    "health_and_safety": "Правильная осанка при письме, физминутка.",
    "reflection": ["Удалось ли избежать путаницы в разрядах?"],
}


def draft() -> dict:
    return copy.deepcopy(DRAFT)


def get(data, path):
    for key in path:
        data = data[key]
    return data


def with_value(path, value) -> dict:
    data = draft()
    get(data, path[:-1])[path[-1]] = value
    return data


def without(path) -> dict:
    data = draft()
    del get(data, path[:-1])[path[-1]]
    return data


def with_text(path, text) -> dict:
    """Образец, в строку которого по `path` дописан `text`."""
    return with_value(path, f"{get(DRAFT, path)} {text}")


def generate(ai: FakeAi):
    return generators(ai, "gpt-4.1")["lesson_plan"].generate(PREPARED)


def rejected(data) -> JobError:
    with pytest.raises(JobError) as exc:
        generate(FakeAi().returns_json(data))
    return exc.value


def schema_nodes(node: dict):
    """Каждый узел схемы — сам объект и всё, что под ним: свойства,
    элементы списков и варианты `anyOf`."""
    yield node
    for sub in node.get("properties", {}).values():
        yield from schema_nodes(sub)
    if "items" in node:
        yield from schema_nodes(node["items"])
    for sub in node.get("anyOf", []):
        yield from schema_nodes(sub)


SCHEMA = strict_schema(LessonPlanDraft)
PROPS = SCHEMA["properties"]


# --- Схема, которую видит модель ---


def test_schema_has_no_code_and_no_server_blocks():
    """Код цели модели не дают: поля, куда его можно вписать, нет ни в одном
    блоке. Раздел и цели обучения кладёт сервер — появись они в схеме, модель
    писала бы их сама и сочиняла код."""
    names = {name for node in schema_nodes(SCHEMA) for name in node.get("properties", {})}

    assert "code" not in names
    assert not names & {"section", "learning_objectives"}
    # Сверка по имени поля ловит не всё: код мог бы прийти и вложенным ключом
    assert '"code"' not in json.dumps(SCHEMA)


def test_every_object_in_schema_is_strict():
    """Строгий режим провайдера требует у каждого объекта все поля
    обязательными и запрет лишних, а часть ключей JSON-схемы отвергает
    целиком — тогда падал бы сам запрос. Запрет лишних заодно не даёт модели
    дописать блок от себя."""
    nodes = list(schema_nodes(SCHEMA))
    objects = [node for node in nodes if node.get("type") == "object"]

    # Корень, языковые цели и шаг в каждом из трёх этапов
    assert len(objects) == 5
    for node in objects:
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    for node in nodes:
        assert set(node) <= ALLOWED_KEYS
        assert not set(node) & FORBIDDEN_KEYS


def test_schema_fields_have_descriptions():
    """Описание поля — единственная инструкция модели к блоку: что писать
    в «дифференциацию» или в колонку «ученики», промпт не повторяет. Ссылка
    на вложенную модель не должна съедать описание поля."""
    assert len(LessonPlanDraft.model_fields) == 13
    for name in LessonPlanDraft.model_fields:
        assert PROPS[name].get("description", "").strip(), name
    for stage in STAGE_MINUTES:
        step = PROPS[stage]["items"]["properties"]
        for name in STEP_FIELDS:
            assert step[name].get("description", "").strip(), (stage, name)
    for name in ("vocabulary", "phrases"):
        assert PROPS["language_goals"]["properties"][name]["description"].strip()


def test_schema_follows_form_order():
    """Модель пишет блоки в порядке схемы, и порядок формы ҚМЖ — тот, в котором
    урок думается: цели, потом ход урока, потом рефлексия. Тот же порядок
    держит документ."""
    assert list(PROPS) == FORM_ORDER
    assert SCHEMA["required"] == FORM_ORDER


def test_schema_and_prompt_show_no_objective_code():
    """Пример кода в описании поля — готовый образец для подражания: модель
    вписала бы похожую строку в цель урока, и ответ отбивался бы раз за разом."""
    descriptions = [node.get("description", "") for node in schema_nodes(SCHEMA)]

    assert objective_codes_in(json.dumps(SCHEMA, ensure_ascii=False)) == []
    assert objective_codes_in(descriptions) == []
    assert objective_codes_in(LESSON_PLAN.system) == []


def test_stage_minutes_cover_lesson():
    """Время этапов задано формой, а не моделью, и выгрузка печатает его
    отсюда: дыра или нахлёст между этапами — ошибка в документе, который
    сдают завучу. Этапы в схеме и во времени — одни и те же поля."""
    spans = list(STAGE_MINUTES.values())

    assert len(spans) == 3
    assert spans[0][0] == 0
    assert spans[-1][1] == 40
    for before, after in pairwise(spans):
        assert after[0] == before[1]
    assert sum(end - start for start, end in spans) == 40
    assert list(STAGE_MINUTES) == [name for name in PROPS if name.startswith("stage_")]
    # Описание этапа говорит модели то же время, что печатает форма
    for name, (start, end) in STAGE_MINUTES.items():
        assert f"{start}–{end} минут" in PROPS[name]["description"]


# --- Разбор ответа модели ---


def test_valid_reply_becomes_document_with_server_blocks_first():
    """Документ — блоки сервера впереди, потом блоки модели в порядке формы,
    в каком бы порядке ключей модель ни ответила. Цель обучения с кодом —
    дословно из справочника: проверка на код её не касается, она не от модели.
    Токены — в счёт материала: за ответ заплачено."""
    reply = dict(reversed(list(draft().items())))

    result = generate(FakeAi().returns_json(reply, tokens_in=1234, tokens_out=2345))

    assert list(result.content) == ["section", "learning_objectives", *FORM_ORDER]
    assert result.content["section"] == PREPARED.section
    assert result.content["learning_objectives"] == PREPARED.objectives
    assert result.content["learning_objectives"][0]["code"] == CODE
    assert {name: result.content[name] for name in FORM_ORDER} == DRAFT
    assert (result.tokens_in, result.tokens_out) == (1234, 2345)


def test_request_to_model():
    """Модель получает схему ҚМЖ целиком и промпт вида, а в тексте запроса нет
    кода цели: генератор держит код из справочника в руках, но отдаёт модели
    только бриф."""
    ai = FakeAi().returns_json(draft())

    generate(ai)

    assert len(ai.calls) == 1
    call = ai.calls[0]
    assert call["name"] == "lesson_plan"
    assert call["schema"] == strict_schema(LessonPlanDraft)
    assert call["system"] == LESSON_PLAN.system
    assert [message.role for message in call["messages"]] == ["user"]
    assert call["messages"][0].content == LESSON_PLAN.user_message(BRIEF)
    assert objective_codes_in([call["system"], call["messages"][0].content]) == []


@pytest.mark.parametrize("text", ["не json", ""])
def test_not_json_is_rejected_with_tokens(text):
    """Пустой текст — это и отказ модели отвечать (`refusal`): провайдер тогда
    текста не присылает. Повтор имеет смысл, а токены попытки идут в счёт —
    кривой ответ стоит тех же денег."""
    with pytest.raises(JobError) as exc:
        generate(FakeAi().returns_json(text, tokens_in=321, tokens_out=654))

    assert exc.value.code == "model_output_invalid"
    assert exc.value.retryable is True
    assert (exc.value.tokens_in, exc.value.tokens_out) == (321, 654)


@pytest.mark.parametrize(
    "path",
    [(name,) for name in FORM_ORDER]
    + [("language_goals", "phrases"), ("stage_middle", 0, "resources")],
)
def test_missing_field_is_rejected(path):
    """Строгий режим обещает все поля, но проверяем не обещание: документ
    без блока — не ҚМЖ, его не примет завуч."""
    err = rejected(without(path))

    assert err.code == "model_output_invalid"
    assert err.retryable is True


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("learning_objectives",), [{"code": CODE, "text": OBJECTIVE}]),
        (("code",), CODE),
        (("section",), {"title": "Числа и величины"}),
        (("stage_middle", 0, "code"), CODE),
        (("language_goals", "code"), CODE),
    ],
)
def test_extra_field_is_rejected(path, value):
    """Лишнее поле — модель дописала блок от себя, чаще всего цель обучения
    с кодом. Молча выкинуть его нельзя: раз модель полезла в блок сервера,
    остальное тоже под подозрением."""
    err = rejected(with_value(path, value))

    assert err.code == "model_output_invalid"
    assert err.retryable is True


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("values",), "   "),
        (("lesson_goals",), []),
        (("stage_middle",), []),
        (("language_goals", "vocabulary", 0), "  "),
        (("stage_beginning", 0, "teacher"), ""),
        (("stage_end", 0, "resources", 0), " "),
    ],
)
def test_empty_block_is_rejected(path, value):
    """Строгий режим гарантирует, что поле есть, но не что в нём что-то
    написано: пустой блок учителю пришлось бы дописывать самому, а это
    ровно та работа, от которой «Жоспар» избавляет."""
    err = rejected(with_value(path, value))

    assert err.code == "model_output_invalid"
    assert err.retryable is True


def test_step_without_resources_is_accepted():
    """Физминутке и устному опросу ресурсы не нужны: пустой список у шага
    законен, и отбивать такой ответ — зря жечь попытку и деньги."""
    data = with_value(("stage_middle", 1, "resources"), [])

    result = generate(FakeAi().returns_json(data))

    assert result.content["stage_middle"][1]["resources"] == []


@pytest.mark.parametrize(
    "path",
    [
        ("lesson_goals", 0),
        ("assessment_criteria", 1),
        ("language_goals", "vocabulary", 0),
        ("language_goals", "phrases", 1),
        ("values",),
        ("cross_curricular_links",),
        ("prior_knowledge",),
        ("stage_beginning", 0, "teacher"),
        ("stage_middle", 1, "teacher"),
        ("stage_middle", 0, "students"),
        ("stage_end", 0, "resources", 1),
        ("differentiation",),
        ("assessment",),
        ("health_and_safety",),
        ("reflection", 0),
    ],
)
def test_objective_code_in_any_text_is_rejected(path):
    """Кода модели не давали — значит, найденный код она сочинила, а сочинённый
    код проваливает проверку у завуча. Искать надо везде, вплоть до колонки
    шага в середине урока: в какое поле он попадёт, заранее не угадать."""
    err = rejected(with_text(path, f"(цель {CODE})"))

    assert err.code == "model_output_invalid"
    assert err.retryable is True


@pytest.mark.parametrize("text", ["до 16.09.2022", "как в разделе 1.2"])
def test_date_and_section_number_are_not_codes(text):
    """Дата и номер раздела похожи на код только на глаз: отбей их проверка —
    ответ с безобидной строкой уходил бы на повтор, пока не кончатся попытки."""
    data = with_text(("stage_middle", 1, "teacher"), text)

    result = generate(FakeAi().returns_json(data))

    assert result.content["stage_middle"][1]["teacher"].endswith(text)


def test_provider_failure_is_retryable():
    """Сеть, таймаут, потолок токенов в минуту — пройдёт само, повтор нужен.
    Модель не ответила — и платить не за что."""
    with pytest.raises(JobError) as exc:
        generate(FakeAi().fails())

    assert exc.value.code == "ai_failed"
    assert exc.value.retryable is True
    assert (exc.value.tokens_in, exc.value.tokens_out) == (0, 0)


# --- Поиск и вырезание кода цели ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"Сложение {CODE} двузначных чисел", "Сложение двузначных чисел"),
        (f"{CODE} Сложение двузначных чисел", "Сложение двузначных чисел"),
        (f"Сложение двузначных чисел {CODE}", "Сложение двузначных чисел"),
        (f"Сложение {CODE}, устно", "Сложение, устно"),
        (f"Сложение по цели {CODE}.", "Сложение по цели."),
        ("40+17, 57-40", "40+17, 57-40"),
        ("35±12", "35±12"),
        ("Урок 16.09.2022", "Урок 16.09.2022"),
        ("Раздел 1.2", "Раздел 1.2"),
        ("Пункт 1.2.3.4.5", "Пункт 1.2.3.4.5"),
    ],
)
def test_strip_objective_codes(text, expected):
    """Код вырезается из всего, что уходит модели, — и без следов: двойной
    пробел или пробел перед запятой модель переняла бы в свой текст. Числа
    урока, даты и номер раздела должны дойти до модели как есть: в них смысл
    задания."""
    assert strip_objective_codes(text) == expected


def test_objective_codes_in_finds_nested():
    """Ответ модели — дерево из словарей и списков, и код ищется на любой
    глубине; числа и даты рядом с ним находками не считаются."""
    value = {
        "values": "без кода",
        "stage_middle": [
            {"teacher": f"по цели {CODE}", "resources": ["16.09.2022", "3.1.1.1 и 4.2.3.10"]}
        ],
        "language_goals": {"phrases": [["1.2", "36 + 23"]]},
        "count": 40,
    }

    assert objective_codes_in(value) == [CODE, "3.1.1.1", "4.2.3.10"]
