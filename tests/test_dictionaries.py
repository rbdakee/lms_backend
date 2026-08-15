def test_dictionaries_are_public(client):
    resp = client.get("/dictionaries")
    assert resp.status_code == 200
    body = resp.json()

    # 17 областей и 3 города республиканского значения
    assert len(body["regions"]) == 20
    assert "Астана" in body["regions"]

    assert len(body["subjects"]) > 0
    assert all(isinstance(s, str) for s in body["subjects"])

    assert len(body["categories"]) == 6
    assert body["categories"][0] == {"id": 1, "title": "Цифровые навыки"}
