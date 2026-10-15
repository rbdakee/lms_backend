"""«Жоспар»: сайдбар чатов — закрепление, группы, порядок, архив (15.10.2026)."""

# ruff: noqa: F811, F401
# Фикстура `scene` берётся из тестов чата: pytest находит её в модуле по имени

import pytest

from tests.conftest import login
from tests.test_lesson_ai_chat import OTHER_PHONE, new_chat, say, scene

pytestmark = pytest.mark.usefixtures("lesson_ai_open")


def listing(client, **params):
    resp = client.get("/lesson_ai/chats", params={"per_page": 100, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def ids(client, **params):
    return [row["id"] for row in listing(client, **params)]


def chat_patch(client, chat_id, **body):
    return client.patch(f"/lesson_ai/chats/{chat_id}", json=body)


def group(client, name="Математика"):
    resp = client.post("/lesson_ai/chat_groups", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def three(client, sms):
    login(client, sms)
    return [new_chat(client)["id"] for _ in range(3)]


def test_new_chats_are_fresh_first_and_plain(client, three):
    a, b, c = three
    assert ids(client) == [c, b, a]
    row = listing(client)[0]
    assert (row["pinned"], row["group_id"], row["position"], row["archived"]) == (
        False,
        None,
        None,
        False,
    )


def test_pin_goes_first_and_unpin_returns(client, three):
    a, b, c = three
    resp = chat_patch(client, a, pinned=True)
    assert resp.status_code == 200 and resp.json()["pinned"] is True
    assert ids(client) == [a, c, b]
    chat_patch(client, b, pinned=True)
    assert ids(client) == [b, a, c]  # свежезакреплённый — выше
    chat_patch(client, a, pinned=False)
    assert ids(client) == [b, c, a]


def test_reorder_pinned_section(client, three):
    a, b, c = three
    for chat_id in three:
        chat_patch(client, chat_id, pinned=True)
    resp = client.post(
        "/lesson_ai/chats/reorder",
        json={"section": "pinned", "chat_ids": [b, c, a]},
    )
    assert resp.status_code == 204
    assert ids(client) == [b, c, a]


def test_groups_crud_and_validation(client, three):
    g = group(client, "  Физика ")
    assert g["name"] == "Физика" and g["chats_count"] == 0
    for bad in ("", "   ", "я" * 61):
        resp = client.post("/lesson_ai/chat_groups", json={"name": bad})
        assert resp.status_code == 422, bad
    renamed = client.patch(f"/lesson_ai/chat_groups/{g['id']}", json={"name": "Химия"})
    assert renamed.json()["name"] == "Химия"
    assert client.patch(f"/lesson_ai/chat_groups/{g['id']}", json={"name": ""}).status_code == 422
    assert [x["name"] for x in client.get("/lesson_ai/chat_groups").json()["items"]] == ["Химия"]


def test_group_order_and_reorder(client, three):
    g1, g2, g3 = (group(client, n) for n in "ABC")
    resp = client.post(
        "/lesson_ai/chat_groups/reorder", json={"group_ids": [g3["id"], g1["id"], g2["id"]]}
    )
    assert resp.status_code == 204
    got = client.get("/lesson_ai/chat_groups").json()["items"]
    assert [x["id"] for x in got] == [g3["id"], g1["id"], g2["id"]]
    chat_patch(client, three[0], group_id=g2["id"])
    chat_patch(client, three[1], group_id=g3["id"])
    # группы по их порядку, потом без группы
    assert ids(client) == [three[1], three[0], three[2]]


def test_move_to_group_and_delete_group_ungroups(client, three):
    a, b, c = three
    g = group(client)
    assert chat_patch(client, a, group_id=g["id"]).json()["group_id"] == g["id"]
    assert client.get("/lesson_ai/chat_groups").json()["items"][0]["chats_count"] == 1
    assert ids(client) == [a, c, b]
    assert client.delete(f"/lesson_ai/chat_groups/{g['id']}").status_code == 204
    rows = {r["id"]: r for r in listing(client)}
    assert rows[a]["group_id"] is None and len(rows) == 3
    assert chat_patch(client, a, group_id=None).json()["group_id"] is None


def test_reorder_into_group_moves_chats(client, three):
    a, b, c = three
    g = group(client)
    resp = client.post(
        "/lesson_ai/chats/reorder",
        json={"section": "group", "group_id": g["id"], "chat_ids": [c, a]},
    )
    assert resp.status_code == 204
    rows = {r["id"]: r for r in listing(client)}
    assert rows[a]["group_id"] == rows[c]["group_id"] == g["id"]
    assert ids(client) == [c, a, b]
    # в «без группы» и из закрепа
    chat_patch(client, c, pinned=True)
    client.post("/lesson_ai/chats/reorder", json={"section": "ungrouped", "chat_ids": [c]})
    row = next(r for r in listing(client) if r["id"] == c)
    assert row["group_id"] is None and row["pinned"] is False


def test_archive_hides_unpins_and_restores_to_living_group(client, three):
    a, b, c = three
    g = group(client)
    chat_patch(client, a, group_id=g["id"])
    chat_patch(client, a, pinned=True)
    resp = chat_patch(client, a, archived=True)
    assert resp.json()["archived"] is True and resp.json()["pinned"] is False
    assert a not in ids(client)
    assert ids(client, archived="true") == [a]
    assert client.get("/lesson_ai/chat_groups").json()["items"][0]["chats_count"] == 0
    # закрепить архивный нельзя: архив снимает закрепление и держит его снятым
    assert chat_patch(client, a, pinned=True).json()["pinned"] is False
    back = chat_patch(client, a, archived=False).json()
    assert back["archived"] is False and back["group_id"] == g["id"]
    chat_patch(client, a, archived=True)
    client.delete(f"/lesson_ai/chat_groups/{g['id']}")
    assert chat_patch(client, a, archived=False).json()["group_id"] is None


def test_archived_chat_opens_and_reorder_rejects_it(client, three):
    a = three[0]
    chat_patch(client, a, archived=True)
    assert client.get(f"/lesson_ai/chats/{a}").status_code == 200
    resp = client.post("/lesson_ai/chats/reorder", json={"section": "pinned", "chat_ids": [a]})
    assert resp.status_code == 404


def test_reorder_validation(client, three):
    a, b, _ = three
    g = group(client)
    post = lambda **body: client.post("/lesson_ai/chats/reorder", json=body)  # noqa: E731
    assert post(section="group", chat_ids=[a]).status_code == 422
    assert post(section="pinned", group_id=g["id"], chat_ids=[a]).status_code == 422
    assert post(section="pinned", chat_ids=[a, a]).status_code == 422
    assert post(section="pinned", chat_ids=[a, 999999]).status_code == 404
    assert post(section="group", group_id=999999, chat_ids=[a]).status_code == 404
    assert post(section="nope", chat_ids=[a]).status_code == 422


def test_writing_to_archived_chat_returns_it(client, three, ai):
    a = three[0]
    chat_patch(client, a, archived=True)
    say(client, ai, a, "привет")
    row = next(r for r in listing(client) if r["id"] == a)
    assert row["archived"] is False


def test_foreign_objects_are_404(client, client2, sms, three):
    a, *_ = three
    g = group(client)
    login(client2, sms, phone=OTHER_PHONE)
    mine2 = new_chat(client2)["id"]
    responses = [
        chat_patch(client2, a, pinned=True),
        chat_patch(client2, mine2, group_id=g["id"]),
        client2.post("/lesson_ai/chats/reorder", json={"section": "pinned", "chat_ids": [a]}),
        client2.post(
            "/lesson_ai/chats/reorder",
            json={"section": "group", "group_id": g["id"], "chat_ids": [mine2]},
        ),
        client2.patch(f"/lesson_ai/chat_groups/{g['id']}", json={"name": "x"}),
        client2.delete(f"/lesson_ai/chat_groups/{g['id']}"),
        client2.post("/lesson_ai/chat_groups/reorder", json={"group_ids": [g["id"]]}),
    ]
    for resp in responses:
        assert resp.status_code == 404, resp.text
    assert ids(client2) == [mine2]
    assert client2.get("/lesson_ai/chat_groups").json()["items"] == []
    assert chat_patch(client, a, pinned=True).json()["pinned"] is True


# -- чат комплекта: «Мои уроки» и страница комплекта ведут в рабочее место ----


def test_kit_has_chat_id_and_open_chat_is_idempotent(client, sms, scene):
    from tests.test_lesson_ai_chat import confirm, ready_chat

    login(client, sms)
    chat = ready_chat(client, scene)
    kit_id = confirm(client, chat["id"]).json()["kit"]["id"]
    assert client.get(f"/lesson_ai/kits/{kit_id}").json()["chat_id"] == chat["id"]
    assert client.get("/lesson_ai/kits").json()["items"][0]["chat_id"] == chat["id"]
    chat_patch(client, chat["id"], archived=True)
    resp = client.post(f"/lesson_ai/kits/{kit_id}/chat")
    assert resp.status_code == 200 and resp.json()["id"] == chat["id"]
    assert ids(client) == [chat["id"]]


def test_open_chat_for_direct_kit_creates_edit_chat(client, sms, scene):
    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from app.adapters.db.base import get_engine
    from app.adapters.db.models import LessonAiChat
    from tests.test_lesson_ai_chat import confirm, ready_chat

    login(client, sms)
    old = ready_chat(client, scene)
    kit_id = confirm(client, old["id"]).json()["kit"]["id"]
    # прямой заказ: чата у комплекта нет
    with Session(get_engine()) as db, db.begin():
        db.execute(update(LessonAiChat).where(LessonAiChat.id == old["id"]).values(kit_id=None))
    assert client.get(f"/lesson_ai/kits/{kit_id}").json()["chat_id"] is None
    resp = client.post(f"/lesson_ai/kits/{kit_id}/chat")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["status"], body["mode"], body["kit_id"]) == ("confirmed", "edit", kit_id)
    assert body["order"]["topic"] and body["order"]["objectives"]
    assert [m["event"]["type"] for m in body["items"]] == ["kit_opened"]
    assert client.get(f"/lesson_ai/kits/{kit_id}").json()["chat_id"] == body["id"]
    assert client.post(f"/lesson_ai/kits/{kit_id}/chat").json()["id"] == body["id"]


def test_foreign_kit_chat_is_404(client, client2, sms, scene):
    from tests.test_lesson_ai_chat import confirm, ready_chat

    login(client, sms)
    kit_id = confirm(client, ready_chat(client, scene)["id"]).json()["kit"]["id"]
    login(client2, sms, phone=OTHER_PHONE)
    assert client2.post(f"/lesson_ai/kits/{kit_id}/chat").status_code == 404
