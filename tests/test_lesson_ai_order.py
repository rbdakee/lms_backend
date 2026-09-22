"""«Жоспар»: порядок генерации комплекта, заказ с уровнями и ключи с ответами.

Правила — TEACHER_AI_BRIEF, разделы 4.2, 4.3 и 7, и `CONTRACT.md`, раздел
«Жоспар: семь видов материалов» (сессия L5):

- план урока идёт первым, пять видов получают его на вход — без кода цели,
  блоками в порядке формы; упавший план соседей не валит;
- ключи с ответами модель не пишет: их собирает сервер из поля `answer`
  заданий, домашнего и проверочной, и пересобирает после правки задачи;
- игра получает две-три механики каталога как строительные блоки, и сервер
  записывает, какие отдал.

Проверяется не чтение кода, а то, что реально ушло подставному ИИ и что легло
в базу. Фоновых воркеров нет: задания берутся руками через `run_next`, ИИ
отвечает по виду материала образцами из `tests/samples/lesson_ai`.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.adapters.ai.openai_chat import AiUnavailableError
from app.adapters.db.base import get_engine
from app.adapters.db.models import LessonAiJob, LessonKit, LessonMaterial
from app.application.ports import AiReply
from app.domain.lesson_ai import MATERIAL_KINDS
from app.lesson_ai.games.mechanic import load_catalog
from tests.conftest import FakeAi, login
from tests.test_lesson_ai_blocks import BLOCK_LOCKED, edit, regenerate, restore, url
from tests.test_lesson_ai_kits import (
    CODE,
    DRAFT,
    OBJECTIVE_TEXT,
    Scene,
    count,
    hurry_retries,
    invalid,
    order,
    run_next,
)
from tests.test_lesson_ai_queue import claim_and_commit, execute, fetch

SAMPLES = Path(__file__).parent / "samples" / "lesson_ai"

# Ответ модели на каждый вид: план — черновик из L3, остальные — образцы L5
REPLIES = {"lesson_plan": DRAFT} | {
    kind: json.loads((SAMPLES / f"{kind}.json").read_text(encoding="utf-8"))
    for kind in ("presentation", "tasks", "homework", "assessment", "game")
}
# Пять видов, которые ждут план и получают его на вход (раздел 4.2)
AFTER_PLAN = ("presentation", "tasks", "homework", "assessment", "game")
ALL_KINDS = list(MATERIAL_KINDS)

PLAN_HEADER = "План урока (ҚМЖ), которому материал обязан соответствовать"
SERVER_HEAD = {
    "section": {"title": "Числа и величины", "subsection": "Операции над числами"},
    "learning_objectives": [{"code": CODE, "text": OBJECTIVE_TEXT, "marker": ""}],
}
NO_SOURCES = {
    "code": "no_sources",
    "message": "Не из чего собрать ключи: задания, домашняя и проверочная не готовы",
}
KEY_WITHOUT_SOURCES = (
    "Ключи с ответами собираются из заданий, домашнего задания или проверочной"
    " — отметьте хотя бы одно из них"
)


class KindAi(FakeAi):
    """Подставной ИИ, который отвечает по виду материала, а не по очереди
    реплик: порядок взятия заданий — предмет проверки, и тест не должен
    подгонять под него ответы.

    `failing` — виды, на которых провайдер молчит (`ai_failed`, с повтором).
    """

    def __init__(self, *failing: str):
        super().__init__()
        self.failing = set(failing)

    def complete_json(self, *, system, messages, name, schema) -> AiReply:
        self.calls.append(
            {"system": system, "messages": list(messages), "name": name, "schema": schema}
        )
        if name in self.failing:
            raise AiUnavailableError("Модель не ответила")
        text_ = json.dumps(REPLIES[name], ensure_ascii=False)
        return AiReply(text=text_, tokens_in=900, tokens_out=1400)

    @property
    def names(self) -> list[str]:
        return [call["name"] for call in self.calls]

    def message(self, kind: str) -> str:
        """Сообщение последнего вызова модели по виду."""
        [*_, call] = [call for call in self.calls if call["name"] == kind]
        return "\n".join(message.content for message in call["messages"])


@pytest.fixture
def scene() -> Scene:
    return Scene()


def kit_of(client, sms, scene: Scene, kinds, **kw) -> int:
    login(client, sms)
    resp = order(client, scene.objective.id, kinds=list(kinds), **kw)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def drain(ai) -> None:
    """Выбрать очередь до дна, не дожидаясь пауз между попытками."""
    for _ in range(50):
        if run_next(ai):
            continue
        if not fetch("SELECT id FROM lesson_ai_job WHERE status = 'queued'"):
            return
        hurry_retries()
    raise AssertionError("очередь не опустела")


def jobs(kit_id: int) -> dict[str, dict]:
    rows = fetch(
        "SELECT kind, status, attempts, error, model, tokens_in, tokens_out"
        " FROM lesson_ai_job WHERE kit_id = :kit_id",
        kit_id=kit_id,
    )
    return {row["kind"]: row for row in rows}


def materials(client, kit_id: int) -> dict[str, dict]:
    body = client.get(f"/lesson_ai/kits/{kit_id}").json()
    return {material["kind"]: material for material in body["materials"]}


def block_kinds(client, material_id: int) -> list[str]:
    resp = client.get(url(material_id))
    assert resp.status_code == 200, resp.text
    return [item["kind"] for item in resp.json()["items"]]


def shown_plan(message: str) -> dict | None:
    """План урока, как его увидела модель, — JSON после заголовка раздела."""
    at = message.find(PLAN_HEADER)
    if at < 0:
        return None
    start = message.index("{", at)
    plan, _end = json.JSONDecoder().raw_decode(message, start)
    return plan


def answers(block: str, groups) -> list[dict]:
    """Группа ключей по форме контракта: блок источника, заголовок группы
    и задачи с номером внутри группы."""
    return [
        {
            "block": block,
            "title": group["title"],
            "answers": [
                {"number": number, "task": task["text"], "answer": task["answer"]}
                for number, task in enumerate(group["tasks"], start=1)
            ],
        }
        for group in groups
    ]


TASKS, HOMEWORK, ASSESSMENT = REPLIES["tasks"], REPLIES["homework"], REPLIES["assessment"]
KEY_TASKS = [
    group
    for block in ("cards_support", "cards_core", "cards_challenge")
    for group in answers(block, TASKS[block])
]
KEY_HOMEWORK = answers("required", [HOMEWORK["required"]]) + answers(
    "optional", [HOMEWORK["optional"]]
)
KEY_ASSESSMENT = answers("variant_1", [ASSESSMENT["variant_1"]]) + answers(
    "variant_2", [ASSESSMENT["variant_2"]]
)


# -- заказ ------------------------------------------------------------------


def test_levels_default_to_the_middle(client, sms, scene):
    """Учитель проскочил вопрос об уровнях — комплект всё равно заказан,
    с серединой: `active` и `standard` (раздел 4.3). В ответе и в базе."""
    kit_id = kit_of(client, sms, scene, ["lesson_plan"])

    body = client.get(f"/lesson_ai/kits/{kit_id}").json()
    assert (body["engagement_level"], body["difficulty_level"]) == ("active", "standard")
    with OrmSession(get_engine()) as db:
        kit = db.get(LessonKit, kit_id)
        assert (kit.engagement_level, kit.difficulty_level) == ("active", "standard")


@pytest.mark.parametrize(
    ("engagement", "difficulty"),
    [("calm", "basic"), ("full_game", "advanced"), ("active", "standard")],
)
def test_explicit_levels_are_kept(client, sms, scene, engagement, difficulty):
    """Присланные уровни доходят до базы как есть: от них зависят задания,
    а на L7 — ключ банка готовых материалов."""
    login(client, sms)
    resp = order(
        client,
        scene.objective.id,
        engagement_level=engagement,
        difficulty_level=difficulty,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["engagement_level"], body["difficulty_level"]) == (engagement, difficulty)
    with OrmSession(get_engine()) as db:
        kit = db.get(LessonKit, body["id"])
        assert (kit.engagement_level, kit.difficulty_level) == (engagement, difficulty)


@pytest.mark.parametrize(
    "patch",
    [
        {"engagement_level": "loud"},
        {"engagement_level": "gamified"},
        {"difficulty_level": "hard"},
        {"difficulty_level": ""},
    ],
)
def test_unknown_level_is_422_on_its_field(client, sms, scene, patch):
    """Уровень — одно из трёх значений: чужое отбивается на своём поле,
    и ничего не заводится."""
    login(client, sms)

    resp = order(client, scene.objective.id, **patch)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert [item["field"] for item in error["details"]["fields"]] == list(patch)
    assert count(LessonKit) == 0


def test_all_seven_kinds_are_accepted_in_brief_order(client, sms, scene):
    """Все семь видов — в одном заказе. Материалы и задания заводятся по
    одному на вид и в порядке брифа, а не запроса: план первым."""
    login(client, sms)

    resp = order(client, scene.objective.id, kinds=list(reversed(ALL_KINDS)))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert [(m["kind"], m["status"]) for m in body["materials"]] == [
        (kind, "queued") for kind in ALL_KINDS
    ]
    assert body["status"] == "generating"
    assert (count(LessonMaterial), count(LessonAiJob)) == (7, 7)
    assert {kind: job["status"] for kind, job in jobs(body["id"]).items()} == {
        kind: "queued" for kind in ALL_KINDS
    }


def test_unknown_kind_is_422(client, sms, scene):
    """Вид вне семи — отказ на его месте в списке, комплект не заведён."""
    login(client, sms)

    resp = order(client, scene.objective.id, kinds=["tasks", "crossword"])

    assert resp.status_code == 422
    assert [item["field"] for item in resp.json()["error"]["details"]["fields"]] == ["kinds.1"]
    assert count(LessonKit) == 0


@pytest.mark.parametrize(
    "kinds",
    [["answer_key"], ["lesson_plan", "answer_key"], ["presentation", "game", "answer_key"]],
)
def test_answer_key_without_sources_is_refused(client, sms, scene, kinds):
    """Ключам не из чего собраться без заданий, домашнего или проверочной:
    отказ на поле `kinds` текстом контракта, и ничего не заведено —
    иначе в очереди висел бы материал, обречённый упасть."""
    login(client, sms)

    resp = order(client, scene.objective.id, kinds=kinds)

    assert resp.status_code == 422
    assert resp.json() == invalid("kinds", KEY_WITHOUT_SOURCES)
    assert (count(LessonKit), count(LessonMaterial), count(LessonAiJob)) == (0, 0, 0)


@pytest.mark.parametrize("source", ["tasks", "homework", "assessment"])
def test_answer_key_with_one_source_is_accepted(client, sms, scene, source):
    """Одного источника ключам достаточно."""
    login(client, sms)

    resp = order(client, scene.objective.id, kinds=["answer_key", source])

    assert resp.status_code == 201, resp.text
    assert [m["kind"] for m in resp.json()["materials"]] == [source, "answer_key"]


# -- очередь: план первым ---------------------------------------------------


def test_plan_is_taken_first_even_behind_older_neighbours(client, sms, scene):
    """Соседи стоят в очереди раньше плана по `run_after` — всё равно первым
    берётся план: остальные пишутся по нему."""
    kit_id = kit_of(client, sms, scene, ALL_KINDS)
    execute(
        "UPDATE lesson_ai_job SET run_after = now() - interval '1 hour'"
        " WHERE kit_id = :kit_id AND kind <> 'lesson_plan'",
        kit_id=kit_id,
    )
    ai = KindAi()

    assert run_next(ai)

    assert ai.names == ["lesson_plan"]
    assert jobs(kit_id)["lesson_plan"]["status"] == "done"


def test_neighbours_wait_while_plan_waits_for_retry(client, sms, scene):
    """План отказал и ждёт повтора — пять видов не берутся: иначе они
    собрались бы без плана, хотя план ещё будет. Дождались плана — пошли,
    с планом на входе."""
    kit_id = kit_of(client, sms, scene, ["lesson_plan", *AFTER_PLAN])
    ai = KindAi("lesson_plan")

    assert run_next(ai)
    assert jobs(kit_id)["lesson_plan"]["status"] == "queued"
    assert not run_next(ai)
    assert ai.names == ["lesson_plan"]
    assert {
        kind: job["attempts"] for kind, job in jobs(kit_id).items() if kind != "lesson_plan"
    } == {kind: 0 for kind in AFTER_PLAN}
    shown = materials(client, kit_id)
    assert [shown[kind]["status"] for kind in AFTER_PLAN] == ["queued"] * 5

    ai.failing.clear()
    hurry_retries()
    drain(ai)

    assert ai.names == ["lesson_plan", "lesson_plan", *AFTER_PLAN]
    for kind in AFTER_PLAN:
        assert shown_plan(ai.message(kind)) is not None, kind
    assert client.get(f"/lesson_ai/kits/{kit_id}").json()["status"] == "ready"


def test_neighbours_wait_while_plan_is_running(client, sms, scene):
    """План взял соседний воркер и ещё пишет — соседи ждут его, а не
    генерируются параллельно без плана."""
    kit_id = kit_of(client, sms, scene, ["lesson_plan", "tasks", "game"])
    claim_and_commit()
    assert jobs(kit_id)["lesson_plan"]["status"] == "running"
    ai = KindAi()

    assert not run_next(ai)

    assert ai.calls == []
    assert {job["status"] for kind, job in jobs(kit_id).items() if kind != "lesson_plan"} == {
        "queued"
    }


def test_answer_key_waits_for_every_live_source(client, sms, scene):
    """Ключи стоят в очереди первыми, но ждут, пока живо хоть одно из
    заданий, домашнего и проверочной: иначе в ключах не хватило бы ответов
    к ещё не собранному материалу."""
    kit_id = kit_of(client, sms, scene, ["tasks", "homework", "assessment", "answer_key"])
    execute(
        "UPDATE lesson_ai_job SET run_after = now() - interval '1 hour'"
        " WHERE kit_id = :kit_id AND kind = 'answer_key'",
        kit_id=kit_id,
    )
    ai = KindAi("homework")

    assert run_next(ai)
    assert run_next(ai)
    assert run_next(ai)
    # Задания и проверочная готовы, домашнее ждёт повтора — ключи не берутся
    assert ai.names == ["tasks", "homework", "assessment"]
    assert jobs(kit_id)["homework"]["status"] == "queued"
    assert not run_next(ai)
    assert jobs(kit_id)["answer_key"]["attempts"] == 0

    ai.failing.clear()
    hurry_retries()
    assert run_next(ai)
    assert jobs(kit_id)["answer_key"]["status"] == "queued"
    assert run_next(ai)

    assert jobs(kit_id)["answer_key"]["status"] == "done"
    key = materials(client, kit_id)["answer_key"]["content"]
    assert key["homework"] == KEY_HOMEWORK


def test_failed_plan_releases_neighbours_without_a_plan(client, sms, scene):
    """План упал окончательно — соседи не остаются в очереди навсегда:
    они генерируются по цели и теме, без раздела плана в сообщении.
    Комплект — частично готов, а не пропал."""
    kit_id = kit_of(client, sms, scene, ["lesson_plan", "tasks", "presentation"])
    ai = KindAi("lesson_plan")

    drain(ai)

    assert ai.names == ["lesson_plan"] * 3 + ["presentation", "tasks"]
    assert jobs(kit_id)["lesson_plan"]["status"] == "failed"
    for kind in ("tasks", "presentation"):
        message = ai.message(kind)
        assert PLAN_HEADER not in message
        assert "План урока" not in message
        assert f"Цель обучения по программе: {OBJECTIVE_TEXT}" in message.splitlines()
    shown = materials(client, kit_id)
    assert [shown[kind]["status"] for kind in ("lesson_plan", "presentation", "tasks")] == [
        "failed",
        "ready",
        "ready",
    ]
    assert client.get(f"/lesson_ai/kits/{kit_id}").json()["status"] == "partial"


def test_plan_holds_only_its_own_kit(client, sms, scene):
    """Зависимость — внутри комплекта: план одного учителя, ждущий повтора,
    не держит задания соседнего комплекта."""
    first = kit_of(client, sms, scene, ["lesson_plan", "tasks"])
    second = kit_of(client, sms, scene, ["tasks"])
    ai = KindAi("lesson_plan")

    assert run_next(ai)
    assert run_next(ai)
    assert not run_next(ai)

    assert ai.names == ["lesson_plan", "tasks"]
    assert jobs(first)["tasks"]["status"] == "queued"
    assert jobs(second)["tasks"]["status"] == "done"
    # У второго комплекта плана нет — и в сообщении его нет
    assert PLAN_HEADER not in ai.message("tasks")


# -- план на входе ----------------------------------------------------------


def test_every_neighbour_gets_the_plan_in_form_order_without_code(client, sms, scene):
    """Пять видов получают готовый план: блоки модели в порядке формы
    (начало урока раньше середины и конца — JSONB разложил бы наоборот),
    без раздела и цели обучения и без кода цели."""
    kit_id = kit_of(client, sms, scene, ALL_KINDS)
    ai = KindAi()

    drain(ai)

    assert ai.names == ["lesson_plan", *AFTER_PLAN]
    expected = list(DRAFT)
    for kind in AFTER_PLAN:
        message = ai.message(kind)
        plan = shown_plan(message)
        assert plan == DRAFT, kind
        assert list(plan) == expected, kind
        first, middle, end = (
            message.index(f'"{name}"') for name in ("stage_beginning", "stage_middle", "stage_end")
        )
        assert first < middle < end, kind
        assert CODE not in message, kind
        assert '"learning_objectives"' not in message, kind
    # План сам себя на вход не получает
    assert PLAN_HEADER not in ai.message("lesson_plan")
    assert client.get(f"/lesson_ai/kits/{kit_id}").json()["status"] == "ready"


def test_code_written_into_the_plan_by_hand_does_not_reach_the_model(client, sms, ai, scene):
    """Учитель вписал код цели в план руками до того, как собрались соседи:
    в плане он остаётся, а модель соседнего вида его не видит — ей код
    не дают ни в одном виде (раздел 4.1, правило 2)."""
    kit_id = kit_of(client, sms, scene, ["lesson_plan", "tasks", "homework"])
    fake = KindAi()
    assert run_next(fake)
    plan_id = materials(client, kit_id)["lesson_plan"]["id"]
    middle = [
        {
            "teacher": f"Цель {CODE}: разбирает 41 + 25 по разрядам.",
            "students": "Решают по цепочке, ориентир — 2.1.2.5.",
            "resources": ["доска"],
        }
    ]
    resp = edit(client, plan_id, "stage_middle", middle)
    assert resp.status_code == 200, resp.text

    drain(fake)

    assert fake.names == ["lesson_plan", "tasks", "homework"]
    for kind in ("tasks", "homework"):
        message = fake.message(kind)
        plan = shown_plan(message)
        # Правка дошла — это текущий план, а не снимок генерации
        assert plan["stage_middle"] == [
            {
                "teacher": "Цель: разбирает 41 + 25 по разрядам.",
                "students": "Решают по цепочке, ориентир —.",
                "resources": ["доска"],
            }
        ]
        for code in (CODE, "2.1.2.5"):
            assert code not in message, kind
    assert materials(client, kit_id)["lesson_plan"]["content"]["stage_middle"] == middle


# -- ключи с ответами -------------------------------------------------------


def test_answer_key_is_assembled_without_the_model(client, sms, ai, scene):
    """Ключи собирает сервер из поля `answer`: модель не вызывается, расхода
    нет. Документ — шапка из справочника, затем ключи по видам в порядке
    брифа, группы — в порядке документа источника."""
    kit_id = kit_of(client, sms, scene, ["tasks", "homework", "assessment", "answer_key"])
    fake = KindAi()
    for _ in range(3):
        assert run_next(fake)
    calls = len(fake.calls)

    assert run_next(fake)

    assert len(fake.calls) == calls
    job = jobs(kit_id)["answer_key"]
    assert (job["status"], job["attempts"], job["model"]) == ("done", 1, None)
    assert (job["tokens_in"], job["tokens_out"]) == (0, 0)
    key = materials(client, kit_id)["answer_key"]
    assert key["status"] == "ready"
    assert key["content"] == {
        **SERVER_HEAD,
        "tasks": KEY_TASKS,
        "homework": KEY_HOMEWORK,
        "assessment": KEY_ASSESSMENT,
    }
    # Сверка с примером контракта — не только с тем, как тест сам собрал ключи
    assert key["content"]["tasks"][2]["block"] == "cards_core"
    assert key["content"]["tasks"][2]["title"] == "Раунд 1. Устный счёт"
    assert key["content"]["tasks"][2]["answers"][0] == {
        "number": 1,
        "task": "57 − 40 = ___",
        "answer": "17",
    }
    assert block_kinds(client, key["id"]) == [
        "section",
        "learning_objectives",
        "tasks",
        "homework",
        "assessment",
    ]
    assert client.get(f"/lesson_ai/kits/{kit_id}").json()["status"] == "ready"


def test_failed_source_leaves_keys_from_the_rest(client, sms, ai, scene):
    """Проверочная не собралась — ключи всё равно есть, к заданиям
    и домашнему: один сбойный вид не лишает учителя остального."""
    kit_id = kit_of(client, sms, scene, ["tasks", "homework", "assessment", "answer_key"])

    drain(KindAi("assessment"))

    shown = materials(client, kit_id)
    assert shown["assessment"]["status"] == "failed"
    key = shown["answer_key"]
    assert (key["status"], key["error"]) == ("ready", None)
    assert key["content"] == {**SERVER_HEAD, "tasks": KEY_TASKS, "homework": KEY_HOMEWORK}
    assert block_kinds(client, key["id"]) == [
        "section",
        "learning_objectives",
        "tasks",
        "homework",
    ]
    assert client.get(f"/lesson_ai/kits/{kit_id}").json()["status"] == "partial"


def test_no_sources_fails_the_key_once(client, sms, ai, scene):
    """Все три источника упали — ключам не из чего собраться. Отказ
    окончательный, с первой попытки: повтор ничего не добавит."""
    kit_id = kit_of(client, sms, scene, ["tasks", "homework", "assessment", "answer_key"])
    fake = KindAi("tasks", "homework", "assessment")

    drain(fake)

    assert len(fake.calls) == 9
    job = jobs(kit_id)["answer_key"]
    assert (job["status"], job["attempts"], job["error"]) == ("failed", 1, "no_sources")
    assert (job["model"], job["tokens_in"], job["tokens_out"]) == (None, 0, 0)
    key = materials(client, kit_id)["answer_key"]
    assert (key["status"], key["content"], key["error"]) == ("failed", None, NO_SOURCES)
    assert client.get(f"/lesson_ai/kits/{kit_id}").json()["status"] == "failed"
    assert not run_next(fake)


@pytest.fixture
def full_kit(client, sms, ai, scene) -> dict[str, dict]:
    """Готовый комплект из всех семи видов — материалы по видам."""
    kit_id = kit_of(client, sms, scene, ALL_KINDS)
    drain(KindAi())
    shown = materials(client, kit_id)
    assert {material["status"] for material in shown.values()} == {"ready"}
    return shown


@pytest.mark.parametrize(
    "block", ["section", "learning_objectives", "tasks", "homework", "assessment"]
)
def test_answer_key_blocks_are_locked(client, full_kit, block):
    """Ключи — сводка ответов, а не текст: ни правки, ни перегенерации,
    ни отката ни у одного блока (`409 block_locked`)."""
    key_id = full_kit["answer_key"]["id"]
    history = client.get(url(key_id, block, "versions"))
    assert history.status_code == 200
    version_id = history.json()["items"][0]["id"]
    before = full_kit["answer_key"]["content"]

    for resp in (
        edit(client, key_id, block, before[block]),
        regenerate(client, key_id, block, instruction="сделай короче"),
        restore(client, key_id, block, version_id),
    ):
        assert resp.status_code == 409, resp.text
        assert resp.json() == BLOCK_LOCKED

    assert materials(client, _kit_id(key_id))["answer_key"]["content"] == before


def _kit_id(material_id: int) -> int:
    return fetch("SELECT kit_id FROM lesson_material WHERE id = :id", id=material_id)[0]["kit_id"]


@pytest.mark.parametrize(
    ("kind", "block", "group"),
    [("tasks", "cards_core", 2), ("homework", "required", 0)],
)
def test_editing_a_task_rebuilds_the_keys(client, full_kit, kind, block, group):
    """Учитель поправил задачу и ответ — ключи пересобраны той же правкой:
    иначе тетради проверялись бы по старому ответу. Новый ответ виден
    в документе ключей, в его блоке и новой версией блока."""
    source_id, key_id = full_kit[kind]["id"], full_kit["answer_key"]["id"]
    kit_id = _kit_id(key_id)
    value = json.loads(json.dumps(full_kit[kind]["content"][block]))
    first_group = value[0] if isinstance(value, list) else value
    first_group["tasks"][0] = {"text": "60 + 7 = ___", "answer": "67 — проверено учителем"}
    key_versions = len(client.get(url(key_id, kind, "versions")).json()["items"])

    resp = edit(client, source_id, block, value)

    assert resp.status_code == 200, resp.text
    expected = {"number": 1, "task": "60 + 7 = ___", "answer": "67 — проверено учителем"}
    key = materials(client, kit_id)["answer_key"]["content"]
    assert key[kind][group]["answers"][0] == expected
    items = client.get(url(key_id)).json()["items"]
    shown = {item["kind"]: item["content"] for item in items}
    assert shown[kind][group]["answers"][0] == expected
    assert [item["kind"] for item in items] == [
        "section",
        "learning_objectives",
        "tasks",
        "homework",
        "assessment",
    ]
    assert shown["section"] == SERVER_HEAD["section"]
    assert shown["learning_objectives"] == SERVER_HEAD["learning_objectives"]
    history = client.get(url(key_id, kind, "versions")).json()["items"]
    assert len(history) == key_versions + 1
    assert history[0]["content"][group]["answers"][0] == expected


@pytest.mark.parametrize(
    ("kind", "block", "value"),
    [
        (
            "lesson_plan",
            "stage_middle",
            [{"teacher": "Разбирает 60 + 7 на доске.", "students": "Решают.", "resources": []}],
        ),
        ("game", "scoring", "Очко за каждый верный ответ, двойное — за ответ без подсказки."),
    ],
)
def test_editing_a_non_source_leaves_the_keys_alone(client, full_kit, kind, block, value):
    """Правка плана или игры ключей не касается: ни документа, ни истории."""
    key_id = full_kit["answer_key"]["id"]
    before = fetch(
        "SELECT count(*) AS n FROM lesson_material_version v"
        " JOIN lesson_material_block b ON b.id = v.block_id WHERE b.material_id = :id",
        id=key_id,
    )

    resp = edit(client, full_kit[kind]["id"], block, value)

    assert resp.status_code == 200, resp.text
    after = fetch(
        "SELECT count(*) AS n FROM lesson_material_version v"
        " JOIN lesson_material_block b ON b.id = v.block_id WHERE b.material_id = :id",
        id=key_id,
    )
    assert after == before
    key = materials(client, _kit_id(key_id))["answer_key"]
    assert key["content"] == full_kit["answer_key"]["content"]


# -- игра: механики каталога ------------------------------------------------


def test_game_records_the_mechanics_it_was_given(client, sms, scene):
    """Игре отдают две-три механики каталога строительными блоками, и сервер
    записывает, какие именно: модель могла бы ответить неправду. У остальных
    видов механик нет."""
    kit_id = kit_of(client, sms, scene, ["lesson_plan", "tasks", "game"])
    ai = KindAi()

    drain(ai)

    rows = fetch(
        "SELECT kind, mechanics FROM lesson_material WHERE kit_id = :kit_id", kit_id=kit_id
    )
    mechanics = {row["kind"]: row["mechanics"] for row in rows}
    catalog = {mechanic.id: mechanic for mechanic in load_catalog()}
    game = mechanics.pop("game")
    assert 2 <= len(game) <= 3
    assert len(set(game)) == len(game)
    assert set(game) <= set(catalog)
    assert mechanics == {"lesson_plan": [], "tasks": []}
    message = ai.message("game")
    for slug in game:
        assert f"### {catalog[slug].title}" in message.splitlines()
    # Не отданные механики модель не видит — это блоки, а не меню
    others = [m.title for slug, m in catalog.items() if slug not in game]
    assert not [title for title in others if f"### {title}" in message]
    assert "### " not in ai.message("tasks")


# -- статус комплекта из семи видов -----------------------------------------


def test_full_kit_is_ready(client, full_kit):
    """Семь видов собраны — комплект готов, у всех материалов есть документ."""
    kit_id = _kit_id(full_kit["lesson_plan"]["id"])

    body = client.get(f"/lesson_ai/kits/{kit_id}").json()

    assert body["status"] == "ready"
    assert [m["kind"] for m in body["materials"]] == ALL_KINDS
    for material in body["materials"]:
        assert material["content"], material["kind"]
        assert material["error"] is None


def test_one_failed_kind_makes_the_kit_partial(client, sms, scene):
    """Упала одна игра — комплект частично готов: шесть материалов на месте,
    у седьмого видна причина."""
    kit_id = kit_of(client, sms, scene, ALL_KINDS)

    drain(KindAi("game"))

    body = client.get(f"/lesson_ai/kits/{kit_id}").json()
    assert body["status"] == "partial"
    statuses = {m["kind"]: (m["status"], m["error"]) for m in body["materials"]}
    assert statuses.pop("game") == (
        "failed",
        {"code": "ai_failed", "message": "Модель не ответила"},
    )
    assert set(statuses.values()) == {("ready", None)}


def test_server_blocks_head_every_generated_kind(client, full_kit):
    """У всех видов, кроме плана и ключей, впереди два блока сервера —
    раздел и цель с кодом дословно из справочника, закрытые для правки."""
    for kind in AFTER_PLAN:
        material = full_kit[kind]
        assert {name: material["content"][name] for name in SERVER_HEAD} == SERVER_HEAD, kind
        items = client.get(url(material["id"])).json()["items"]
        assert [(i["kind"], i["editable"]) for i in items[:2]] == [
            ("section", False),
            ("learning_objectives", False),
        ], kind
        assert [i["kind"] for i in items[2:]] == list(REPLIES[kind]), kind
    with OrmSession(get_engine()) as db:
        plan = db.scalar(select(LessonMaterial).where(LessonMaterial.kind == "lesson_plan"))
        assert plan.content["learning_objectives"] == SERVER_HEAD["learning_objectives"]
