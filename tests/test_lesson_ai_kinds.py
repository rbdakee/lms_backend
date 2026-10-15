"""Шесть видов «Жоспара» с моделью и ключи, которые собирает сервер.

Устройство у видов общее (`KindSpec` + `SchemaGenerator`), поэтому и проверки
общие — по каждому виду одни и те же: схема годится строгому режиму
провайдера и не даёт модели поля кода; ответ по образцу ложится в схему,
а сочинённый код, пустое поле и кривой JSON отбиваются; сообщение модели
несёт план урока и оба уровня заказа, но не код цели (TEACHER_AI_BRIEF,
разделы 4.1–4.3; TEACHER_AI_PROMPTS, L5, «Что нельзя сломать»).

Базы генерация не видит: `Prepared` собирается руками, а там, где нужна
настоящая `prepare`, подменяется репозиторий — так проверяется ровно
вырезание кодов, а не справочник. Образцы ответов — сокращённые настоящие
ответы модели из живых прогонов L5 (`tests/samples/lesson_ai/`), образец
ҚМЖ — из test_lesson_ai_schema.
"""

import copy
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.lesson_ai import (
    ANSWER_SOURCES,
    MATERIAL_KINDS,
    WAITS_FOR,
    JobError,
    assemble_answer_key,
    objective_codes_in,
)
from app.lesson_ai import context
from app.lesson_ai.answer_key import AnswerKeyAssembler
from app.lesson_ai.context import DIFFICULTY_NAMES, ENGAGEMENT_NAMES
from app.lesson_ai.games.mechanic import load_catalog
from app.lesson_ai.generate import KindPrepared, SchemaGenerator
from app.lesson_ai.kinds import SPECS, generators
from app.lesson_ai.schemas import strict_schema
from tests.conftest import FakeAi
from tests.test_lesson_ai_schema import (
    ALLOWED_KEYS,
    BRIEF,
    CODE,
    DRAFT,
    FORBIDDEN_KEYS,
    OBJECTIVE,
    PREPARED,
    schema_nodes,
)

# Доступ к «Жоспару» (L7) в этих тестах открыт: они проверяют продукт,
# а сама проверка доступа — в `test_lesson_ai_access.py`
pytestmark = pytest.mark.usefixtures("lesson_ai_open")

SPEC = {spec.kind: spec for spec in SPECS}
KINDS = [spec.kind for spec in SPECS]
SAMPLES_DIR = Path(__file__).parent / "samples" / "lesson_ai"
SAMPLES = {
    "lesson_plan": DRAFT,
    **{
        kind: json.loads((SAMPLES_DIR / f"{kind}.json").read_text(encoding="utf-8"))
        for kind in ("presentation", "tasks", "homework", "assessment", "game")
    },
}
# Всё, что похоже на код цели хоть отдалённо: четыре числа через точку.
# Шире, чем `objective_codes_in`, — в промпте и схеме не нужен даже похожий образец
CODE_LIKE = re.compile(r"\d+\.\d+\.\d+\.\d+")
LEVELS = [("calm", "basic"), ("active", "standard"), ("full_game", "advanced")]


def sample(kind: str) -> dict:
    return copy.deepcopy(SAMPLES[kind])


def schema(kind: str) -> dict:
    return strict_schema(SPEC[kind].draft)


def generate(kind: str, ai: FakeAi, prepared=PREPARED):
    return generators(ai, "gpt-4.1")[kind].generate(prepared)


def rejected(kind: str, data) -> JobError:
    with pytest.raises(JobError) as exc:
        generate(kind, FakeAi().returns_json(data))
    return exc.value


def string_paths(value, path=()):
    """Пути ко всем строкам ответа на любой глубине — туда модель может
    вписать код или оставить пусто."""
    if isinstance(value, str):
        yield path
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from string_paths(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from string_paths(item, (*path, index))


def object_paths(value, path=()):
    """Пути ко всем вложенным объектам ответа, кроме корня."""
    if isinstance(value, dict):
        if path:
            yield path
        items = value.items()
    elif isinstance(value, list):
        items = enumerate(value)
    else:
        return
    for key, item in items:
        yield from object_paths(item, (*path, key))


def put(data: dict, path, value) -> dict:
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


def get(data, path):
    for key in path:
        data = data[key]
    return data


# --- Схема, которую видит модель ---


@pytest.mark.parametrize("kind", KINDS)
def test_every_object_in_schema_is_strict(kind):
    """Строгий режим провайдера требует у каждого объекта все поля
    обязательными и запрет лишних, а часть ключей JSON-схемы отвергает
    целиком — тогда падал бы сам запрос, а не отвечал хуже."""
    nodes = list(schema_nodes(schema(kind)))
    objects = [node for node in nodes if node.get("type") == "object"]

    assert objects
    for node in objects:
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    for node in nodes:
        assert set(node) <= ALLOWED_KEYS
        assert not set(node) & FORBIDDEN_KEYS


@pytest.mark.parametrize("kind", KINDS)
def test_schema_has_no_code_field(kind):
    """Модели поле `code` не дают вовсе, ни в одном виде (раздел 4.1,
    правило 2): сочинять код ей некуда. Блоки сервера — раздел и цели
    обучения — тоже не её: появись они в схеме, модель писала бы их сама."""
    data = schema(kind)
    names = {name for node in schema_nodes(data) for name in node.get("properties", {})}

    assert "code" not in names
    assert not names & {"section", "learning_objectives"}
    assert '"code"' not in json.dumps(data)


@pytest.mark.parametrize("kind", KINDS)
def test_schema_and_prompt_show_nothing_like_a_code(kind):
    """Пример кода в описании поля или в системном промпте — готовый образец
    для подражания: модель вписала бы похожую строку, и ответ отбивался бы
    раз за разом."""
    descriptions = [node.get("description", "") for node in schema_nodes(schema(kind))]

    assert not CODE_LIKE.search(json.dumps(schema(kind), ensure_ascii=False))
    assert not [text for text in descriptions if CODE_LIKE.search(text)]
    assert not CODE_LIKE.search(SPEC[kind].system)


@pytest.mark.parametrize("kind", KINDS)
def test_schema_fields_have_descriptions(kind):
    """Описание поля — инструкция модели к блоку; промпт её не повторяет.
    Развёрнутая ссылка на вложенную модель не должна съедать описание."""
    for node in schema_nodes(schema(kind)):
        for name, sub in node.get("properties", {}).items():
            assert sub.get("description", "").strip(), (kind, name)


# --- Какие виды умеет генерация ---


def test_generators_cover_all_seven_kinds():
    """Очередь спрашивает генератор по виду задания: вид без генератора упал
    бы у учителя внутренней ошибкой. Ключи с ответами модели не заказываются
    (правило № 2 L5) — у сборщика нет ни схемы, ни модели, и расхода нет."""
    found = generators(FakeAi(), "gpt-4.1")

    assert set(found) == set(MATERIAL_KINDS)
    assert len(found) == 7
    assert [spec.kind for spec in SPECS] == [k for k in MATERIAL_KINDS if k != "answer_key"]
    assert isinstance(found["answer_key"], AnswerKeyAssembler)
    assert found["answer_key"].model is None
    assert not hasattr(found["answer_key"], "schema")
    for kind in KINDS:
        assert isinstance(found[kind], SchemaGenerator)
        assert found[kind].model == "gpt-4.1"
        assert found[kind].schema == schema(kind)


def test_kind_specs_follow_generation_order():
    """План урока идёт первым и сам плана на вход не получает; остальные
    пять обязаны ему соответствовать — и ждут его в очереди (раздел 4.2).
    Презентация видит уровни только через план (4.3), игра получает
    строительные блоки каталога."""
    assert SPEC["lesson_plan"].with_plan is False
    for kind in KINDS:
        if kind != "lesson_plan":
            assert SPEC[kind].with_plan is True, kind
        # Кто получает план на вход, тот и ждёт его в очереди
        assert ("lesson_plan" in WAITS_FOR.get(kind, ())) is SPEC[kind].with_plan, kind
    assert SPEC["presentation"].levels is False
    assert all(SPEC[kind].levels for kind in KINDS if kind != "presentation")
    # Строительные блоки каталога — у игры; методрекомендации НАО — у заданий,
    # домашки и проверочной. Оба идут в сообщение пользователя, не в систему
    assert SPEC["game"].references is not None
    assert all(
        SPEC[kind].references is not None
        for kind in ("assessment", "tasks", "homework")
    )
    assert SPEC["lesson_plan"].references is None
    assert SPEC["presentation"].references is None


# --- Разбор ответа модели ---


@pytest.mark.parametrize("kind", KINDS)
def test_sample_is_valid_and_has_no_code(kind):
    """Образец — настоящий ответ модели: не ляжет в схему он — значит, схема
    разошлась с тем, что модель умеет писать."""
    SPEC[kind].draft.model_validate(sample(kind))

    assert objective_codes_in(sample(kind)) == []


@pytest.mark.parametrize("kind", KINDS)
def test_document_is_server_blocks_then_model_blocks(kind):
    """Документ — блоки сервера впереди, потом блоки модели в порядке схемы,
    в каком бы порядке ключей модель ни ответила. Цель с кодом — дословно
    из справочника, проверка на код её не касается."""
    reply = dict(reversed(list(sample(kind).items())))

    result = generate(kind, FakeAi().returns_json(reply, tokens_in=11, tokens_out=22))

    order = list(schema(kind)["properties"])
    assert list(result.content) == ["section", "learning_objectives", *order]
    assert result.content["section"] == PREPARED.section
    assert result.content["learning_objectives"] == PREPARED.objectives
    assert result.content["learning_objectives"][0]["code"] == CODE
    assert {name: result.content[name] for name in order} == SAMPLES[kind]
    assert (result.tokens_in, result.tokens_out) == (11, 22)


def test_game_keeps_picked_mechanics():
    """Какие механики каталога ушли модели, пишется в материал: по ним
    разбирают жалобу на игру и считают, что каталог даёт."""
    prepared = KindPrepared(
        brief=BRIEF,
        section=PREPARED.section,
        objectives=PREPARED.objectives,
        mechanics=("shop", "decode_word"),
    )

    result = generate("game", FakeAi().returns_json(sample("game")), prepared)

    assert result.mechanics == ("shop", "decode_word")


@pytest.mark.parametrize("kind", KINDS)
def test_objective_code_in_any_string_is_rejected(kind):
    """Кода модели не давали — значит, найденный код она сочинила. Искать надо
    в каждой строке ответа: в какое поле он попадёт, заранее не угадать."""
    paths = list(string_paths(SAMPLES[kind]))
    assert paths

    for path in paths:
        data = put(sample(kind), path, f"{get(SAMPLES[kind], path)} (цель {CODE})")
        err = rejected(kind, data)
        assert (err.code, err.retryable) == ("model_output_invalid", True), path


@pytest.mark.parametrize("kind", KINDS)
def test_empty_string_in_any_field_is_rejected(kind):
    """Строгий режим обещает, что поле есть, но не что оно не пустое: пустой
    блок учителю пришлось бы дописывать самому. Слаг перечисления пустым
    тоже не бывает."""
    for path in string_paths(SAMPLES[kind]):
        for empty in ("", "   "):
            err = rejected(kind, put(sample(kind), path, empty))
            assert (err.code, err.retryable) == ("model_output_invalid", True), path


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("broken", ["не json", "", "cut"])
def test_broken_json_is_rejected_with_tokens(kind, broken):
    """Кривой, пустой или оборванный на полуслове ответ — повод спросить
    ещё раз; токены попытки идут в счёт."""
    text = json.dumps(SAMPLES[kind], ensure_ascii=False)[:-20] if broken == "cut" else broken

    with pytest.raises(JobError) as exc:
        generate(kind, FakeAi().returns_json(text, tokens_in=5, tokens_out=6))

    assert (exc.value.code, exc.value.retryable) == ("model_output_invalid", True)
    assert (exc.value.tokens_in, exc.value.tokens_out) == (5, 6)


@pytest.mark.parametrize("kind", KINDS)
def test_extra_code_field_is_rejected(kind):
    """Лишнее поле — модель дописала от себя, чаще всего код цели. Молча
    выкинуть его нельзя: раз модель полезла не в своё, остальное тоже под
    подозрением."""
    err = rejected(kind, {**sample(kind), "code": CODE})
    assert err.code == "model_output_invalid"

    # И в любом вложенном объекте: карточке, шаге, варианте, задаче
    paths = list(object_paths(SAMPLES[kind]))
    assert paths
    for path in paths:
        data = sample(kind)
        get(data, path)["code"] = CODE
        assert rejected(kind, data).code == "model_output_invalid", path


# --- Сообщение модели ---


class FakeRepo:
    """Справочник и готовый план — без базы. Код цели в них есть везде, где
    он бывает в жизни: в тексте цели от склейки ячеек, в блоках сервера плана
    и в строках, которые учитель вписал в план руками."""

    plan_calls: list = []

    def __init__(self, db):
        pass

    def kit_objectives(self, kit_ids):
        objective = SimpleNamespace(code=CODE, text=f"{OBJECTIVE} {CODE}", marker="", id=1)
        program = SimpleNamespace(status="published")
        subject = SimpleNamespace(title="Математика", code="math")
        return {kit_id: [(objective, program, subject)] for kit_id in kit_ids}

    def ready_blocks(self, kit_id, kinds):
        FakeRepo.plan_calls.append(kinds)
        plan = copy.deepcopy(DRAFT)
        plan["values"] = f"{DRAFT['values']} (цель {CODE})"
        plan["stage_middle"][0]["teacher"] += f" По цели {CODE}."
        server = [
            ("section", {"title": "Числа и величины", "subsection": "Операции над числами"}),
            ("learning_objectives", [{"code": CODE, "text": OBJECTIVE, "marker": ""}]),
        ]
        return [("lesson_plan", server + list(plan.items()))]


@pytest.fixture
def prepared_for(monkeypatch):
    """`prepare` генератора вида — настоящая, с подменённым справочником."""
    FakeRepo.plan_calls = []
    monkeypatch.setattr(context, "LessonKitRepo", FakeRepo)
    monkeypatch.setattr(
        context, "objective_sections", lambda db, objective: ("Числа и величины", "Операции")
    )

    def prepared(kind, engagement="active", difficulty="standard"):
        kit = SimpleNamespace(
            id=17,
            lang="ru",
            grade=2,
            topic=f"Сложение {CODE} двузначных чисел",
            class_size=24,
            notes=f"Хочу по цели {CODE}, с палочками",
            engagement_level=engagement,
            difficulty_level=difficulty,
        )
        return generators(FakeAi(), "gpt-4.1")[kind].prepare(None, kit)

    return prepared


@pytest.mark.parametrize("kind", KINDS)
def test_message_has_no_objective_code(kind, prepared_for):
    """Код вписан в тему, в пожелания, в формулировку цели и в план —
    до модели он не доходит ни в одном виде. Цель с кодом сервер держит
    у себя и кладёт в документ сам."""
    prepared = prepared_for(kind)

    message = SPEC[kind].user_message(prepared.brief)

    assert CODE not in message
    assert objective_codes_in(message) == []
    assert "Хочу по цели, с палочками" in message
    assert prepared.objectives[0]["code"] == CODE


@pytest.mark.parametrize("kind", KINDS)
def test_message_carries_lesson_plan(kind, prepared_for):
    """План идёт первым, пять видов получают его на вход и обязаны ему
    соответствовать (правило № 3 L5). Уходит он без блоков сервера — в них
    цель с кодом. Самому плану плана нет: его и в базе не ищут."""
    prepared = prepared_for(kind)

    message = SPEC[kind].user_message(prepared.brief)

    if kind == "lesson_plan":
        assert prepared.brief.plan is None
        assert FakeRepo.plan_calls == []
        assert "План урока (ҚМЖ)" not in message
        return
    assert "План урока (ҚМЖ)" in message
    assert DRAFT["values"] in message
    assert DRAFT["stage_middle"][1]["teacher"] in message
    assert '"learning_objectives"' not in message
    assert '"section"' not in message


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(("engagement", "difficulty"), LEVELS)
def test_message_carries_levels_in_words(kind, engagement, difficulty, prepared_for):
    """Модели нужен смысл уровня, а не слаг (раздел 4.3). Презентация уровней
    не видит: они доходят до неё через план, иначе слайды спорили бы с ходом
    урока."""
    message = SPEC[kind].user_message(prepared_for(kind, engagement, difficulty).brief)

    if kind == "presentation":
        for words in [*ENGAGEMENT_NAMES.values(), *DIFFICULTY_NAMES.values()]:
            assert words not in message
        return
    assert ENGAGEMENT_NAMES[engagement] in message
    assert DIFFICULTY_NAMES[difficulty] in message
    # Чужие уровни не просачиваются: модель прочла бы два противоречивых заказа
    for other, words in ENGAGEMENT_NAMES.items():
        assert (words in message) is (other == engagement)


def test_game_message_carries_references_whole(prepared_for):
    """Строительные блоки каталога — в сообщение целиком, а не в систему:
    переменный каталог в системе ломал бы кэш провайдера. Слаги отданных
    механик сервер запоминает."""
    prepared = prepared_for("game")

    message = SPEC["game"].user_message(prepared.brief)

    assert prepared.brief.references
    assert prepared.brief.references in message
    assert prepared.mechanics
    assert prepared.brief.references not in SPEC["game"].system


def test_game_message_without_references_has_no_section():
    """Каталог пуст или фильтр ничего не дал — игра всё равно генерируется,
    а пустой заголовок раздела модель читала бы как пустое меню."""
    message = SPEC["game"].user_message(BRIEF)

    assert "Строительные блоки для игры" not in message


def test_references_go_only_to_game(prepared_for):
    """Каталог игр — строительный материал одной игры; остальным видам он
    только съел бы токены."""
    for kind in KINDS:
        if kind != "game":
            assert prepared_for(kind).brief.references == "", kind


@pytest.mark.parametrize("kind", KINDS)
def test_system_prompt_does_not_depend_on_lesson(kind):
    """Одинаковое начало запроса провайдер берёт из кэша: системный промпт
    и схема одни на все уроки, всё переменное — в сообщении."""
    other = replace(
        BRIEF,
        lang="kz",
        grade=4,
        topic="Көбейту",
        notes="Қысқа",
        engagement_level="full_game",
        difficulty_level="advanced",
        plan={"values": "Другое"},
        references="Блоки" if kind == "game" else "",
    )
    ai = FakeAi().returns_json(sample(kind)).returns_json(sample(kind))

    generate(kind, ai, PREPARED)
    generate(kind, ai, replace(PREPARED, brief=other))

    first, second = ai.calls
    assert first["system"] == second["system"] == SPEC[kind].system
    assert first["schema"] == second["schema"]
    assert first["messages"][0].content != second["messages"][0].content


# --- Ключи с ответами ---


def answer_sources(*kinds):
    return [(kind, list(SAMPLES[kind].items())) for kind in kinds]


def test_answer_key_is_assembled_from_three_kinds():
    """Ключи — сводка полей `answer` из заданий, домашнего и проверочной,
    без модели (раздел 4.2). Каждая группа помнит блок-источник: без него
    карточки разных уровней с одинаковым заголовком в ключах не различить."""
    key = assemble_answer_key(answer_sources("tasks", "homework", "assessment"))

    assert list(key) == ["tasks", "homework", "assessment"]
    tasks = SAMPLES["tasks"]
    expected_blocks = [
        name for name in ("cards_support", "cards_core", "cards_challenge") for _ in tasks[name]
    ]
    assert [group["block"] for group in key["tasks"]] == expected_blocks
    assert [group["block"] for group in key["homework"]] == ["required", "optional"]
    assert [group["block"] for group in key["assessment"]] == ["variant_1", "variant_2"]

    first = key["tasks"][0]
    assert first["title"] == tasks["cards_support"][0]["title"]
    assert first["answers"][0] == {
        "number": 1,
        "task": tasks["cards_support"][0]["tasks"][0]["text"],
        "answer": tasks["cards_support"][0]["tasks"][0]["answer"],
    }
    for groups in key.values():
        for group in groups:
            numbers = [item["number"] for item in group["answers"]]
            assert numbers == list(range(1, len(numbers) + 1))


def test_answer_key_counts_only_objects_with_answer():
    """Задача — объект с `text` и `answer`. Дескриптор проверочной или строка
    без ответа задачей не считается: ключ без ответа учителю бесполезен."""
    blocks = [
        ("no_answer", {"title": "Без ответов", "tasks": [{"text": "2 + 2"}]}),
        ("mixed", {"title": "Смесь", "tasks": [{"text": "2 + 2", "answer": "4"}, {"text": "3"}]}),
        ("good", {"title": "С ответом", "tasks": [{"text": "5 + 5", "answer": "10"}]}),
    ]

    key = assemble_answer_key([("tasks", blocks)])

    assert [group["block"] for group in key["tasks"]] == ["good"]
    # Дескрипторы проверочной — рядом с задачами, но в ключи не попадают
    assessment = assemble_answer_key(answer_sources("assessment"))["assessment"]
    answers = [item["answer"] for group in assessment for item in group["answers"]]
    assert len(answers) == sum(
        len(SAMPLES["assessment"][name]["tasks"]) for name in ("variant_1", "variant_2")
    )


def test_game_is_not_an_answer_source():
    """Ответы игры живут в файле игры (`host_key`), в ключи комплекта игру
    без решения владельца не подмешивают. Да и формы задачи у неё нет."""
    assert ANSWER_SOURCES == ("tasks", "homework", "assessment")
    assert "game" not in ANSWER_SOURCES
    assert WAITS_FOR["answer_key"] == ANSWER_SOURCES
    assert assemble_answer_key(answer_sources("game")) == {}


def test_empty_sources_give_empty_key():
    """Не из чего собрать — пустой словарь: сборщик отвечает на него
    окончательным отказом `no_sources`, а не пустым файлом."""
    assert assemble_answer_key([]) == {}
    assert assemble_answer_key([("tasks", [])]) == {}


# --- Игра: схема шире прочих ---


def test_game_schema_is_not_a_menu_of_mechanics():
    """Схема игры не выбирает из готовых механик (правило № 4 L5): ни одно
    перечисление не содержит слага каталога, а в схеме и системном промпте
    нет названия ни одной карточки. Иначе тысяча учителей получила бы одну
    и ту же игру."""
    mechanics = load_catalog()
    assert mechanics
    slugs = {mechanic.id for mechanic in mechanics}
    game_schema = schema("game")
    enums = [value for node in schema_nodes(game_schema) for value in node.get("enum", [])]
    text = (json.dumps(game_schema, ensure_ascii=False) + SPEC["game"].system).lower()

    assert not slugs & set(enums)
    for mechanic in mechanics:
        assert mechanic.id not in text, mechanic.id
        assert mechanic.title.lower() not in text, mechanic.title
