"""«Жоспар», режим правки: пересборка комплекта под текущие цели и кнопки-подсказки
(решение владельца 16.10.2026) — ручка экрана, инструмент чата, суточный лимит,
версии блоков и правило промпта про два пути после смены целей."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import LessonAiJob, LessonKit, LessonMaterial
from app.api.deps import get_lesson_kits_limiter
from app.application.ratelimit import SlidingWindowLimiter
from app.lesson_ai.prompts import edit_chat
from app.main import app
from tests.conftest import login
from tests.test_lesson_ai_chat import OTHER_PHONE
from tests.test_lesson_ai_chat_edit import (
    PRESENTATION,
    events,
    feed,
    generate,
    material_id,
    speak,
    tool_result,
    versions,
)
from tests.test_lesson_ai_edit_goals import two_goal_chat
from tests.test_lesson_ai_kits import DRAFT
from tests.test_lesson_ai_multi import ChatGoals

pytestmark = pytest.mark.usefixtures("lesson_ai_open")

NEW_DRAFT = {**DRAFT, "lesson_goals": ["Новая цель после пересборки"]}


def regen(client, kit_id: int, **body):
    return client.post(f"/lesson_ai/kits/{kit_id}/regenerate", json=body)


def job_count(kit_id: int) -> int:
    with OrmSession(get_engine()) as db:
        return db.scalar(select(func.count()).select_from(LessonAiJob).where(
            LessonAiJob.kit_id == kit_id, LessonAiJob.status == "queued"))


@pytest.fixture
def goals() -> ChatGoals:
    return ChatGoals()


def ready_kit(client, ai, goals):
    chat = two_goal_chat(client, goals, kinds=("lesson_plan", "presentation"))
    generate(ai, DRAFT, PRESENTATION)
    return chat


# -- ручка экрана ------------------------------------------------------------


def test_regenerate_queues_all_materials_keeps_versions_and_writes_events(
    client, sms, ai, goals
):
    login(client, sms)
    chat = ready_kit(client, ai, goals)
    kit_id = chat["kit_id"]
    mid = material_id(client, chat["id"], "lesson_plan")
    before = len(versions(client, mid, "lesson_goals"))

    resp = regen(client, kit_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "generating"
    assert {m["status"] for m in body["materials"]} == {"queued"}
    assert job_count(kit_id) == 2
    statuses = [e["status"] for e in events(feed(client, chat["id"])) if e["type"] == "kit_status"]
    assert statuses[-1] == "generating"

    generate(ai, NEW_DRAFT, PRESENTATION)

    kit = client.get(f"/lesson_ai/kits/{kit_id}").json()
    assert kit["status"] == "ready"
    plan = next(m for m in kit["materials"] if m["kind"] == "lesson_plan")
    assert plan["content"]["lesson_goals"] == NEW_DRAFT["lesson_goals"]
    # Прежнее содержимое осталось в истории блока, новое легло поверх
    history = versions(client, mid, "lesson_goals")
    assert len(history) == before + 1
    assert DRAFT["lesson_goals"] in [v["content"] for v in history]
    statuses = [e["status"] for e in events(feed(client, chat["id"])) if e["type"] == "kit_status"]
    assert statuses[-1] == "ready"
    with OrmSession(get_engine()) as db:
        assert db.get(LessonKit, kit_id).personal is True
        assert {m.bank_id for m in db.scalars(select(LessonMaterial))} == {None}


def test_regenerate_uses_current_goals_and_the_same_lesson_params(client, sms, ai, goals):
    login(client, sms)
    chat = ready_kit(client, ai, goals)
    assert client.put(
        f"/lesson_ai/kits/{chat['kit_id']}/objectives", json={"objective_ids": [goals.ru_only.id]}
    ).status_code == 200
    assert regen(client, chat["kit_id"]).status_code == 200
    ai.calls.clear()
    generate(ai, NEW_DRAFT, PRESENTATION)
    prompt = ai.calls[0]["messages"][-1].content
    assert goals.ru_only.text in prompt
    assert goals.objective.text not in prompt
    assert "Сложение" in prompt


def test_regenerate_some_kinds_leaves_the_rest_alone(client, sms, ai, goals):
    login(client, sms)
    chat = ready_kit(client, ai, goals)

    resp = regen(client, chat["kit_id"], kinds=["presentation"])

    assert resp.status_code == 200, resp.text
    by_kind = {m["kind"]: m["status"] for m in resp.json()["materials"]}
    assert by_kind == {"lesson_plan": "ready", "presentation": "queued"}
    assert job_count(chat["kit_id"]) == 1


def test_regenerate_refused_while_the_kit_is_building(client, sms, ai, goals):
    login(client, sms)
    chat = two_goal_chat(client, goals)
    resp = regen(client, chat["kit_id"])
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "conflict")
    assert job_count(chat["kit_id"]) == 1


def test_regenerate_hides_a_foreign_kit(client, client2, sms, ai, goals):
    login(client, sms)
    chat = ready_kit(client, ai, goals)
    login(client2, sms, phone=OTHER_PHONE)
    assert regen(client2, chat["kit_id"]).status_code == 404
    assert regen(client2, 999999).status_code == 404
    assert job_count(chat["kit_id"]) == 0


def test_regenerate_rejects_a_kind_the_kit_does_not_have(client, sms, ai, goals):
    login(client, sms)
    chat = ready_kit(client, ai, goals)
    resp = regen(client, chat["kit_id"], kinds=["game"])
    assert resp.status_code == 422


def test_regenerate_costs_one_kit_from_the_daily_limit(client, sms, ai, goals):
    """Заказ съел один комплект из двух; пересборка всего — ровно один (не по числу видов);
    следующая — отказ с понятным текстом, и ничего не ставится в очередь."""
    limiter = SlidingWindowLimiter(2, 86400)
    app.dependency_overrides[get_lesson_kits_limiter] = lambda: limiter
    login(client, sms)
    chat = ready_kit(client, ai, goals)

    assert regen(client, chat["kit_id"]).status_code == 200
    generate(ai, NEW_DRAFT, PRESENTATION)
    resp = regen(client, chat["kit_id"])

    assert (resp.status_code, resp.json()["error"]["code"]) == (429, "rate_limited")
    assert "пересборка считается как новый комплект" in resp.json()["error"]["message"]
    assert job_count(chat["kit_id"]) == 0


# -- инструмент чата ---------------------------------------------------------


def test_regenerate_kit_tool_queues_the_kit(client, sms, ai, goals):
    login(client, sms)
    chat = ready_kit(client, ai, goals)
    ai.wants("regenerate_kit")
    ai.says("Пересборка идёт")

    out = speak(client, chat["id"], "Пересобрать урок под новые цели")

    result = tool_result(out)
    assert result["ok"] is True and result["kinds"] == ["lesson_plan", "presentation"]
    assert job_count(chat["kit_id"]) == 2


def test_regenerate_kit_tool_reports_a_running_build_to_the_model(client, sms, ai, goals):
    login(client, sms)
    chat = two_goal_chat(client, goals)
    ai.wants("regenerate_kit")
    ai.says("Комплект ещё собирается")

    out = speak(client, chat["id"], "пересобери")

    assert tool_result(out)["error"]["code"] == "conflict"
    assert job_count(chat["kit_id"]) == 1


def test_regenerate_kit_tool_rejects_bad_kinds(client, sms, ai, goals):
    login(client, sms)
    chat = ready_kit(client, ai, goals)
    ai.wants("regenerate_kit", kinds=["nothing"])
    ai.says("Ок")
    out = speak(client, chat["id"], "пересобери")
    assert tool_result(out)["error"]["code"] == "bad_arguments"


# -- промпт и кнопки ---------------------------------------------------------


def test_prompt_names_the_two_paths_and_forbids_asking_what_first():
    system = edit_chat.SYSTEM
    assert "«Пересобрать урок под новые цели» (рекомендуется" in system
    assert "«Поправить только связанное»" in system
    assert "Не спрашивай, «что переписать первым»" in system
    assert "regenerate_kit" in system
    assert "САМ по оглавлению" in system


def test_buttons_follow_a_goal_change_and_reach_the_feed(client, sms, ai, goals):
    login(client, sms)
    chat = ready_kit(client, ai, goals)
    ai.wants("set_objectives", objective_ids=[goals.ru_only.id])
    ai.says("Цель убрана. Пересобрать урок или поправить связанное?")

    out = speak(client, chat["id"], "давай ту би уберём")

    done = next(data for name, data in out if name == "done")
    last = [m for m in done["chat"]["items"] if m["role"] == "assistant"][-1]
    assert [s["text"] for s in last["suggestions"]] == [
        "Пересобрать урок под новые цели",
        "Поправить только связанное",
    ]
    items = feed(client, chat["id"])["items"]
    assert [m for m in items if m["role"] == "assistant"][-1]["suggestions"] == last["suggestions"]


def test_no_buttons_on_an_ordinary_turn(client, sms, ai, goals):
    login(client, sms)
    chat = ready_kit(client, ai, goals)
    ai.says("Слайдов три")
    speak(client, chat["id"], "сколько слайдов")
    items = feed(client, chat["id"])["items"]
    assert [m for m in items if m["role"] == "assistant"][-1]["suggestions"] is None


def test_suggestions_language_and_cap():
    kz = edit_chat.suggestions(
        [{"type": "objectives_changed"}], ["set_objectives"], stopped_at_cap=False,
        text="мақсатты алып тастаңыз",
    )
    assert [s["text"] for s in kz] == [edit_chat.REBUILD[1], edit_chat.RELATED[1]]
    cap = edit_chat.suggestions([], [], stopped_at_cap=True, text="продолжай")
    assert cap == [{"label": "Продолжай", "text": "Продолжай"}]
    after_rebuild = edit_chat.suggestions(
        [{"type": "objectives_changed"}], ["set_objectives", "regenerate_kit"],
        stopped_at_cap=False, text="",
    )
    assert after_rebuild is None
    after_rewrite = edit_chat.suggestions(
        [{"type": "objectives_changed"}, {"type": "block_changed"}], [],
        stopped_at_cap=False, text="",
    )
    assert after_rewrite is None
