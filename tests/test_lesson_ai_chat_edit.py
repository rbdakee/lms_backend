"""«Жоспар» как рабочее место: чат живёт после генерации комплекта
(решение владельца 15.10.2026, один чат — одна сессия — один комплект).

Проверяется то, на чём держится режим правки **в коде, а не в промпте**:

- после «Готовить» реплики принимаются, разбора заказа в ходе нет, у модели
  другой промпт и другие инструменты, а в ленте — события о комплекте;
- правка блока из чата идёт через тот же сценарий, что кнопка ИИ и ручная
  правка: та же схема, те же версии с пометкой источника, те же ключи;
- каждая правка — событие в ленте, по которому экран подсветит материал;
- чужой комплект недоступен ни ленте, ни инструментам; лимиты и учёт — прежние.

Ход режима правки — один запрос разговора с инструментами и, если просят
переписать блок, запрос переписчика между ними (`complete_json`): очередь
подставного ИИ одна, поэтому ответы кладутся в порядке вызовов.
"""

import json

import pytest

from app.api.deps import get_lesson_chat_limiter, get_lesson_kits_limiter
from app.lesson_ai import edit_tools
from app.lesson_ai.prompts import edit_chat
from app.main import app
from tests.conftest import login
from tests.test_lesson_ai_blocks import NEW_MIDDLE
from tests.test_lesson_ai_chat import (
    OTHER_PHONE,
    Scene,
    confirm,
    done,
    get_chat,
    new_chat,
    read,
    ready_chat,
    turn_calls,
)
from tests.test_lesson_ai_kits import DRAFT, hurry_retries, order
from tests.test_lesson_ai_queue import fetch
from tests.test_lesson_ai_usage import NOTES, run_next, usage

pytestmark = pytest.mark.usefixtures("lesson_ai_open")

SLIDE = {
    "title": "Проверь себя",
    "lines": ["34 + 25 = ?", "52 + 16 = ?"],
    "visual": "Две строки примеров на доске крупным шрифтом.",
    "notes": "Конец середины урока: дети решают в тетрадях, потом сверяются.",
}
PRESENTATION = {
    "goals": ["Научимся складывать двузначные числа."],
    "slides_beginning": [
        {
            "title": "Вспомним",
            "lines": ["34 = 3 дес. 4 ед."],
            "visual": "Палочки.",
            "notes": "Разминка.",
        }
    ],
    "slides_middle": [
        {"title": "Новое", "lines": ["41 + 25 = 66"], "visual": "Разряды.", "notes": "Объяснение."},
        {"title": "Реши", "lines": ["23 + 14"], "visual": "Таблица.", "notes": "Пара."},
    ],
    "slides_end": [
        {"title": "Итог", "lines": ["Что узнали?"], "visual": "Фишки.", "notes": "Рефлексия."}
    ],
}


@pytest.fixture
def scene() -> Scene:
    return Scene()


# -- хелперы ---------------------------------------------------------------


def speak(client, chat_id: int, text: str = "добавь слайд") -> list[tuple[str, dict]]:
    """Реплика в режиме правки: разбора заказа нет, сразу разговор."""
    return read(client.post(f"/lesson_ai/chats/{chat_id}/messages", json={"text": text}))


def ordered_chat(client, scene: Scene, kinds=("lesson_plan",)) -> dict:
    """Чат, из которого заказан комплект, — с пожеланиями, мимо банка."""
    chat = ready_chat(client, scene)
    resp = confirm(client, chat["id"], kinds=list(kinds), notes=NOTES)
    assert resp.status_code == 201, resp.text
    return resp.json()["chat"]


def generate(ai, *drafts) -> None:
    """Воркер закрывает задания комплекта: по ответу модели на каждый материал."""
    for draft in drafts:
        ai.returns_json(draft)
        assert run_next(ai)


def feed(client, chat_id: int, after_id: int = 0) -> dict:
    resp = client.get(f"/lesson_ai/chats/{chat_id}/messages", params={"after_id": after_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def events(body: dict) -> list[dict]:
    return [row["event"] for row in body["items"] if row["role"] == "event"]


def blocks(client, material_id: int) -> dict:
    items = client.get(f"/lesson_ai/materials/{material_id}/blocks").json()["items"]
    return {item["kind"]: item["content"] for item in items}


def material_id(client, chat_id: int, kind: str) -> int:
    kit = get_chat(client, chat_id)["kit"]
    return next(m["id"] for m in kit["materials"] if m["kind"] == kind)


def versions(client, mid: int, block: str) -> list[dict]:
    return client.get(f"/lesson_ai/materials/{mid}/blocks/{block}/versions").json()["items"]


def tool_result(events_: list[tuple[str, dict]]) -> dict:
    [found] = [data["tool_result"] for name, data in events_ if name == "tool"]
    return found


# -- 1. чат после «Готовить» -----------------------------------------------


def test_chat_keeps_going_after_confirm(client, sms, ai, scene):
    """Реплика в подтверждённом чате принимается; режим — правка; модели
    уходит свой промпт и свои инструменты, а разбора заказа в ходе нет."""
    login(client, sms)
    chat = ordered_chat(client, scene)
    assert (chat["status"], chat["mode"]) == ("confirmed", "edit")
    generate(ai, DRAFT)
    ai.calls.clear()
    ai.says("Какой слайд добавить?")

    body = done(speak(client, chat["id"], "привет"))

    assert body["chat"]["mode"] == "edit"
    assert body["chat"]["items"][-1]["content"] == "Какой слайд добавить?"
    [call] = ai.calls
    assert "schema" not in call
    assert call["system"] == edit_chat.SYSTEM
    assert [t["function"]["name"] for t in call["tools"]] == [
        edit_tools.GET_MATERIAL,
        edit_tools.GET_BLOCK,
        edit_tools.REWRITE,
        edit_tools.ADD,
        edit_tools.REPLACE,
        edit_tools.DELETE,
        edit_tools.SET_OBJECTIVES,
        edit_tools.REGENERATE,
        *edit_tools.SEARCH_TOOLS,
    ]
    # Изменчивое — в служебном сообщении, а не в системном промпте
    state = next(m.content for m in call["messages"] if m.role == "system")
    assert state.startswith("Комплект сейчас") and "lesson_plan" in state and "готов" in state
    assert call["system"] == edit_chat.SYSTEM


def test_system_prompt_is_the_same_for_every_edit_chat(client, sms, ai, scene):
    """Кэш провайдера держится на побайтно одинаковом начале запроса."""
    login(client, sms)
    first = ordered_chat(client, scene)
    generate(ai, DRAFT)
    speak(client, first["id"])
    second = ordered_chat(client, scene)
    generate(ai, DRAFT)
    speak(client, second["id"], "ещё один")

    systems = {call["system"] for call in turn_calls(ai)}
    assert systems == {edit_chat.SYSTEM}


def test_feed_has_kit_events_in_order(client, sms, ai, scene):
    """«Комплект готовится» пишет «Готовить», «готов» — воркер, закрывший
    последнее задание; опрос `after_id` отдаёт только новое."""
    login(client, sms)
    chat = ordered_chat(client, scene)
    first = feed(client, chat["id"])
    assert [e["status"] for e in events(first)] == ["generating"]
    assert first["items"][0]["content"] == "Комплект готовится — это займёт несколько минут"
    assert (first["mode"], first["kit"]["status"]) == ("edit", "generating")

    generate(ai, DRAFT)

    last_id = first["items"][-1]["id"]
    news = feed(client, chat["id"], after_id=last_id)
    assert [e["status"] for e in events(news)] == ["ready"]
    assert news["items"][0]["event"]["type"] == "kit_status"
    assert news["items"][0]["content"] == "Комплект готов — материалы справа"
    assert news["kit"]["status"] == "ready"
    assert [m["status"] for m in news["kit"]["materials"]] == ["ready"]
    # Повторный опрос от последней строки — пусто
    assert feed(client, chat["id"], after_id=news["items"][-1]["id"])["items"] == []


def test_failed_material_is_a_partial_event(client, sms, ai, scene):
    """Один вид не собрался — комплект «собран не целиком», и в событии видно,
    какой именно."""
    login(client, sms)
    chat = ordered_chat(client, scene, kinds=("lesson_plan", "presentation"))
    generate(ai, DRAFT)
    ai.fails().fails().fails()
    assert run_next(ai)
    hurry_retries()
    assert run_next(ai)
    hurry_retries()
    assert run_next(ai)

    last = events(feed(client, chat["id"]))[-1]

    assert last["status"] == "partial" and last["failed_kinds"] == ["presentation"]
    row = feed(client, chat["id"])["items"][-1]
    assert row["content"] == "Комплект собран не целиком — не получилось: Презентация"


def test_direct_kit_has_no_chat_events(client, sms, ai, scene):
    """Комплект, заказанный не из чата, никаких событий не пишет."""
    login(client, sms)
    assert order(client, scene.objective.id, notes=NOTES).status_code == 201
    generate(ai, DRAFT)
    assert fetch("SELECT id FROM lesson_ai_chat_message") == []


# -- 2. правка из чата -----------------------------------------------------


def test_add_item_changes_the_block_and_writes_an_event(client, sms, ai, scene):
    """«Добавь шаг» — add_item: блок меняется той же правкой, что ручная
    (версия с пометкой `edited`), а в ленте — событие с местом изменения."""
    login(client, sms)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)
    mid = material_id(client, chat["id"], "lesson_plan")
    before = blocks(client, mid)
    step = {
        "teacher": "Даёт пары примеров 52 + 16 и 34 + 25 для самостоятельной работы.",
        "students": "Решают в тетрадях и сверяются в парах.",
        "resources": ["тетради"],
    }
    ai.wants("add_item", kind="lesson_plan", block="stage_middle", item=step, position=None)
    ai.says("Добавил шаг в середину урока.")

    events_ = speak(client, chat["id"], "добавь в середину самостоятельную работу")

    after = blocks(client, mid)
    assert after["stage_middle"] == [*before["stage_middle"], step]
    # Соседние блоки не тронуты
    assert {k: v for k, v in after.items() if k != "stage_middle"} == {
        k: v for k, v in before.items() if k != "stage_middle"
    }
    [version, *_] = versions(client, mid, "stage_middle")
    assert (version["source"], version["content"]) == ("edited", after["stage_middle"])
    changed = [data for name, data in events_ if name == "material_changed"]
    expected = {
        "type": "block_changed",
        "action": "item_added",
        "material_id": mid,
        "kind": "lesson_plan",
        "block": "stage_middle",
        "index": 2,
        "slide": None,
    }
    assert changed == [expected]
    rows = done(events_)["chat"]["items"][-4:]
    assert [r["role"] for r in rows] == ["user", "tool", "event", "assistant"]
    assert rows[2]["event"] == expected
    assert rows[2]["content"] == "Краткосрочный план: добавлен пункт 2 блока «stage_middle»"


def test_slide_number_runs_through_the_whole_presentation(client, sms, ai, scene):
    """Слайды лежат тремя блоками, а учитель считает подряд: титульный и
    «Сегодня на уроке» — первые два."""
    login(client, sms)
    chat = ordered_chat(client, scene, kinds=("lesson_plan", "presentation"))
    generate(ai, DRAFT, PRESENTATION)
    mid = material_id(client, chat["id"], "presentation")
    ai.wants("add_item", kind="presentation", block="slides_middle", item=SLIDE, position=2)
    ai.says("Готово")

    events_ = speak(client, chat["id"], "добавь слайд проверь себя")

    [changed] = [d for n, d in events_ if n == "material_changed"]
    # титульный 1, цели 2, начало 3, середина: «Новое» 4, новый 5
    assert (changed["index"], changed["slide"]) == (2, 5)
    assert blocks(client, mid)["slides_middle"][1] == SLIDE
    last = done(events_)["chat"]["items"][-2]
    assert last["content"] == "Презентация: добавлен слайд 5"


def test_replace_and_delete_item(client, sms, ai, scene):
    login(client, sms)
    chat = ordered_chat(client, scene, kinds=("lesson_plan", "presentation"))
    generate(ai, DRAFT, PRESENTATION)
    mid = material_id(client, chat["id"], "presentation")
    ai.wants("replace_item", kind="presentation", block="slides_middle", index=1, item=SLIDE)
    ai.wants("delete_item", call_id="call_2", kind="presentation", block="slides_middle", index=2)
    ai.says("Заменил первый слайд середины и убрал второй.")

    events_ = speak(client, chat["id"], "замени слайд 4 и убери 5")

    assert blocks(client, mid)["slides_middle"] == [SLIDE]
    assert [
        (e["action"], e["slide"])
        for e in events(feed(client, chat["id"]))
        if e["type"] == "block_changed"
    ] == [
        ("item_replaced", 4),
        ("item_deleted", 5),
    ]
    assert {v["source"] for v in versions(client, mid, "slides_middle")[:2]} == {"edited"}
    assert done(events_)["stopped_at_cap"] is False


def test_rewrite_block_uses_the_same_rewriter_as_the_ai_button(client, sms, ai, scene):
    """Переписывание из чата — тот же механизм, что звёздочка на блоке: версия
    `regenerated` с просьбой учителя, расход — строкой `block`."""
    login(client, sms)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)
    mid = material_id(client, chat["id"], "lesson_plan")
    before = blocks(client, mid)
    ai.wants(
        "rewrite_block",
        kind="lesson_plan",
        block="stage_middle",
        instruction="сделай середину подвижнее, с игрой по рядам",
    )
    ai.returns_json({"stage_middle": NEW_MIDDLE})
    ai.says("Переписал середину урока.", tokens_in=300, tokens_out=20)

    events_ = speak(client, chat["id"], "сделай середину подвижнее")

    assert blocks(client, mid)["stage_middle"] == NEW_MIDDLE
    [version, *_] = versions(client, mid, "stage_middle")
    assert (version["source"], version["instruction"]) == (
        "regenerated",
        "сделай середину подвижнее, с игрой по рядам",
    )
    assert before["stage_middle"] != NEW_MIDDLE
    [changed] = [d for n, d in events_ if n == "material_changed"]
    assert (changed["action"], changed["block"], changed["index"]) == (
        "rewritten",
        "stage_middle",
        None,
    )
    sources = [row["source"] for row in usage()]
    assert sources.count("block") == 1 and sources.count("chat") == 1
    # Переписчик получил тот же системный промпт вида, что и кнопка ИИ
    [rewrite_call] = [c for c in ai.calls if c.get("name") == "lesson_plan_stage_middle"]
    assert "сделай середину подвижнее" in rewrite_call["messages"][0].content


def test_refusals_are_read_by_the_model_and_change_nothing(client, sms, ai, scene):
    """Блок сервера, пустой список, несуществующий номер, код цели в тексте,
    пункт не по схеме — отказы инструмента: материал остаётся как был."""
    login(client, sms)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)
    mid = material_id(client, chat["id"], "lesson_plan")
    before = blocks(client, mid)
    calls = [
        (
            "rewrite_block",
            dict(kind="lesson_plan", block="learning_objectives", instruction="x"),
            "block_locked",
        ),
        ("get_block", dict(kind="lesson_plan", block="section"), "block_locked"),
        (
            "delete_item",
            dict(kind="lesson_plan", block="stage_middle", index=1),
            "validation_error",
        ),
        ("delete_item", dict(kind="lesson_plan", block="stage_middle", index=5), "bad_arguments"),
        (
            "add_item",
            dict(kind="lesson_plan", block="stage_middle", item={"teacher": "x"}),
            "validation_error",
        ),
        (
            "add_item",
            dict(kind="lesson_plan", block="lesson_goals", item="Цель 2.1.2.6"),
            "bad_arguments",
        ),
        ("add_item", dict(kind="lesson_plan", block="values", item="x"), "not_a_list"),
        ("make_everything", dict(kind="lesson_plan"), "unknown_tool"),
    ]
    for number, (name, args, _code) in enumerate(calls, start=1):
        ai.wants(name, call_id=f"call_{number}", **args)
    ai.says("Не получилось.")

    events_ = speak(client, chat["id"], "сломай всё")

    results = [d["tool_result"] for n, d in events_ if n == "tool"]
    assert [r["error"]["code"] for r in results] == [c[2] for c in calls]
    assert blocks(client, mid) == before
    assert not [d for n, d in events_ if n == "material_changed"]
    assert events(feed(client, chat["id"]))[-1]["type"] == "kit_status"


def test_unknown_material_block_and_kind_are_refusals(client, sms, ai, scene):
    login(client, sms)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)
    ai.wants("add_item", call_id="c1", kind="lesson_plan", block="nope", item="x")
    ai.wants("get_material", call_id="c2", kind="game")
    ai.wants("get_material", call_id="c3", kind="video")
    ai.says("Нет такого.")

    events_ = speak(client, chat["id"], "покажи игру")

    codes = [d["tool_result"]["error"]["code"] for n, d in events_ if n == "tool"]
    assert codes == ["no_block", "no_material", "bad_arguments"]


def test_get_material_is_short_and_hides_server_blocks(client, sms, ai, scene):
    login(client, sms)
    chat = ordered_chat(client, scene, kinds=("lesson_plan", "presentation"))
    generate(ai, DRAFT, PRESENTATION)
    ai.wants("get_material", kind="presentation")
    ai.says("Вот оглавление.")

    result = tool_result(speak(client, chat["id"], "что в презентации?"))

    by_name = {b["block"]: b for b in result["blocks"]}
    assert by_name["slides_middle"] == {
        "block": "slides_middle",
        "editable": True,
        "type": "list",
        "count": 2,
        "items": ["Новое", "Реши"],
    }
    assert result["kind"] == "presentation"
    # Текст слайдов в оглавление не попадает
    assert "41 + 25" not in json.dumps(result, ensure_ascii=False)


def test_get_block_never_shows_objective_codes(client, sms, ai, scene):
    """Учитель мог вписать код цели в блок руками — модель его не видит,
    как и при перегенерации."""
    login(client, sms)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)
    mid = material_id(client, chat["id"], "lesson_plan")
    client.put(
        f"/lesson_ai/materials/{mid}/blocks/values", json={"content": "Цель 2.1.2.6, уважение."}
    )
    ai.wants("get_block", kind="lesson_plan", block="values")
    ai.says("Прочёл.")

    result = tool_result(speak(client, chat["id"], "покажи ценности"))

    assert "2.1.2.6" not in json.dumps(result, ensure_ascii=False)


def test_material_not_ready_while_generating(client, sms, ai, scene):
    login(client, sms)
    chat = ordered_chat(client, scene)
    ai.wants("add_item", kind="lesson_plan", block="stage_middle", item="x")
    ai.says("Подождём.")

    events_ = speak(client, chat["id"], "добавь шаг")

    assert tool_result(events_)["error"]["code"] == "material_not_ready"
    state = next(m.content for m in turn_calls(ai)[0]["messages"] if m.role == "system")
    assert "lesson_plan (краткосрочный план (КСП; по-казахски ҚМЖ)): ждёт очереди" in state


def test_edit_events_do_not_reach_the_model(client, sms, ai, scene):
    """Лента для экрана богаче, чем история для модели: события в запрос не идут."""
    login(client, sms)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)
    ai.says("Привет")
    speak(client, chat["id"], "первая")
    ai.says("Ещё раз")
    speak(client, chat["id"], "вторая")

    history = turn_calls(ai)[-1]["messages"]
    assert all("Комплект готов" not in (m.content or "") for m in history)
    assert [m.role for m in history] == ["user", "assistant", "system", "user"]


# -- 3. доступ, лимиты, учёт -----------------------------------------------


def test_foreign_edit_chat_is_404(client, client2, sms, ai, scene):
    login(client, sms)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)
    mid = material_id(client, chat["id"], "lesson_plan")
    login(client2, sms, phone=OTHER_PHONE)
    ai.calls.clear()

    for resp in [
        client2.get(f"/lesson_ai/chats/{chat['id']}/messages"),
        client2.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "удали всё"}),
        client2.get(f"/lesson_ai/chats/{chat['id']}"),
        client2.get(f"/lesson_ai/materials/{mid}/blocks"),
    ]:
        assert resp.status_code == 404, resp.text
    assert ai.calls == []


def test_tools_reach_only_the_kit_of_their_own_chat(client, client2, sms, ai, scene):
    """У инструментов нет номера комплекта: правка из чата второго учителя
    идёт только в его собственный комплект, а чат без заказа — режим заказа,
    инструментов правки у него нет."""
    login(client, sms)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)
    mid = material_id(client, chat["id"], "lesson_plan")
    login(client2, sms, phone=OTHER_PHONE)
    mine = ordered_chat(client2, scene)
    generate(ai, DRAFT)
    ai.wants("add_item", kind="lesson_plan", block="lesson_goals", item="Цель из чата второго.")
    ai.says("Готово")

    speak(client2, mine["id"], "добавь цель")

    assert "Цель из чата второго." not in blocks(client, mid)["lesson_goals"]
    own = blocks(client2, material_id(client2, mine["id"], "lesson_plan"))
    assert "Цель из чата второго." in own["lesson_goals"]

    empty = new_chat(client)
    ai.calls.clear()
    ai.says("Расскажите об уроке")
    path = f"/lesson_ai/chats/{empty['id']}/messages"
    assert client.post(path, json={"text": "что в плане?"}).status_code == 200
    assert ai.calls[-1]["system"] != edit_chat.SYSTEM


def test_edit_turns_are_limited_like_order_turns(client, sms, ai, scene):
    login(client, sms)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)
    limiter = app.dependency_overrides[get_lesson_chat_limiter]()
    uid = fetch('SELECT id FROM "user"')[0]["id"]
    while limiter.hit(str(uid)) == 0:
        pass
    ai.calls.clear()

    resp = client.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "ещё слайд"})

    assert resp.status_code == 429, resp.text
    assert ai.calls == []


def test_edit_turn_spends_tokens_as_chat(client, sms, ai, scene):
    login(client, sms)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)
    before = len(usage("chat"))
    ai.says("Привет", tokens_in=500, tokens_out=40)

    speak(client, chat["id"], "привет")

    rows = usage("chat")
    assert len(rows) == before + 1
    assert (rows[-1]["tokens_in"], rows[-1]["tokens_out"], rows[-1]["chat_id"]) == (
        500,
        40,
        chat["id"],
    )


def test_edits_do_not_touch_the_daily_kit_limit(client, sms, ai, scene):
    """Правки из чата — не комплекты: суточный счётчик заказов стоит на месте."""
    login(client, sms)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)
    kits = app.dependency_overrides[get_lesson_kits_limiter]()
    ordered_count = len(kits.hits[str(fetch('SELECT id FROM "user"')[0]["id"])])
    ai.wants("add_item", kind="lesson_plan", block="lesson_goals", item="Цель.")
    ai.says("Готово")

    speak(client, chat["id"], "добавь цель")

    assert len(kits.hits[str(fetch('SELECT id FROM "user"')[0]["id"])]) == ordered_count == 1


def test_order_actions_stay_closed_after_confirm(client, sms, ai, scene):
    """Кнопки заказа и повторное «Готовить» по-прежнему отвечают 409: чат
    продолжается, но заказ уже отправлен."""
    login(client, sms)
    chat = ordered_chat(client, scene)
    kit_id = chat["kit_id"]

    for resp in [
        client.patch(f"/lesson_ai/chats/{chat['id']}/order", json={"topic": "Другая тема"}),
        confirm(client, chat["id"]),
    ]:
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["details"] == {"kit_id": kit_id}


# -- 4. боковая панель -----------------------------------------------------


def test_chat_list_rows_for_the_sidebar(client, sms, ai, scene):
    login(client, sms)
    empty = new_chat(client)
    chat = ordered_chat(client, scene)
    generate(ai, DRAFT)

    rows = {r["id"]: r for r in client.get("/lesson_ai/chats").json()["items"]}

    assert rows[empty["id"]]["title"] is None
    assert (rows[empty["id"]]["mode"], rows[empty["id"]]["kit_status"]) == ("order", None)
    row = rows[chat["id"]]
    assert (row["mode"], row["kit_status"], row["kit_id"]) == ("edit", "ready", chat["kit_id"])
    assert row["title"] == row["topic"]
    # Комплект собрался позже заказа: чат поднялся наверх истории
    assert list(rows)[0] == chat["id"]


def test_chat_out_carries_the_kit(client, sms, ai, scene):
    login(client, sms)
    chat = ordered_chat(client, scene, kinds=("lesson_plan", "presentation"))
    generate(ai, DRAFT)

    body = get_chat(client, chat["id"])

    assert body["kit"]["id"] == chat["kit_id"] == body["kit_id"]
    assert [(m["kind"], m["status"]) for m in body["kit"]["materials"]] == [
        ("lesson_plan", "ready"),
        ("presentation", "queued"),
    ]
    assert body["kit"]["status"] == "generating"
