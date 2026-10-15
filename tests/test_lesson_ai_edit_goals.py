"""«Жоспар», режим правки: помощник знает урок и меняет цели после генерации
(решение владельца 15.10.2026) — состояние в сообщении, `set_objectives`, ручка экрана."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import LessonMaterial
from app.domain.lesson_ai import MATERIAL_KINDS
from app.lesson_ai import edit_tools
from app.lesson_ai.kinds import SPECS
from app.lesson_ai.kit_map import kinds_catalog, kit_outline
from app.lesson_ai.prompts import edit_chat
from app.lesson_ai.prompts.order_chat import KIND_LABELS
from tests.conftest import login
from tests.test_lesson_ai_chat import confirm as confirm_chat
from tests.test_lesson_ai_chat import new_chat, patch, turn_calls
from tests.test_lesson_ai_chat_edit import (
    PRESENTATION,
    blocks,
    events,
    feed,
    generate,
    material_id,
    speak,
    tool_result,
)
from tests.test_lesson_ai_kits import DRAFT, OBJECTIVE_TEXT, OTHER_PHONE
from tests.test_lesson_ai_multi import ChatGoals
from tests.test_lesson_ai_usage import NOTES

pytestmark = pytest.mark.usefixtures("lesson_ai_open")


@pytest.fixture
def goals() -> ChatGoals:
    return ChatGoals()


def two_goal_chat(client, goals: ChatGoals, kinds=("lesson_plan",)) -> dict:
    chat = new_chat(client)
    ids = [goals.objective.id, goals.ru_only.id]
    resp = patch(client, chat["id"], objective_ids=ids, lang="ru", topic="Сложение")
    assert resp.status_code == 200, resp.text
    done = confirm_chat(client, chat["id"], kinds=list(kinds), notes=NOTES)
    assert done.status_code == 201, done.text
    return done.json()["chat"]


def state_of(ai) -> str:
    return turn_calls(ai)[-1]["messages"][-2].content


def kit_goals(client, kit_id: int) -> list[int]:
    return [o["id"] for o in client.get(f"/lesson_ai/kits/{kit_id}").json()["objectives"]]


# -- состояние в сообщении -------------------------------------------------


def test_state_names_goals_subject_and_materials(client, sms, ai, goals):
    login(client, sms)
    chat = two_goal_chat(client, goals)
    generate(ai, DRAFT)
    ai.says("Какую убрать?")

    speak(client, chat["id"], "давай одну цель уберем из урока")

    state = state_of(ai)
    assert "- предмет:" in state and "(math)" in state and "2 класс" in state
    assert f"1. {goals.objective.code} — {OBJECTIVE_TEXT}" in state
    assert f"2. {goals.ru_only.code} — {goals.ru_only.text}" in state
    assert "lesson_plan (краткосрочный план (КСП; по-казахски ҚМЖ)): готов" in state
    assert "· stage_middle: список, " in state
    assert "· learning_objectives: сервер, не правится" in state
    assert f"id {goals.objective.id}" in state


def test_edit_tools_include_goal_search_and_set_objectives(client, sms, ai, goals):
    login(client, sms)
    chat = two_goal_chat(client, goals)
    generate(ai, DRAFT)
    ai.says("Ок")
    speak(client, chat["id"])
    names = {t["function"]["name"] for t in turn_calls(ai)[-1]["tools"]}
    assert {
        "set_objectives", "find_objectives", "browse_objectives", "show_objectives",
        "get_block", "rewrite_block",
    } <= names


def sample_content(spec) -> dict:
    return {name: "Пример текста блока" for name in spec.draft.model_fields}


def test_outline_is_built_for_all_seven_kinds():
    materials = [
        {
            "kind": spec.kind,
            "status": "ready",
            "blocks": [
                {"name": name, "content": value, "server": False}
                for name, value in sample_content(spec).items()
            ],
        }
        for spec in SPECS
    ] + [{"kind": "answer_key", "status": "ready", "blocks": [
        {"name": "tasks", "content": [], "server": True}]}]
    text = "\n".join(kit_outline(materials))
    for kind in MATERIAL_KINDS:
        assert f"- {kind} ({KIND_LABELS[kind]}): готов" in text
    for spec in SPECS:
        for name in spec.draft.model_fields:
            assert f"· {name}:" in text


def test_catalog_is_built_from_the_schemas_and_lives_in_the_system_prompt():
    for spec in SPECS:
        for name in spec.draft.model_fields:
            assert f"· {name} — " in kinds_catalog()
    assert kinds_catalog() in edit_chat.SYSTEM
    assert "answer_key" in edit_chat.SYSTEM


def test_slides_are_numbered_through_the_deck():
    lines = kit_outline([{
        "kind": "presentation", "status": "ready",
        "blocks": [
            {"name": name, "content": value, "server": False}
            for name, value in PRESENTATION.items()
        ],
    }])
    text = "\n".join(lines)
    assert "слайд 3 «Вспомним»" in text
    assert "слайд 4 «Новое»; слайд 5 «Реши»" in text
    assert "слайд 6 «Итог»" in text


def test_state_size_on_a_full_kit_stays_small():
    """Оглавление комплекта с длинными блоками — в пределах трёх тысяч токенов
    (знаки / 4), даже когда пунктов много."""
    long_item = {"title": "Заголовок " * 20, "teacher": "Текст шага " * 40, "tasks": [1, 2]}
    materials = [
        {"kind": spec.kind, "status": "ready", "blocks": [
            {"name": name, "content": [long_item] * 30, "server": False}
            for name in spec.draft.model_fields
        ]}
        for spec in SPECS
    ]
    text = "\n".join(kit_outline(materials))
    assert len(text) // 4 <= 3000
    # Структура осталась целиком: у каждого блока видно число пунктов
    assert text.count("список, 30") == sum(len(spec.draft.model_fields) for spec in SPECS)


# -- set_objectives --------------------------------------------------------


def test_set_objectives_changes_the_kit_the_server_block_and_the_feed(client, sms, ai, goals):
    login(client, sms)
    chat = two_goal_chat(client, goals, kinds=("lesson_plan", "presentation"))
    generate(ai, DRAFT, PRESENTATION)
    kit_id = chat["kit_id"]
    new_set = [goals.ru_only.id, goals.extra.id]
    ai.wants("set_objectives", objective_ids=new_set)
    ai.says("Цели изменены. Переписать критерии?")

    out = speak(client, chat["id"], "замени первую цель на 2.1.2.9")

    assert tool_result(out)["ok"] is True
    assert kit_goals(client, kit_id) == new_set
    codes = [goals.ru_only.code, goals.extra.code]
    for kind in ("lesson_plan", "presentation"):
        mid = material_id(client, chat["id"], kind)
        shown = blocks(client, mid)["learning_objectives"]
        assert [o["code"] for o in shown] == codes
    # Остальное не тронуто: переписать блоки модель только предложит
    assert blocks(client, material_id(client, chat["id"], "lesson_plan"))["lesson_goals"] == (
        DRAFT["lesson_goals"]
    )
    [event] = [e for e in events(feed(client, chat["id"])) if e["type"] == "objectives_changed"]
    assert [o["id"] for o in event["objectives"]] == new_set
    changed = [data for name, data in out if name == "material_changed"]
    assert changed and changed[0]["type"] == "objectives_changed"
    with OrmSession(get_engine()) as db:
        banks = db.scalars(select(LessonMaterial.bank_id).where(LessonMaterial.kit_id == kit_id))
        assert set(banks) == {None}


def test_set_objectives_refuses_a_mixed_set_and_changes_nothing(client, sms, ai, goals):
    login(client, sms)
    chat = two_goal_chat(client, goals)
    generate(ai, DRAFT)
    before = kit_goals(client, chat["kit_id"])
    ai.wants("set_objectives", objective_ids=[goals.objective.id, goals.first_grade.id])
    ai.says("Не получилось")

    out = speak(client, chat["id"], "добавь цель первого класса")

    assert tool_result(out)["error"]["code"] == "objectives_mixed"
    assert kit_goals(client, chat["kit_id"]) == before


def test_set_objectives_is_a_known_tool_with_bad_arguments_refused(client, sms, ai, goals):
    login(client, sms)
    chat = two_goal_chat(client, goals)
    generate(ai, DRAFT)
    ai.wants("set_objectives", objective_ids=[])
    ai.says("Ок")
    out = speak(client, chat["id"], "убери всё")
    assert tool_result(out)["error"]["code"] == "bad_arguments"
    assert edit_tools.SET_OBJECTIVES in {s["function"]["name"] for s in edit_tools.SCHEMAS}


# -- ручка экрана ----------------------------------------------------------


def test_put_objectives_route_changes_the_set_and_writes_the_event(client, sms, ai, goals):
    login(client, sms)
    chat = two_goal_chat(client, goals)
    generate(ai, DRAFT)
    kit_id = chat["kit_id"]

    resp = client.put(
        f"/lesson_ai/kits/{kit_id}/objectives",
        json={"objective_ids": [goals.ru_only.id]},
    )

    assert resp.status_code == 200, resp.text
    assert [o["id"] for o in resp.json()["objectives"]] == [goals.ru_only.id]
    mid = material_id(client, chat["id"], "lesson_plan")
    assert [o["code"] for o in blocks(client, mid)["learning_objectives"]] == [goals.ru_only.code]
    [event] = [e for e in events(feed(client, chat["id"])) if e["type"] == "objectives_changed"]
    assert event["objectives"] == [{"id": goals.ru_only.id, "code": goals.ru_only.code}]


def test_put_objectives_route_refuses_mixed_empty_and_foreign(client, client2, sms, ai, goals):
    login(client, sms)
    chat = two_goal_chat(client, goals)
    generate(ai, DRAFT)
    url = f"/lesson_ai/kits/{chat['kit_id']}/objectives"

    mixed = client.put(url, json={"objective_ids": [goals.objective.id, goals.first_grade.id]})
    assert (mixed.status_code, mixed.json()["error"]["code"]) == (422, "objectives_mixed")
    assert client.put(url, json={"objective_ids": []}).status_code == 422
    assert kit_goals(client, chat["kit_id"]) == [goals.objective.id, goals.ru_only.id]


def test_put_objectives_route_hides_a_foreign_kit(client, client2, sms, ai, goals):
    login(client, sms)
    chat = two_goal_chat(client, goals)
    login(client2, sms, phone=OTHER_PHONE)
    resp = client2.put(
        f"/lesson_ai/kits/{chat['kit_id']}/objectives", json={"objective_ids": [goals.extra.id]}
    )
    assert resp.status_code == 404
