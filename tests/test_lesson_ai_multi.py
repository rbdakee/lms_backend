"""«Жоспар»: урок по нескольким целям обучения (решение владельца 15.10.2026).

Цели урока — упорядоченный список из одной программы и одного класса, без
потолка по числу; банк выдаёт материал на совпадение набора; чат больше трёх
целей переспрашивает один раз; поиск целей знает, на каком языке они написаны.
"""

import pytest

from app.adapters.curriculum.parser import objectives_language
from app.adapters.db.base import get_engine
from app.adapters.db.lesson_ai_repos import LessonChatRepo
from app.domain.lesson_ai import BANK_KINDS
from app.lesson_ai.order_tools import BROWSE_MAX_CHARS, OrderCtx, OrderTools
from tests.conftest import PHONE, login, login_named
from tests.test_admin_curriculum import make_objective, make_program, make_section, make_subject
from tests.test_lesson_ai_bank import bank, materials
from tests.test_lesson_ai_chat import (
    Scene as ChatScene,
)
from tests.test_lesson_ai_chat import (
    done,
    get_chat,
    new_chat,
    patch,
    say,
    turn_calls,
)
from tests.test_lesson_ai_kits import DRAFT, OTHER_PHONE, TOPIC, Scene, run_next
from tests.test_lesson_ai_order import ALL_KINDS, KindAi, drain

pytestmark = pytest.mark.usefixtures("lesson_ai_open")


class Goals(Scene):
    """Три цели второго класса в одной программе, цель третьего класса и цель
    другого предмета — чтобы каждая проверка отсекала ровно одну."""

    def __init__(self):
        super().__init__()
        self.b = make_objective(
            self.program.id, self.subsection.id, "2.1.2.7", text="решать задачи на сумму"
        )
        self.c = make_objective(
            self.program.id, self.subsection.id, "2.1.2.8", text="сравнивать числа до ста"
        )
        self.third_grade = make_objective(
            self.program.id, self.subsection.id, "3.1.2.1", text="умножать на два"
        )
        other = make_subject(code="music", title="Музыка")
        program = make_program(other.id, lang="ru", status="published")
        section = make_section(program.id, "1", "Слушание")
        self.music = make_objective(program.id, section.id, "2.1.1.1", text="слушать музыку")


@pytest.fixture
def goals() -> Goals:
    return Goals()


def kit(client, ids, **kw):
    body = {"objective_ids": ids, "topic": TOPIC, "kinds": ["lesson_plan"]} | kw
    return client.post("/lesson_ai/kits", json=body)


# -- комплект ----------------------------------------------------------------


def test_kit_keeps_the_teachers_order_and_collapses_duplicates(client, sms, goals):
    login(client, sms)
    resp = kit(client, [goals.b.id, goals.objective.id, goals.b.id, goals.c.id])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert [o["code"] for o in body["objectives"]] == ["2.1.2.7", "2.1.2.6", "2.1.2.8"]
    assert "objective" not in body
    got = client.get(f"/lesson_ai/kits/{body['id']}").json()
    assert [o["id"] for o in got["objectives"]] == [goals.b.id, goals.objective.id, goals.c.id]
    listed = client.get("/lesson_ai/kits").json()["items"][0]
    assert [o["code"] for o in listed["objectives"]] == ["2.1.2.7", "2.1.2.6", "2.1.2.8"]
    # Поиск «Моих уроков» видит любую из целей
    found = client.get("/lesson_ai/kits", params={"q": "2.1.2.8"}).json()
    assert [row["id"] for row in found["items"]] == [body["id"]]


def test_every_goal_reaches_the_model_and_every_code_the_document(client, sms, ai, goals):
    login(client, sms)
    kit_id = kit(client, [goals.c.id, goals.b.id]).json()["id"]
    ai.returns_json(DRAFT)
    assert run_next(ai)

    sent = "\n".join(m.content for m in ai.calls[-1]["messages"])
    assert "сравнивать числа до ста" in sent and "решать задачи на сумму" in sent
    assert sent.index("сравнивать числа до ста") < sent.index("решать задачи на сумму")
    assert "2.1.2.8" not in sent and "2.1.2.7" not in sent
    material = client.get(f"/lesson_ai/kits/{kit_id}").json()["materials"][0]
    assert [o["code"] for o in material["content"]["learning_objectives"]] == [
        "2.1.2.8",
        "2.1.2.7",
    ]


@pytest.mark.parametrize("other", ["third_grade", "music"])
def test_goals_of_other_grade_or_subject_are_refused(client, sms, goals, other):
    login(client, sms)
    resp = kit(client, [goals.objective.id, getattr(goals, other).id])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "objectives_mixed"
    assert client.get("/lesson_ai/kits").json()["total"] == 0


def test_empty_and_unknown_goal_lists_are_refused(client, sms, goals):
    login(client, sms)
    assert kit(client, []).status_code == 422
    assert kit(client, [goals.objective.id, goals.objective.id + 9999]).status_code == 404


# -- банк: набор целей целиком ---------------------------------------------------


def order_as(client, sms, ids, phone):
    login_named(client, sms, phone=phone, last_name="Сейткалиева", first_name="Жанар", school="Л")
    resp = kit(client, ids, kinds=list(ALL_KINDS))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_bank_issues_only_on_the_same_set_in_any_order(client, sms, goals):
    first = order_as(client, sms, [goals.objective.id, goals.b.id], PHONE)
    ai = KindAi()
    drain(ai)
    assert sorted(row["kind"] for row in bank()) == sorted(BANK_KINDS)
    assert {row["objective_key"] for row in bank()} == {f"{goals.objective.id},{goals.b.id}"}
    assert len(materials(first)) == len(ALL_KINDS)

    # Тот же набор в другом порядке — всё из банка, модель молчит, цели в порядке второго учителя
    second = order_as(client, sms, [goals.b.id, goals.objective.id], OTHER_PHONE)
    silent = KindAi()
    drain(silent)
    assert silent.calls == []
    plan = materials(second)["lesson_plan"]
    assert plan["bank_id"] is not None
    assert [o["code"] for o in plan["content"]["learning_objectives"]] == ["2.1.2.7", "2.1.2.6"]

    # Подмножество и надмножество — промах, генерируется заново
    third = order_as(client, sms, [goals.objective.id], "+7 (707) 555-01-03")
    miss = KindAi()
    drain(miss)
    assert miss.names and materials(third)["lesson_plan"]["bank_id"] != plan["bank_id"]


# -- чат -----------------------------------------------------------------------------


class ChatGoals(ChatScene):
    def __init__(self):
        super().__init__()
        section = make_section(self.ru.id, "1.3", "Ещё")
        self.extra = make_objective(self.ru.id, section.id, "2.1.2.9", text="складывать до ста")
        self.fifth = make_objective(self.ru.id, section.id, "2.1.3.1", text="вычитать до ста")


@pytest.fixture
def chat_goals() -> ChatGoals:
    return ChatGoals()


def state_of(ai) -> str:
    return turn_calls(ai)[-1]["messages"][-2].content


def test_several_goals_in_the_order_card_keep_the_order(client, sms, chat_goals):
    login(client, sms)
    chat = new_chat(client)
    ids = [chat_goals.ru_only.id, chat_goals.objective.id, chat_goals.within_100.id]
    order = patch(client, chat["id"], objective_ids=ids).json()["order"]
    assert [o["id"] for o in order["objectives"]] == ids
    assert (order["subject_code"], order["grade"]) == ("math", 2)
    assert "objectives" in order and "objective" not in order


def test_chat_refuses_mixed_goals_on_the_button_and_trims_them_in_the_parse(
    client, sms, ai, chat_goals
):
    login(client, sms)
    chat = new_chat(client)
    resp = patch(
        client, chat["id"], objective_ids=[chat_goals.objective.id, chat_goals.first_grade.id]
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "objectives_mixed"

    # Разбор реплики не падает, а оставляет цели первой программы и класса
    ids = [chat_goals.objective.id, chat_goals.first_grade.id]
    say(
        client, ai, chat["id"], "покажи",
        then=lambda a: a.wants("show_objectives", ids=ids).says("Эти?"),
    )
    turn = done(say(client, ai, chat["id"], "обе", extract={"objective_ids": ids}))
    assert [o["id"] for o in turn["chat"]["order"]["objectives"]] == [chat_goals.objective.id]


def test_more_than_three_goals_ask_once_then_remember_the_yes(client, sms, ai, chat_goals):
    login(client, sms)
    chat = new_chat(client)
    ids = [g.id for g in (chat_goals.objective, chat_goals.ru_only, chat_goals.within_100)]
    patch(client, chat["id"], objective_ids=ids)
    say(client, ai, chat["id"], "тема?")
    assert "Ты ещё не переспрашивал" not in state_of(ai)

    ids.append(chat_goals.extra.id)
    order = patch(client, chat["id"], objective_ids=ids).json()["order"]
    assert order["many_objectives_confirmed"] is False
    say(client, ai, chat["id"], "ещё одна")
    assert "Целей 4, это больше трёх" in state_of(ai)
    assert "Ты ещё не переспрашивал" in state_of(ai)

    turn = done(say(client, ai, chat["id"], "да, столько", extract={"many_confirmed": True}))
    assert turn["chat"]["order"]["many_objectives_confirmed"] is True
    say(client, ai, chat["id"], "дальше")
    assert "уже подтвердил" in state_of(ai) and "не переспрашивай" in state_of(ai)

    # Набор изменился — прежнее «да» к нему не относится
    ids.append(chat_goals.fifth.id)
    order = patch(client, chat["id"], objective_ids=ids).json()["order"]
    assert order["many_objectives_confirmed"] is False
    assert get_chat(client, chat["id"])["order"]["many_objectives_confirmed"] is False


def test_many_goals_rule_lives_in_the_cached_system_prompt():
    from app.lesson_ai.prompts import order_chat

    text = " ".join(order_chat.SYSTEM.split())
    assert "Целей больше трёх — один раз переспроси" in text
    assert "Ответ «да» принимай без спора" in text
    assert "many_confirmed" in order_chat.EXTRACT_SCHEMA["properties"]


def test_confirm_makes_a_kit_with_all_goals(client, sms, chat_goals):
    login(client, sms)
    chat = new_chat(client)
    ids = [chat_goals.ru_only.id, chat_goals.objective.id]
    patch(client, chat["id"], objective_ids=ids, lang="ru", topic=TOPIC)
    resp = client.post(
        f"/lesson_ai/chats/{chat['id']}/confirm",
        json={
            "kinds": ["lesson_plan"],
            "engagement_level": "active",
            "difficulty_level": "standard",
        },
    )
    assert resp.status_code == 201, resp.text
    assert [o["id"] for o in resp.json()["kit"]["objectives"]] == ids


# -- название чата --------------------------------------------------------------------


def titles(client) -> dict[int, str | None]:
    return {row["id"]: row["title"] for row in client.get("/lesson_ai/chats").json()["items"]}


def test_chat_title_follows_the_lesson_not_the_first_message(client, sms, ai, chat_goals):
    login(client, sms)
    chat = new_chat(client)
    cid = chat["id"]
    say(client, ai, cid, "что ты умеешь?")
    assert titles(client)[cid] is None, "первая реплика в название не идёт"

    patch(client, cid, objective_ids=[chat_goals.objective.id])
    title = titles(client)[cid]
    assert title.startswith("Математика, 2 кл.: выполнять устно сложение")
    assert title.endswith("…") or len(title) < 100

    patch(client, cid, topic="Сложение в зоопарке")
    assert titles(client)[cid] == "Сложение в зоопарке"
    patch(client, cid, topic=None)
    assert titles(client)[cid].startswith("Математика, 2 кл.")
    patch(client, cid, objective_ids=None)
    assert titles(client)[cid] is None


# -- инструменты поиска -------------------------------------------------------------


class English:
    def __init__(self):
        self.subject = make_subject(code="english", title="Английский язык")
        self.ru = make_program(self.subject.id, lang="ru", status="published")
        section = make_section(self.ru.id, "3.5", "Грамматика")
        self.verb = make_objective(
            self.ru.id, section.id, "3.5.1.2", text="use the verb to be in simple sentences"
        )
        self.other = make_objective(
            self.ru.id, section.id, "3.5.1.3", text="use let's + verb, verbs go/like + verb + ing"
        )


@pytest.fixture
def english() -> English:
    return English()


@pytest.fixture
def tools():
    from sqlalchemy.orm import Session

    db = Session(get_engine())
    ctx = OrderCtx(LessonChatRepo(db), user_id=1, chat_id=1)

    def call(name: str, **args) -> dict:
        return OrderTools().call(name, args, ctx)

    yield call
    db.close()


def test_objectives_language_is_set_by_the_registry_not_guessed():
    assert objectives_language("english", "ru") == objectives_language("english", "kz") == "en"
    assert objectives_language("german", "kz") == "de"
    assert objectives_language("french", "ru") == "fr"
    assert (objectives_language("math", "ru"), objectives_language("math", "kz")) == ("ru", "kz")


def test_list_subjects_tells_grades_and_goal_language(tools, english, goals):
    items = {row["code"]: row for row in tools("list_subjects")["items"]}
    assert items["english"]["grades"] == {"ru": [3]}
    assert items["english"]["objectives_language"] == {"ru": "en"}
    assert items["math"]["objectives_language"] == {"ru": "ru"}
    assert items["math"]["grades"] == {"ru": [2, 3]}


def test_wrong_language_query_gets_a_hint_and_the_right_one_finds_the_goal(tools, english):
    miss = tools("find_objectives", queries=["глаголы"], subject_code="english")
    assert miss["items"] == []
    assert "английском" in miss["hint"] and "browse_objectives" in miss["hint"]

    hit = tools("find_objectives", queries=["глаголы", "verb"], subject_code="english")
    assert [row["code"] for row in hit["items"]] == ["3.5.1.2", "3.5.1.3"]


def test_find_unites_queries_without_duplicates_and_keeps_old_query(tools, english):
    both = tools("find_objectives", queries=["verb", "let's", "verb"], subject_code="english")
    codes = [row["code"] for row in both["items"]]
    assert sorted(codes) == ["3.5.1.2", "3.5.1.3"]
    assert len(codes) == len(set(codes))
    assert [r["code"] for r in tools("find_objectives", query="sentences")["items"]] == ["3.5.1.2"]


def test_missing_grade_is_named_in_the_hint(tools, english):
    miss = tools("find_objectives", queries=["verb"], subject_code="english", grade=2)
    assert miss["items"] == []
    assert "В 2 классе" in miss["hint"] and "3" in miss["hint"]
    browse = tools("browse_objectives", subject_code="english", grade=2)
    assert browse["sections"] == [] and "В 2 классе" in browse["hint"]


def test_browse_groups_by_section_and_show_returns_cards(tools, english):
    result = tools("browse_objectives", subject_code="english", grade=3)
    assert result["objectives_language"] == "en" and "items" not in result
    [section] = result["sections"]
    assert section["title"] == "Грамматика"
    assert [row["code"] for row in section["items"]] == ["3.5.1.2", "3.5.1.3"]
    ids = [row["id"] for row in section["items"]]
    cards = tools("show_objectives", ids=[ids[1], 999999, ids[0]])
    assert [row["code"] for row in cards["items"]] == ["3.5.1.3", "3.5.1.2"]
    assert cards["items"][0]["text"] == "use let's + verb, verbs go/like + verb + ing"


def test_big_grade_comes_by_sections_first(tools):
    subject = make_subject(code="math", title="Математика")
    program = make_program(subject.id, lang="ru", status="published")
    for number in range(1, 5):
        section = make_section(program.id, str(number), f"Раздел {number}")
        for index in range(1, 40):
            make_objective(
                program.id, section.id, f"2.{number}.1.{index}", text="слово " * 25
            )
    outline = tools("browse_objectives", subject_code="math", grade=2)
    assert [s["count"] for s in outline["sections"]] == [39] * 4
    assert "section" in outline["message"]
    part = tools("browse_objectives", subject_code="math", grade=2, section="раздел 2")
    assert len(part["sections"]) == 1 and len(part["sections"][0]["items"]) == 39
    import json

    assert len(json.dumps(part, ensure_ascii=False)) < BROWSE_MAX_CHARS
    assert tools("browse_objectives", subject_code="math", grade=2, section="нет")["error"]


def test_unknown_subject_is_refused_not_empty(tools, english):
    assert tools("browse_objectives", subject_code="nope", grade=3)["error"]["code"] == (
        "unknown_subject"
    )


def test_questions_about_the_product_are_not_off_topic_in_the_prompt():
    from app.lesson_ai.prompts import order_chat

    text = " ".join(order_chat.SYSTEM.split())
    for phrase in [
        "Вопросы о тебе самом и о продукте — не «не по теме»",
        "умеешь ли на казахском или русском",
        "Сначала отвечай прямо",
        "краткосрочный план (КСП; по-казахски ҚМЖ)",
        "и сразу предложи начать",
    ]:
        assert phrase in text, phrase
    assert "заказ" not in text.replace("Слов «заказ» и «заявка» не произноси", "")
