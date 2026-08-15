from tests.conftest import login


def test_me_requires_auth(client):
    resp = client.get("/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_patch_profile(client, sms):
    login(client, sms)
    resp = client.patch(
        "/me",
        json={
            "last_name": "Нурланова",
            "first_name": "Айгуль",
            "middle_name": "Сериковна",
            "school": "КГУ «Средняя школа №27»",
            "position": "Учитель математики",
            "region": "Алматы",
            "city": "Алматы",
            "subject": "Математика",
            "experience": 12,
            "lang": "kz",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["onboarding_done"] is True
    assert body["experience"] == 12
    assert body["lang"] == "kz"

    me = client.get("/me").json()
    assert me["last_name"] == "Нурланова"


def test_patch_partial_and_clear(client, sms):
    login(client, sms)
    client.patch("/me", json={"first_name": "Айгуль", "last_name": "Нурланова"})
    resp = client.patch("/me", json={"experience": None, "first_name": "  Дана  "})
    body = resp.json()
    assert body["experience"] is None
    assert body["first_name"] == "Дана"
    assert body["last_name"] == "Нурланова"


def test_patch_rejects_unknown_field(client, sms):
    login(client, sms)
    resp = client.patch("/me", json={"is_admin": True})
    assert resp.status_code == 422


def test_patch_rejects_bad_lang(client, sms):
    login(client, sms)
    resp = client.patch("/me", json={"lang": "en"})
    assert resp.status_code == 422


def test_sessions_list_marks_current(client, client2, sms):
    login(client, sms)
    login(client2, sms)

    items = client.get("/me/sessions").json()["items"]
    assert len(items) == 2
    assert sum(1 for s in items if s["is_current"]) == 1
    for s in items:
        assert {"id", "created_at", "last_seen_at", "user_agent", "is_current"} <= set(s)


def test_revoke_other_session(client, client2, sms):
    login(client, sms)
    login(client2, sms)

    other = next(s for s in client.get("/me/sessions").json()["items"] if not s["is_current"])
    resp = client.delete(f"/me/sessions/{other['id']}")
    assert resp.status_code == 204

    assert client2.get("/me").status_code == 401
    resp = client.delete(f"/me/sessions/{other['id']}")
    assert resp.status_code == 404


def test_logout_others(client, client2, sms):
    login(client, sms)
    login(client2, sms)

    resp = client.post("/auth/logout_others")
    assert resp.status_code == 200
    assert resp.json() == {"revoked_count": 1}
    assert client2.get("/me").status_code == 401
    assert client.get("/me").status_code == 200


def test_logout(client, sms):
    login(client, sms)
    resp = client.post("/auth/logout")
    assert resp.status_code == 204
    assert client.get("/me").status_code == 401


def test_logout_without_session_is_ok(client):
    assert client.post("/auth/logout").status_code == 204
