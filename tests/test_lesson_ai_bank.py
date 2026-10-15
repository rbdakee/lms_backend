"""«Жоспар»: банк готовых материалов (TEACHER_AI_BRIEF, раздел 7; журнал
решений L7, записи 6 и 7).

- Комплект без пожеланий идёт через банк: каждый материал либо выдаётся
  готовым вариантом, либо генерируется обезличенно и ложится в банк.
  Комплект с пожеланиями — личный: банк не читается и не пополняется.
- Ключ — цель, вид, язык и оба уровня; у соседей плана ещё и вариант плана:
  соседи из банка выдаются только к плану из банка и только построенные
  по нему.
- Учителю не выдаётся вариант, который у него уже есть; из остальных —
  самый старый.
- Обезличенность держится тем, что модель не видит личного, а у строки
  банка нет ссылки на учителя.

Проверяется то, что ушло подставному ИИ и что легло в базу. Фоновых воркеров
нет: задания берутся руками, ИИ отвечает по виду материала образцами L5.
"""

import json

import pytest

from app.domain.lesson_ai import BANK_KINDS, MAX_ATTEMPTS
from tests.conftest import PHONE, FakeAi, login_named
from tests.test_admin_curriculum import make_objective
from tests.test_lesson_ai_blocks import NEW_MIDDLE, edit, regenerate
from tests.test_lesson_ai_kits import OTHER_PHONE, Scene, order, run_next
from tests.test_lesson_ai_order import (
    AFTER_PLAN,
    ALL_KINDS,
    PLAN_HEADER,
    REPLIES,
    KindAi,
    drain,
    shown_plan,
)
from tests.test_lesson_ai_queue import execute, fetch

# Доступ к «Жоспару» (L7) в этих тестах открыт: они проверяют продукт,
# а сама проверка доступа — в `test_lesson_ai_access.py`
pytestmark = pytest.mark.usefixtures("lesson_ai_open")

THIRD_PHONE = "+7 (707) 555-01-03"

# Личное учителя, которого в банке быть не должно. Строки выбраны так,
# чтобы не встречаться в образцах ответов модели
TOPIC = "Сложение в зоопарке Караганды"
LAST_NAME = "Сейткалиева"
FIRST_NAME = "Жанар"
SCHOOL = "Гимназия № 159 имени Абая"
NOTES = "в классе двое новеньких, Арман и Дана путают разряды"

BANK_COLUMNS = {
    "id",
    "objective_id",
    "kind",
    "lang",
    "engagement_level",
    "difficulty_level",
    "plan_bank_id",
    "content",
    "block_order",
    "mechanics",
    "model",
    "created_at",
}


@pytest.fixture
def scene() -> Scene:
    return Scene()


def kit_of(client, sms, scene: Scene, phone=PHONE, kinds=ALL_KINDS, **kw) -> dict:
    """Заказ от учителя с заполненным профилем: ФИО и школа в базе есть,
    и проверка обезличенности ищет их там, куда они попасть не должны."""
    login_named(client, sms, phone=phone, last_name=LAST_NAME, first_name=FIRST_NAME, school=SCHOOL)
    body = {"topic": TOPIC, "kinds": list(kinds)} | kw
    resp = order(client, scene.objective.id, **body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def banked(client, sms, scene: Scene, phone=PHONE, kinds=ALL_KINDS, **kw) -> int:
    """Заказ и очередь до дна: комплект собран."""
    kit_id = kit_of(client, sms, scene, phone, kinds, **kw)["id"]
    drain(KindAi())
    return kit_id


def bank(kind: str | None = None) -> list[dict]:
    rows = fetch("SELECT * FROM lesson_material_bank ORDER BY id")
    return [row for row in rows if kind is None or row["kind"] == kind]


def materials(kit_id: int) -> dict[str, dict]:
    rows = fetch(
        "SELECT id, kind, status, bank_id, content, mechanics FROM lesson_material"
        " WHERE kit_id = :kit_id",
        kit_id=kit_id,
    )
    return {row["kind"]: row for row in rows}


def job_kinds(kit_id: int) -> list[str]:
    rows = fetch("SELECT kind FROM lesson_ai_job WHERE kit_id = :kit_id ORDER BY id", kit_id=kit_id)
    return [row["kind"] for row in rows]


def block_rows(material_id: int) -> list[tuple[str, object]]:
    rows = fetch(
        "SELECT kind, content FROM lesson_material_block WHERE material_id = :id"
        " ORDER BY order_index",
        id=material_id,
    )
    return [(row["kind"], row["content"]) for row in rows]


def kit_status(kit_id: int) -> str:
    return fetch("SELECT status FROM lesson_kit WHERE id = :id", id=kit_id)[0]["status"]


def messages(ai: FakeAi) -> list[str]:
    return ["\n".join(m.content for m in call["messages"]) for call in ai.calls]


# -- первый комплект кладёт, второй получает ------------------------------


def test_first_kit_generates_and_banks_every_kind_but_keys(client, sms, scene):
    """Банк пуст — генерируется всё, и каждый вид с моделью ложится в банк
    под свой ключ; соседи — с вариантом плана, по которому построены.
    Материал учителя помечен вариантом: второй раз его не выдадут."""
    kit_id = banked(client, sms, scene)

    rows = bank()
    found = materials(kit_id)
    assert sorted(row["kind"] for row in rows) == sorted(BANK_KINDS)
    [plan] = bank("lesson_plan")
    for row in rows:
        assert (row["objective_id"], row["lang"]) == (scene.objective.id, "ru")
        assert (row["engagement_level"], row["difficulty_level"]) == ("active", "standard")
        assert row["model"] == "gpt-test"
        assert row["plan_bank_id"] == (None if row["kind"] == "lesson_plan" else plan["id"])
        material = found[row["kind"]]
        assert material["bank_id"] == row["id"]
        assert row["content"] == material["content"]
        # Порядок блоков — форма документа, как у блоков учителя
        assert row["block_order"] == [kind for kind, _ in block_rows(material["id"])]
        assert row["mechanics"] == material["mechanics"]
    assert found["answer_key"]["bank_id"] is None
    assert bank("game")[0]["mechanics"]
    assert kit_status(kit_id) == "ready"


def test_second_teacher_gets_everything_ready_without_the_model(client, sms, scene):
    """Второй учитель с тем же ключом получает комплект из базы: ни одного
    вызова модели, в очереди — только ключи, которые сервер собирает
    из выданных заданий. Документ — вариант банка, блоки — в порядке формы."""
    first = banked(client, sms, scene)
    ai = KindAi()

    created = kit_of(client, sms, scene, phone=OTHER_PHONE)
    kit_id = created["id"]

    assert job_kinds(kit_id) == ["answer_key"]
    statuses = {m["kind"]: m["status"] for m in created["materials"]}
    assert statuses == {kind: "ready" for kind in BANK_KINDS} | {"answer_key": "queued"}
    assert created["status"] == "generating"
    found = materials(kit_id)
    for row in bank():
        material = found[row["kind"]]
        assert material["bank_id"] == row["id"]
        assert material["content"] == row["content"]
        assert material["mechanics"] == row["mechanics"]
        assert block_rows(material["id"]) == [
            (name, row["content"][name]) for name in row["block_order"]
        ]
    # У выданного — первая версия каждого блока, как у сгенерированного
    versions = fetch(
        "SELECT count(*) AS n FROM lesson_material_version v"
        " JOIN lesson_material_block b ON b.id = v.block_id WHERE b.material_id = :id",
        id=found["lesson_plan"]["id"],
    )
    assert versions[0]["n"] == len(bank("lesson_plan")[0]["block_order"])

    drain(ai)

    assert ai.calls == []
    assert kit_status(kit_id) == "ready"
    # Ключи — из выданных заданий, и в банк они не ходят
    assert materials(kit_id)["answer_key"]["content"] == materials(first)["answer_key"]["content"]
    assert materials(kit_id)["answer_key"]["bank_id"] is None
    assert len(bank()) == len(BANK_KINDS)


def test_kit_fully_from_the_bank_is_ready_at_once(client, sms, scene):
    """Комплект без ключей, собранный из банка целиком, готов в ответе
    на заказ: заданий нет, и закрыть их, пересчитав статус, некому."""
    banked(client, sms, scene)

    created = kit_of(client, sms, scene, phone=OTHER_PHONE, kinds=["lesson_plan", "tasks"])

    assert created["status"] == "ready"
    assert job_kinds(created["id"]) == []
    assert kit_status(created["id"]) == "ready"


# -- варианты: учителю не выдаётся то, что у него уже есть -----------------


def test_same_teacher_gets_a_new_variant_and_the_next_one_the_oldest(client, sms, scene):
    """Второй урок по той же цели у того же учителя — не копия первого:
    генерируется новый вариант и ложится в банк рядом. Третий учитель
    получает самый старый."""
    first = banked(client, sms, scene)
    ai = KindAi()

    second = kit_of(client, sms, scene)["id"]
    assert sorted(job_kinds(second)) == sorted(ALL_KINDS)
    drain(ai)

    assert sorted(ai.names) == sorted(BANK_KINDS)
    assert len(bank()) == 2 * len(BANK_KINDS)
    for kind in BANK_KINDS:
        old, new = bank(kind)
        assert materials(first)[kind]["bank_id"] == old["id"]
        assert materials(second)[kind]["bank_id"] == new["id"]
    old_plan, new_plan = bank("lesson_plan")
    # Соседи нового плана построены по нему, а не по старому
    for kind in AFTER_PLAN:
        assert bank(kind)[1]["plan_bank_id"] == new_plan["id"]

    third = kit_of(client, sms, scene, phone=THIRD_PHONE)["id"]

    assert job_kinds(third) == ["answer_key"]
    for kind in BANK_KINDS:
        assert materials(third)[kind]["bank_id"] == bank(kind)[0]["id"]


def other_objective(scene: Scene) -> int:
    return make_objective(scene.program.id, scene.subsection.id, "2.1.2.7", text="другая цель").id


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("lang", "'kz'"),
        ("engagement_level", "'calm'"),
        ("difficulty_level", "'advanced'"),
        ("objective_id", None),
    ],
)
def test_other_key_is_a_miss(client, sms, scene, column, value):
    """Любое поле ключа не совпало — банк не отвечает, всё генерируется
    и ложится в банк вторым вариантом."""
    banked(client, sms, scene)
    execute(f"UPDATE lesson_material_bank SET {column} = {value or other_objective(scene)}")
    ai = KindAi()

    kit_id = kit_of(client, sms, scene, phone=OTHER_PHONE)["id"]
    drain(ai)

    assert sorted(ai.names) == sorted(BANK_KINDS)
    assert sorted(job_kinds(kit_id)) == sorted(ALL_KINDS)
    assert len(bank()) == 2 * len(BANK_KINDS)


@pytest.mark.parametrize(
    "levels",
    [{"engagement_level": "calm"}, {"difficulty_level": "basic"}],
    ids=["engagement", "difficulty"],
)
def test_other_level_in_the_order_is_a_miss(client, sms, scene, levels):
    """Уровни заказа доходят до ключа банка: класс «с запасом» не получает
    материал, написанный для «догнать программу»."""
    banked(client, sms, scene)
    ai = KindAi()

    kit_id = kit_of(client, sms, scene, phone=OTHER_PHONE, **levels)["id"]
    drain(ai)

    assert sorted(ai.names) == sorted(BANK_KINDS)
    [(name, value)] = levels.items()
    assert {row[name] for row in bank()[len(BANK_KINDS) :]} == {value}
    assert kit_status(kit_id) == "ready"


# -- личный комплект --------------------------------------------------------


def test_kit_with_notes_neither_reads_nor_fills_the_bank(client, sms, scene):
    """Пожелания — место, куда учитель пишет о своём классе своими словами:
    такой комплект генерируется лично под них, как с L3, и в банк не ходит
    ни за готовым, ни с новым."""
    banked(client, sms, scene)
    ai = KindAi()

    kit_id = kit_of(client, sms, scene, phone=OTHER_PHONE, notes=NOTES, class_size=24)["id"]
    drain(ai)

    assert sorted(ai.names) == sorted(BANK_KINDS)
    assert len(bank()) == len(BANK_KINDS)
    assert {m["bank_id"] for m in materials(kit_id).values()} == {None}
    for message in messages(ai):
        lines = message.splitlines()
        assert f"Тема урока: {TOPIC}" in lines
        assert "Детей в классе: 24" in lines
        assert f"Пожелания учителя: {NOTES}" in lines


# -- обезличенность ---------------------------------------------------------


def test_bank_generation_never_shows_the_model_anything_personal(client, sms, scene):
    """Модель, которая пишет для банка, не видит ни темы, ни числа детей:
    обезличенность держится тем, что личного ей не дали."""
    ai = KindAi()
    kit_of(client, sms, scene, class_size=24)

    drain(ai)

    assert sorted(ai.names) == sorted(BANK_KINDS)
    for message in messages(ai):
        assert TOPIC not in message
        assert "Тема урока" not in message
        assert "Детей в классе" not in message
        assert "Пожелания" not in message
        # Урок по цели в сообщении по-прежнему есть
        assert "Цель обучения по программе:" in message


def test_bank_row_knows_nothing_about_the_teacher(client, sms, scene):
    """В строке банка нет ни темы, ни ФИО, ни школы — и нет колонки,
    по которой учителя можно найти. Правка учителя после генерации
    пишет только его материал."""
    kit_id = banked(client, sms, scene, class_size=24)
    columns = fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'lesson_material_bank'"
    )
    before = bank()

    plan_id = materials(kit_id)["lesson_plan"]["id"]
    assert edit(client, plan_id, "values", f"{TOPIC}: {SCHOOL}").status_code == 200

    assert {row["column_name"] for row in columns} == BANK_COLUMNS
    assert bank() == before
    dumped = json.dumps(before, ensure_ascii=False, default=str)
    for personal in (TOPIC, LAST_NAME, FIRST_NAME, SCHOOL):
        assert personal not in dumped
    assert materials(kit_id)["lesson_plan"]["content"]["values"] == f"{TOPIC}: {SCHOOL}"


def test_block_regeneration_stays_personal(client, sms, ai, scene):
    """Перегенерация блока — по просьбе учителя и по его теме: она идёт
    мимо банка и строку банка не трогает."""
    kit_id = banked(client, sms, scene, class_size=24)
    before = bank()
    ai.returns_json({"stage_middle": NEW_MIDDLE})

    resp = regenerate(client, materials(kit_id)["lesson_plan"]["id"], "stage_middle")

    assert resp.status_code == 200, resp.text
    lines = messages(ai)[-1].splitlines()
    assert f"Тема урока: {TOPIC}" in lines
    assert "Детей в классе: 24" in lines
    assert bank() == before


# -- цепочка плана ----------------------------------------------------------


def test_neighbours_come_from_the_bank_only_with_their_plan(client, sms, scene):
    """Задания из банка — только к плану из банка и только построенные
    по нему. Без плана в заказе — только варианты, сделанные без плана."""
    banked(client, sms, scene, kinds=["lesson_plan", "tasks"])
    [plan] = bank("lesson_plan")
    [with_plan] = bank("tasks")
    # Задания без плана: у этого учителя вариант с планом есть, но он
    # и не подошёл бы — генерируется вариант «без плана»
    banked(client, sms, scene, kinds=["tasks"])
    without_plan = bank("tasks")[1]
    assert (with_plan["plan_bank_id"], without_plan["plan_bank_id"]) == (plan["id"], None)

    chained = kit_of(client, sms, scene, phone=OTHER_PHONE, kinds=["lesson_plan", "tasks"])
    alone = kit_of(client, sms, scene, phone=THIRD_PHONE, kinds=["tasks"])

    assert job_kinds(chained["id"]) == job_kinds(alone["id"]) == []
    assert materials(chained["id"])["lesson_plan"]["bank_id"] == plan["id"]
    assert materials(chained["id"])["tasks"]["bank_id"] == with_plan["id"]
    assert materials(alone["id"])["tasks"]["bank_id"] == without_plan["id"]


def test_plan_generated_anew_takes_its_neighbours_along(client, sms, scene):
    """План в банке не нашёлся — соседи генерируются по новому плану, даже
    если подходящий по остальному ключу вариант в банке лежит, и ложатся
    в банк вслед за своим планом."""
    banked(client, sms, scene, phone=OTHER_PHONE, kinds=["tasks"])
    ai = KindAi()

    kit_id = kit_of(client, sms, scene, kinds=["lesson_plan", "tasks"])["id"]
    assert job_kinds(kit_id) == ["lesson_plan", "tasks"]
    drain(ai)

    assert ai.names == ["lesson_plan", "tasks"]
    [plan] = bank("lesson_plan")
    loose, chained = bank("tasks")
    assert (loose["plan_bank_id"], chained["plan_bank_id"]) == (None, plan["id"])
    assert materials(kit_id)["tasks"]["bank_id"] == chained["id"]


def test_neighbour_is_built_on_the_bank_plan_not_the_edited_one(client, sms, scene):
    """План выдан из банка, учитель его поправил, а домашнее ещё в очереди:
    модель видит план банка, а правка учителя не уходит ни модели, ни в банк —
    иначе через соседа личное утекло бы в банк."""
    banked(client, sms, scene, kinds=["lesson_plan", "tasks"])
    [plan] = bank("lesson_plan")
    ai = KindAi()
    kit_id = kit_of(client, sms, scene, phone=OTHER_PHONE, kinds=["lesson_plan", "homework"])["id"]
    assert job_kinds(kit_id) == ["homework"]
    mine = f"{SCHOOL}: разбираем с Арманом"

    resp = edit(client, materials(kit_id)["lesson_plan"]["id"], "values", mine)
    assert resp.status_code == 200, resp.text
    drain(ai)

    assert ai.names == ["homework"]
    message = ai.message("homework")
    assert mine not in message
    assert shown_plan(message)["values"] == REPLIES["lesson_plan"]["values"]
    assert shown_plan(message) == {
        name: plan["content"][name]
        for name in plan["block_order"]
        if name not in ("section", "learning_objectives")
    }
    [homework] = bank("homework")
    assert homework["plan_bank_id"] == plan["id"]
    assert mine not in json.dumps(bank(), ensure_ascii=False, default=str)


def test_plan_outside_the_bank_keeps_neighbours_out_of_it(client, sms, scene):
    """План комплекта готов, но не из банка (сделан до банка или вариант
    удалён): соседи строятся по плану учителя — и поэтому в банк не ложатся,
    ключа плана у них нет. Темы модель всё равно не видит."""
    kit_id = kit_of(client, sms, scene, kinds=["lesson_plan", "tasks"])["id"]
    ai = KindAi()

    # План берётся первым: он один и собирается
    assert run_next(KindAi())
    execute("UPDATE lesson_material SET bank_id = NULL WHERE kind = 'lesson_plan'")
    drain(ai)

    assert ai.names == ["tasks"]
    assert PLAN_HEADER in ai.message("tasks")
    assert TOPIC not in ai.message("tasks")
    assert [row["kind"] for row in bank()] == ["lesson_plan"]
    assert materials(kit_id)["tasks"]["bank_id"] is None


def test_failed_plan_leaves_neighbours_as_variants_without_a_plan(client, sms, scene):
    """План упал — соседи собираются без него (с L5) и ложатся в банк
    вариантом «без плана»: это законный вариант, его и выдают заказу
    без плана."""
    ai = KindAi("lesson_plan")
    kit_of(client, sms, scene, kinds=["lesson_plan", "tasks"])

    drain(ai)

    assert [row["kind"] for row in bank()] == ["tasks"]
    assert bank("tasks")[0]["plan_bank_id"] is None
    assert PLAN_HEADER not in ai.message("tasks")


# -- неудачная генерация ----------------------------------------------------


@pytest.mark.parametrize("broken", ["silent", "invalid"])
def test_failed_generation_is_not_banked(client, sms, scene, broken):
    """Провайдер не ответил или ответил не по схеме — материал упал,
    и в банке от него ничего нет."""
    ai = FakeAi()
    for _ in range(MAX_ATTEMPTS):
        if broken == "silent":
            ai.fails()
        else:
            ai.returns_json("{не json")
    kit_id = kit_of(client, sms, scene, kinds=["lesson_plan"])["id"]

    drain(ai)

    assert len(ai.calls) == MAX_ATTEMPTS
    assert materials(kit_id)["lesson_plan"]["status"] == "failed"
    assert bank() == []


def test_kit_from_the_bank_is_in_the_receivers_list(client, sms, scene):
    """«Мои уроки»: комплект, собранный из банка, — комплект получателя,
    а не донора; материалы в нём сразу готовы."""
    donor = banked(client, sms, scene)
    received = kit_of(client, sms, scene, phone=OTHER_PHONE, kinds=["lesson_plan", "tasks"])

    rows = client.get("/lesson_ai/kits").json()

    assert [row["id"] for row in rows["items"]] == [received["id"]]
    assert donor not in [row["id"] for row in rows["items"]]
    assert rows["items"][0]["status"] == "ready"
    assert [(m["kind"], m["status"]) for m in rows["items"][0]["materials"]] == [
        ("lesson_plan", "ready"),
        ("tasks", "ready"),
    ]
