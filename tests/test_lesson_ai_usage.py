"""«Жоспар»: учёт расхода по учителю (сессия L7).

Правила — `.handoff/L7.decisions.md`, запись 4: строка `lesson_ai_usage` на каждый
оплаченный вызов модели, и на неудачный тоже — кривой ответ стоит тех же денег.

- очередь пишет расход каждой попытки в той же транзакции, что закрывает
  задание; ключи с ответами модель не зовут — строки нет;
- перегенерация блока пишет расход сразу после ответа модели и своей
  транзакцией: блок может не записаться, а заплачено уже;
- чат пишет ход целиком — разбор и разговор; оборвался разговор — ход
  откатывается, а разбор, который успел ответить, пишется отдельно;
- стоимость — по прайсу `MODEL_PRICES_USD_PER_M` в момент записи; модели
  в прайсе нет — стоимость пустая, токены всё равно пишутся;
- месячного потолка нет: таблицу не читает ни одна проверка.

Модель в учёте — из настройки `LESSON_AI_MODEL`, поэтому очередь здесь
собирается с моделью из настроек, а не с тестовой `gpt-test`, как в соседних
файлах: иначе стоимость была бы пустой и проверять было бы нечего.
"""

import json
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import LessonAiUsage
from app.api.deps import get_lesson_block_ai, get_lesson_chat_limiter
from app.application.lesson_ai_queue import JobRunner
from app.application.ports import AiDelta, AiReply
from app.config import get_settings
from app.domain.lesson_ai import MODEL_PRICES_USD_PER_M, usage_cost
from app.lesson_ai.kinds import generators
from app.main import app
from tests.conftest import FakeAi, login, seed, user_id
from tests.test_lesson_ai_blocks import (
    INSTRUCTION,
    NEW_MIDDLE,
    TEACHER_MIDDLE,
    edit,
    ready,
    regenerate,
)
from tests.test_lesson_ai_chat import (
    NOTHING,
    OTHER_PHONE,
    SilentAi,
    StreamingChatAi,
    confirm,
    new_chat,
    ready_chat,
    say,
)
from tests.test_lesson_ai_chat import Scene as ChatScene
from tests.test_lesson_ai_kits import DRAFT, Scene, hurry_retries, order
from tests.test_lesson_ai_order import KindAi
from tests.test_lesson_ai_queue import fetch, job_of

# Доступ к «Жоспару» в этих тестах открыт: они проверяют учёт, а сама
# проверка доступа — в `test_lesson_ai_access.py`
pytestmark = pytest.mark.usefixtures("lesson_ai_open")

# Комплекты заказываются с пожеланиями: такой комплект личный и в банк готовых
# материалов не ходит (`bankable`) — каждый материал генерируется моделью,
# и расход каждого вызова виден. Учёт так не зависит от содержимого банка.
NOTES = "в классе двое новеньких"

UNKNOWN_MODEL = "gpt-9-preview-2031-01-01"


@pytest.fixture
def scene() -> Scene:
    return Scene()


def model() -> str:
    return get_settings().lesson_ai_model


def run_next(ai) -> bool:
    """Одно задание очереди — как его берёт воркер в бою: модель из настроек
    (`worker.py` собирает генераторы так же)."""
    runner = JobRunner(lambda: OrmSession(get_engine()), generators(ai, model()), lease_sec=600)
    return runner.run_next()


def usage(source: str | None = None) -> list[dict]:
    rows = fetch(
        "SELECT user_id, source, kit_id, chat_id, kind, model, tokens_in, tokens_out,"
        " cost_usd FROM lesson_ai_usage ORDER BY id"
    )
    return [row for row in rows if source is None or row["source"] == source]


def kit_ordered(client, scene: Scene, kinds=("lesson_plan",)) -> int:
    resp = order(client, scene.objective.id, kinds=list(kinds), notes=NOTES)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# -- 1. генерация очередью --------------------------------------------------


def test_successful_generation_is_one_material_row(client, sms, ai, scene):
    login(client, sms)
    kit_id = kit_ordered(client, scene)
    ai.returns_json(DRAFT, tokens_in=1234, tokens_out=2345)

    assert run_next(ai)

    assert usage() == [
        {
            "user_id": user_id(client),
            "source": "material",
            "kit_id": kit_id,
            "chat_id": None,
            "kind": "lesson_plan",
            "model": model(),
            "tokens_in": 1234,
            "tokens_out": 2345,
            "cost_usd": usage_cost(model(), 1234, 2345),
        }
    ]


def test_every_misshaped_attempt_is_paid_for(client, sms, ai, scene):
    """Ответ не по схеме — отказ задания, но модель ответила, и заплачено:
    строка на каждую попытку, а их сумма — ровно расход задания."""
    login(client, sms)
    kit_id = kit_ordered(client, scene)
    tokens = [(100, 10), (200, 20), (300, 30)]
    for tokens_in, tokens_out in tokens:
        ai.returns_json("не json {", tokens_in=tokens_in, tokens_out=tokens_out)

    for _ in tokens:
        assert run_next(ai)
        hurry_retries()

    job = job_of(kit_id)
    assert (job["status"], job["error"], job["attempts"]) == (
        "failed", "model_output_invalid", len(tokens)
    )
    rows = usage()
    assert [(row["tokens_in"], row["tokens_out"]) for row in rows] == tokens
    assert {(row["source"], row["kit_id"], row["kind"]) for row in rows} == {
        ("material", kit_id, "lesson_plan")
    }
    assert [row["cost_usd"] for row in rows] == [usage_cost(model(), *pair) for pair in tokens]
    assert sum(row["tokens_in"] for row in rows) == job["tokens_in"]
    assert sum(row["tokens_out"] for row in rows) == job["tokens_out"]


def test_misshaped_then_good_is_two_rows(client, sms, ai, scene):
    login(client, sms)
    kit_id = kit_ordered(client, scene)
    ai.returns_json("{", tokens_in=100, tokens_out=10)
    ai.returns_json(DRAFT, tokens_in=900, tokens_out=1400)

    assert run_next(ai)
    hurry_retries()
    assert run_next(ai)

    job = job_of(kit_id)
    assert job["status"] == "done"
    assert [(row["tokens_in"], row["tokens_out"]) for row in usage()] == [(100, 10), (900, 1400)]
    assert (job["tokens_in"], job["tokens_out"]) == (1000, 1410)


def test_silent_provider_costs_nothing(client, sms, ai, scene):
    """Провайдер не ответил вовсе — платить не за что, и строки нет."""
    login(client, sms)
    kit_ordered(client, scene)
    ai.fails()

    assert run_next(ai)

    assert usage() == []


def test_answer_key_calls_no_model_and_costs_nothing(client, sms, scene):
    login(client, sms)
    kit_id = kit_ordered(client, scene, kinds=["tasks", "answer_key"])
    fake = KindAi()

    while run_next(fake):
        pass

    assert fetch(
        "SELECT kind, status FROM lesson_ai_job WHERE kit_id = :kit_id ORDER BY kind",
        kit_id=kit_id,
    ) == [{"kind": "answer_key", "status": "done"}, {"kind": "tasks", "status": "done"}]
    assert [(row["kind"], row["tokens_in"], row["tokens_out"]) for row in usage()] == [
        ("tasks", 900, 1400)
    ]


def test_generation_is_charged_to_the_kit_owner(client, client2, sms, scene):
    """Учителя у воркера нет — расход пишется владельцу комплекта, каждому свой."""
    login(client, sms)
    login(client2, sms, phone=OTHER_PHONE)
    first = kit_ordered(client, scene)
    second = kit_ordered(client2, scene, kinds=["lesson_plan", "tasks"])
    fake = KindAi()

    while run_next(fake):
        pass

    owners = {(row["kit_id"], row["kind"]): row["user_id"] for row in usage()}
    assert owners == {
        (first, "lesson_plan"): user_id(client),
        (second, "lesson_plan"): user_id(client2),
        (second, "tasks"): user_id(client2),
    }


# -- 2. перегенерация блока ---------------------------------------------------


def test_block_regeneration_is_one_block_row(client, sms, ai, scene):
    material_id = ready(client, sms, ai, scene, notes=NOTES)
    kit_id = usage("material")[0]["kit_id"]
    ai.returns_json({"stage_middle": NEW_MIDDLE}, tokens_in=500, tokens_out=70)

    resp = regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION)

    assert resp.status_code == 200, resp.text
    assert usage("block") == [
        {
            "user_id": user_id(client),
            "source": "block",
            "kit_id": kit_id,
            "chat_id": None,
            "kind": "lesson_plan",
            "model": model(),
            "tokens_in": 500,
            "tokens_out": 70,
            "cost_usd": usage_cost(model(), 500, 70),
        }
    ]


def test_block_asked_twice_is_paid_for_both(client, sms, ai, scene):
    """Первый ответ не по форме переспрошен — строка одна на запрос учителя,
    но токены обоих вызовов."""
    material_id = ready(client, sms, ai, scene, notes=NOTES)
    ai.returns_json('{"stage_middle": ', tokens_in=400, tokens_out=5)
    ai.returns_json({"stage_middle": NEW_MIDDLE}, tokens_in=500, tokens_out=70)

    resp = regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION)

    assert resp.status_code == 200, resp.text
    [row] = usage("block")
    assert (row["tokens_in"], row["tokens_out"]) == (900, 75)


def test_block_changed_meanwhile_is_still_paid_for(
    client, client2, sms, ai, scene, monkeypatch
):
    """Учитель поправил блок во второй вкладке, пока модель думала: ответ
    модели не записан (`409 block_changed`), а расход — записан: провайдеру
    заплачено до того, как выяснилось, что блок изменился."""
    material_id = ready(client, sms, ai, scene, notes=NOTES)
    login(client2, sms)
    ai.returns_json({"stage_middle": NEW_MIDDLE}, tokens_in=500, tokens_out=70)
    real = FakeAi.complete_json

    def thinking(self, **kw):
        reply = real(self, **kw)
        assert edit(client2, material_id, "stage_middle", TEACHER_MIDDLE).status_code == 200
        return reply

    monkeypatch.setattr(FakeAi, "complete_json", thinking)

    resp = regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION)

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "block_changed"
    [row] = usage("block")
    assert (row["user_id"], row["tokens_in"], row["tokens_out"]) == (user_id(client), 500, 70)


def test_silent_provider_on_block_costs_nothing(client, sms, ai, scene):
    material_id = ready(client, sms, ai, scene, notes=NOTES)
    ai.fails()

    resp = regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION)

    assert resp.status_code == 502, resp.text
    assert usage("block") == []


def test_twice_misshaped_block_is_still_paid_for(client, sms, ai, scene):
    """Запись 4: строка на каждый оплаченный вызов, и на неудачный тоже —
    очередь и разбор чата так и делают. Модель ответила дважды, ответы
    не по форме — заплачено за оба."""
    material_id = ready(client, sms, ai, scene, notes=NOTES)
    ai.returns_json('{"stage_middle": ', tokens_in=400, tokens_out=5)
    ai.returns_json('{"stage_middle": ', tokens_in=410, tokens_out=6)

    resp = regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION)

    assert resp.status_code == 502, resp.text
    [row] = usage("block")
    assert (row["tokens_in"], row["tokens_out"]) == (810, 11)


# -- 3. чат --------------------------------------------------------------------


@pytest.fixture
def chat_scene() -> ChatScene:
    return ChatScene()


def assistant_tokens() -> list[tuple[int, int]]:
    rows = fetch(
        "SELECT tokens_in, tokens_out FROM lesson_ai_chat_message"
        " WHERE role = 'assistant' ORDER BY id"
    )
    return [(row["tokens_in"], row["tokens_out"]) for row in rows]


def test_chat_turn_is_one_row_with_extract_and_conversation(client, sms, ai, chat_scene):
    login(client, sms)
    chat = new_chat(client)

    say(
        client, ai, chat["id"], "завтра урок во втором классе",
        tokens_in=900, tokens_out=60,
        then=lambda ai: ai.says("Какая тема?", tokens_in=320, tokens_out=40),
    )

    assert usage() == [
        {
            "user_id": user_id(client),
            "source": "chat",
            "kit_id": None,
            "chat_id": chat["id"],
            "kind": None,
            "model": model(),
            "tokens_in": 1220,
            "tokens_out": 100,
            "cost_usd": usage_cost(model(), 1220, 100),
        }
    ]
    # Строка учёта и расход хода в ленте — одно и то же число
    assert assistant_tokens() == [(1220, 100)]


def test_every_chat_turn_is_its_own_row(client, sms, ai, chat_scene):
    login(client, sms)
    chat = new_chat(client)
    for number in range(1, 4):
        say(
            client, ai, chat["id"], f"реплика {number}",
            tokens_in=100 * number, tokens_out=10 * number,
            then=lambda ai: ai.says("Понял", tokens_in=50, tokens_out=5),
        )

    rows = usage("chat")
    assert [(row["tokens_in"], row["tokens_out"]) for row in rows] == assistant_tokens()
    assert assistant_tokens() == [(150, 15), (250, 25), (350, 35)]


class BrokenStreamAi(StreamingChatAi):
    """Разговор начал отвечать кусками и оборвался, не дойдя до ответа
    целиком: связь порвалась посреди стрима."""

    def stream(self, *, system, messages, tools=None):
        self.calls.append({"tools": tools})
        yield AiDelta(text="Сейчас ")


def broken_stream_ai() -> BrokenStreamAi:
    fake = BrokenStreamAi()
    fake.extracts.append(
        AiReply(text=json.dumps({**NOTHING, "topic": "Тема"}), tokens_in=10, tokens_out=10)
    )
    return fake


@pytest.mark.parametrize("provider", [SilentAi, broken_stream_ai], ids=["silent", "broken"])
def test_broken_turn_keeps_the_extract_cost(client, sms, chat_scene, provider):
    """Хода не было — ни реплики, ни ответа в ленте. Но разбор реплики успел
    ответить, за него заплачено, и он в учёте отдельной строкой."""
    login(client, sms)
    chat = new_chat(client)
    fake = provider()
    app.dependency_overrides[get_lesson_block_ai] = lambda: fake
    try:
        resp = client.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "казахский"})
    finally:
        app.dependency_overrides.pop(get_lesson_block_ai, None)

    assert resp.status_code == 200, resp.text
    assert "event: error" in resp.text
    assert fetch("SELECT id FROM lesson_ai_chat_message") == []
    [row] = usage()
    assert (row["source"], row["chat_id"], row["user_id"]) == ("chat", chat["id"], user_id(client))
    assert (row["tokens_in"], row["tokens_out"]) == (10, 10)
    assert row["cost_usd"] == usage_cost(model(), 10, 10)


def test_refusals_before_the_stream_cost_nothing(
    client, sms, ai, chat_scene, lesson_chat_limiter_clock
):
    """Отказ до стрима — модель не звали, строки нет: пустой текст,
    потолок реплик."""
    login(client, sms)
    chat = new_chat(client)
    assert client.post(
        f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "  "}
    ).status_code == 422

    limiter = app.dependency_overrides[get_lesson_chat_limiter]()
    while limiter.hit(str(user_id(client))) == 0:
        pass
    resp = client.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "привет"})
    assert resp.status_code == 429, resp.text

    assert ai.calls == []
    assert usage() == []


# -- 4. стоимость ----------------------------------------------------------------


def test_cost_is_exact_by_the_price_list(client, sms, ai, chat_scene):
    """Стоимость — доллары за миллион токенов, в `Decimal` без округления
    по дороге. Число посчитано руками по прайсу, а не той же функцией."""
    price_in, price_out = MODEL_PRICES_USD_PER_M[model()]
    login(client, sms)
    chat = new_chat(client)

    say(client, ai, chat["id"], "привет", tokens_in=1000, tokens_out=0,
        then=lambda ai: ai.says("Здравствуйте", tokens_in=220, tokens_out=100))

    [row] = usage()
    expected = (price_in * 1220 + price_out * 100) / Decimal(1_000_000)
    assert row["cost_usd"] == expected
    if model() == "gpt-4.1":
        assert row["cost_usd"] == Decimal("0.00324")


@pytest.mark.parametrize("name", sorted(MODEL_PRICES_USD_PER_M))
def test_usage_cost_is_per_million(name):
    price_in, price_out = MODEL_PRICES_USD_PER_M[name]
    assert usage_cost(name, 1_000_000, 0) == price_in
    assert usage_cost(name, 0, 1_000_000) == price_out
    assert usage_cost(name, 2_000_000, 3_000_000) == 2 * price_in + 3 * price_out
    assert isinstance(usage_cost(name, 1, 1), Decimal)


def test_unknown_model_has_no_price():
    assert usage_cost(UNKNOWN_MODEL, 1000, 1000) is None
    assert usage_cost(None, 1000, 1000) is None


def test_unknown_model_writes_tokens_without_cost(client, sms, ai, scene, monkeypatch):
    """Модели нет в прайсе — стоимость пустая, а токены записаны: цену
    допишут в прайс, а сколько потрачено, иначе не узнать никогда."""
    monkeypatch.setattr(get_settings(), "lesson_ai_model", UNKNOWN_MODEL)
    material_id = ready(client, sms, ai, scene, notes=NOTES)
    ai.returns_json({"stage_middle": NEW_MIDDLE}, tokens_in=500, tokens_out=70)
    assert regenerate(client, material_id, "stage_middle").status_code == 200
    chat = new_chat(client)
    say(client, ai, chat["id"], "привет", tokens_in=30, tokens_out=3)

    # План собран очередью соседнего файла с моделью `gpt-test` — её в прайсе
    # тоже нет; ручки перегенерации и чата берут `LESSON_AI_MODEL` из настройки
    rows = usage()
    assert [(row["source"], row["model"], row["cost_usd"]) for row in rows] == [
        ("material", "gpt-test", None),
        ("block", UNKNOWN_MODEL, None),
        ("chat", UNKNOWN_MODEL, None),
    ]
    assert [(row["tokens_in"], row["tokens_out"]) for row in rows] == [
        (900, 1400), (500, 70), (30, 3)
    ]


def test_unknown_model_in_the_queue_writes_tokens_without_cost(
    client, sms, ai, scene, monkeypatch
):
    monkeypatch.setattr(get_settings(), "lesson_ai_model", UNKNOWN_MODEL)
    login(client, sms)
    kit_ordered(client, scene)
    ai.returns_json(DRAFT, tokens_in=900, tokens_out=1400)

    assert run_next(ai)

    [row] = usage()
    assert (row["model"], row["tokens_in"], row["tokens_out"], row["cost_usd"]) == (
        UNKNOWN_MODEL, 900, 1400, None
    )


# -- 5. чей расход и потолка нет -------------------------------------------------


def test_chat_usage_is_written_to_its_own_teacher(client, client2, sms, ai, chat_scene):
    login(client, sms)
    login(client2, sms, phone=OTHER_PHONE)
    first = new_chat(client)
    second = new_chat(client2)

    say(client, ai, first["id"], "первый", tokens_in=100, tokens_out=10)
    say(client2, ai, second["id"], "второй", tokens_in=200, tokens_out=20)

    assert {(row["user_id"], row["chat_id"], row["tokens_in"]) for row in usage()} == {
        (user_id(client), first["id"], 100),
        (user_id(client2), second["id"], 200),
    }


def test_no_monthly_ceiling(client, sms, ai, scene):
    """Месячного потолка нет (решение владельца 19.09.2026): учитель, на которого
    уже потрачены сотни долларов, заказывает комплект, говорит с чатом и
    перегенерирует блок как ни в чём не бывало."""
    login(client, sms)
    teacher = user_id(client)
    for _ in range(3):
        seed(
            LessonAiUsage(
                user_id=teacher,
                source="material",
                kind="lesson_plan",
                model="gpt-4.1",
                tokens_in=100_000_000,
                tokens_out=0,
                cost_usd=Decimal("200"),
            )
        )

    material_id = ready(client, sms, ai, scene, notes=NOTES)
    ai.returns_json({"stage_middle": NEW_MIDDLE}, tokens_in=500, tokens_out=70)
    assert regenerate(client, material_id, "stage_middle").status_code == 200
    chat = new_chat(client)
    say(client, ai, chat["id"], "привет", tokens_in=30, tokens_out=3)
    kit_ordered(client, scene)

    rows = usage()
    assert sum(row["cost_usd"] for row in rows[:3]) == Decimal("600")
    assert [row["source"] for row in rows[3:]] == ["material", "block", "chat"]
