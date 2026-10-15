"""MCP: метаданные OAuth, вход (OAuth и личный токен), `/mcp`, допуск к персданным,
журнал и лимит (`MCP_BRIEF`)."""

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.adapters.db.base import get_engine
from app.api.routers import mcp_oauth
from app.assistant import registry
from app.config import get_settings
from app.main import app, create_app
from app.mcp_server import server as mcp_server
from tests.conftest import login, login_admin, make_user

REDIRECT = "http://localhost:6274/callback"
VERIFIER = "v" * 50
TEACHER_PHONE = "+77010000077"
SURNAME = "Сериккызы"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def challenge(verifier: str = VERIFIER) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.fixture(autouse=True)
def _fresh_limiters():
    """Счётчики лимитов живут в памяти процесса и переживают тест."""
    mcp_oauth._register_limiter.hits.clear()
    mcp_server._limiter.hits.clear()
    yield
    mcp_server._limiter.hits.clear()


@pytest.fixture
def api():
    """Клиент без куки: так ходит MCP-клиент. Куку админа держит `client`."""
    with TestClient(app) as c:
        yield c


def register(api, **meta) -> dict:
    body = {"client_name": "Claude", "redirect_uris": [REDIRECT]}
    body.update(meta)
    resp = api.post("/oauth/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def auth_params(client_id: str, **over) -> dict:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": challenge(),
        "code_challenge_method": "S256",
        "state": "xyz",
        "scope": "mcp",
    }
    params.update(over)
    return params


def get_code(client, api, *, pd_allowed=False, **over) -> tuple[str, dict]:
    """Весь путь до кода: регистрация → согласие админа → redirect_to."""
    reg = register(api)
    params = auth_params(reg["client_id"], **over)
    resp = client.post(
        "/admin/mcp/authorize", json={**params, "decision": "allow", "pd_allowed": pd_allowed}
    )
    assert resp.status_code == 200, resp.text
    query = parse_qs(urlsplit(resp.json()["redirect_to"]).query)
    return query["code"][0], reg


def exchange(api, code, client_id, **over):
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    }
    form.update(over)
    return api.post("/oauth/token", data=form)


def tokens(client, api, **kw) -> dict:
    code, reg = get_code(client, api, **kw)
    resp = exchange(api, code, reg["client_id"])
    assert resp.status_code == 200, resp.text
    return {**resp.json(), "client_id": reg["client_id"]}


def rpc(api, token, method, params=None, *, id_=1):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return api.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
    )


def call(api, token, name, args=None):
    resp = rpc(api, token, "tools/call", {"name": name, "arguments": args or {}})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    body = json.loads(result["content"][0]["text"])
    return result.get("isError", False), body


def personal_token(client, name="Codex", pd_allowed=False) -> dict:
    resp = client.post("/admin/mcp/tokens", json={"name": name, "pd_allowed": pd_allowed})
    assert resp.status_code == 200, resp.text
    return resp.json()


def admin_scene(client, sms):
    return login_admin(client, sms).json()["id"]


# -- метаданные ----------------------------------------------------------


def test_protected_resource_metadata(api):
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        body = api.get(path).json()
        base = get_settings().public_base_url.rstrip("/")
        assert body["resource"] == f"{base}/mcp"
        assert body["authorization_servers"] == [base]
        assert body["scopes_supported"] == ["mcp"]


def test_authorization_server_metadata(api):
    body = api.get("/.well-known/oauth-authorization-server").json()
    base = get_settings().public_base_url.rstrip("/")
    assert body["issuer"] == base
    assert body["authorization_endpoint"] == f"{base}/oauth/authorize"
    assert body["token_endpoint"] == f"{base}/oauth/token"
    assert body["registration_endpoint"] == f"{base}/oauth/register"
    # PKCE обязателен и только S256; клиенты публичные
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint_auth_methods_supported"] == ["none"]
    assert "refresh_token" in body["grant_types_supported"]
    assert body["response_types_supported"] == ["code"]


def test_mcp_without_token_points_to_the_metadata(api):
    resp = api.post(
        "/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert resp.status_code == 401
    challenge_header = resp.headers["www-authenticate"]
    assert challenge_header.startswith("Bearer resource_metadata=")
    assert "/.well-known/oauth-protected-resource" in challenge_header
    assert "invalid_token" not in challenge_header


def test_mcp_with_a_wrong_token_says_invalid_token(api):
    resp = rpc(api, "mcp_at_nonsense", "tools/list")
    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["www-authenticate"]


# -- регистрация клиента -------------------------------------------------


def test_register_returns_a_public_client(api):
    reg = register(api, client_name="  ChatGPT  ")
    assert reg["client_id"].startswith("mcp_client_")
    assert reg["client_name"] == "ChatGPT"
    assert reg["redirect_uris"] == [REDIRECT]
    assert reg["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in reg


@pytest.mark.parametrize(
    "uris",
    [
        [],
        "http://localhost/cb",
        ["javascript:alert(1)"],
        ["http://evil.example/cb"],
        ["https://claude.ai/cb#frag"],
    ],
)
def test_register_refuses_unsafe_redirect_uris(api, uris):
    resp = api.post("/oauth/register", json={"client_name": "X", "redirect_uris": uris})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


def test_register_accepts_https_and_app_schemes(api):
    reg = register(
        api, redirect_uris=["https://claude.ai/api/mcp/auth_callback", "cursor://mcp/cb"]
    )
    assert len(reg["redirect_uris"]) == 2


# -- /oauth/authorize и экран согласия -----------------------------------


def test_authorize_redirects_to_the_consent_screen(api):
    reg = register(api)
    resp = api.get("/oauth/authorize", params=auth_params(reg["client_id"]), follow_redirects=False)
    assert resp.status_code == 302
    target = urlsplit(resp.headers["location"])
    admin = urlsplit(get_settings().admin_base_url)
    assert (target.scheme, target.netloc, target.path) == (
        admin.scheme,
        admin.netloc,
        "/mcp/authorize",
    )
    assert parse_qs(target.query)["client_id"] == [reg["client_id"]]
    assert parse_qs(target.query)["state"] == ["xyz"]


def test_authorize_with_a_foreign_redirect_uri_does_not_redirect(api):
    reg = register(api)
    params = auth_params(reg["client_id"], redirect_uri="http://localhost:9999/evil")
    resp = api.get("/oauth/authorize", params=params, follow_redirects=False)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


def test_authorize_with_an_unknown_client_is_refused(api):
    resp = api.get("/oauth/authorize", params=auth_params("nobody"), follow_redirects=False)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client"


def test_authorize_without_pkce_goes_back_with_an_error(api):
    reg = register(api)
    params = auth_params(reg["client_id"], code_challenge_method="plain")
    resp = api.get("/oauth/authorize", params=params, follow_redirects=False)
    assert resp.status_code == 302
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert resp.headers["location"].startswith(REDIRECT)
    assert query["error"] == ["invalid_request"]
    assert query["state"] == ["xyz"]


def test_authorize_info_describes_the_client(client, api, sms):
    admin_scene(client, sms)
    reg = register(api, client_name="Claude")
    body = client.get("/admin/mcp/authorize_info", params=auth_params(reg["client_id"])).json()
    assert body == {
        "valid": True,
        "error_code": None,
        "error_message": None,
        "client": {"client_id": reg["client_id"], "name": "Claude"},
        "redirect_uri": REDIRECT,
        "scope": "mcp",
    }


def test_authorize_info_flags_a_bad_request(client, api, sms):
    admin_scene(client, sms)
    reg = register(api)
    bad = client.get(
        "/admin/mcp/authorize_info", params=auth_params(reg["client_id"], code_challenge="")
    ).json()
    assert bad["valid"] is False and bad["error_code"] == "invalid_request"
    assert bad["client"]["name"] == "Claude"
    unknown = client.get("/admin/mcp/authorize_info", params=auth_params("nobody")).json()
    assert unknown["valid"] is False and unknown["client"] is None


def test_consent_deny_returns_access_denied(client, api, sms):
    admin_scene(client, sms)
    reg = register(api)
    resp = client.post(
        "/admin/mcp/authorize",
        json={**auth_params(reg["client_id"]), "decision": "deny"},
    )
    query = parse_qs(urlsplit(resp.json()["redirect_to"]).query)
    assert query == {"error": ["access_denied"], "state": ["xyz"]}
    assert resp.json()["redirect_to"].startswith(REDIRECT + "?")


def test_consent_with_a_foreign_redirect_uri_is_a_plain_error(client, api, sms):
    admin_scene(client, sms)
    reg = register(api)
    resp = client.post(
        "/admin/mcp/authorize",
        json={
            **auth_params(reg["client_id"], redirect_uri="http://localhost:1/x"),
            "decision": "allow",
        },
    )
    assert resp.status_code == 422


def test_consent_screen_needs_an_admin(client, api, sms):
    reg = register(api)
    body = {**auth_params(reg["client_id"]), "decision": "allow"}
    assert api.post("/admin/mcp/authorize", json=body).status_code == 401
    login(client, sms)
    assert client.post("/admin/mcp/authorize", json=body).status_code == 403
    assert client.get("/admin/mcp/connections").status_code == 403
    assert client.post("/admin/mcp/tokens", json={"name": "x"}).status_code == 403


# -- обмен кода и обновление ---------------------------------------------


def test_full_oauth_flow_with_pkce(client, api, sms):
    admin_scene(client, sms)
    got = tokens(client, api)
    assert got["token_type"] == "Bearer"
    assert got["expires_in"] == get_settings().mcp_access_ttl_min * 60
    assert got["access_token"].startswith("mcp_at_") and got["refresh_token"].startswith("mcp_rt_")
    assert rpc(api, got["access_token"], "tools/list").status_code == 200


def test_tokens_are_stored_only_as_hashes(client, api, sms):
    admin_scene(client, sms)
    got = tokens(client, api)
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT access_hash, refresh_hash FROM mcp_connection")).one()
    assert got["access_token"] not in row and got["refresh_token"] not in row
    assert row.access_hash == hashlib.sha256(got["access_token"].encode()).hexdigest()


def test_wrong_verifier_is_refused_and_burns_the_code(client, api, sms):
    admin_scene(client, sms)
    code, reg = get_code(client, api)
    bad = exchange(api, code, reg["client_id"], code_verifier="w" * 50)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    # Подбирать verifier на живом коде нельзя: после неудачи код уже не годится
    again = exchange(api, code, reg["client_id"])
    assert again.json()["error"] == "invalid_grant"


def test_code_is_single_use(client, api, sms):
    admin_scene(client, sms)
    code, reg = get_code(client, api)
    assert exchange(api, code, reg["client_id"]).status_code == 200
    assert exchange(api, code, reg["client_id"]).json()["error"] == "invalid_grant"


def test_code_with_a_foreign_redirect_uri_or_client_is_refused(client, api, sms):
    admin_scene(client, sms)
    code, reg = get_code(client, api)
    resp = exchange(api, code, reg["client_id"], redirect_uri="http://localhost:1/other")
    assert resp.json()["error"] == "invalid_grant"
    code, reg = get_code(client, api)
    other = register(api)
    assert exchange(api, code, other["client_id"]).json()["error"] == "invalid_grant"


def test_expired_code_is_refused(client, api, sms):
    admin_scene(client, sms)
    code, reg = get_code(client, api)
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE mcp_auth_code SET expires_at = now() - interval '1 minute'"))
    assert exchange(api, code, reg["client_id"]).json()["error"] == "invalid_grant"


def test_code_of_a_demoted_admin_is_refused(client, api, sms):
    admin_scene(client, sms)
    code, reg = get_code(client, api)
    with get_engine().begin() as conn:
        conn.execute(text('UPDATE "user" SET is_admin = false'))
    assert exchange(api, code, reg["client_id"]).json()["error"] == "invalid_grant"


def test_unknown_grant_type(api):
    resp = api.post("/oauth/token", data={"grant_type": "password"})
    assert resp.status_code == 400 and resp.json()["error"] == "unsupported_grant_type"


def test_refresh_rotates_the_pair(client, api, sms):
    admin_scene(client, sms)
    first = tokens(client, api)
    resp = api.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    )
    assert resp.status_code == 200, resp.text
    second = resp.json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    # Старая пара умерла в тот же момент
    assert rpc(api, first["access_token"], "tools/list").status_code == 401
    assert rpc(api, second["access_token"], "tools/list").status_code == 200
    again = api.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
    )
    assert again.json()["error"] == "invalid_grant"


def test_refresh_after_revoke_or_demotion_is_refused(client, api, sms):
    admin_scene(client, sms)
    got = tokens(client, api)
    conn_id = client.get("/admin/mcp/connections").json()["items"][0]["id"]
    client.delete(f"/admin/mcp/connections/{conn_id}")
    refresh = {"grant_type": "refresh_token", "refresh_token": got["refresh_token"]}
    assert api.post("/oauth/token", data=refresh).json()["error"] == "invalid_grant"

    got = tokens(client, api)
    with get_engine().begin() as conn:
        conn.execute(text('UPDATE "user" SET is_admin = false'))
    refresh["refresh_token"] = got["refresh_token"]
    assert api.post("/oauth/token", data=refresh).json()["error"] == "invalid_grant"


def test_expired_access_token_is_refused(client, api, sms):
    admin_scene(client, sms)
    got = tokens(client, api)
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE mcp_connection SET access_expires_at = now() - interval '1 second'")
        )
    assert rpc(api, got["access_token"], "tools/list").status_code == 401


# -- личные токены и подключения -----------------------------------------


def test_personal_token_is_shown_once(client, api, sms):
    admin_scene(client, sms)
    created = personal_token(client, "Codex на ноутбуке", pd_allowed=True)
    assert created["token"].startswith("mcp_pat_")
    assert {k: created[k] for k in ("name", "kind", "pd_allowed")} == {
        "name": "Codex на ноутбуке",
        "kind": "token",
        "pd_allowed": True,
    }
    listed = client.get("/admin/mcp/connections").json()
    assert [item["name"] for item in listed["items"]] == ["Codex на ноутбуке"]
    assert "token" not in listed["items"][0]
    assert set(listed["items"][0]) == {"id", "name", "kind", "pd_allowed", "created_at"}
    assert rpc(api, created["token"], "tools/list").status_code == 200


def test_token_needs_a_name(client, sms):
    admin_scene(client, sms)
    assert client.post("/admin/mcp/tokens", json={"name": "   "}).status_code == 422


def test_connections_list_has_both_kinds_and_only_mine(client, client2, api, sms):
    admin_scene(client, sms)
    tokens(client, api)
    personal_token(client)
    other = login_admin(client2, sms, "+7 (700) 000-00-98")
    assert other.status_code == 200
    personal_token(client2, "Чужой")
    mine = client.get("/admin/mcp/connections").json()["items"]
    assert sorted(item["kind"] for item in mine) == ["oauth", "token"]
    assert "Чужой" not in [item["name"] for item in mine]
    assert any(item["name"] == "Claude" for item in mine)


def test_revoke_acts_immediately_and_hides_the_row(client, api, sms):
    admin_scene(client, sms)
    created = personal_token(client)
    assert rpc(api, created["token"], "tools/list").status_code == 200
    assert client.delete(f"/admin/mcp/connections/{created['id']}").status_code == 204
    assert rpc(api, created["token"], "tools/list").status_code == 401
    assert client.get("/admin/mcp/connections").json()["items"] == []
    assert client.delete(f"/admin/mcp/connections/{created['id']}").status_code == 404


def test_revoke_oauth_connection_cuts_the_access_token(client, api, sms):
    admin_scene(client, sms)
    got = tokens(client, api)
    conn_id = client.get("/admin/mcp/connections").json()["items"][0]["id"]
    client.delete(f"/admin/mcp/connections/{conn_id}")
    assert rpc(api, got["access_token"], "tools/list").status_code == 401


def test_other_admin_cannot_touch_my_connection(client, client2, sms):
    admin_scene(client, sms)
    created = personal_token(client)
    login_admin(client2, sms, "+7 (700) 000-00-98")
    assert client2.delete(f"/admin/mcp/connections/{created['id']}").status_code == 404
    patched = client2.patch(f"/admin/mcp/connections/{created['id']}", json={"pd_allowed": True})
    assert patched.status_code == 404


def test_demoted_admin_is_cut_off_at_once(client, api, sms):
    admin_scene(client, sms)
    created = personal_token(client)
    assert rpc(api, created["token"], "tools/list").status_code == 200
    with get_engine().begin() as conn:
        conn.execute(text('UPDATE "user" SET is_admin = false'))
    assert rpc(api, created["token"], "tools/list").status_code == 401


def test_blocked_admin_is_cut_off(client, api, sms):
    admin_scene(client, sms)
    created = personal_token(client)
    with get_engine().begin() as conn:
        conn.execute(text('UPDATE "user" SET is_blocked = true'))
    assert rpc(api, created["token"], "tools/list").status_code == 401


def test_teacher_cannot_get_a_token(client, sms):
    login(client, sms)
    assert client.post("/admin/mcp/tokens", json={"name": "x"}).status_code == 403


# -- tools/list ----------------------------------------------------------


def test_tools_list_is_the_registry_with_annotations(client, api, sms):
    admin_scene(client, sms)
    token = personal_token(client)["token"]
    tools = rpc(api, token, "tools/list").json()["result"]["tools"]
    assert sorted(t["name"] for t in tools) == sorted(registry.all_tools())
    by_name = {t["name"]: t for t in tools}
    for name, spec in registry.all_tools().items():
        tool = by_name[name]
        ann = tool["annotations"]
        assert tool["inputSchema"] == spec.input_schema()
        if spec.danger == "read":
            assert ann["readOnlyHint"] is True, name
        elif spec.danger == "write":
            assert ann["readOnlyHint"] is False and ann["destructiveHint"] is False, name
        else:
            assert ann["readOnlyHint"] is False and ann["destructiveHint"] is True, name
            assert "спроси человека" in tool["description"], name


def test_tools_list_does_not_leak_a_free_form_tool(client, api, sms):
    """Свободного HTTP-запроса, SQL и «прочитать файл» нет — только реестр."""
    admin_scene(client, sms)
    token = personal_token(client)["token"]
    names = {t["name"] for t in rpc(api, token, "tools/list").json()["result"]["tools"]}
    assert not {n for n in names if any(w in n for w in ("sql", "http", "shell", "file_read"))}


# -- tools/call ----------------------------------------------------------


def test_call_read_write_confirm(client, api, sms):
    admin_scene(client, sms)
    token = personal_token(client)["token"]

    err, listed = call(api, token, "categories_list")
    assert not err and len(json.dumps(listed)) > 2

    err, created = call(api, token, "category_create", {"title": "Робототехника"})
    assert not err, created
    err, listed = call(api, token, "categories_list")
    assert "Робототехника" in json.dumps(listed, ensure_ascii=False)

    category_id = next(
        c["id"]
        for c in client.get("/dictionaries").json()["categories"]
        if c["title"] == "Робототехника"
    )
    # confirm исполняется сразу: подтверждения на сервере нет, спрашивает клиент
    err, deleted = call(api, token, "category_delete", {"category_id": category_id})
    assert not err, deleted
    titles = [c["title"] for c in client.get("/dictionaries").json()["categories"]]
    assert "Робототехника" not in titles


def test_tool_errors_come_back_as_is_error(client, api, sms):
    admin_scene(client, sms)
    token = personal_token(client)["token"]

    err, body = call(api, token, "no_such_tool")
    assert err and body["error"]["code"] == "unknown_tool"

    err, body = call(api, token, "course_card", {})
    assert err and body["error"]["code"] == "missing_argument"

    err, body = call(api, token, "course_card", {"course_id": 999999})
    assert err and body["error"]["code"] == "not_found"

    err, body = call(api, token, "categories_list", {"extra": 1})
    assert err and body["error"]["code"] == "unknown_argument"


def test_failed_write_does_not_leave_half_a_change(client, api, sms):
    admin_scene(client, sms)
    token = personal_token(client)["token"]
    err, _ = call(api, token, "course_update", {"course_id": 999999, "title": "Нет такого"})
    assert err
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM course")).scalar() == 0


# -- персональные данные -------------------------------------------------


def a_teacher():
    return make_user(
        TEACHER_PHONE, last_name=SURNAME, first_name="Айгерим", school="КГУ «Школа №5»"
    )


def test_lists_are_masked_whatever_the_flag(client, api, sms):
    admin_scene(client, sms)
    a_teacher()
    for pd_allowed in (False, True):
        token = personal_token(client, pd_allowed=pd_allowed)["token"]
        err, body = call(api, token, "teachers_list")
        assert not err
        text_ = json.dumps(body, ensure_ascii=False)
        assert SURNAME not in text_ and TEACHER_PHONE not in text_


def test_card_is_masked_by_default_and_open_with_the_flag(client, api, sms):
    admin_scene(client, sms)
    person = a_teacher()
    token = personal_token(client, pd_allowed=False)
    err, body = call(api, token["token"], "teacher_card", {"user_id": person.id})
    assert err and body["error"]["code"] == "pd_not_allowed"
    assert SURNAME not in json.dumps(body, ensure_ascii=False)

    # Галочка снимается и ставится на экране подключений — действует со следующего вызова
    patched = client.patch(f"/admin/mcp/connections/{token['id']}", json={"pd_allowed": True})
    assert patched.status_code == 200 and patched.json()["pd_allowed"] is True
    err, body = call(api, token["token"], "teacher_card", {"user_id": person.id})
    assert not err and body["last_name"] == SURNAME and body["phone"] == TEACHER_PHONE

    client.patch(f"/admin/mcp/connections/{token['id']}", json={"pd_allowed": False})
    err, body = call(api, token["token"], "teacher_card", {"user_id": person.id})
    assert err


def test_oauth_consent_flag_reaches_the_connection(client, api, sms):
    admin_scene(client, sms)
    person = a_teacher()
    allowed = tokens(client, api, pd_allowed=True)
    err, body = call(api, allowed["access_token"], "teacher_card", {"user_id": person.id})
    assert not err and body["phone"] == TEACHER_PHONE
    items = client.get("/admin/mcp/connections").json()["items"]
    assert items[0]["pd_allowed"] is True

    denied = tokens(client, api, pd_allowed=False)
    err, body = call(api, denied["access_token"], "teacher_card", {"user_id": person.id})
    assert err and body["error"]["code"] == "pd_not_allowed"


def test_teacher_update_and_enrollment_grant_follow_the_flag(client, api, sms):
    admin_scene(client, sms)
    person = a_teacher()
    closed = personal_token(client, "закрыт", pd_allowed=False)["token"]
    opened = personal_token(client, "открыт", pd_allowed=True)["token"]

    err, body = call(
        api, closed, "teacher_update", {"user_id": person.id, "school": "Другая школа"}
    )
    assert err and body["error"]["code"] == "pd_not_allowed"
    err, body = call(
        api, opened, "teacher_update", {"user_id": person.id, "school": "Другая школа"}
    )
    assert not err and body["changed"] == ["school"]

    err, body = call(
        api,
        closed,
        "enrollment_grant",
        {"user_id": person.id, "course_id": 1, "platform": "p1", "paid": True},
    )
    assert err and body["error"]["code"] == "pd_not_allowed"


# -- журнал и лимит ------------------------------------------------------


def log_rows():
    with get_engine().connect() as conn:
        return conn.execute(
            text("SELECT connection_id, tool, args, ok, error_code FROM mcp_call_log ORDER BY id")
        ).all()


def test_every_call_is_logged_with_masked_args(client, api, sms):
    admin_scene(client, sms)
    person = a_teacher()
    created = personal_token(client, pd_allowed=True)
    token = created["token"]
    call(api, token, "categories_list")
    call(
        api,
        token,
        "teacher_update",
        {"user_id": person.id, "phone": "+77010009999", "school": "Новая"},
    )
    call(api, token, "course_card", {"course_id": 999999})
    call(api, token, "no_such_tool")

    rows = log_rows()
    assert [(r.tool, r.ok, r.error_code) for r in rows] == [
        ("categories_list", True, None),
        ("teacher_update", True, None),
        ("course_card", False, "not_found"),
        ("no_such_tool", False, "unknown_tool"),
    ]
    assert {r.connection_id for r in rows} == {created["id"]}
    update_args = rows[1].args
    assert update_args == {"user_id": person.id, "phone": "скрыто", "school": "скрыто"}
    dump = json.dumps([r.args for r in rows], ensure_ascii=False)
    assert "77010009999" not in dump and "Новая" not in dump


def test_list_calls_are_not_logged(client, api, sms):
    admin_scene(client, sms)
    token = personal_token(client)["token"]
    rpc(api, token, "tools/list")
    assert log_rows() == []


def test_rate_limit_per_connection(client, api, sms, monkeypatch):
    admin_scene(client, sms)
    first = personal_token(client, "первый")["token"]
    second = personal_token(client, "второй")["token"]
    monkeypatch.setattr(mcp_server._limiter, "limit", 2)
    mcp_server._limiter.hits.clear()

    assert call(api, first, "categories_list")[0] is False
    assert call(api, first, "categories_list")[0] is False
    err, body = call(api, first, "categories_list")
    assert err and body["error"]["code"] == "rate_limited"
    # Другое подключение не страдает от чужого цикла
    assert call(api, second, "categories_list")[0] is False
    assert [r.error_code for r in log_rows() if not r.ok] == ["rate_limited"]
    mcp_server._limiter.hits.clear()


def test_default_rate_limit_is_set():
    assert get_settings().mcp_calls_per_min == 120


# -- выключатель ---------------------------------------------------------


def test_disabled_flag_removes_every_route(monkeypatch):
    cfg = get_settings().model_copy(update={"mcp_enabled": False})
    monkeypatch.setattr("app.main.get_settings", lambda: cfg)
    off = create_app()
    paths = {getattr(r, "path", "") for r in off.routes}
    assert "/mcp" not in paths
    assert not any(p.startswith(("/oauth", "/.well-known", "/admin/mcp")) for p in paths)


def test_protocol_negotiation_works_without_initialize(client, api, sms):
    """Stateless: сессии нет, клиент может начать сразу с tools/list;
    `initialize` тоже отвечает."""
    admin_scene(client, sms)
    token = personal_token(client)["token"]
    init = rpc(
        api,
        token,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert init.status_code == 200
    assert init.json()["result"]["serverInfo"]["name"] == "lms-admin"
    assert "mcp-session-id" not in {k.lower() for k in init.headers}
