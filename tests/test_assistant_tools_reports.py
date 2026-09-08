"""Показатели, отчёт по курсу и сертификаты глазами ассистента.

Два места, где ошибка не видна глазом и стоит дороже всего:

- **числа без площадки.** «12 заявок» — неверный ответ, если фильтра не было;
- **держатель сертификата.** У документа есть `holder_name` — снимок ФИО
  на бумаге — и ИИН владельца. Ни того, ни другого в ответе инструмента быть
  не должно ни при каких условиях.

Персональные данные в сцене вымышленные, ИИН — только через `fake_iin`.
"""

import json

from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import Certificate
from app.assistant import registry
from tests.conftest import (
    fake_iin,
    login_admin,
    make_certificate,
    make_certificate_request,
    make_course,
    make_enrollment,
    make_lead,
    make_lesson,
    make_module,
    make_progress,
    make_user,
)

# Номера вымышленные: настоящих в тестах не бывает (общий CLAUDE.md).
TEACHER_PHONE = "+77010000001"
OTHER_PHONE = "+77010000002"

# ФИО на бумаге — снимок, а не живое поле профиля: у сертификата оно своё,
# и искать в ответе надо именно его.
HOLDER = "Смагулова Гульмира Токтарбековна"




def dumped(result: dict) -> str:
    """Ответ инструмента ровно в том виде, в каком он уходит в OpenAI."""
    return json.dumps(result, ensure_ascii=False, default=str)


def scene(client, sms):
    admin_id = login_admin(client, sms).json()["id"]
    thread = client.post("/admin/assistant/threads", json={}).json()
    return admin_id, thread["id"]


# -- показатели ------------------------------------------------------------


def test_dashboard_counts_each_platform_separately(client, sms, tool_ctx):
    """«12 заявок» без площадки — брак: счётчики приходят и по каждой."""
    admin_id, _ = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    make_lead(teacher.id, course.id, platform="p1")
    make_lead(teacher.id, course.id, platform="p2")
    make_certificate_request(teacher.id, course.id, platform="p2")

    result = registry.call("dashboard", {}, tool_ctx(admin_id))

    assert result["counts"]["leads_new"] == 2
    assert result["by_platform"]["p1"]["leads_new"] == 1
    assert result["by_platform"]["p2"]["leads_new"] == 1
    assert result["by_platform"]["p2"]["certificate_requests"] == 1
    assert result["by_platform"]["p1"]["certificate_requests"] == 0


def test_dashboard_with_a_platform_still_shows_both(client, sms, tool_ctx):
    admin_id, _ = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    make_lead(teacher.id, course.id, platform="p1")
    make_lead(teacher.id, course.id, platform="p2")

    result = registry.call("dashboard", {"platform": "p1"}, tool_ctx(admin_id))

    assert result["platform"] == "p1"
    assert result["counts"]["leads_new"] == 1
    # Разбивка приходит и с фильтром: по ней видно, что осталось за кадром
    assert result["by_platform"]["p2"]["leads_new"] == 1
    assert [row["platform"] for row in result["leads"]] == ["p1"]


def test_dashboard_queues_have_no_names_and_no_phones(client, sms, tool_ctx):
    """Списки под плитками маскированы: кто ждёт — номером, сколько ждёт —
    днями."""
    admin_id, _ = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course(title="Критериальное оценивание")
    make_lead(teacher.id, course.id)

    result = registry.call("dashboard", {}, tool_ctx(admin_id))

    text = dumped(result)
    assert "Смагулова" not in text
    assert TEACHER_PHONE not in text and "7701" not in text
    assert result["leads"][0]["teacher"]["ref"] == f"Учитель #{teacher.id}"
    assert result["leads"][0]["waiting_days"] == 0


# -- отчёт по курсу --------------------------------------------------------


def test_course_report_has_a_funnel_and_platform_numbers(client, sms, tool_ctx):
    admin_id, _ = scene(client, sms)
    course = make_course(title="Оценивание", platforms={"p1": 45000, "p2": 60000})
    module = make_module(course.id)
    first = make_lesson(module.id, title="Критерии")
    make_lesson(module.id, title="Обратная связь")
    on_p1 = make_user(TEACHER_PHONE)
    on_p2 = make_user(OTHER_PHONE)
    make_enrollment(on_p1.id, course.id, platform="p1")
    make_enrollment(on_p2.id, course.id, platform="p2")
    make_progress(on_p1.id, first.id, platform="p1")

    result = registry.call("course_report", {"course_id": course.id}, tool_ctx(admin_id))

    assert result["summary"]["granted"] == 2
    assert result["summary"]["started"] == 1
    assert result["by_platform"] == {
        "p1": {"granted": 1, "certificates": 0},
        "p2": {"granted": 1, "certificates": 0},
    }
    assert [row["title"] for row in result["funnel"]] == ["Критерии", "Обратная связь"]
    assert [row["reached"] for row in result["funnel"]] == [1, 0]
    assert result["funnel_truncated"] is False


def test_course_participants_have_no_names_schools_or_regions(client, sms, tool_ctx):
    """Сценарий отчёта отдаёт ФИО, школу и регион — таблицу админ читает
    глазами. Ассистенту остаются номер, площадка и числа."""
    admin_id, _ = scene(client, sms)
    course = make_course()
    make_lesson(make_module(course.id).id)
    teacher = make_user(TEACHER_PHONE)
    make_enrollment(teacher.id, course.id)

    result = registry.call("course_participants", {"course_id": course.id}, tool_ctx(admin_id))

    text = dumped(result)
    assert "Смагулова" not in text
    assert "КГУ" not in text and "Алматы" not in text
    assert TEACHER_PHONE not in text and "7701" not in text
    assert result["total"] == 1
    assert result["items"][0]["ref"] == f"Учитель #{teacher.id}"
    # Числа и состояния на месте — вырезано только то, что описывает человека
    assert result["items"][0]["progress_percent"] == 0
    assert result["items"][0]["certificate"] in ("issued", "ready", "in_progress")


def test_one_person_on_two_platforms_is_two_rows(client, sms, tool_ctx):
    """Строка отчёта — доступ, а не человек: курс на обеих площадках
    изучается дважды."""
    admin_id, _ = scene(client, sms)
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    make_lesson(make_module(course.id).id)
    teacher = make_user(TEACHER_PHONE)
    make_enrollment(teacher.id, course.id, platform="p1")
    make_enrollment(teacher.id, course.id, platform="p2")

    result = registry.call("course_participants", {"course_id": course.id}, tool_ctx(admin_id))

    assert result["total"] == 2
    assert sorted(row["platform"] for row in result["items"]) == ["p1", "p2"]
    assert result["by_platform"] == {"p1": 1, "p2": 1}


# -- сертификаты -----------------------------------------------------------


def test_certificates_list_hides_the_holder_and_the_iin(client, sms, tool_ctx):
    """У сертификата есть снимок ФИО на бумаге и ИИН держателя. Ни того,
    ни другого в ответе инструмента быть не должно."""
    admin_id, _ = scene(client, sms)
    iin = fake_iin()
    teacher = make_user(TEACHER_PHONE, iin=iin)
    course = make_course()
    make_certificate(teacher.id, course.id, holder_name=HOLDER, registration_number="АК-14/2026")

    result = registry.call("certificates_list", {}, tool_ctx(admin_id))

    text = dumped(result)
    assert HOLDER not in text
    assert "Смагулова" not in text
    assert iin not in text
    assert TEACHER_PHONE not in text and "7701" not in text
    assert result["items"][0]["teacher"]["ref"] == f"Учитель #{teacher.id}"
    # Номера документа — не персданные: по ним админ его и находит
    assert result["items"][0]["registration_number"] == "АК-14/2026"


def test_certificate_card_hides_the_holder_and_the_iin(client, sms, tool_ctx):
    admin_id, _ = scene(client, sms)
    iin = fake_iin()
    teacher = make_user(TEACHER_PHONE, iin=iin)
    certificate = make_certificate(teacher.id, make_course().id, holder_name=HOLDER)

    result = registry.call(
        "certificate_card", {"certificate_id": certificate.id}, tool_ctx(admin_id)
    )

    text = dumped(result)
    assert HOLDER not in text and iin not in text
    assert result["certificate"]["status"] == "issued"


def test_certificates_without_platform_count_both(client, sms, tool_ctx):
    admin_id, _ = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    other = make_user(OTHER_PHONE)
    course = make_course(platforms={"p1": 45000, "p2": 60000})
    make_certificate_request(teacher.id, course.id, platform="p1")
    make_certificate_request(other.id, course.id, platform="p2")

    result = registry.call("certificates_list", {"status": "requested"}, tool_ctx(admin_id))

    assert result["total"] == 2
    assert result["by_platform"] == {"p1": 1, "p2": 1}


def test_issuing_and_revoking_need_confirmation():
    """Сертификат стоит на регистрационном номере академии, которого система
    не знает и проверить не может: выдача и отзыв идут через кнопку."""
    for name in (
        "certificate_issue",
        "certificate_revoke",
        "certificate_set_registration_number",
    ):
        assert registry.get(name).danger == "confirm", name


def test_registration_number_is_a_required_argument():
    """Номер называет администратор — придумать его модели нечем: без
    аргумента вызов не проходит вовсе."""
    schema = next(
        item
        for item in registry.openai_tools()
        if item["function"]["name"] == "certificate_issue"
    )["function"]
    assert "registration_number" in schema["parameters"]["required"]
    assert "ВЫДУМЫВАТЬ ЕГО НЕЛЬЗЯ" in schema["description"]


def test_certificate_issue_is_a_proposal_and_not_an_issue(client, sms, tool_ctx):
    admin_id, thread_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course(title="Оценивание", platforms={"p2": 60000})
    request = make_certificate_request(
        teacher.id, course.id, course_title="Оценивание", platform="p2", holder_name=HOLDER
    )

    called = tool_ctx(admin_id, thread_id)
    result = registry.call(
        "certificate_issue",
        {"certificate_id": request.id, "registration_number": "АК-14/2026"},
        called,
    )
    called.db.commit()

    what = result["what_happens"]
    assert "Оценивание" in what and "72 ч" in what
    # Предложение опасного действия называет площадку (ASSISTANT_BRIEF, 4)
    assert "p2" in what
    assert "АК-14/2026" in what
    assert f"Учитель #{teacher.id}" in what
    # Ни ФИО с бумаги, ни имени в карточке подтверждения нет
    assert HOLDER not in what and "Смагулова" not in what
    # Документ не выписан: реестр его не трогал
    assert called.db.get(Certificate, request.id).number is None

    resp = client.post(f"/admin/assistant/proposals/{result['proposal_id']}/execute")
    assert resp.status_code == 200, resp.text
    with OrmSession(get_engine()) as db:
        issued = db.get(Certificate, request.id)
        assert issued.number is not None
        assert issued.registration_number == "АК-14/2026"
        assert issued.issued_at is not None


def test_issued_certificate_comes_back_masked(client, sms, tool_ctx):
    admin_id, thread_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE, iin=fake_iin())
    request = make_certificate_request(
        teacher.id, make_course().id, holder_name=HOLDER
    )

    called = tool_ctx(admin_id, thread_id)
    proposal = registry.call(
        "certificate_issue",
        {"certificate_id": request.id, "registration_number": "АК-15/2026"},
        called,
    )
    called.db.commit()

    body = client.post(f"/admin/assistant/proposals/{proposal['proposal_id']}/execute").json()

    text = dumped(body["result"])
    assert HOLDER not in text and teacher.iin not in text
    assert body["result"]["certificate"]["status"] == "issued"


def test_issue_preview_warns_about_a_taken_registration_number(client, sms, tool_ctx):
    """Повтор номера академии — предупреждение, а не запрет: чужой нумерации
    мы не знаем. Видно оно до кнопки, а не после сохранения."""
    admin_id, thread_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    other = make_user(OTHER_PHONE)
    course = make_course()
    make_certificate(other.id, course.id, registration_number="АК-14/2026")
    request = make_certificate_request(teacher.id, course.id)

    called = tool_ctx(admin_id, thread_id)
    result = registry.call(
        "certificate_issue",
        {"certificate_id": request.id, "registration_number": "АК-14/2026"},
        called,
    )
    called.db.commit()

    assert "такой номер уже есть" in result["what_happens"].lower()


def test_issue_preview_says_when_it_will_not_pass(client, sms, tool_ctx):
    """Отказ виден до кнопки, а не после неё: выданный документ второй бумагой
    не выписывается."""
    admin_id, thread_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    issued = make_certificate(teacher.id, make_course().id)

    called = tool_ctx(admin_id, thread_id)
    result = registry.call(
        "certificate_issue",
        {"certificate_id": issued.id, "registration_number": "АК-16/2026"},
        called,
    )
    called.db.commit()

    assert "Не выдастся" in result["what_happens"]
    resp = client.post(f"/admin/assistant/proposals/{result['proposal_id']}/execute")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "certificate_issued"


def test_certificate_revoke_is_a_proposal_and_not_a_revoke(client, sms, tool_ctx):
    admin_id, thread_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    course = make_course(platforms={"p2": 60000})
    issued = make_certificate(
        teacher.id, course.id, course_title="Оценивание", platform="p2", holder_name=HOLDER
    )

    called = tool_ctx(admin_id, thread_id)
    result = registry.call("certificate_revoke", {"certificate_id": issued.id}, called)
    called.db.commit()

    what = result["what_happens"]
    assert "Оценивание" in what and "p2" in what and HOLDER not in what
    assert called.db.get(Certificate, issued.id).revoked_at is None

    client.post(f"/admin/assistant/proposals/{result['proposal_id']}/execute")
    with OrmSession(get_engine()) as db:
        assert db.get(Certificate, issued.id).revoked_at is not None


def test_registration_number_can_be_filled_in_later(client, sms, tool_ctx):
    """У документов, выданных до 04.09.2026, номер академии пустой — админ
    проставляет его руками."""
    admin_id, thread_id = scene(client, sms)
    teacher = make_user(TEACHER_PHONE)
    old = make_certificate(teacher.id, make_course().id, registration_number="")

    called = tool_ctx(admin_id, thread_id)
    result = registry.call(
        "certificate_set_registration_number",
        {"certificate_id": old.id, "registration_number": "АК-01/2025"},
        called,
    )
    called.db.commit()

    assert "Сейчас номера у него нет" in result["what_happens"]
    assert called.db.get(Certificate, old.id).registration_number == ""

    client.post(f"/admin/assistant/proposals/{result['proposal_id']}/execute")
    with OrmSession(get_engine()) as db:
        assert db.get(Certificate, old.id).registration_number == "АК-01/2025"
