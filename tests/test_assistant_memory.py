"""Память ассистента: индекс, границы и запрет персданных.

Инструменты памяти (`memory_list`, `memory_read`, `memory_write`,
`memory_delete`) проверяются отдельно — здесь сам сервис, на котором
они стоят.
"""

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import User
from app.application.assistant_memory import BODY_MAX, TOTAL_MAX, MemoryService
from app.assistant.registry import ToolError
from tests.conftest import make_course, make_user


@pytest.fixture
def memory():
    """Сервис на своей сессии — и она закрывается: оставленная открытой,
    она держит транзакцию, и следующий тест виснет на TRUNCATE."""
    db = OrmSession(get_engine())
    try:
        yield MemoryService(db)
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def admin(memory) -> User:
    user = make_user("+77010000010", is_admin=True)
    return memory.db.get(User, user.id)


def test_write_then_read(memory, admin):
    memory.write(
        admin,
        name="pricing",
        description="как считаем цену курса",
        body="Цена своя у каждой площадки: на p1 45 000, на p2 60 000.",
    )
    assert memory.read("pricing")["body"].startswith("Цена своя")


def test_index_has_names_and_descriptions_only(memory, admin):
    memory.write(admin, name="pricing", description="цены", body="длинное тело")
    index = memory.index()
    assert index == [
        {"name": "pricing", "description": "цены", "scope": "global", "course_id": None}
    ]


def test_course_memory_joins_the_index_when_the_course_is_pinned(memory, admin):
    course = make_course()
    other = make_course()
    memory.write(admin, name="common", description="общее", body="х")
    memory.write(admin, name="tone", description="тон курса", body="х", course_id=course.id)
    memory.write(admin, name="tone", description="тон другого", body="х", course_id=other.id)

    assert [row["name"] for row in memory.index()] == ["common"]
    assert sorted(row["name"] for row in memory.index(course.id)) == ["common", "tone"]
    assert memory.read("tone", course.id)["description"] == "тон курса"


def test_same_name_in_global_and_course_are_different_records(memory, admin):
    course = make_course()
    memory.write(admin, name="tone", description="общий тон", body="общий")
    memory.write(admin, name="tone", description="тон курса", body="курсовой", course_id=course.id)
    assert memory.read("tone")["body"] == "общий"
    assert memory.read("tone", course.id)["body"] == "курсовой"


def test_second_write_updates(memory, admin):
    memory.write(admin, name="pricing", description="цены", body="было")
    memory.write(admin, name="pricing", description="цены", body="стало")
    assert memory.read("pricing")["body"] == "стало"
    assert len(memory.index()) == 1


def test_delete(memory, admin):
    memory.write(admin, name="pricing", description="цены", body="х")
    memory.delete("pricing")
    assert memory.index() == []
    with pytest.raises(ToolError) as exc:
        memory.read("pricing")
    assert exc.value.code == "memory_not_found"


# -- запрет персданных ----------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Позвонить на +7 701 000 00 11",
        "телефон 87010000011",
        "ИИН 990101300123",
        "номер +77010000011 просил перезвонить",
    ],
)
def test_personal_data_is_refused_by_the_server(memory, admin, body):
    """Запрет ПД — проверка на сервере, а не инструкция модели: модель
    уговаривается, проверка нет."""
    with pytest.raises(ToolError) as exc:
        memory.write(admin, name="note", description="заметка", body=body)
    assert exc.value.code == "pd_in_memory"
    assert memory.index() == []


def test_refusal_does_not_repeat_the_personal_data(memory, admin):
    """Иначе персданные переезжают из памяти в ленту чата и в лог — то есть
    ровно туда, куда их не пускает CLAUDE.md."""
    with pytest.raises(ToolError) as exc:
        memory.write(admin, name="note", description="заметка", body="ИИН 990101300123")
    assert "990101300123" not in exc.value.message


def test_personal_data_is_refused_in_the_description_too(memory, admin):
    with pytest.raises(ToolError):
        memory.write(admin, name="note", description="звонить +77010000011", body="х")


def test_ordinary_numbers_are_allowed(memory, admin):
    """Цена, часы и даты — не персданные, и запрет не должен их ловить."""
    memory.write(
        admin,
        name="pricing",
        description="цена курса 45000 тенге на p1",
        body="Курс 72 часа, старт 09.09.2026, цена 45 000 на p1 и 60 000 на p2.",
    )
    assert memory.read("pricing")["description"].startswith("цена курса")


# -- границы --------------------------------------------------------------


def test_name_must_be_a_slug(memory, admin):
    with pytest.raises(ToolError) as exc:
        memory.write(admin, name="Цены Курса", description="цены", body="х")
    assert exc.value.code == "bad_memory_name"


def test_description_is_required(memory, admin):
    """Индекс без описания перестаёт работать как индекс."""
    with pytest.raises(ToolError):
        memory.write(admin, name="pricing", description="  ", body="х")


def test_body_has_a_ceiling(memory, admin):
    with pytest.raises(ToolError):
        memory.write(admin, name="pricing", description="цены", body="я" * (BODY_MAX + 1))


def test_hundred_records_is_the_ceiling(memory, admin):
    for number in range(TOTAL_MAX):
        memory.write(admin, name=f"note-{number}", description="заметка", body="х")
    with pytest.raises(ToolError) as exc:
        memory.write(admin, name="one-more", description="заметка", body="х")
    assert exc.value.code == "memory_full"


# -- экран ----------------------------------------------------------------


def test_memory_screen_shows_everything(client, sms):
    from tests.conftest import login_admin

    login_admin(client, sms)
    admin_id = client.get("/me").json()["id"]
    course = make_course()
    with OrmSession(get_engine()) as db:
        service = MemoryService(db)
        author = db.get(User, admin_id)
        service.write(author, name="common", description="общее", body="х")
        service.write(author, name="tone", description="тон", body="х", course_id=course.id)
        db.commit()

    items = client.get("/admin/assistant/memory").json()["items"]
    assert [(row["name"], row["scope"]) for row in items] == [
        ("common", "global"),
        ("tone", "course"),
    ]
    # Тела на экране индекса нет — записи открываются по одной
    assert "body" not in items[0]
