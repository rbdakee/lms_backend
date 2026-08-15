def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_openapi_opens(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert "/auth/request_code" in resp.json()["paths"]


def test_unknown_path_error_format(client):
    resp = client.get("/no_such_path")
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"]


def test_validation_error_format(client):
    resp = client.post("/auth/request_code", json={})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    fields = {f["field"] for f in error["details"]["fields"]}
    assert {"phone", "consent"} <= fields
