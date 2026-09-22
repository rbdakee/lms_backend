"""«Жоспар»: суточный лимит комплектов на учителя (сессия L7).

Правила — TEACHER_AI_BRIEF, раздел 7, и `.handoff/L7.decisions.md`, запись 5:

- лимит считается в комплектах, а не в материалах: учитель думает уроками;
- окно скользящее — 24 часа от заказа, а не календарные сутки;
- прямой заказ и «Готовить» в чате идут через один сценарий и один счётчик;
- счёт стоит после всех проверок заказа: отбитый заказ комплект не съедает;
- лимит реплик чата и лимит комплектов — два разных счётчика.

Сутки в тесте не переждать: лимитер получает подставные часы фикстурой
`lesson_kits_limiter_clock`, и окно сдвигается ими по-настоящему. Число
комплектов берётся из настройки, а не выписывается: владелец меняет его
строкой в `.env`, и тест обязан проверять то, что стоит в бою.
"""

import pytest

from app.adapters.db.models import LessonAiChatMessage, LessonAiJob, LessonKit
from app.api import deps
from app.api.deps import get_lesson_chat_limiter
from app.config import Settings, get_settings
from app.main import app
from tests.conftest import login
from tests.test_lesson_ai_chat import (
    OTHER_PHONE,
    Scene,
    confirm,
    count,
    get_chat,
    new_chat,
    ready_chat,
    say,
)
from tests.test_lesson_ai_kits import TOPIC, order

# Доступ к «Жоспару» в этих тестах открыт: они проверяют лимит, а сама
# проверка доступа — в `test_lesson_ai_access.py`
pytestmark = pytest.mark.usefixtures("lesson_ai_open")

DAY = 86400

# Комплекты заказываются с пожеланиями: такой комплект личный и в банк готовых
# материалов не ходит (`bankable`). Лимит от банка не зависит и считает оба
# вида комплектов одинаково, а тест так не зависит от того, что лежит в банке.
NOTES = "в классе двое новеньких"


@pytest.fixture
def scene() -> Scene:
    return Scene()


def per_day() -> int:
    return get_settings().lesson_ai_kits_per_day


def ordered(client, scene: Scene, **kw):
    """Прямой заказ из одного плана урока — с пожеланиями, мимо банка."""
    body = {"kinds": ["lesson_plan"], "notes": NOTES}
    body.update(kw)
    return order(client, scene.objective.id, **body)


def order_ok(client, scene: Scene) -> int:
    resp = ordered(client, scene)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def confirm_ok(client, scene: Scene) -> dict:
    chat = ready_chat(client, scene)
    resp = confirm(client, chat["id"], notes=NOTES)
    assert resp.status_code == 201, resp.text
    return resp.json()


def assert_rate_limited(resp) -> int:
    assert resp.status_code == 429, resp.text
    error = resp.json()["error"]
    assert error["code"] == "rate_limited"
    wait = error["details"]["retry_after_sec"]
    assert 0 < wait <= DAY
    return wait


# -- 1. сколько можно за сутки ------------------------------------------------


def test_setting_is_read_and_defaults_to_ten(monkeypatch):
    """Число — настройка `LESSON_AI_KITS_PER_DAY`, по умолчанию 10, и боевой
    счётчик собран из неё на сутки. Число 10 здесь проверяется как значение
    по умолчанию, а остальные тесты берут настройку, как она есть."""
    assert Settings.model_fields["lesson_ai_kits_per_day"].default == 10
    monkeypatch.setenv("LESSON_AI_KITS_PER_DAY", "3")
    assert Settings().lesson_ai_kits_per_day == 3

    limiter = deps.get_lesson_kits_limiter()
    assert (limiter.limit, limiter.window_sec) == (per_day(), DAY)


def test_limit_counts_kits_and_the_next_one_is_refused(client, sms, scene):
    login(client, sms)
    for _ in range(per_day()):
        order_ok(client, scene)

    wait = assert_rate_limited(ordered(client, scene))

    # Все заказы пришли в одну секунду подставных часов — ждать ровно сутки
    assert wait == DAY
    # Отбитый заказ ничего не завёл: ни комплекта, ни заданий в очереди
    assert count(LessonKit) == per_day()
    assert count(LessonAiJob) == per_day()


def test_limit_counts_kits_not_materials(client, sms, scene):
    """Комплект из трёх материалов — один комплект из суточных, а не три."""
    login(client, sms)
    kinds = ["lesson_plan", "tasks", "homework"]
    for _ in range(per_day()):
        assert ordered(client, scene, kinds=kinds).status_code == 201

    assert_rate_limited(ordered(client, scene, kinds=kinds))
    assert count(LessonAiJob) == per_day() * len(kinds)


def test_window_slides_from_each_order(client, sms, scene, lesson_kits_limiter_clock):
    """Окно — 24 часа от заказа: самый старый выходит из окна, и место
    освобождается ровно одно, а не все разом в полночь."""
    clock = lesson_kits_limiter_clock
    login(client, sms)
    order_ok(client, scene)
    clock.shift(3600)
    for _ in range(per_day() - 1):
        order_ok(client, scene)

    # 23:59 от первого заказа — ещё нельзя, и ждать осталась минута
    clock.shift(DAY - 3600 - 60)
    assert assert_rate_limited(ordered(client, scene)) == 60

    # Ровно сутки от первого — его место освободилось, одно
    clock.shift(60)
    order_ok(client, scene)
    # Остальные заказаны часом позже — до них ещё час
    assert assert_rate_limited(ordered(client, scene)) == 3600
    assert count(LessonKit) == per_day() + 1


# -- 2. два пути — один счётчик -----------------------------------------------


def test_direct_order_and_chat_confirm_share_one_counter(client, sms, scene):
    """Половина суточных — прямым заказом, половина — «Готовить» в чате;
    следующий отбит любым путём."""
    login(client, sms)
    for number in range(per_day()):
        if number % 2:
            confirm_ok(client, scene)
        else:
            order_ok(client, scene)

    assert_rate_limited(ordered(client, scene))
    assert_rate_limited(confirm(client, ready_chat(client, scene)["id"], notes=NOTES))
    assert count(LessonKit) == per_day()


def test_chat_confirm_alone_is_limited(client, sms, scene):
    login(client, sms)
    for _ in range(per_day()):
        confirm_ok(client, scene)

    assert_rate_limited(ordered(client, scene))
    assert count(LessonKit) == per_day()


def test_refused_confirm_leaves_the_chat_collecting(
    client, sms, scene, lesson_kits_limiter_clock
):
    """Отказ по лимиту в чате — не подтверждение: чат остаётся собирающим,
    без комплекта, и «Готовить» можно нажать снова, когда окно сдвинется."""
    login(client, sms)
    for _ in range(per_day()):
        order_ok(client, scene)
    chat = ready_chat(client, scene)

    assert_rate_limited(confirm(client, chat["id"], notes=NOTES))

    after = get_chat(client, chat["id"])
    assert (after["status"], after["kit_id"]) == ("collecting", None)
    assert after["order"] == chat["order"]
    assert count(LessonKit) == per_day()

    lesson_kits_limiter_clock.shift(DAY)
    resp = confirm(client, chat["id"], notes=NOTES)
    assert resp.status_code == 201, resp.text
    after = get_chat(client, chat["id"])
    assert (after["status"], after["kit_id"]) == ("confirmed", resp.json()["kit"]["id"])


# -- 3. что лимит не расходует --------------------------------------------------


def bad_orders(client, scene: Scene) -> list:
    """Заказы, которые отбивают проверки сценария комплекта: каждый обязан
    получить свой отказ, а не комплект и не 429."""
    return [
        ("validation_error", ordered(client, scene, topic="   ")),
        ("validation_error", ordered(client, scene, kinds=["lesson_plan", "answer_key"])),
        ("program_not_published", order(
            client, scene.hidden.id, topic=TOPIC, kinds=["lesson_plan"], notes=NOTES
        )),
        ("not_found", order(
            client, 999999, topic=TOPIC, kinds=["lesson_plan"], notes=NOTES
        )),
    ]


def test_refused_orders_do_not_spend_the_limit(client, sms, scene):
    login(client, sms)
    # Отбитых больше, чем суточных: посчитайся они — полных суток бы не осталось
    for _ in range(per_day()):
        for code, resp in bad_orders(client, scene):
            assert resp.status_code in (404, 409, 422), resp.text
            assert resp.json()["error"]["code"] == code

    for _ in range(per_day()):
        order_ok(client, scene)
    assert_rate_limited(ordered(client, scene))


def test_exhausted_limit_still_answers_with_the_order_error(client, sms, scene):
    """Лимит — после проверок: негодный заказ и при исчерпанном лимите
    получает свою причину, а не «подождите сутки» — ждать ему бесполезно."""
    login(client, sms)
    for _ in range(per_day()):
        order_ok(client, scene)

    for code, resp in bad_orders(client, scene):
        assert resp.json()["error"]["code"] == code, resp.text


def test_repeated_confirm_does_not_spend_the_limit(client, sms, scene):
    """Повторное «Готовить» уже подтверждённого чата — 409 `chat_confirmed`,
    и комплекта из суточных оно не съедает."""
    login(client, sms)
    chat_id = confirm_ok(client, scene)["chat"]["id"]

    for _ in range(per_day()):
        resp = confirm(client, chat_id, notes=NOTES)
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "chat_confirmed"

    for _ in range(per_day() - 1):
        order_ok(client, scene)
    assert_rate_limited(ordered(client, scene))


def test_incomplete_chat_order_does_not_spend_the_limit(client, sms, scene):
    login(client, sms)
    chat = new_chat(client)
    for _ in range(per_day()):
        resp = confirm(client, chat["id"], notes=NOTES)
        assert resp.json()["error"]["code"] == "order_incomplete", resp.text

    for _ in range(per_day()):
        order_ok(client, scene)


# -- 4. на учителя ----------------------------------------------------------------


def test_limit_is_per_teacher(client, client2, sms, scene):
    login(client, sms)
    login(client2, sms, phone=OTHER_PHONE)
    for _ in range(per_day()):
        order_ok(client, scene)
    assert_rate_limited(ordered(client, scene))

    for _ in range(per_day()):
        order_ok(client2, scene)
    assert_rate_limited(ordered(client2, scene))
    assert count(LessonKit) == 2 * per_day()


# -- 5. лимит реплик и лимит комплектов — разные ----------------------------------


def test_exhausted_messages_do_not_block_kits(client, sms, ai, scene):
    login(client, sms)
    chat = ready_chat(client, scene)
    user_key = str(client.get("/me").json()["id"])
    messages = app.dependency_overrides[get_lesson_chat_limiter]()
    for _ in range(get_settings().lesson_ai_chat_messages_per_hour):
        assert messages.hit(user_key) == 0
    resp = client.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "ещё"})
    assert_rate_limited(resp)

    order_ok(client, scene)
    resp = confirm(client, chat["id"], notes=NOTES)
    assert resp.status_code == 201, resp.text


def test_exhausted_kits_do_not_block_messages(client, sms, ai, scene):
    login(client, sms)
    for _ in range(per_day()):
        order_ok(client, scene)
    chat = ready_chat(client, scene)
    assert_rate_limited(confirm(client, chat["id"], notes=NOTES))

    events = say(client, ai, chat["id"], "а можно ещё уточнить тему?")

    assert events[-1][0] == "done"
    assert count(LessonAiChatMessage) == 2
