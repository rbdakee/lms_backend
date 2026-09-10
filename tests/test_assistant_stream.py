"""Стрим ответа и учёт токенов — фаза B3.

Здесь проверяется ровно то, ради чего стрим включали: карточка вызова уходит
на экран **в момент вызова**, а не после всего ответа, и текст приходит
кусками. Плюс две вещи, которые ломаются молча:

- **расход в стриме приходится просить** (`stream_options.include_usage`).
  Не попросили — токены стали бы нулями, и счёт за месяц объяснить нечем;
- **статус отдан до первого события**, поэтому молчание провайдера уже нельзя
  сообщить кодом 502: оно приезжает событием `error`, и ход при этом
  не состоялся.

Живого хода в OpenAI здесь нет: подставной провайдер отдаёт куски сам.
"""

import pytest

from app.api.deps import get_ai
from app.application.ports import AiDelta, AiReply, AiToolCall
from app.main import app
from tests.conftest import login_admin, make_course
from tests.test_assistant_chat import events, new_thread, read, turn_of


class StreamingAi:
    """Провайдер, который умеет стрим: тот же сценарий, что у `FakeAi`,
    но текст отдаётся кусками, как это делает настоящий."""

    def __init__(self):
        self.replies: list[AiReply] = []
        self.calls: list[dict] = []

    def says(self, text: str = "Готово", **kw) -> "StreamingAi":
        self.replies.append(AiReply(text=text, **kw))
        return self

    def wants(self, name: str, call_id: str = "call_1", **arguments) -> "StreamingAi":
        self.replies.append(
            AiReply(text="", tool_calls=(AiToolCall(id=call_id, name=name, arguments=arguments),))
        )
        return self

    def complete(self, *, system, messages, tools=None) -> AiReply:
        """Обычным вызовом идёт только заголовок чата: стрим ему не нужен."""
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        return self.replies.pop(0) if self.replies else AiReply(text="Готово")

    def stream(self, *, system, messages, tools=None):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        reply = self.replies.pop(0) if self.replies else AiReply(text="Готово")
        # Куски по словам: настоящий провайдер режет мельче, но нам важно,
        # что их больше одного и что они приходят до готового ответа
        for word in reply.text.split():
            yield AiDelta(text=word + " ")
        yield AiDelta(reply=reply)


@pytest.fixture
def stream_ai():
    fake = StreamingAi()
    app.dependency_overrides[get_ai] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_ai, None)


def test_answer_arrives_in_pieces(client, sms, stream_ai):
    login_admin(client, sms)
    thread = new_thread(client)
    stream_ai.says("Открытых курсов шесть", tokens_in=900, tokens_out=12)

    got = events(client, thread["id"], "какие курсы открыты?")

    names = [name for name, _ in got]
    assert names.count("delta") == 3, "текст обязан приехать кусками, а не разом"
    assert names[-1] == "done"
    assert "".join(data["text"] for name, data in got if name == "delta").strip() == (
        "Открытых курсов шесть"
    )


def test_tool_card_arrives_before_the_answer(client, sms, stream_ai):
    """Иначе на длинной задаче экран молчит полминуты."""
    login_admin(client, sms)
    thread = new_thread(client)
    make_course(title="Критериальное оценивание")
    stream_ai.wants("courses_list", status="open").says("Один курс.")

    names = [name for name, _ in events(client, thread["id"], "какие курсы?")]

    assert names.index("tool") < names.index("delta") < names.index("done")


def test_tool_event_carries_the_card_as_it_lands_in_the_feed(client, sms, stream_ai):
    login_admin(client, sms)
    thread = new_thread(client)
    make_course(title="Критериальное оценивание")
    stream_ai.wants("courses_list", status="open").says("Один курс.")

    got = events(client, thread["id"], "какие курсы?")
    card = next(data for name, data in got if name == "tool")
    done = next(data for name, data in got if name == "done")

    assert card["tool_name"] == "courses_list"
    assert card["tool_args"] == {"status": "open"}
    assert card["tool_result"]["total"] == 1
    # То же самое лежит в ленте: событие — это не отдельная правда
    in_feed = next(row for row in done["items"] if row["role"] == "tool")
    assert (in_feed["tool_name"], in_feed["tool_args"]) == (card["tool_name"], card["tool_args"])


def test_tokens_from_the_stream_reach_the_feed_and_the_done(client, sms, stream_ai):
    """Расход в стриме приходит служебным событием провайдера, и потерять его
    легко: тогда счёт за месяц объяснить будет нечем."""
    login_admin(client, sms)
    thread = new_thread(client)
    stream_ai.says("Готово.", tokens_in=26332, tokens_out=83)

    done = turn_of(
        client.post(
            f"/admin/assistant/threads/{thread['id']}/messages", json={"text": "привет"}
        )
    )

    answer = done["items"][-1]
    assert (answer["tokens_in"], answer["tokens_out"]) == (26332, 83)
    assert (done["tokens_in"], done["tokens_out"]) == (26332, 83)


def test_tokens_of_a_turn_with_tools_are_summed(client, sms, stream_ai):
    """Ход с вызовом — это два запроса к модели, и платим мы за оба."""
    login_admin(client, sms)
    thread = new_thread(client)
    make_course()
    stream_ai.replies.append(
        AiReply(
            text="",
            tool_calls=(AiToolCall(id="call_1", name="courses_list", arguments={}),),
            tokens_in=800,
            tokens_out=15,
        )
    )
    stream_ai.says("Один курс.", tokens_in=1200, tokens_out=20)

    done = turn_of(
        client.post(f"/admin/assistant/threads/{thread['id']}/messages", json={"text": "курсы?"})
    )

    assert (done["tokens_in"], done["tokens_out"]) == (2000, 35)
    assert done["items"][-1]["tokens_in"] == 2000


def test_a_broken_stream_is_not_a_turn(client, sms, stream_ai):
    """Стрим оборвался, не дойдя до ответа целиком: ход не состоялся, и в ленте
    не остаётся ни вопроса админа, ни половины вызовов."""
    login_admin(client, sms)
    thread = new_thread(client)

    class Broken(StreamingAi):
        def stream(self, *, system, messages, tools=None):
            yield AiDelta(text="начал отвеч")

    app.dependency_overrides[get_ai] = Broken
    try:
        got = events(client, thread["id"], "привет")
    finally:
        app.dependency_overrides.pop(get_ai, None)

    assert [name for name, _ in got] == ["delta", "error"]
    assert got[-1][1]["error"]["code"] == "ai_failed"
    assert client.get(f"/admin/assistant/threads/{thread['id']}/messages").json()["items"] == []


def test_the_handle_answers_as_an_event_stream(client, sms, stream_ai):
    login_admin(client, sms)
    thread = new_thread(client)

    resp = client.post(
        f"/admin/assistant/threads/{thread['id']}/messages", json={"text": "привет"}
    )

    assert resp.headers["content-type"].startswith("text/event-stream")
    # Ответ буферизуется прокси, если не сказать обратного, — и стрима не будет
    assert resp.headers["x-accel-buffering"] == "no"
    assert [name for name, _ in read(resp)][-1] == "done"


def test_limit_is_still_an_ordinary_refusal(client, sms, stream_ai):
    """Отказ по потолку обязан приезжать кодом 429 с числом секунд: он случается
    до начала стрима, и прятать его в тело `200` было бы враньём про статус."""
    login_admin(client, sms)
    thread = new_thread(client)
    url = f"/admin/assistant/threads/{thread['id']}/messages"
    from app.config import get_settings

    for _ in range(get_settings().assistant_messages_per_hour):
        assert client.post(url, json={"text": "ещё"}).status_code == 200

    resp = client.post(url, json={"text": "ещё"})
    assert resp.status_code == 429
    assert resp.json()["error"]["details"]["retry_after_sec"] > 0


# -- заголовок чата --------------------------------------------------------


def test_the_assistant_titles_the_chat_itself(client, sms, stream_ai):
    """После первого ответа чат подписывает модель, а не первые 60 символов
    вопроса (ASSISTANT_BRIEF, раздел 8)."""
    login_admin(client, sms)
    thread = new_thread(client)
    stream_ai.says("Открытых курсов шесть.")
    # Второй ответ — на отдельный дешёвый запрос за заголовком
    stream_ai.says("«Открытые курсы».")

    done = turn_of(
        client.post(
            f"/admin/assistant/threads/{thread['id']}/messages",
            json={"text": "какие у нас сейчас открыты курсы, посмотри пожалуйста"},
        )
    )

    assert done["thread"]["title"] == "Открытые курсы"
    # Заголовок пишется один раз: второй ход его не переписывает
    stream_ai.says("Ещё что-то.")
    again = turn_of(
        client.post(
            f"/admin/assistant/threads/{thread['id']}/messages", json={"text": "а ещё?"}
        )
    )
    assert again["thread"]["title"] == "Открытые курсы"


def test_a_title_costs_its_own_cheap_request(client, sms, stream_ai):
    """Заголовок спрашивается **без** инструментов: системный промпт хода —
    это описания 68 инструментов, и платить за них второй раз незачем."""
    login_admin(client, sms)
    thread = new_thread(client)
    stream_ai.says("Готово.")
    stream_ai.says("Короткий заголовок")

    client.post(f"/admin/assistant/threads/{thread['id']}/messages", json={"text": "привет"})

    last = stream_ai.calls[-1]
    assert last["tools"] is None
    assert "courses_list" not in last["system"]
    assert len(last["system"]) < 600


def test_a_silent_provider_leaves_the_fallback_title(client, sms, stream_ai):
    """Заголовок — украшение списка: его неудача не отнимает состоявшийся ход."""
    login_admin(client, sms)
    thread = new_thread(client)
    stream_ai.says("Ответ на месте.")

    class Mute(StreamingAi):
        def complete(self, *, system, messages, tools=None):
            from app.adapters.ai.openai_chat import AiUnavailableError

            raise AiUnavailableError("нет связи")

    mute = Mute()
    mute.says("Ответ на месте.")
    app.dependency_overrides[get_ai] = lambda: mute
    try:
        done = turn_of(
            client.post(
                f"/admin/assistant/threads/{thread['id']}/messages",
                json={"text": "сколько заявок ждут больше трёх дней"},
            )
        )
    finally:
        app.dependency_overrides.pop(get_ai, None)

    assert done["thread"]["title"] == "сколько заявок ждут больше трёх дней"
    assert done["items"][-1]["content"] == "Ответ на месте."
