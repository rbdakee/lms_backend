"""Учителя, заявки, доступы и работы: место, где ошибка стоит персданных.

Две главные проверки сессии живут здесь, и обе — про то, чего не видно
глазом в ленте чата:

- **ни один список не показывает ни фамилии, ни телефона** — проверяется
  поиском по тексту ответа на каждом заведённом списке, а не чтением кода;
- **`teacher_card` без допуска отвечает отказом**, а с допуском на чтение
  не открывает правку.

Персданные в тестах вымышленные, ИИН — только через `fake_iin()`.
"""

import json

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import Enrollment, Lead, User
from app.assistant import registry
from app.assistant.registry import ToolError
from app.assistant.tools import people
from tests.conftest import (
    execute,
    fake_iin,
    login_admin,
    make_course,
    make_enrollment,
    make_lead,
    make_module,
    make_quiz,
    make_submission,
    make_task,
    make_user,
    plan,
)

# Вымышленные номер и фамилия: их ищут в тексте ответа инструментов, и найти
# их там нельзя ни в одном списке.
PHONE = "+77010000042"
SURNAME = "Сериккызы"
SCHOOL = "КГУ «Средняя школа №5»"
REGION = "Алматы"


def a_teacher(phone: str = PHONE) -> User:
    return make_user(
        phone,
        last_name=SURNAME,
        first_name="Айгерим",
        middle_name="Бахытовна",
        school=SCHOOL,
        region=REGION,
        iin=fake_iin(),
    )


def as_text(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


# -- допуск к персональным данным ----------------------------------------


def test_teacher_card_without_a_grant_refuses(client, sms, tool_ctx):
    """Главная проверка сессии: карточка человека без допуска не открывается,
    и отказ говорит по существу — «нужен допуск», а не «мне не разрешено»."""
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()

    with pytest.raises(ToolError) as exc:
        registry.call("teacher_card", {"user_id": person.id}, tool_ctx(admin))

    assert exc.value.code == "pd_grant_required"
    assert exc.value.details["subject_user_id"] == person.id
    # В отказе персданных нет: он уходит модели и остаётся в ленте
    assert SURNAME not in exc.value.message
    assert PHONE not in exc.value.message


def test_read_grant_opens_the_card_and_iin_is_the_last_field(client, sms, tool_ctx):
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()

    card = registry.call(
        "teacher_card", {"user_id": person.id}, tool_ctx(admin, grants={person.id: "read"})
    )

    assert card["last_name"] == SURNAME
    assert card["phone"] == PHONE
    assert card["school"] == SCHOOL
    # ИИН чувствительнее всего остального и потому идёт последним
    assert list(card)[-1] == "iin"
    assert card["iin"] == person.iin


def test_read_grant_does_not_open_the_edit(client, sms, tool_ctx):
    """Допуск открывает дверь, но правка чужих данных требует своего уровня."""
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()

    with pytest.raises(ToolError) as exc:
        registry.call(
            "teacher_update",
            {"user_id": person.id, "school": "Другая школа"},
            tool_ctx(admin, grants={person.id: "read"}),
        )

    assert exc.value.code == "pd_grant_level"


def test_only_one_tool_here_shows_a_person(client, sms):
    """Массовой выгрузки персданных нет вовсе: допуск бывает на одного
    человека, и инструмент с персданными в этом файле ровно один."""
    mine = {
        name: spec
        for name, spec in registry.all_tools().items()
        if spec.func.__module__ == people.__name__
    }
    assert [name for name, spec in mine.items() if spec.pd] == ["teacher_card"]
    assert registry.get("teacher_card").args["user_id"].required


# -- маскирование --------------------------------------------------------


def test_no_list_shows_a_name_or_a_phone(client, sms, tool_ctx):
    """Списки маскированы всегда, без исключений и без «тут же только регион».

    Проверяется на каждом списке, который заводит этот файл: фамилия, номер
    и ИИН ищутся в тексте ответа и не находятся.
    """
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course(title="Критериальное оценивание")
    module = make_module(course.id)
    make_quiz(module.id)
    task = make_task(module.id)
    submission = make_submission(person.id, task.id)
    make_enrollment(person.id, course.id)
    make_lead(person.id, course.id)
    ctx = tool_ctx(admin)

    calls = {
        "teachers_list": {},
        "teachers_find": {"query": SURNAME[:4]},
        "leads_list": {},
        "submissions_list": {},
        "teacher_courses": {"user_id": person.id},
        "teacher_quizzes": {"user_id": person.id},
        "submission_card": {"submission_id": submission.id},
    }
    for name, args in calls.items():
        text = as_text(registry.call(name, args, ctx))
        assert SURNAME not in text, name
        assert PHONE not in text, name
        assert person.iin not in text, name
        # Человек в ответе есть — иначе проверка ничего не значит
        assert f"Учитель #{person.id}" in text, name

    # И списки не пустые: маскировали не пустоту
    assert registry.call("teachers_list", {}, ctx)["total"] == 1
    assert registry.call("leads_list", {}, ctx)["total"] == 1
    assert registry.call("submissions_list", {}, ctx)["total"] == 1
    assert registry.call("teacher_courses", {"user_id": person.id}, ctx)["total"] == 1


def test_teachers_find_gives_ids_and_courses_but_no_names(client, sms, tool_ctx):
    """Админ пишет «посмотри Серикову» — ассистенту возвращаются user_id,
    регион и курсы, а имена в карточку выбора подставит браузер."""
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course(title="Оценивание")
    make_enrollment(person.id, course.id)

    found = registry.call("teachers_find", {"query": "Серик"}, tool_ctx(admin))

    assert found["total"] == 1
    row = found["items"][0]
    assert row["user_id"] == person.id
    assert row["ref"] == f"Учитель #{person.id}"
    assert row["region"] == REGION
    assert row["courses"] == [{"course_id": course.id, "title": "Оценивание", "platform": "p1"}]
    assert SURNAME not in as_text(found)


# -- опасные действия ----------------------------------------------------


def test_actions_on_a_person_are_confirmed():
    """Выдача доступа, отзыв, пересдача, вердикт и правка данных — кнопка
    админа, а не решение модели."""
    for name in (
        "enrollment_grant",
        "enrollment_revoke",
        "quiz_retake_allow",
        "submission_review",
        "teacher_update",
    ):
        assert registry.get(name).danger == "confirm", name


def test_access_is_not_granted_without_a_platform(client, sms, tool_ctx):
    """Доступ, выданный на одной площадке, курса на другой не открывает —
    поэтому площадка обязательна, и угадывать её нельзя."""
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course()

    with pytest.raises(ToolError) as exc:
        registry.call(
            "enrollment_grant",
            {"user_id": person.id, "course_id": course.id, "paid": True},
            tool_ctx(admin, grants={person.id: "write"}),
        )

    assert exc.value.code == "missing_argument"


def test_grant_preview_names_the_course_and_the_platform(client, sms, tool_ctx):
    """Предложение опасного действия называет площадку: «откроет доступ
    к курсу „Оценивание“ на p2», а не «откроет доступ»."""
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course(title="Оценивание", platforms={"p1": 45000, "p2": 60000})
    ctx = tool_ctx(admin, grants={person.id: "write"})

    result = plan(
        "enrollment_grant",
        {"user_id": person.id, "course_id": course.id, "platform": "p2", "paid": True},
        ctx,
    )
    ctx.db.commit()

    what = result["what_happens"]
    assert "Оценивание" in what
    assert "p2" in what
    assert "60000" in what
    assert "Оплата получена" in what
    # Имени человека в тексте предложения нет: он лежит в базе и в ленте
    assert SURNAME not in what
    # Доступа пока нет: инструмент не исполнился, а предложил
    with OrmSession(get_engine()) as db:
        assert db.query(Enrollment).count() == 0


def test_access_needs_a_write_grant(client, sms, tool_ctx):
    """Выдача доступа этому человеку открывается допуском с правкой
    (ASSISTANT_BRIEF, раздел 5): на чтении она не проходит."""
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course()

    with pytest.raises(ToolError) as exc:
        registry.call(
            "enrollment_grant",
            {"user_id": person.id, "course_id": course.id, "platform": "p1", "paid": True},
            tool_ctx(admin, grants={person.id: "read"}),
        )

    assert exc.value.code == "pd_grant_level"


def test_grant_opens_the_course_and_closes_the_lead(client, sms, tool_ctx):
    """Выдача доводится до конца: доступ появился на своей
    площадке, а открытая заявка закрылась сама."""
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    lead = make_lead(person.id, course.id, platform="p2")

    ctx = tool_ctx(admin, grants={person.id: "write"})
    granted = registry.call(
        "enrollment_grant",
        {
            "user_id": person.id,
            "course_id": course.id,
            "platform": "p2",
            "paid": True,
            "note": "оплата 08.09",
        },
        ctx,
    )
    ctx.db.commit()

    assert granted["platform"] == "p2"
    assert granted["paid"] is True
    with OrmSession(get_engine()) as db:
        enrollment = db.query(Enrollment).one()
        assert (enrollment.user_id, enrollment.platform) == (person.id, "p2")
        assert db.get(Lead, lead.id).status == "granted"


def test_revoke_preview_says_what_stays(client, sms, tool_ctx):
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course(title="Оценивание")
    enrollment = make_enrollment(person.id, course.id)
    ctx = tool_ctx(admin)

    result = plan("enrollment_revoke", {"enrollment_id": enrollment.id}, ctx)
    ctx.db.commit()

    what = result["what_happens"]
    assert "Оценивание" in what
    assert "p1" in what
    assert "сертификаты остаются" in what
    assert SURNAME not in what


def test_retake_preview_names_the_quiz_and_the_platform(client, sms, tool_ctx):
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course(title="Оценивание")
    module = make_module(course.id)
    quiz = make_quiz(module.id, title="Итоговый тест")
    ctx = tool_ctx(admin)

    result = plan(
        "quiz_retake_allow",
        {
            "user_id": person.id,
            "quiz_id": quiz.id,
            "platform": "p1",
            "reason": "сбой видео на защите",
        },
        ctx,
    )
    ctx.db.commit()

    what = result["what_happens"]
    assert "Итоговый тест" in what
    assert "Оценивание" in what
    assert "p1" in what
    assert "сбой видео" in what


def test_review_preview_names_the_verdict_and_the_platform(client, sms, tool_ctx):
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course(title="Оценивание")
    module = make_module(course.id)
    task = make_task(module.id, title="Разбор урока")
    submission = make_submission(person.id, task.id, platform="p2")
    ctx = tool_ctx(admin)

    result = plan(
        "submission_review",
        {"submission_id": submission.id, "verdict": "rework", "comment": "добавьте критерии"},
        ctx,
    )
    ctx.db.commit()

    what = result["what_happens"]
    assert "доработку" in what
    assert "Разбор урока" in what
    assert "p2" in what
    assert SURNAME not in what


# -- правка данных человека ----------------------------------------------


def test_edit_needs_a_write_grant_and_keeps_values_out_of_the_preview(client, sms, tool_ctx):
    """Правка данных требует допуска с правкой.

    В описании последствий — какие поля меняются, а не на что: оно может
    попасть в журнал, и новым ФИО там копиться незачем.
    """
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()

    ctx = tool_ctx(admin, grants={person.id: "write"})
    result = plan(
        "teacher_update",
        {"user_id": person.id, "school": "КГУ «Гимназия №1»", "subject": "Химия"},
        ctx,
    )
    ctx.db.commit()

    what = result["what_happens"]
    assert "школа" in what and "предмет" in what
    assert "Гимназия" not in what
    assert "Химия" not in what

    executed = execute(result)

    # В ответе имена полей, а не значения: результат сохраняется как есть
    assert executed == {"user_id": person.id, "changed": ["school", "subject"]}
    with OrmSession(get_engine()) as db:
        changed = db.get(User, person.id)
        assert (changed.school, changed.subject) == ("КГУ «Гимназия №1»", "Химия")


def test_edit_without_fields_is_refused(client, sms, tool_ctx):
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()

    with pytest.raises(ToolError) as exc:
        registry.call(
            "teacher_update",
            {"user_id": person.id},
            tool_ctx(admin, grants={person.id: "write"}),
        )

    assert exc.value.code == "no_fields"


# -- заявки и работы -----------------------------------------------------


def test_leads_are_counted_on_both_platforms(client, sms, tool_ctx):
    """«12 заявок» без площадки — брак: числа видно по каждой."""
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    other = a_teacher("+77010000043")
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    make_lead(person.id, course.id, platform="p1")
    make_lead(other.id, course.id, platform="p2")
    ctx = tool_ctx(admin)

    both = registry.call("leads_list", {}, ctx)
    only_second = registry.call("leads_list", {"platform": "p2"}, ctx)

    assert both["total"] == 2
    assert both["by_platform"] == {"p1": 1, "p2": 1}
    assert only_second["total"] == 1
    assert only_second["by_platform"] == {"p1": 0, "p2": 1}
    assert {row["waiting_days"] for row in both["items"]} == {0}


def test_lead_note_is_written_but_not_read_back(client, sms, tool_ctx):
    """Заметку по заявке админ пишет свободно, и чужие имена в ней заводятся
    сами: в ответе виден только признак, что заметка есть."""
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course()
    lead = make_lead(person.id, course.id)
    ctx = tool_ctx(admin)

    result = registry.call(
        "lead_update",
        {"lead_id": lead.id, "status": "contacted", "note": "перезвонить после 18:00"},
        ctx,
    )
    ctx.db.commit()

    assert registry.get("lead_update").danger == "write"
    assert result["status"] == "contacted"
    assert result["has_note"] is True
    assert "перезвонить" not in as_text(result)


def test_submissions_queue_shows_days_and_platforms(client, sms, tool_ctx):
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    module = make_module(course.id)
    task = make_task(module.id)
    make_submission(person.id, task.id, platform="p1")
    make_submission(person.id, task.id, platform="p2")
    ctx = tool_ctx(admin)

    queue = registry.call("submissions_list", {"status": "pending"}, ctx)

    assert queue["total"] == 2
    assert queue["by_platform"] == {"p1": 1, "p2": 1}
    assert all(row["waiting_days"] == 0 for row in queue["items"])
    assert all(row["teacher"]["user_id"] == person.id for row in queue["items"])


def test_queue_is_plain_json(client, sms, tool_ctx):
    """Ответ списка сериализуется без подсказок: `datetime` в нём не остаётся,
    даты уходят строкой, иначе падал бы вызов у любого вызывающего."""
    admin = login_admin(client, sms).json()["id"]
    person = a_teacher()
    course = make_course()
    make_lead(person.id, course.id)

    result = registry.call("leads_list", {}, tool_ctx(admin))

    text = json.dumps(result, ensure_ascii=False)
    assert result["total"] == 1
    assert result["by_platform"] == {"p1": 1, "p2": 0}
    assert result["items"][0]["teacher"]["ref"] == f"Учитель #{person.id}"
    assert SURNAME not in text
