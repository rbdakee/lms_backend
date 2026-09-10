"""Чат: ход, лента, промпт, допуски, деньги.

Живого хода в OpenAI здесь нет — вместо провайдера подставной `FakeAi`
(`tests/conftest.py`). Зато видно то, чего не видно снаружи: что ушло
в системном промпте и что легло в базу.
"""

import json

from app.adapters.db.repos import now_utc
from app.application.assistant_chat import AssistantChatService
from app.assistant.loop import Step
from app.assistant.prompt import today
from app.config import get_settings
from tests.conftest import login, login_admin, make_course, make_user


def new_thread(client, **body) -> dict:
    resp = client.post("/admin/assistant/threads", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def read(resp) -> list[tuple[str, dict]]:
    """Ответ хода, разобранный на события `text/event-stream`.

    Отказы, которые остались обычными (`429`, `422`, `404`), сюда не приходят:
    они случаются до начала стрима, и тесты на них зовут `client.post` сами.
    """
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    out: list[tuple[str, dict]] = []
    for block in resp.text.strip().split("\n\n"):
        if not block.strip():
            continue
        name = block.split("event: ", 1)[1].split("\n", 1)[0]
        out.append((name, json.loads(block.split("data: ", 1)[1])))
    return out


def turn_of(resp) -> dict:
    """То, что пришло событием `done`: лента целиком и расход хода."""
    done = [data for name, data in read(resp) if name == "done"]
    assert len(done) == 1, "ход обязан закончиться ровно одним `done`"
    return done[0]


def events(client, thread_id: int, text: str = "привет") -> list[tuple[str, dict]]:
    return read(
        client.post(f"/admin/assistant/threads/{thread_id}/messages", json={"text": text})
    )


def turn(client, thread_id: int, text: str = "привет") -> dict:
    return turn_of(
        client.post(f"/admin/assistant/threads/{thread_id}/messages", json={"text": text})
    )


def turn_calls(ai) -> list[dict]:
    """Запросы **хода**, без запроса за заголовком чата.

    Заголовок спрашивается отдельно и дёшево — без списка инструментов, —
    поэтому ходы от него отличаются наличием `tools`. Без этого разделения
    `ai.calls[-1]` показывал бы промпт заголовка, а не тот, с которым
    работал ассистент.
    """
    return [call for call in ai.calls if call["tools"]]


def turn_system(ai, index: int = -1) -> str:
    return turn_calls(ai)[index]["system"]


def test_only_admin_gets_the_assistant(client, sms):
    """У учителя чата нет и не появляется (ASSISTANT_BRIEF, решение 10)."""
    login(client, sms)
    assert client.get("/admin/assistant/threads").status_code == 403


def test_empty_key_means_no_assistant_but_a_live_api(client, sms, monkeypatch):
    """Пустой ключ значит «страницы нет», а не «сервис не поднимается»:
    остальное API работает как работало."""
    login_admin(client, sms)
    monkeypatch.setattr(get_settings(), "openai_api_key", "")
    resp = client.get("/admin/assistant/threads")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "assistant_off"
    # А обычная админская ручка на месте
    assert client.get("/admin/overview").status_code == 200


def test_settings_say_whether_the_assistant_exists(client, sms, monkeypatch):
    login_admin(client, sms)
    assert client.get("/admin/settings").json()["assistant"] == {"enabled": True}
    monkeypatch.setattr(get_settings(), "assistant_enabled", False)
    assert client.get("/admin/settings").json()["assistant"] == {"enabled": False}


def test_threads_are_personal(client, client2, sms):
    """Чужие чаты не видны — на сервере, а не скрытием в интерфейсе."""
    login_admin(client, sms)
    mine = new_thread(client)
    login_admin(client2, sms, phone="+7 (700) 000-00-88")
    assert client2.get("/admin/assistant/threads").json()["items"] == []
    assert client2.get(f"/admin/assistant/threads/{mine['id']}/messages").status_code == 404
    assert client2.delete(f"/admin/assistant/threads/{mine['id']}").status_code == 404


def test_turn_writes_question_and_answer(client, sms, ai):
    login_admin(client, sms)
    thread = new_thread(client)
    ai.says("На обеих площадках 12 заявок: 9 на p1, 3 на p2.", tokens_in=800, tokens_out=40)

    body = turn(client, thread["id"], "сколько заявок?")
    items = body["items"]
    assert [row["role"] for row in items] == ["user", "assistant"]
    assert items[1]["content"].startswith("На обеих площадках")
    # Токены пишутся, чтобы счёт за месяц можно было объяснить
    assert (items[1]["tokens_in"], items[1]["tokens_out"]) == (800, 40)
    # И тем же числом уходят в `done` — экран показывает цену хода
    assert (body["tokens_in"], body["tokens_out"]) == (800, 40)
    assert body["stopped_at_cap"] is False


def test_tool_call_lands_in_the_feed_as_a_card(client, sms, ai):
    login_admin(client, sms)
    thread = new_thread(client)
    make_course(title="Критериальное оценивание")
    ai.wants("courses_list", status="open").says("Один курс, оба на p1.")

    items = turn(client, thread["id"], "какие курсы?")["items"]
    assert [row["role"] for row in items] == ["user", "tool", "assistant"]
    card = items[1]
    assert card["tool_name"] == "courses_list"
    assert card["tool_args"] == {"status": "open"}
    assert card["tool_result"]["total"] == 1
    # Числа по площадкам считаются всегда: «1 курс» без площадки — брак
    assert card["tool_result"]["by_platform"] == {"p1": 1, "p2": 0}


def test_model_sees_the_tool_list(client, sms, ai):
    login_admin(client, sms)
    thread = new_thread(client)
    client.post(f"/admin/assistant/threads/{thread['id']}/messages", json={"text": "привет"})
    names = [item["function"]["name"] for item in turn_calls(ai)[-1]["tools"]]
    assert "courses_list" in names
    assert "course_delete" in names


def test_system_prompt_has_the_date_and_both_platforms(client, sms, ai):
    """Без сегодняшней даты модель считает «ждёт 4 дня» от даты обучения.
    Про две площадки она забывает первым делом — поэтому это первый абзац."""
    login_admin(client, sms)
    thread = new_thread(client)
    client.post(f"/admin/assistant/threads/{thread['id']}/messages", json={"text": "привет"})

    system = turn_system(ai)
    assert today(now_utc()) in system
    assert system.index("Две учебные площадки") < system.index("Что это за система")
    assert "Допусков к персональным данным нет" in system
    assert "Курс у чата не закреплён" in system
    # Шестая часть: что из инструментов опасно, видно в промпте, а не только
    # в описаниях
    assert "course_delete" in system.split("## Инструменты")[1]


def test_prompt_names_the_pinned_course_and_the_grants(client, sms, ai):
    login_admin(client, sms)
    course = make_course(title="Оценивание")
    teacher = make_user("+77010000002")
    thread = new_thread(client, course_id=course.id)
    client.post(
        f"/admin/assistant/threads/{thread['id']}/grants",
        json={"user_id": teacher.id, "level": "read"},
    )
    client.post(f"/admin/assistant/threads/{thread['id']}/messages", json={"text": "привет"})

    system = turn_system(ai)
    assert f"#{course.id} «Оценивание»" in system
    assert f"учитель #{teacher.id} — чтение" in system


def test_prompt_carries_the_memory_index(client, sms, ai):
    login_admin(client, sms)
    thread = new_thread(client)
    from sqlalchemy.orm import Session as OrmSession

    from app.adapters.db.base import get_engine
    from app.adapters.db.models import AssistantMemory

    admin_id = client.get("/me").json()["id"]
    with OrmSession(get_engine()) as db:
        db.add(
            AssistantMemory(
                scope="global",
                name="pricing",
                description="как считаем цену курса на каждой площадке",
                body="тело",
                author_id=admin_id,
            )
        )
        db.commit()

    client.post(f"/admin/assistant/threads/{thread['id']}/messages", json={"text": "привет"})
    system = turn_system(ai)
    assert "`pricing` — как считаем цену" in system
    # Тело в промпт не уходит: индекс всегда, содержимое по нужде
    assert "тело" not in system.split("## Память")[1].split("## Состояние")[0]


def test_history_comes_back_to_the_model_next_turn(client, sms, ai):
    """Хвост истории отдаётся модели как есть, вместе с вызовом и его
    результатом: иначе следующим ходом ассистент не помнит, что смотрел."""
    login_admin(client, sms)
    thread = new_thread(client)
    make_course()
    ai.wants("courses_list").says("Один курс.").says("Он же.")

    client.post(f"/admin/assistant/threads/{thread['id']}/messages", json={"text": "курсы?"})
    client.post(f"/admin/assistant/threads/{thread['id']}/messages", json={"text": "а теперь?"})

    roles = [message.role for message in ai.calls[-1]["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant", "user"]
    asked = ai.calls[-1]["messages"][1]
    answered = ai.calls[-1]["messages"][2]
    assert asked.tool_calls[0].name == "courses_list"
    assert answered.tool_call_id == asked.tool_calls[0].id


def test_dangerous_tool_in_a_turn_only_proposes(client, sms, ai):
    """Через цикл вызовов опасное действие тоже не исполняется."""
    login_admin(client, sms)
    thread = new_thread(client)
    course = make_course(title="Черновик")
    ai.wants("course_delete", course_id=course.id).says("Готов удалить — подтвердите.")

    card = turn(client, thread["id"], "удали черновик")["items"][1]
    assert card["tool_result"]["status"] == "waiting_confirmation"
    assert card["proposal"]["status"] == "waiting"
    assert "Черновик" in card["proposal"]["what_happens"]
    # Курс на месте
    assert client.get(f"/admin/courses/{course.id}").status_code == 200


def test_proposal_card_becomes_a_result(client, sms, ai):
    login_admin(client, sms)
    thread = new_thread(client)
    course = make_course()
    ai.wants("course_delete", course_id=course.id).says("Подтвердите.")
    proposal_id = turn(client, thread["id"], "удали")["items"][1]["proposal"]["id"]

    assert client.post(f"/admin/assistant/proposals/{proposal_id}/execute").status_code == 200
    feed = client.get(f"/admin/assistant/threads/{thread['id']}/messages").json()["items"]
    assert feed[1]["proposal"]["status"] == "executed"


def test_proposal_can_be_cancelled(client, sms, ai):
    login_admin(client, sms)
    thread = new_thread(client)
    course = make_course()
    ai.wants("course_delete", course_id=course.id).says("Подтвердите.")
    proposal_id = turn(client, thread["id"], "удали")["items"][1]["proposal"]["id"]

    assert client.post(f"/admin/assistant/proposals/{proposal_id}/cancel").status_code == 200
    again = client.post(f"/admin/assistant/proposals/{proposal_id}/execute")
    assert again.json()["error"]["code"] == "cancelled"


def test_tool_cap_stops_the_turn_honestly(client, sms, ai, monkeypatch):
    """Цикл без потолка на длинной задаче стоит реальных денег. Упёрся —
    останавливается и говорит, где встал."""
    monkeypatch.setattr(get_settings(), "assistant_max_tool_calls", 2)
    login_admin(client, sms)
    thread = new_thread(client)
    make_course()
    for number in range(3):
        ai.wants("courses_list", call_id=f"call_{number}")
    ai.says("Успел посмотреть курсы, но не уложился в один ход.")

    body = turn(client, thread["id"], "разберись")
    assert body["stopped_at_cap"] is True
    assert sum(1 for row in body["items"] if row["role"] == "tool") == 2
    assert body["items"][-1]["content"].startswith("Успел посмотреть")
    # Последний запрос — без инструментов: договорить, а не звать ещё раз
    assert ai.calls[-1]["tools"] is None


def test_failed_tool_does_not_break_the_turn(client, sms, ai):
    """Курса нет — модель читает отказ и говорит об этом, а не падает 500."""
    login_admin(client, sms)
    thread = new_thread(client)
    ai.wants("course_card", course_id=999).says("Курса 999 нет.")
    items = turn(client, thread["id"], "курс 999")["items"]
    assert items[1]["tool_result"]["error"]["code"] == "not_found"


def test_messages_are_limited_per_admin(client, sms, ai, monkeypatch):
    monkeypatch.setattr(get_settings(), "assistant_messages_per_hour", 60)
    login_admin(client, sms)
    thread = new_thread(client)
    url = f"/admin/assistant/threads/{thread['id']}/messages"
    for _ in range(get_settings().assistant_messages_per_hour):
        assert client.post(url, json={"text": "ещё"}).status_code == 200
    resp = client.post(url, json={"text": "ещё"})
    assert resp.status_code == 429
    assert resp.json()["error"]["details"]["retry_after_sec"] > 0


def test_provider_failure_leaves_no_half_turn(client, sms, ai):
    """Провайдер не ответил — хода не было: в ленте не остаётся ни оборванного
    вопроса, ни половины вызовов."""
    login_admin(client, sms)
    thread = new_thread(client)

    class Broken:
        def complete(self, **kw):
            from app.adapters.ai.openai_chat import AiUnavailableError

            raise AiUnavailableError("нет связи")

    from app.api.deps import get_ai
    from app.main import app

    app.dependency_overrides[get_ai] = Broken
    try:
        got = events(client, thread["id"])
    finally:
        app.dependency_overrides.pop(get_ai, None)
    # Статус отдан до первого события, поэтому отказ приезжает событием,
    # а не кодом 502 — и `done` в ходе не бывает вовсе
    assert [name for name, _ in got] == ["error"]
    assert got[0][1]["error"]["code"] == "ai_failed"
    assert client.get(f"/admin/assistant/threads/{thread['id']}/messages").json()["items"] == []


# -- допуски -------------------------------------------------------------


def test_grant_and_revoke(client, sms):
    login_admin(client, sms)
    teacher = make_user("+77010000003")
    thread = new_thread(client)
    url = f"/admin/assistant/threads/{thread['id']}/grants"

    resp = client.post(url, json={"user_id": teacher.id, "level": "read"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["subject_user_id"] == teacher.id
    assert resp.json()["level"] == "read"
    assert len(client.get(url).json()["items"]) == 1

    assert client.delete(f"{url}/{teacher.id}").status_code == 204
    assert client.get(url).json()["items"] == []


def test_grant_does_not_leak_the_name(client, sms):
    """Имени в допуске нет и не будет: чип в шапке подписывает браузер."""
    login_admin(client, sms)
    teacher = make_user("+77010000004", last_name="Сериккызы")
    thread = new_thread(client)
    body = client.post(
        f"/admin/assistant/threads/{thread['id']}/grants",
        json={"user_id": teacher.id, "level": "read"},
    ).text
    assert "Сериккызы" not in body


def test_grant_is_not_inherited_by_another_chat(client, sms):
    """Допуск привязан к паре «чат + человек»."""
    login_admin(client, sms)
    teacher = make_user("+77010000005")
    first = new_thread(client)
    second = new_thread(client)
    client.post(
        f"/admin/assistant/threads/{first['id']}/grants",
        json={"user_id": teacher.id, "level": "read"},
    )
    assert client.get(f"/admin/assistant/threads/{second['id']}/grants").json()["items"] == []


# -- персданные в ленте ---------------------------------------------------


def test_pd_result_is_replaced_by_a_note(monkeypatch):
    """Инструмент, помеченный `pd`, отдаёт модели настоящую карточку, а в базу
    вместо неё ложится пометка: назавтра админ увидит строку, а не данные."""
    from app.assistant import registry

    class FakeSpec:
        pd = True

    monkeypatch.setattr(registry, "all_tools", lambda: {"teacher_card": FakeSpec()})
    step = Step("call_1", "teacher_card", {"user_id": 418}, {"last_name": "Сериккызы"})
    stored = AssistantChatService._stored_result(step, now_utc())
    assert stored["hidden"] == "pd_not_stored"
    assert "учитель #418" in stored["note"]
    assert "Сериккызы" not in str(stored)


def test_pd_refusal_is_stored_as_is(monkeypatch):
    """Отказ — не персданные: его модель должна увидеть и следующим ходом."""
    from app.assistant import registry

    class FakeSpec:
        pd = True

    monkeypatch.setattr(registry, "all_tools", lambda: {"teacher_card": FakeSpec()})
    step = Step("call_1", "teacher_card", {"user_id": 418}, {"error": {"code": "x"}})
    assert AssistantChatService._stored_result(step, now_utc()) == step.result


def test_new_personal_data_does_not_stay_in_the_chat(client, sms, ai):
    """Правка чужих данных приходит новым ФИО и телефоном. Инструмент их
    применяет, но карточка вызова живёт в ленте вечно — значений в ней
    не остаётся, только имена полей (ASSISTANT_BRIEF, раздел 5)."""
    login_admin(client, sms)
    teacher = make_user("+77010000006")
    thread = new_thread(client)
    client.post(
        f"/admin/assistant/threads/{thread['id']}/grants",
        json={"user_id": teacher.id, "level": "write"},
    )
    ai.wants(
        "teacher_update", user_id=teacher.id, school="КГУ «Школа №5»", phone="+77010000077"
    ).says("Готов исправить — подтвердите.")

    resp = client.post(
        f"/admin/assistant/threads/{thread['id']}/messages",
        json={"text": "исправь школу и телефон"},
    )
    card = next(row for row in turn_of(resp)["items"] if row["role"] == "tool")
    assert card["tool_args"]["school"] == "скрыто"
    assert card["tool_args"]["phone"] == "скрыто"
    # Идентификатор человека — не персональные данные, он остаётся
    assert card["tool_args"]["user_id"] == teacher.id
    assert "77010000077" not in resp.text

    # Строка предложения значения держит — без них подтверждать нечего, —
    # но сразу после выполнения они стираются и оттуда
    proposal_id = card["proposal"]["id"]
    assert client.post(f"/admin/assistant/proposals/{proposal_id}/execute").status_code == 200

    from sqlalchemy.orm import Session as OrmSession

    from app.adapters.db.base import get_engine
    from app.adapters.db.models import AssistantProposal, User

    with OrmSession(get_engine()) as db:
        row = db.get(AssistantProposal, proposal_id)
        assert row.tool_args["school"] == "скрыто"
        assert row.tool_args["phone"] == "скрыто"
        # А в самой карточке человека правка на месте: скрыта история, не работа
        assert db.get(User, teacher.id).school == "КГУ «Школа №5»"


def test_dates_from_tools_do_not_break_the_turn(client, sms, ai):
    """Сценарии отдают `date` у даты старта курса. Модели это уходит строкой,
    а вот запись в JSONB падала бы — и роняла весь ход, а не один вызов."""
    from datetime import UTC, datetime

    login_admin(client, sms)
    course = make_course(status="planned", starts_at=datetime(2026, 10, 1, tzinfo=UTC))
    ai.wants("course_card", course_id=course.id).says("Курс стартует 1 октября.")

    resp = client.post(
        f"/admin/assistant/threads/{new_thread(client)['id']}/messages",
        json={"text": "когда старт?"},
    )
    card = next(row for row in turn_of(resp)["items"] if row["role"] == "tool")
    assert card["tool_result"]["starts_at"].startswith("2026-10-01")
