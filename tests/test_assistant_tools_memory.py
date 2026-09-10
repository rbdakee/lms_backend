"""Четыре инструмента памяти: индекс, чтение, запись, удаление.

Сам сервис памяти — границы, запрет персданных, две области — проверен
в `test_assistant_memory.py`, и здесь не переписывается. Здесь проверяется
то, что видит модель: пометки опасности, состав ответа инструмента и то,
как запись доходит до системного промпта.

Два места, где ошибка не видна глазом и стоит дороже всего:

- **`memory_list` не отдаёт тел.** Индекс всегда, содержимое по нужде —
  это суть схемы, а не экономия токенов;
- **отказ по персданным идёт через инструмент** и не повторяет сам текст,
  иначе телефон переезжает из памяти в ленту чата и в лог.

Номера и ИИН в сцене вымышленные (общий `CLAUDE.md`).
"""

import json

import pytest

from app.assistant import registry
from app.assistant.registry import ToolError
from tests.conftest import fake_iin, login_admin, make_course, make_user
from tests.test_assistant_chat import turn_state

# Номер вымышленный: настоящих в тестах не бывает. Отличается от админского
# номера `conftest`, чтобы тесты с входом по HTTP заводили своего человека.
MEMORY_ADMIN_PHONE = "+77010000010"

PRICING = {
    "name": "pricing",
    "description": "как считаем цену курса на каждой площадке",
    "body": "Цена своя у каждой площадки: на p1 45 000, на p2 60 000.",
}


@pytest.fixture
def admin_id() -> int:
    """Админ заводится напрямую: инструменты памяти зовутся мимо HTTP,
    и вход по коду им не нужен."""
    return make_user(MEMORY_ADMIN_PHONE, is_admin=True).id


@pytest.fixture
def ctx(tool_ctx, admin_id):
    """Контекст вызова без чата и без закреплённого курса."""
    return tool_ctx(admin_id)


def dumped(result: dict) -> str:
    """Ответ инструмента ровно в том виде, в каком он уходит в OpenAI."""
    return json.dumps(result, ensure_ascii=False, default=str)


# -- пометки ---------------------------------------------------------------


def test_marks_are_the_ones_the_brief_named():
    """`memory_delete` помечен `write`, а не `confirm`, хотя удаляет: запись
    памяти — заметка ассистента о себе, а не чужая работа (раздел 7)."""
    assert registry.get("memory_list").danger == "read"
    assert registry.get("memory_read").danger == "read"
    assert registry.get("memory_write").danger == "write"
    assert registry.get("memory_delete").danger == "write"


def test_the_memory_toolset_is_closed():
    """Состав из раздела 7 закрытый: пятого инструмента памяти не бывает."""
    names = sorted(name for name in registry.all_tools() if name.startswith("memory"))
    assert names == ["memory_delete", "memory_list", "memory_read", "memory_write"]


# -- индекс и тело ---------------------------------------------------------


def test_write_then_read_the_body(ctx):
    registry.call("memory_write", dict(PRICING), ctx)
    record = registry.call("memory_read", {"name": "pricing"}, ctx)
    assert record["body"].startswith("Цена своя")
    assert record["scope"] == "global"


def test_index_gives_names_and_descriptions_but_no_bodies(ctx):
    """Тело грузится только по `memory_read` — иначе индекс перестаёт быть
    индексом и начинает стоить как переписка."""
    registry.call("memory_write", dict(PRICING), ctx)
    result = registry.call("memory_list", {}, ctx)

    assert result["total"] == 1
    assert result["items"] == [
        {
            "name": "pricing",
            "description": PRICING["description"],
            "scope": "global",
            "course_id": None,
        }
    ]
    assert "45 000" not in dumped(result)


def test_reading_a_missing_record_is_a_refusal_not_a_crash(ctx):
    with pytest.raises(ToolError) as exc:
        registry.call("memory_read", {"name": "pricing"}, ctx)
    assert exc.value.code == "memory_not_found"
    assert "pricing" in exc.value.message


def test_name_case_does_not_hide_a_record(ctx):
    """Имена лежат слагом в нижнем регистре: «Pricing» из ответа модели —
    та же запись, и отказывать по регистру было бы придиркой."""
    registry.call("memory_write", dict(PRICING), ctx)
    assert registry.call("memory_read", {"name": " Pricing "}, ctx)["name"] == "pricing"


def test_delete_removes_the_record_from_the_index(ctx):
    registry.call("memory_write", dict(PRICING), ctx)
    assert registry.call("memory_delete", {"name": "pricing"}, ctx)["deleted"] is True
    assert registry.call("memory_list", {}, ctx)["items"] == []


def test_deleting_a_missing_record_is_a_refusal(ctx):
    with pytest.raises(ToolError) as exc:
        registry.call("memory_delete", {"name": "pricing"}, ctx)
    assert exc.value.code == "memory_not_found"


# -- область записи --------------------------------------------------------


def test_list_mixes_the_pinned_course_memory_into_the_common_one(tool_ctx, admin_id):
    """Память курса подмешивается в индекс автоматически, когда у чата
    закреплён курс, — ответ инструмента совпадает с тем, что уже в промпте."""
    course = make_course()
    other = make_course()
    ctx = tool_ctx(admin_id, course_id=course.id)
    registry.call("memory_write", {"name": "common", "description": "общее", "body": "х"}, ctx)
    registry.call(
        "memory_write",
        {"name": "tone", "description": "тон курса", "body": "х", "course_id": course.id},
        ctx,
    )
    registry.call(
        "memory_write",
        {"name": "tone-other", "description": "тон", "body": "х", "course_id": other.id},
        ctx,
    )

    names = [row["name"] for row in registry.call("memory_list", {}, ctx)["items"]]
    assert names == ["common", "tone"]
    # В память другого курса ассистент заглядывает по прямому запросу
    alien = registry.call("memory_list", {"course_id": other.id}, ctx)
    assert [row["name"] for row in alien["items"]] == ["common", "tone-other"]


def test_a_record_is_common_until_the_course_is_named(tool_ctx, admin_id):
    """Умолчание записи — общая память, даже когда курс закреплён: лишняя
    строка в чужом индексе стоит дешевле договорённости, которой в других
    чатах не видно вовсе."""
    course = make_course()
    ctx = tool_ctx(admin_id, course_id=course.id)
    registry.call("memory_write", dict(PRICING), ctx)
    assert registry.call("memory_list", {}, ctx)["items"][0]["scope"] == "global"


def test_writing_into_a_course_that_does_not_exist_is_a_refusal(ctx):
    """У `course_id` внешний ключ: выдуманный номер уронил бы весь ход
    на flush, а модель прочитала бы 500 вместо отказа."""
    with pytest.raises(ToolError) as exc:
        registry.call(
            "memory_write",
            {"name": "tone", "description": "тон", "body": "х", "course_id": 4242},
            ctx,
        )
    assert exc.value.code == "not_found"


def test_course_record_reaches_only_the_chat_with_that_course(client, sms, ai, tool_ctx):
    """Запись курса доходит до промпта своего чата — и не доходит до чужого.
    Проверяется по системному промпту, а не по ответу инструмента: до модели
    доезжает именно он."""
    admin_id = login_admin(client, sms).json()["id"]
    course = make_course(title="Оценивание")
    other = make_course(title="Другой курс")
    ctx = tool_ctx(admin_id, course_id=course.id)
    registry.call(
        "memory_write",
        {
            "name": "tone",
            "description": "тон ответов по этому курсу",
            "body": "Отвечаем по-казахски с обязательной вычиткой.",
            "course_id": course.id,
        },
        ctx,
    )
    ctx.db.commit()

    mine = client.post("/admin/assistant/threads", json={"course_id": course.id}).json()
    client.post(f"/admin/assistant/threads/{mine['id']}/messages", json={"text": "привет"})
    state = turn_state(ai)
    assert "`tone` — тон ответов по этому курсу" in state
    # Тело в промпт не уходит: индекс всегда, содержимое по нужде
    assert "вычиткой" not in state.split("## Память")[1].split("## Состояние")[0]

    alien = client.post("/admin/assistant/threads", json={"course_id": other.id}).json()
    client.post(f"/admin/assistant/threads/{alien['id']}/messages", json={"text": "привет"})
    assert "`tone`" not in turn_state(ai)


# -- персональные данные ---------------------------------------------------


@pytest.mark.parametrize("kind", ["телефон", "ИИН"])
def test_personal_data_is_refused_by_the_tool(ctx, kind):
    """Запрет ПД — проверка на сервере, а не инструкция модели. Инструмент
    отдаёт её отказом, который модель прочитает и исправит."""
    secret = "+77010000011" if kind == "телефон" else fake_iin()

    with pytest.raises(ToolError) as exc:
        registry.call(
            "memory_write",
            {"name": "note", "description": "заметка", "body": f"{kind} {secret}"},
            ctx,
        )

    assert exc.value.code == "pd_in_memory"
    # Сам текст в отказ не попадает: иначе персданные переезжают из памяти
    # в ленту чата и в лог — ровно туда, куда их не пускает CLAUDE.md
    assert secret not in dumped(exc.value.as_result())
    assert registry.call("memory_list", {}, ctx)["items"] == []
