"""«Жоспар»: чат сбора заказа — вход в продукт (сессия L6).

Учитель пишет своими словами, какой у него урок; помощник ищет цель
в справочнике, спрашивает язык и тему, а «Готовить» заводит комплект.
Проверяется то, на чём чат держится **в коде, а не в промпте**
(`.handoff/L6.decisions.md`, записи 4–11):

- заказ пишет сервер по разбору реплики, и каждое поле проходит проверку:
  цель — только опубликованная и только показанная учителю в этом чате;
- язык и цель согласованы всегда: казахский заказ берёт казахскую строку
  приказа того же кода, а не перевод русской;
- пока не собраны все пятеро — ни комплекта, ни задания в очереди;
- оба уровня приходят с подтверждением обязательными полями.

Ход — два запроса к подставному ИИ из одной очереди: сначала разбор реплики
(`complete_json`), потом разговор с инструментами (`complete`). Поэтому
на каждую реплику сначала кладётся разбор — `say()` делает это сам.
"""

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.adapters.ai.openai_chat import AiUnavailableError
from app.adapters.db.base import get_engine
from app.adapters.db.lesson_ai_repos import LessonChatRepo
from app.adapters.db.models import LessonAiChatMessage, LessonAiJob, LessonKit
from app.api.deps import get_lesson_block_ai, get_lesson_chat_limiter
from app.application.ports import AiMessage, AiReply, AiToolCall
from app.config import get_settings
from app.domain.lesson_ai import DEFAULT_KINDS, MATERIAL_KINDS
from app.lesson_ai.order_tools import OrderCtx, OrderTools
from app.lesson_ai.prompts import order_chat as prompt
from app.main import app
from tests.conftest import login
from tests.test_admin_curriculum import (
    make_objective,
    make_program,
    make_section,
    make_subject,
)
from tests.test_assistant_stream import StreamingAi
from tests.test_lesson_ai_kits import CODE, OBJECTIVE_TEXT, TOPIC, set_program_status

OTHER_PHONE = "+7 (707) 555-01-02"

# Та же цель 2.1.2.6 в казахской редакции приказа: официальный текст, а не перевод
KZ_TEXT = (
    "40+17, 57-40, 57-17, 35±12 сияқты жағдайларда ондықтан аттамай екі таңбалы"
    " сандарды ауызша қосу және азайту"
)
FIRST_REPLY = "завтра 2 класс, складываем двузначные без перехода через десяток"
# Запрос словами программы — так его пишет модель по описанию инструмента
QUERY = "сложение вычитание двузначных чисел без перехода через десяток"

# Разбор, в котором учитель не сказал ничего: все восемь полей схемы — null.
# Тест кладёт в очередь только то, что учитель «сказал»
NOTHING = dict.fromkeys(prompt.EXTRACT_SCHEMA["required"])
LEVELS = {"engagement_level": "active", "difficulty_level": "standard"}
ALL_MISSING = ["subject", "grade", "lang", "objective", "topic"]


class Scene:
    """Математика в двух редакциях одного приказа, плюс загруженный, но ещё
    не опубликованный приказ в обеих редакциях.

    Цели разложены так, чтобы каждая проверка отсекала ровно одну:

    - 2.1.2.6 есть в обеих редакциях — смена языка меняет строку;
    - 2.1.2.7 опубликована только по-русски, а казахская строка того же кода
      лежит в загруженном приказе — смена языка обязана её не найти;
    - 2.1.2.8 — только в загруженном приказе, и слова у неё те же, что
      у главной цели: поиск без фильтра публикации её бы нашёл.
    """

    def __init__(self):
        self.subject = make_subject()
        self.ru = make_program(self.subject.id, lang="ru", status="published")
        self.kz = make_program(self.subject.id, lang="kz", status="published")
        ru_section = make_section(self.ru.id, "1", "Числа и величины")
        ru_sub = make_section(
            self.ru.id, "1.2", "Операции над числами", parent_id=ru_section.id
        )
        kz_section = make_section(self.kz.id, "1", "Сандар және шамалар")
        kz_sub = make_section(
            self.kz.id, "1.2", "Сандарға амалдар қолдану", parent_id=kz_section.id
        )
        self.first_grade = make_objective(
            self.ru.id, ru_sub.id, "1.1.2.1",
            text="считать в прямом и обратном порядке в пределах 10",
        )
        self.within_100 = make_objective(
            self.ru.id, ru_sub.id, "2.1.2.5",
            text="выполнять устно сложение и вычитание чисел в пределах 100",
        )
        self.objective = make_objective(self.ru.id, ru_sub.id, CODE, text=OBJECTIVE_TEXT)
        self.ru_only = make_objective(
            self.ru.id, ru_sub.id, "2.1.2.7",
            text="решать задачи на сложение и вычитание двузначных чисел",
        )
        self.kz_within_100 = make_objective(
            self.kz.id, kz_sub.id, "2.1.2.5",
            text="100 көлеміндегі сандарды ауызша қосу және азайту",
        )
        self.kz_objective = make_objective(self.kz.id, kz_sub.id, CODE, text=KZ_TEXT)

        self.draft_ru = make_program(
            self.subject.id, lang="ru", status="loaded", order_number="400"
        )
        draft_ru_section = make_section(self.draft_ru.id, "1", "Числа и величины")
        self.hidden = make_objective(
            self.draft_ru.id, draft_ru_section.id, "2.1.2.8",
            text="сравнивать сложение и вычитание двузначных чисел без перехода через десяток",
        )
        self.draft_kz = make_program(
            self.subject.id, lang="kz", status="loaded", order_number="400"
        )
        draft_kz_section = make_section(self.draft_kz.id, "1", "Сандар және шамалар")
        self.hidden_kz_twin = make_objective(
            self.draft_kz.id, draft_kz_section.id, "2.1.2.7",
            text="екі таңбалы сандарды қосу мен азайтуға есептер шығару",
        )


@pytest.fixture
def scene() -> Scene:
    return Scene()


# -- хелперы ---------------------------------------------------------------


def read(resp) -> list[tuple[str, dict]]:
    """Ответ хода, разобранный на события `text/event-stream`. Отказы до
    стрима (`404`, `409`, `422`, `429`) сюда не приходят — их тест
    проверяет обычным ответом."""
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    out: list[tuple[str, dict]] = []
    for block in resp.text.strip().split("\n\n"):
        if not block.strip():
            continue
        head, data = block.split("\n", 1)
        assert head.startswith("event: ") and data.startswith("data: "), block
        out.append((head.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def done(events: list[tuple[str, dict]]) -> dict:
    found = [data for name, data in events if name == "done"]
    assert len(found) == 1, f"ход обязан закончиться ровно одним done: {events}"
    assert events[-1][0] == "done"
    return found[0]


def say(client, ai, chat_id: int, text: str, *, extract=None, then=None, **tokens):
    """Реплика учителя целиком: разбор, потом разговор.

    `extract` — что учитель «сказал» (словарь поверх `NOTHING`) или строка
    как есть — кривой ответ разбора. `then` — что делает модель разговора;
    по умолчанию просто отвечает текстом.
    """
    ai.returns_json(
        extract if isinstance(extract, str) else {**NOTHING, **(extract or {})}, **tokens
    )
    if then is None:
        ai.says("Хорошо")
    else:
        then(ai)
    return read(client.post(f"/lesson_ai/chats/{chat_id}/messages", json={"text": text}))


def new_chat(client) -> dict:
    resp = client.post("/lesson_ai/chats")
    assert resp.status_code == 201, resp.text
    return resp.json()


def get_chat(client, chat_id: int) -> dict:
    resp = client.get(f"/lesson_ai/chats/{chat_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def patch(client, chat_id: int, **body):
    return client.patch(f"/lesson_ai/chats/{chat_id}/order", json=body)


def confirm(client, chat_id: int, **body):
    payload = {"kinds": ["lesson_plan"], **LEVELS}
    payload.update(body)
    return client.post(f"/lesson_ai/chats/{chat_id}/confirm", json=payload)


def ready_chat(client, scene: Scene, *, objective=None, lang="ru", topic=TOPIC) -> dict:
    """Чат с собранными пятью — кнопками, без модели: проверкам подтверждения
    разговор не нужен."""
    chat = new_chat(client)
    fields = {"objective_id": (objective or scene.objective).id, "lang": lang, "topic": topic}
    fields = {name: value for name, value in fields.items() if value is not None}
    resp = patch(client, chat["id"], **fields)
    assert resp.status_code == 200, resp.text
    return resp.json()


def shows(call_id: str = "call_1"):
    """Модель разговора находит цели инструментом и спрашивает «Она?»."""

    def run(ai):
        ai.wants("find_objectives", call_id=call_id, query=QUERY, grade=2, lang=None)
        ai.says("Нашёл в программе цель. Она?")

    return run


def count(model) -> int:
    with OrmSession(get_engine()) as db:
        return db.scalar(select(func.count()).select_from(model))


def item(objective, *, lang="ru") -> dict:
    """Цель так, как её отдаёт инструмент и карточка заказа."""
    section, subsection = (
        ("Числа и величины", "Операции над числами")
        if lang == "ru"
        else ("Сандар және шамалар", "Сандарға амалдар қолдану")
    )
    return {
        "id": objective.id,
        "code": objective.code,
        "text": objective.text,
        "marker": "",
        "grade": objective.grade,
        "lang": lang,
        "subject_code": "math",
        "section": section,
        "subsection": subsection,
    }


def turn_calls(ai) -> list[dict]:
    """Запросы разговора — у них есть ключ `tools`; у разбора его нет."""
    return [call for call in ai.calls if "tools" in call]


def extract_calls(ai) -> list[dict]:
    return [call for call in ai.calls if "schema" in call]


# -- 1. создание чата и чужие чаты -------------------------------------------


def test_new_chat_is_an_empty_order(client, sms, ai):
    """Пустой заказ: не хватает всех пятерых, а уровни и галочки уже стоят
    серединой и списком брифа — экрану есть что предвыбрать."""
    login(client, sms)

    resp = client.post("/lesson_ai/chats")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body == {
        "id": body["id"],
        "status": "collecting",
        "kit_id": None,
        "order": {
            "subject_code": None,
            "grade": None,
            "lang": None,
            "objective": None,
            "topic": None,
            "engagement_level": "active",
            "difficulty_level": "standard",
            "kinds": list(DEFAULT_KINDS),
            "class_size": None,
            "notes": "",
            "missing": ALL_MISSING,
            "ready": False,
        },
        "items": [],
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert get_chat(client, body["id"]) == body
    # Модель при создании не зовут: чат ещё пустой
    assert ai.calls == []


def test_chats_require_login(client, scene):
    for method, path, body in [
        ("post", "/lesson_ai/chats", None),
        ("get", "/lesson_ai/chats/1", None),
        ("post", "/lesson_ai/chats/1/messages", {"text": "привет"}),
        ("patch", "/lesson_ai/chats/1/order", {"topic": TOPIC}),
        ("post", "/lesson_ai/chats/1/confirm", {"kinds": ["lesson_plan"], **LEVELS}),
        ("get", "/lesson_ai/objectives?q=сложение", None),
    ]:
        resp = client.request(method, path, json=body)
        assert resp.status_code == 401, (path, resp.text)
        assert resp.json()["error"]["code"] == "unauthorized"


def test_foreign_chat_is_404_everywhere(client, client2, sms, ai, scene):
    """Чужой чат не существует: 404, а не 403 — и ничего в нём не меняется."""
    login(client, sms)
    chat = ready_chat(client, scene)
    login(client2, sms, phone=OTHER_PHONE)

    responses = [
        client2.get(f"/lesson_ai/chats/{chat['id']}"),
        client2.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "да"}),
        patch(client2, chat["id"], topic="Чужая тема"),
        confirm(client2, chat["id"]),
        client2.get("/lesson_ai/chats/999999"),
    ]

    for resp in responses:
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"] == {"code": "not_found", "message": "Чат не найден"}
    assert get_chat(client, chat["id"]) == chat
    assert count(LessonKit) == 0
    assert ai.calls == []


# -- 2. сценарий брифа целиком -----------------------------------------------


def test_brief_scenario_end_to_end(client, sms, ai, scene):
    """Раздел 1 брифа: описание урока → цель из справочника → «да» → язык →
    тема → «Готовить». В комплект уходит строка базы, которую учитель
    подтвердил, а не пересказ модели."""
    login(client, sms)
    chat = new_chat(client)

    events = say(client, ai, chat["id"], FIRST_REPLY, then=shows())

    # Карточки целей уходят событием в момент поиска — строками справочника
    [(name, tool)] = [event for event in events if event[0] == "tool"]
    assert [name for name, _ in events] == ["tool", "done"]
    assert tool["tool_name"] == "find_objectives"
    assert tool["tool_args"] == {"query": QUERY, "grade": 2, "lang": None}
    items = tool["tool_result"]["items"]
    assert items[0] == item(scene.objective)
    assert items[0]["text"] == OBJECTIVE_TEXT
    # Более подходящая цель выше; казахских строк по русским словам нет,
    # загруженной программы нет вовсе
    assert [row["code"] for row in items] == [CODE, "2.1.2.7", "2.1.2.5"]
    turn = done(events)
    assert turn["stopped_at_cap"] is False
    feed = turn["chat"]["items"]
    assert [row["role"] for row in feed] == ["user", "tool", "assistant"]
    assert feed[0]["content"] == FIRST_REPLY
    assert feed[1]["tool_result"] == tool["tool_result"]
    assert feed[2]["content"] == "Нашёл в программе цель. Она?"
    # Модель показала цель, но подтвердить её может только учитель
    assert turn["chat"]["order"]["objective"] is None

    order = done(say(client, ai, chat["id"], "да", extract={"objective_id": scene.objective.id}))[
        "chat"
    ]["order"]
    assert order["objective"] == item(scene.objective)
    assert (order["subject_code"], order["grade"]) == ("math", 2)
    assert order["missing"] == ["lang", "topic"]

    order = done(say(client, ai, chat["id"], "на русском", extract={"lang": "ru"}))["chat"][
        "order"
    ]
    assert order["lang"] == "ru"
    assert order["missing"] == ["topic"]

    order = done(say(client, ai, chat["id"], TOPIC, extract={"topic": TOPIC}))["chat"]["order"]
    assert order["topic"] == TOPIC
    assert order["missing"] == []
    assert order["ready"] is True

    # Галочки нарочно не по порядку — сервер раскладывает по брифу
    kinds = ["answer_key", "homework", "tasks", "presentation", "lesson_plan"]
    resp = confirm(
        client, chat["id"], kinds=kinds, engagement_level="full_game", difficulty_level="basic"
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    kit = body["kit"]
    assert kit["objective"] == {
        "id": scene.objective.id, "code": CODE, "text": OBJECTIVE_TEXT, "marker": ""
    }
    assert (kit["grade"], kit["lang"], kit["topic"], kit["subject_code"]) == (
        2, "ru", TOPIC, "math"
    )
    assert (kit["engagement_level"], kit["difficulty_level"]) == ("full_game", "basic")
    assert [material["kind"] for material in kit["materials"]] == [
        "lesson_plan", "presentation", "tasks", "homework", "answer_key"
    ]
    assert body["chat"]["status"] == "confirmed"
    assert body["chat"]["kit_id"] == kit["id"]
    assert client.get(f"/lesson_ai/kits/{kit['id']}").json()["id"] == kit["id"]
    with OrmSession(get_engine()) as db:
        [row] = db.scalars(select(LessonKit)).all()
        assert (row.objective_id, row.topic, row.grade, row.lang) == (
            scene.objective.id, TOPIC, 2, "ru"
        )
        jobs = db.scalars(select(LessonAiJob).order_by(LessonAiJob.id)).all()
        assert sorted(job.kind for job in jobs) == sorted(kinds)


class StreamingChatAi(StreamingAi):
    """Провайдер со стримом — как настоящий: текст разговора приходит кусками.
    Разбор заказа идёт обычным запросом, поэтому у него своя очередь."""

    def __init__(self):
        super().__init__()
        self.extracts: list[AiReply] = []

    def complete_json(self, *, system, messages, name, schema) -> AiReply:
        return self.extracts.pop(0)


def test_answer_streams_in_deltas(client, sms, scene):
    login(client, sms)
    chat = new_chat(client)
    fake = StreamingChatAi()
    fake.extracts.append(AiReply(text=json.dumps(NOTHING)))
    fake.says("Урок на каком языке ведёте?")
    app.dependency_overrides[get_lesson_block_ai] = lambda: fake
    try:
        events = read(
            client.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "привет"})
        )
    finally:
        app.dependency_overrides.pop(get_lesson_block_ai, None)

    names = [name for name, _ in events]
    assert names.count("delta") == 5
    assert names[-1] == "done"
    text = "".join(data["text"] for name, data in events if name == "delta").strip()
    assert text == "Урок на каком языке ведёте?"
    assert done(events)["chat"]["items"][-1]["content"] == text


# -- 3. пока не собраны пятеро — ничего не заводится ---------------------------


@pytest.mark.parametrize(
    ("drop", "missing"),
    [
        ("topic", ["topic"]),
        ("lang", ["lang"]),
        ("objective", ["subject", "grade", "objective"]),
        ("everything", ALL_MISSING),
    ],
)
def test_incomplete_order_is_not_confirmed(client, sms, scene, drop, missing):
    login(client, sms)
    if drop == "everything":
        chat = new_chat(client)
    else:
        chat = ready_chat(
            client,
            scene,
            lang=None if drop == "lang" else "ru",
            topic=None if drop == "topic" else TOPIC,
        )
        if drop == "objective":
            assert patch(client, chat["id"], objective_id=None).status_code == 200

    resp = confirm(client, chat["id"])

    assert resp.status_code == 409, resp.text
    assert resp.json() == {
        "error": {
            "code": "order_incomplete",
            "message": "В заказе не хватает данных — ответьте помощнику",
            "details": {"missing": missing},
        }
    }
    assert get_chat(client, chat["id"])["order"]["missing"] == missing
    assert get_chat(client, chat["id"])["status"] == "collecting"
    assert count(LessonKit) == 0
    assert count(LessonAiJob) == 0


# -- 4. оба уровня обязательны -------------------------------------------------


@pytest.mark.parametrize("field", ["engagement_level", "difficulty_level"])
def test_both_levels_are_required_on_confirm(client, sms, scene, field):
    """Середина есть, но в необязательное её не прячут: экран обязан
    показать вопрос и прислать ответ (решение 7)."""
    login(client, sms)
    chat = ready_chat(client, scene)
    body = {"kinds": ["lesson_plan"], **LEVELS}
    body.pop(field)

    resp = client.post(f"/lesson_ai/chats/{chat['id']}/confirm", json=body)

    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert [row["field"] for row in error["details"]["fields"]] == [field]
    assert count(LessonKit) == 0


def test_levels_sent_as_the_middle_pass(client, sms, scene):
    login(client, sms)
    chat = ready_chat(client, scene)

    resp = confirm(client, chat["id"], engagement_level="active", difficulty_level="standard")

    assert resp.status_code == 201, resp.text
    kit = resp.json()["kit"]
    assert (kit["engagement_level"], kit["difficulty_level"]) == ("active", "standard")


# -- 5. цель подтверждает учитель ----------------------------------------------


def test_objective_not_shown_to_the_teacher_is_ignored(client, sms, ai, scene):
    """Разбор принёс номер цели, которой учитель не видел, — заказ не меняется:
    подтвердить можно только показанное карточкой (решение 5)."""
    login(client, sms)
    chat = new_chat(client)
    say(client, ai, chat["id"], FIRST_REPLY, then=shows())
    shown = {row["id"] for row in get_chat(client, chat["id"])["items"][1]["tool_result"]["items"]}
    assert scene.first_grade.id not in shown

    for objective_id in (scene.first_grade.id, scene.hidden.id, 999999):
        order = done(say(client, ai, chat["id"], "да", extract={"objective_id": objective_id}))[
            "chat"
        ]["order"]
        assert order["objective"] is None, objective_id


def test_objective_shown_in_another_chat_does_not_count(client, sms, ai, scene):
    login(client, sms)
    seen = new_chat(client)
    say(client, ai, seen["id"], FIRST_REPLY, then=shows())
    other = new_chat(client)

    order = done(say(client, ai, other["id"], "да", extract={"objective_id": scene.objective.id}))[
        "chat"
    ]["order"]

    assert order["objective"] is None


def test_loaded_program_is_invisible(client, sms, ai, scene):
    """Загруженная программа ещё не вычитана: её цели не находит ни модель,
    ни «выбрать самому», а кнопка отвечает 404, как на несуществующую."""
    login(client, sms)
    chat = new_chat(client)

    events = say(
        client, ai, chat["id"], "сравнивать двузначные",
        then=lambda ai: ai.wants("find_objectives", query="сравнивать двузначных").says("Нет"),
    )
    [(_, tool)] = [event for event in events if event[0] == "tool"]
    assert scene.hidden.id not in {row["id"] for row in tool["tool_result"]["items"]}

    listed = client.get("/lesson_ai/objectives", params={"q": "сравнивать двузначных"}).json()
    assert scene.hidden.id not in {row["id"] for row in listed["items"]}

    resp = patch(client, chat["id"], objective_id=scene.hidden.id)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == {"code": "not_found", "message": "Цель обучения не найдена"}
    assert patch(client, chat["id"], objective_id=999999).status_code == 404
    assert get_chat(client, chat["id"])["order"]["objective"] is None


def test_button_accepts_any_published_objective(client, sms, scene):
    """Кнопка — ввод учителя («выбрать самому»): показа не требуется."""
    login(client, sms)
    chat = new_chat(client)

    resp = patch(client, chat["id"], objective_id=scene.within_100.id)

    assert resp.status_code == 200, resp.text
    order = resp.json()["order"]
    assert order["objective"] == item(scene.within_100)
    assert order["missing"] == ["lang", "topic"]


# -- 6. язык и редакции ----------------------------------------------------------


def test_kazakh_class_gets_the_kazakh_row_of_the_same_code(client, sms, ai, scene):
    """Цель подтверждена в русской редакции, а класс казахский — в заказе
    казахская строка приказа того же кода, и комплект казахский целиком."""
    login(client, sms)
    chat = new_chat(client)
    say(client, ai, chat["id"], FIRST_REPLY, then=shows())
    say(client, ai, chat["id"], "да", extract={"objective_id": scene.objective.id})

    order = done(say(client, ai, chat["id"], "қазақша", extract={"lang": "kz"}))["chat"]["order"]

    assert order["lang"] == "kz"
    assert order["objective"] == item(scene.kz_objective, lang="kz")
    assert order["objective"]["id"] != scene.objective.id
    assert order["objective"]["text"] == KZ_TEXT

    assert patch(client, chat["id"], topic="Екі таңбалы сандарды қосу").status_code == 200
    resp = confirm(client, chat["id"])
    assert resp.status_code == 201, resp.text
    kit = resp.json()["kit"]
    assert kit["lang"] == "kz"
    assert kit["objective"]["id"] == scene.kz_objective.id
    assert kit["objective"]["text"] == KZ_TEXT


def test_confirmed_objective_and_language_in_one_reply(client, sms, ai, scene):
    """«Да, она, класс казахский» — цель и язык одной репликой: сначала
    подтверждается показанная русская строка, потом меняется на казахскую."""
    login(client, sms)
    chat = new_chat(client)
    say(client, ai, chat["id"], FIRST_REPLY, then=shows())

    order = done(
        say(
            client, ai, chat["id"], "да, она, класс казахский",
            extract={"objective_id": scene.objective.id, "lang": "kz"},
        )
    )["chat"]["order"]

    assert order["objective"]["id"] == scene.kz_objective.id
    assert order["lang"] == "kz"


def test_objective_picked_after_language_comes_in_its_edition(client, sms, scene):
    login(client, sms)
    chat = new_chat(client)
    assert patch(client, chat["id"], lang="kz").status_code == 200

    order = patch(client, chat["id"], objective_id=scene.objective.id).json()["order"]

    assert order["objective"] == item(scene.kz_objective, lang="kz")


def test_language_button_without_the_kazakh_row_is_refused(client, sms, scene):
    """Казахской строки 2.1.2.7 в опубликованной программе нет (есть только
    в загруженной) — переводить формулировку нельзя, кнопка получает отказ,
    и заказ не меняется."""
    login(client, sms)
    chat = ready_chat(client, scene, objective=scene.ru_only)

    resp = patch(client, chat["id"], lang="kz")

    assert resp.status_code == 409, resp.text
    assert resp.json() == {
        "error": {
            "code": "objective_lang_missing",
            "message": "Цели 2.1.2.7 нет в казахской редакции опубликованной программы",
            "details": {"code": "2.1.2.7", "lang": "kz"},
        }
    }
    order = get_chat(client, chat["id"])["order"]
    assert (order["lang"], order["objective"]["id"]) == ("ru", scene.ru_only.id)

    # И в обратную сторону: язык уже казахский, а кнопка — у русской цели
    chat = new_chat(client)
    assert patch(client, chat["id"], lang="kz").status_code == 200
    resp = patch(client, chat["id"], objective_id=scene.ru_only.id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "objective_lang_missing"
    assert get_chat(client, chat["id"])["order"]["objective"] is None


def test_language_said_in_words_wins_and_drops_the_objective(client, sms, ai, scene):
    """Словами язык побеждает: учитель назвал его только что, а цель ему
    покажут заново уже из нужной редакции (решение 6)."""
    login(client, sms)
    chat = ready_chat(client, scene, objective=scene.ru_only)

    order = done(say(client, ai, chat["id"], "класс казахский", extract={"lang": "kz"}))["chat"][
        "order"
    ]

    assert order["lang"] == "kz"
    assert order["objective"] is None
    assert order["missing"] == ["subject", "grade", "objective"]


def test_counterpart_prefers_the_same_order(client, sms, scene):
    """Казахская строка берётся сперва из того же приказа: вторая
    опубликованная казахская программа по предмету не должна её перебить."""
    login(client, sms)
    other = make_program(scene.subject.id, lang="kz", status="published", order_number="401")
    section = make_section(other.id, "1", "Сандар және шамалар")
    make_objective(other.id, section.id, CODE, text="басқа бұйрықтың мәтіні")
    chat = ready_chat(client, scene)

    order = patch(client, chat["id"], lang="kz").json()["order"]

    assert order["objective"]["id"] == scene.kz_objective.id


def test_language_switch_while_the_program_is_unpublished(client, sms, scene):
    """Владелец снял русскую программу с публикации поправить формулировку,
    учитель тем временем нажал «Қазақша», потом программу опубликовали снова.
    Язык и цель обязаны остаться согласованными (решение 6): казахский заказ —
    казахская строка, иначе завуч получит комплект не на том языке."""
    login(client, sms)
    chat = ready_chat(client, scene)
    set_program_status(scene.ru.id, "loaded")
    assert patch(client, chat["id"], lang="kz").status_code == 200
    set_program_status(scene.ru.id, "published")

    order = get_chat(client, chat["id"])["order"]
    resp = confirm(client, chat["id"])

    assert order["lang"] == "kz"
    assert order["objective"] is None or order["objective"]["lang"] == "kz"
    if resp.status_code == 201:
        assert resp.json()["kit"]["lang"] == "kz"


# -- 7. разбор реплики -----------------------------------------------------------


def test_extract_drops_invalid_values(client, sms, ai, scene):
    """Разбор — прочтение модели, а не ввод учителя: неподходящее молча
    отбрасывается, годное записывается."""
    login(client, sms)
    chat = new_chat(client)

    order = done(
        say(
            client, ai, chat["id"], "игра и план, весь урок быстро",
            extract={
                "engagement_level": "fast",
                "difficulty_level": "hard",
                "class_size": 0,
                "kinds": ["game", "мусор", "lesson_plan", "game", "tasks"],
                "topic": "т" * 201,
                "notes": "  путают разряды  ",
            },
        )
    )["chat"]["order"]

    assert (order["engagement_level"], order["difficulty_level"]) == ("active", "standard")
    assert order["kinds"] == ["lesson_plan", "tasks", "game"]
    assert order["class_size"] is None
    assert order["topic"] is None
    assert order["notes"] == "путают разряды"

    order = done(
        say(
            client, ai, chat["id"], "61 ребёнок",
            extract={"class_size": 61, "kinds": ["мусор"], "topic": "   "},
        )
    )["chat"]["order"]
    assert order["class_size"] is None
    # Список из одного мусора не стирает прежние галочки
    assert order["kinds"] == ["lesson_plan", "tasks", "game"]
    assert order["topic"] is None

    order = done(
        say(
            client, ai, chat["id"], "24 ребёнка, спокойно, класс сильный",
            extract={
                "class_size": 24,
                "engagement_level": "calm",
                "difficulty_level": "advanced",
                "topic": "т" * 200,
                "notes": "24 ребёнка",
            },
        )
    )["chat"]["order"]
    assert order["class_size"] == 24
    assert (order["engagement_level"], order["difficulty_level"]) == ("calm", "advanced")
    assert order["topic"] == "т" * 200
    assert order["notes"] == "путают разряды; 24 ребёнка"

    # То же пожелание второй раз — без повтора
    order = done(say(client, ai, chat["id"], "да, путают", extract={"notes": "путают разряды"}))[
        "chat"
    ]["order"]
    assert order["notes"] == "путают разряды; 24 ребёнка"


def test_broken_extract_does_not_break_the_turn(client, sms, ai, scene):
    """Разбор вернул не JSON или не ответил — ход всё равно идёт, заказ
    не меняется, а расход кривого разбора учтён: он стоил денег."""
    login(client, sms)
    chat = ready_chat(client, scene)
    before = chat["order"]

    events = say(
        client, ai, chat["id"], "на казахском", extract="не json {",
        tokens_in=300, tokens_out=40,
    )

    turn = done(events)
    assert turn["chat"]["order"] == before
    assert [row["role"] for row in turn["chat"]["items"]] == ["user", "assistant"]

    ai.fails()
    ai.says("Урок на каком языке?")
    turn = done(
        read(client.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "казахский"}))
    )
    assert turn["chat"]["order"] == before
    assert turn["chat"]["items"][-1]["content"] == "Урок на каком языке?"

    with OrmSession(get_engine()) as db:
        rows = db.scalars(
            select(LessonAiChatMessage)
            .where(LessonAiChatMessage.role == "assistant")
            .order_by(LessonAiChatMessage.id)
        ).all()
        assert [(row.tokens_in, row.tokens_out) for row in rows] == [(300, 40), (0, 0)]


def test_turn_cost_is_conversation_plus_extract(client, sms, ai, scene):
    login(client, sms)
    chat = new_chat(client)

    def conversation(ai):
        call = AiToolCall(id="call_1", name="find_objectives", arguments={"query": QUERY})
        ai.replies.append(AiReply(text="", tool_calls=(call,), tokens_in=120, tokens_out=15))
        ai.says("Нашёл. Она?", tokens_in=200, tokens_out=25)

    say(client, ai, chat["id"], FIRST_REPLY, then=conversation, tokens_in=900, tokens_out=60)

    with OrmSession(get_engine()) as db:
        row = db.scalars(
            select(LessonAiChatMessage).where(LessonAiChatMessage.role == "assistant")
        ).one()
        assert (row.tokens_in, row.tokens_out) == (900 + 120 + 200, 60 + 15 + 25)


def test_extract_sees_the_order_shown_goals_and_the_reply(client, sms, ai, scene):
    """Разбору уходит заказ, каким его записал сервер, показанные цели
    номерами и последняя реплика — по строгой схеме, без инструментов."""
    login(client, sms)
    chat = new_chat(client)
    say(client, ai, chat["id"], FIRST_REPLY, then=shows())

    say(client, ai, chat["id"], "да, первая")

    call = extract_calls(ai)[-1]
    assert call["system"] == prompt.EXTRACT_SYSTEM
    assert call["schema"] == prompt.EXTRACT_SCHEMA
    assert call["name"] == "lesson_order"
    [message] = call["messages"]
    assert f"{scene.objective.id} — {CODE} — {OBJECTIVE_TEXT}" in message.content
    assert message.content.rstrip().endswith("Учитель: да, первая")


# -- 8. подтверждение ------------------------------------------------------------


def test_confirm_takes_class_size_and_notes_from_the_chat(client, sms, ai, scene):
    login(client, sms)
    chat = ready_chat(client, scene)
    say(
        client, ai, chat["id"], "24 ребёнка, путают разряды",
        extract={"class_size": 24, "notes": "путают разряды"},
    )

    kit = confirm(client, chat["id"]).json()["kit"]

    assert (kit["class_size"], kit["notes"]) == (24, "путают разряды")


def test_confirm_with_null_erases_class_size_and_notes(client, sms, ai, scene):
    login(client, sms)
    chat = ready_chat(client, scene)
    say(
        client, ai, chat["id"], "24 ребёнка, путают разряды",
        extract={"class_size": 24, "notes": "путают разряды"},
    )

    body = confirm(client, chat["id"], class_size=None, notes=None).json()

    assert (body["kit"]["class_size"], body["kit"]["notes"]) == (None, "")
    assert (body["chat"]["order"]["class_size"], body["chat"]["order"]["notes"]) == (None, "")


def test_confirm_with_explicit_values_overrides_the_chat(client, sms, ai, scene):
    login(client, sms)
    chat = ready_chat(client, scene)
    say(client, ai, chat["id"], "24 ребёнка", extract={"class_size": 24, "notes": "разряды"})

    kit = confirm(client, chat["id"], class_size=18, notes="устный счёт").json()["kit"]

    assert (kit["class_size"], kit["notes"]) == (18, "устный счёт")


def test_confirmed_chat_is_closed(client, sms, ai, scene):
    """Чат — про один урок: после «Готовить» ни реплик, ни кнопок, ни второго
    комплекта; в отказе — номер заведённого, чтобы экран на него перешёл."""
    login(client, sms)
    chat = ready_chat(client, scene)
    body = confirm(client, chat["id"]).json()
    kit_id = body["kit"]["id"]
    assert (body["chat"]["status"], body["chat"]["kit_id"]) == ("confirmed", kit_id)
    closed = {
        "error": {
            "code": "chat_confirmed",
            "message": "Заказ уже отправлен — для следующего урока начните новый чат",
            "details": {"kit_id": kit_id},
        }
    }

    responses = [
        client.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "ещё один"}),
        patch(client, chat["id"], topic="Другая тема"),
        confirm(client, chat["id"]),
    ]

    for resp in responses:
        assert resp.status_code == 409, resp.text
        assert resp.json() == closed
    assert count(LessonKit) == 1
    assert get_chat(client, chat["id"])["order"]["topic"] == TOPIC
    assert ai.calls == []


def test_confirm_refused_by_the_kit_scenario_keeps_the_chat_open(client, sms, scene):
    """Ключи без заданий отбивает тот же сценарий, что прямой заказ, — и чат
    остаётся открытым: учитель поправит галочки и нажмёт снова."""
    login(client, sms)
    chat = ready_chat(client, scene)

    resp = confirm(client, chat["id"], kinds=["answer_key"])

    assert resp.status_code == 422, resp.text
    assert [row["field"] for row in resp.json()["error"]["details"]["fields"]] == ["kinds"]
    after = get_chat(client, chat["id"])
    assert (after["status"], after["kit_id"]) == ("collecting", None)
    assert count(LessonKit) == 0
    assert count(LessonAiJob) == 0
    assert confirm(client, chat["id"], kinds=["tasks", "answer_key"]).status_code == 201


def test_unpublished_program_drops_the_objective_from_the_order(client, sms, scene):
    """Программу сняли с публикации после подтверждения цели — генерация по ней
    закрыта, и учитель видит недостачу, а не отказ после «Готовить»."""
    login(client, sms)
    chat = ready_chat(client, scene)
    assert chat["order"]["ready"] is True

    set_program_status(scene.ru.id, "loaded")

    order = get_chat(client, chat["id"])["order"]
    assert order["objective"] is None
    assert order["missing"] == ["subject", "grade", "objective"]
    resp = confirm(client, chat["id"])
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"] == {"missing": ["subject", "grade", "objective"]}
    assert count(LessonKit) == 0


def test_patch_order_edits_and_erases(client, sms, scene):
    """Кнопки: не прислано — не тронуто, прислан null — стёрто."""
    login(client, sms)
    chat = ready_chat(client, scene)

    order = patch(client, chat["id"], topic="  Новая тема  ").json()["order"]
    assert (order["topic"], order["lang"], order["objective"]["id"]) == (
        "Новая тема", "ru", scene.objective.id
    )
    order = patch(client, chat["id"], lang=None, topic="").json()["order"]
    assert (order["lang"], order["topic"]) == (None, None)
    assert order["objective"]["id"] == scene.objective.id

    resp = patch(client, chat["id"], topic="т" * 201)
    assert resp.status_code == 422, resp.text
    assert [row["field"] for row in resp.json()["error"]["details"]["fields"]] == ["topic"]
    resp = patch(client, chat["id"], lang="en")
    assert resp.status_code == 422, resp.text


# -- 9. провайдер не ответил -------------------------------------------------------


class SilentAi:
    """Разбор отвечает, а разговор — нет: сеть, таймаут, отказ по лимиту."""

    def __init__(self):
        self.calls: list[dict] = []

    def complete_json(self, *, system, messages, name, schema) -> AiReply:
        self.calls.append({"schema": schema})
        return AiReply(
            text=json.dumps({**NOTHING, "topic": "Тема из разбора", "lang": "kz"}),
            tokens_in=10,
            tokens_out=10,
        )

    def complete(self, *, system, messages, tools=None) -> AiReply:
        self.calls.append({"tools": tools})
        raise AiUnavailableError("Модель не ответила")


@pytest.fixture
def silent_ai():
    fake = SilentAi()
    app.dependency_overrides[get_lesson_block_ai] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_lesson_block_ai, None)


def test_silent_provider_rolls_the_turn_back(client, sms, silent_ai, scene):
    """Статус уже отдан, поэтому молчание приезжает событием `error`. Хода
    не было: ни реплики в ленте, ни того, что по ней записал разбор."""
    login(client, sms)
    chat = new_chat(client)

    events = read(
        client.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "казахский"})
    )

    assert events == [
        (
            "error",
            {"error": {"code": "ai_failed", "message": "Модель не ответила — попробуйте ещё раз"}},
        )
    ]
    # Разбор отработал до сбоя — и всё равно откатился вместе с репликой
    assert [list(call) for call in silent_ai.calls] == [["schema"], ["tools"]]
    after = get_chat(client, chat["id"])
    assert after["items"] == []
    assert (after["order"]["topic"], after["order"]["lang"]) == (None, None)
    assert count(LessonAiChatMessage) == 0


# -- 10. проверки до стрима ---------------------------------------------------------


def test_text_is_checked_before_the_stream(client, sms, ai, scene):
    login(client, sms)
    chat = new_chat(client)

    for text in ("", "   \n\t", "ы" * 2001):
        resp = client.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": text})
        assert resp.status_code == 422, resp.text
        error = resp.json()["error"]
        assert error["code"] == "validation_error"
        assert [row["field"] for row in error["details"]["fields"]] == ["text"]

    # Ровно 2000 — ещё можно
    say(client, ai, chat["id"], "ы" * 2000)
    assert ai.calls
    assert count(LessonAiChatMessage) == 2


def test_messages_per_hour_cap(client, sms, ai, scene, lesson_chat_limiter_clock):
    login(client, sms)
    chat = new_chat(client)
    user_key = str(client.get("/me").json()["id"])
    limiter = app.dependency_overrides[get_lesson_chat_limiter]()
    cap = get_settings().lesson_ai_chat_messages_per_hour
    # Потолок набирается мимо модели: шестьдесят ходов подставного ИИ ничего
    # не добавили бы к проверке
    for _ in range(cap - 1):
        assert limiter.hit(user_key) == 0
    say(client, ai, chat["id"], "последняя в этом часу")
    calls = len(ai.calls)

    resp = client.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "ещё"})

    assert resp.status_code == 429, resp.text
    error = resp.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["details"]["retry_after_sec"] > 0
    assert len(ai.calls) == calls, "отказ по потолку не должен дойти до модели"
    assert count(LessonAiChatMessage) == 2

    lesson_chat_limiter_clock.shift(3600)
    say(client, ai, chat["id"], "через час")
    assert count(LessonAiChatMessage) == 4


# -- 11. потолок поисков за ход -------------------------------------------------------


def test_tool_call_cap_ends_the_turn_in_words(client, sms, ai, scene):
    login(client, sms)
    chat = new_chat(client)
    cap = get_settings().lesson_ai_chat_max_tool_calls

    def searches_forever(ai):
        for number in range(cap + 1):
            ai.wants("find_objectives", call_id=f"call_{number}", query=f"{QUERY} {number}")
        ai.says("Пока нашёл вот что — уточните урок")

    events = say(client, ai, chat["id"], FIRST_REPLY, then=searches_forever)

    turn = done(events)
    assert turn["stopped_at_cap"] is True
    assert [name for name, _ in events].count("tool") == cap
    last = turn_calls(ai)[-1]
    assert last["tools"] is None
    assert last["messages"][-1] == AiMessage(role="user", content=prompt.CAP_MESSAGE)
    feed = turn["chat"]["items"]
    assert [row["role"] for row in feed] == ["user", *["tool"] * cap, "assistant"]
    assert feed[-1]["content"] == "Пока нашёл вот что — уточните урок"


# -- 12. служебное сообщение «Заказ сейчас» ------------------------------------------------


def test_state_message_goes_right_before_the_reply(client, sms, ai, scene):
    """Модель разговора видит заказ, каким его уже записал сервер, — отдельным
    сообщением перед репликой, а не в системном промпте."""
    login(client, sms)
    chat = new_chat(client)
    assert patch(client, chat["id"], objective_id=scene.objective.id, lang="ru").status_code == 200

    say(client, ai, chat["id"], "а тема какая?")

    call = turn_calls(ai)[-1]
    assert call["tools"] and {tool["function"]["name"] for tool in call["tools"]} == {
        "find_objectives", "get_objective"
    }
    assert call["messages"][-1] == AiMessage(role="user", content="а тема какая?")
    state = call["messages"][-2]
    assert state.role == "system"
    assert state.content.startswith("Заказ сейчас")
    assert f"- цель обучения: {CODE} — {OBJECTIVE_TEXT} (русская редакция, 2 класс)" in (
        state.content
    )
    assert "- язык обучения: русский" in state.content
    assert "- тема: не названа" in state.content
    assert "- как проводим: не спрашивали" in state.content
    assert state.content.endswith("Не хватает: тема.")


def test_state_message_reflects_what_the_extract_just_wrote(client, sms, ai, scene):
    """Разбор идёт первым: модель уже знает тему, сказанную в этой реплике,
    и не спрашивает её второй раз."""
    login(client, sms)
    chat = ready_chat(client, scene, topic=None)

    say(client, ai, chat["id"], TOPIC, extract={"topic": TOPIC, "engagement_level": "calm"})

    state = turn_calls(ai)[-1]["messages"][-2].content
    assert f"- тема: {TOPIC}" in state
    assert "- как проводим: спокойно" in state
    assert "Не хватает" not in state
    assert "Пятеро собраны" in state


def test_system_prompt_is_the_same_for_every_chat(client, sms, ai, scene):
    """Системный промпт вместе с инструментами провайдер берёт из кэша —
    поэтому всё меняющееся живёт в служебном сообщении, а не в нём."""
    login(client, sms)
    first = new_chat(client)
    second = ready_chat(client, scene)

    say(client, ai, first["id"], FIRST_REPLY)
    say(client, ai, second["id"], "что дальше?")

    systems = [call["system"] for call in turn_calls(ai)]
    assert systems == [prompt.SYSTEM, prompt.SYSTEM]
    assert systems[0].encode() == systems[1].encode()
    tools = [call["tools"] for call in turn_calls(ai)]
    assert tools[0] == tools[1]


def test_history_goes_back_to_the_model(client, sms, ai, scene):
    """Следующий ход видит прошлый: реплику, вызов инструмента с результатом
    и ответ — в той форме, которую принимает провайдер."""
    login(client, sms)
    chat = new_chat(client)
    say(client, ai, chat["id"], FIRST_REPLY, then=shows())

    say(client, ai, chat["id"], "да")

    messages = turn_calls(ai)[-1]["messages"]
    assert [message.role for message in messages] == [
        "user", "assistant", "tool", "assistant", "system", "user"
    ]
    assert messages[0].content == FIRST_REPLY
    assert messages[1].tool_calls[0].name == "find_objectives"
    assert messages[2].tool_call_id == messages[1].tool_calls[0].id
    assert json.loads(messages[2].content)["items"][0]["id"] == scene.objective.id


# -- 13. «выбрать самому» ----------------------------------------------------------------


def test_objectives_search(client, sms, scene):
    login(client, sms)

    resp = client.get("/lesson_ai/objectives", params={"q": QUERY})

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items[0] == item(scene.objective)
    assert [row["code"] for row in items] == [CODE, "2.1.2.7", "2.1.2.5"]

    kz = client.get("/lesson_ai/objectives", params={"q": "екі таңбалы сандарды қосу"}).json()
    assert [row["id"] for row in kz["items"]] == [scene.kz_objective.id, scene.kz_within_100.id]
    only_ru = client.get(
        "/lesson_ai/objectives", params={"q": "екі таңбалы сандарды қосу", "lang": "ru"}
    ).json()
    assert only_ru["items"] == []
    first = client.get("/lesson_ai/objectives", params={"q": "считать сложение", "grade": 1})
    assert [row["id"] for row in first.json()["items"]] == [scene.first_grade.id]
    # Запрос из одних слов разговора — пустой список, а не весь справочник
    assert client.get("/lesson_ai/objectives", params={"q": "завтра урок"}).json() == {
        "items": []
    }


def test_objectives_of_a_grade_in_document_order(client, sms, scene):
    login(client, sms)

    ru = client.get("/lesson_ai/objectives", params={"grade": 2, "lang": "ru"}).json()
    kz = client.get("/lesson_ai/objectives", params={"grade": 2, "lang": "kz"}).json()

    assert ru["items"] == [
        item(scene.within_100), item(scene.objective), item(scene.ru_only)
    ]
    assert [row["id"] for row in kz["items"]] == [scene.kz_within_100.id, scene.kz_objective.id]


@pytest.mark.parametrize(
    "params", [{}, {"q": "   "}, {"grade": 2}, {"lang": "ru"}]
)
def test_objectives_need_words_or_a_grade_and_language(client, sms, scene, params):
    login(client, sms)

    resp = client.get("/lesson_ai/objectives", params=params)

    assert resp.status_code == 422, resp.text
    assert [row["field"] for row in resp.json()["error"]["details"]["fields"]] == ["q"]


# -- 14. инструменты напрямую ---------------------------------------------------------


@pytest.fixture
def tools():
    """Вызов инструмента мимо HTTP, как его делает цикл. Сессия закрывается
    фикстурой: оставленная открытой, она держит транзакцию, и следующий
    тест виснет на TRUNCATE."""
    db = OrmSession(get_engine())
    ctx = OrderCtx(LessonChatRepo(db), user_id=1, chat_id=1)

    def call(name: str, **args) -> dict:
        return OrderTools().call(name, args, ctx)

    yield call
    db.close()


def codes(result: dict) -> list[tuple[str, str]]:
    return [(row["code"], row["lang"]) for row in result["items"]]


def test_find_objectives_filters(tools, scene):
    everything = tools("find_objectives", query=QUERY)
    assert everything["items"][0] == item(scene.objective)
    assert ("2.1.2.8", "ru") not in codes(everything)

    assert codes(tools("find_objectives", query="считать сложение", grade=1)) == [
        ("1.1.2.1", "ru")
    ]
    assert codes(tools("find_objectives", query="екі таңбалы сандарды қосу", lang="kz")) == [
        (CODE, "kz"), ("2.1.2.5", "kz")
    ]
    assert tools("find_objectives", query="екі таңбалы сандарды қосу", lang="ru") == {
        "items": []
    }


def test_find_objectives_puts_the_named_code_first(tools, scene):
    result = tools("find_objectives", query="2.1.2.5 сложение вычитание двузначных чисел")

    assert result["items"][0] == item(scene.within_100)
    assert codes(result)[1] == ("2.1.2.5", "kz")


def test_find_objectives_limit(tools, scene):
    section = make_section(scene.ru.id, "3", "Величины")
    for number in range(1, 13):
        make_objective(
            scene.ru.id, section.id, f"3.1.1.{number}", text=f"сравнивать величины {number}"
        )

    assert len(tools("find_objectives", query="сравнивать величины")["items"]) == 5
    assert len(tools("find_objectives", query="сравнивать величины", limit=2)["items"]) == 2
    assert len(tools("find_objectives", query="сравнивать величины", limit=50)["items"]) == 10


@pytest.mark.parametrize(
    "args",
    [
        {"query": ""},
        {"query": "   "},
        {"query": None},
        {"query": QUERY, "grade": 0},
        {"query": QUERY, "grade": 13},
        {"query": QUERY, "grade": "2"},
        {"query": QUERY, "lang": "en"},
        {"query": QUERY, "limit": -1},
        {"query": "завтра урок класс"},
    ],
)
def test_find_objectives_refuses_bad_arguments(tools, scene, args):
    result = tools("find_objectives", **args)

    assert result["error"]["code"] == "bad_arguments", result
    assert "items" not in result


def test_get_objective(tools, scene):
    both = tools("get_objective", code=CODE)
    assert both["items"] == [item(scene.kz_objective, lang="kz"), item(scene.objective)]
    assert tools("get_objective", code=CODE, lang="ru")["items"] == [item(scene.objective)]
    assert tools("get_objective", code="код 2.1.2.6, пожалуйста", lang="kz")["items"] == [
        item(scene.kz_objective, lang="kz")
    ]

    for code in ("9.9.9.9", "2.1.2.8"):
        assert tools("get_objective", code=code)["error"] == {
            "code": "not_found",
            "message": f"Цели {code} в опубликованной программе нет",
        }
    for args in ({"code": "2.1.2"}, {"code": "16.09.2022"}, {"code": 2126}, {}):
        assert tools("get_objective", **args)["error"]["code"] == "bad_arguments", args
    assert tools("get_objective", code=CODE, lang="en")["error"]["code"] == "bad_arguments"


def test_unknown_tool_is_refused(tools, scene):
    result = tools("generate_lesson_plan", topic=TOPIC)

    assert result["error"]["code"] == "unknown_tool"


def test_order_tools_are_exactly_two():
    assert [schema["function"]["name"] for schema in OrderTools().schemas()] == [
        "find_objectives", "get_objective"
    ]
    # Разбор просит у модели ровно те виды и уровни, что знает сервер
    properties = prompt.EXTRACT_SCHEMA["properties"]
    assert properties["kinds"]["items"]["enum"] == list(MATERIAL_KINDS)
    assert set(prompt.EXTRACT_SCHEMA["required"]) == set(properties)

