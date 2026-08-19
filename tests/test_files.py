import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest

from app.adapters.db.models import LessonFile
from app.adapters.db.repos import now_utc
from app.adapters.storage.local_storage import LocalStorage
from app.config import get_settings
from app.domain.errors import FileTooLargeError
from app.domain.signed_link import sign
from tests.conftest import (
    login,
    make_course,
    make_enrollment,
    make_lesson,
    make_module,
    seed,
    user_id,
)

PDF = b"%PDF-1.4 fake content"


def make_file(lesson_id, **kw):
    fields = {
        "lesson_id": lesson_id,
        "name": "Памятка по критериям.pdf",
        "url": "uploads/2026/08/18/9f3c1a7e4b2d8c05.pdf",
        "size_bytes": len(PDF),
        "mime": "application/pdf",
    }
    fields.update(kw)
    return seed(LessonFile(**fields))


def signed_path(file_id: int, name: str, *, ttl_sec: int = 900, secret: str | None = None) -> str:
    """Ссылка, собранная тестом теми же правилами, что и сервером: подпись
    проверяется по декодированному пути, в адрес имя уходит закодированным."""
    path = f"/files/lesson/{file_id}/{name}"
    expires = int(now_utc().timestamp()) + ttl_sec
    signature = sign(path, expires, secret or get_settings().storage_secret)
    return f"{quote(path)}?e={expires}&s={signature}"


# -- POST /files -------------------------------------------------------


def test_upload_requires_auth(client, storage):
    resp = client.post("/files", files={"file": ("memo.pdf", PDF, "application/pdf")})
    assert resp.status_code == 401
    assert storage.objects == {}


def test_upload_returns_key_and_stores_object(client, sms, storage):
    login(client, sms)
    resp = client.post(
        "/files",
        # Тип из браузера намеренно неверный: сервер определяет его сам
        files={"file": ("Памятка по критериям.pdf", PDF, "application/octet-stream")},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Памятка по критериям.pdf"
    assert body["mime"] == "application/pdf"
    assert body["size_bytes"] == len(PDF)
    day = datetime.now(UTC).strftime("%Y/%m/%d")
    assert re.fullmatch(rf"uploads/{day}/[0-9a-f]{{16}}\.pdf", body["key"])
    # Ключ ушёл наружу, но файл действительно лежит в хранилище
    assert storage.objects[body["key"]] == PDF
    # Строки в базе за загруженным файлом ещё нет — id взяться неоткуда
    assert "id" not in body


def test_upload_strips_path_from_name(client, sms, storage):
    login(client, sms)
    body = client.post("/files", files={"file": ("docs/memo.pdf", PDF)}).json()
    assert body["name"] == "memo.pdf"


def test_upload_413_over_limit(client, sms, storage, monkeypatch):
    # Лимит подменён: гонять через тест настоящие 20 МБ незачем
    monkeypatch.setattr(get_settings(), "upload_max_mb", 1)
    login(client, sms)

    resp = client.post("/files", files={"file": ("big.bin", b"x" * (1024 * 1024 + 1))})

    assert resp.status_code == 413
    assert resp.json()["error"] == {
        "code": "file_too_large",
        "message": "Файл больше 1 МБ",
        "details": {"max_size_mb": 1},
    }
    # Оборванная загрузка не оставляет объекта под уже выданным ключом
    assert storage.objects == {}


def test_upload_422_without_file(client, sms, storage):
    login(client, sms)
    expected = {
        "code": "validation_error",
        "message": "Файл не выбран",
        "details": {"fields": [{"field": "file", "message": "Field required"}]},
    }

    assert client.post("/files").json()["error"] == expected
    empty = client.post("/files", files={"file": ("empty.txt", b"")})
    assert empty.status_code == 422
    assert empty.json()["error"] == expected
    assert storage.objects == {}


# -- GET /files/{id} ---------------------------------------------------


def test_file_link_requires_auth(client):
    file = make_file(make_lesson(make_module(make_course().id).id).id)
    assert client.get(f"/files/{file.id}").status_code == 401


def test_file_link_404_for_missing_hidden_and_invisible(client, sms):
    course = make_course()
    hidden = make_file(make_lesson(make_module(course.id).id, is_hidden=True).id)
    draft = make_course(status="draft")
    draft_file = make_file(make_lesson(make_module(draft.id).id).id)

    login(client, sms)
    uid = user_id(client)
    make_enrollment(uid, course.id)
    make_enrollment(uid, draft.id)

    assert client.get("/files/999999").status_code == 404
    assert client.get(f"/files/{hidden.id}").status_code == 404
    assert client.get(f"/files/{draft_file.id}").status_code == 404


def test_file_link_403_without_enrollment_and_after_revoke(client, sms):
    stranger = make_file(make_lesson(make_module(make_course().id).id).id)
    revoked_course = make_course()
    revoked = make_file(make_lesson(make_module(revoked_course.id).id).id)

    login(client, sms)
    make_enrollment(user_id(client), revoked_course.id, revoked_at=now_utc())

    resp = client.get(f"/files/{stranger.id}")
    assert resp.status_code == 403
    assert resp.json()["error"] == {
        "code": "forbidden",
        "message": "Материалы доступны учителям с доступом к курсу",
    }
    assert client.get(f"/files/{revoked.id}").status_code == 403


def test_file_link_is_signed_and_expires_in_15_min(client, sms):
    course = make_course()
    file = make_file(make_lesson(make_module(course.id).id).id)
    login(client, sms)
    make_enrollment(user_id(client), course.id)

    body = client.get(f"/files/{file.id}").json()

    assert body["url"].startswith(
        f"{get_settings().public_base_url}/files/lesson/{file.id}/"
    )
    assert re.search(r"\?e=\d+&s=\S+$", body["url"])
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert abs(expires_at - (now_utc() + timedelta(minutes=15))) < timedelta(seconds=5)


# -- GET /files/lesson/{id}/{filename} ---------------------------------


def test_download_by_signed_link(client, sms, storage):
    course = make_course()
    file = make_file(make_lesson(make_module(course.id).id).id)
    storage.objects[file.url] = PDF
    login(client, sms)
    make_enrollment(user_id(client), course.id)

    url = client.get(f"/files/{file.id}").json()["url"]
    resp = client.get(url.removeprefix(get_settings().public_base_url))

    assert resp.status_code == 200
    assert resp.content == PDF
    assert resp.headers["content-type"] == "application/pdf"
    assert quote(file.name) in resp.headers["content-disposition"]


def test_download_403_on_forged_and_expired_link(client, storage):
    file = make_file(make_lesson(make_module(make_course().id).id).id)
    storage.objects[file.url] = PDF

    forged = signed_path(file.id, file.name, secret="not-the-secret")
    assert client.get(forged).status_code == 403
    # В адресе может оказаться что угодно, включая кириллицу в подписи
    assert client.get(f"/files/lesson/{file.id}/memo.pdf?e=9999999999&s=подпись").status_code == 403
    # Срок вышел — тот же отказ: раздатчик не объясняет, что именно не так
    expired = client.get(signed_path(file.id, file.name, ttl_sec=-60))
    assert expired.status_code == 403
    assert expired.json()["error"]["code"] == "forbidden"
    # Подпись сделана для другого файла — к этому не подходит
    other = make_file(make_lesson(make_module(make_course().id).id).id, name="Другой.pdf")
    path = signed_path(file.id, file.name).split("?")[1]
    assert client.get(f"/files/lesson/{other.id}/{quote(other.name)}?{path}").status_code == 403


def test_download_404_for_missing_row_and_object(client, storage):
    file = make_file(make_lesson(make_module(make_course().id).id).id)

    assert client.get(signed_path(999999, "memo.pdf")).status_code == 404
    # Строка есть, объекта в хранилище нет
    assert client.get(signed_path(file.id, file.name)).status_code == 404


# -- адаптер локального диска ------------------------------------------
# Остальные тесты работают с хранилищем-фейком, а на диск файл кладёт этот
# адаптер — и в разработке он же его раздаёт.


def test_local_storage_writes_and_reads(tmp_path):
    storage = LocalStorage(tmp_path)
    key = "uploads/2026/08/18/9f3c1a7e4b2d8c05.pdf"

    assert storage.save(key, [b"%PDF", b"-1.4"]) == 8
    assert storage.size(key) == 8
    assert b"".join(storage.read(key)) == b"%PDF-1.4"
    assert storage.size("uploads/2026/08/18/нет.pdf") is None


def test_local_storage_removes_partial_object(tmp_path):
    def chunks():
        yield b"start"
        raise FileTooLargeError(20)

    with pytest.raises(FileTooLargeError):
        LocalStorage(tmp_path).save("uploads/big.bin", chunks())
    assert LocalStorage(tmp_path).size("uploads/big.bin") is None


def test_local_storage_key_outside_root(tmp_path):
    """Ключ, уводящий за пределы каталога, приходит снаружи — из тела PATCH:
    для `size` это «такого объекта нет», и сценарий отдаёт контрактное 404.
    У `save` и `read` ключ наш собственный, и молчать там нельзя."""
    storage = LocalStorage(tmp_path)

    assert storage.size("../../etc/passwd") is None
    with pytest.raises(ValueError, match="хранилища"):
        storage.save("../../etc/passwd", [b"x"])
    with pytest.raises(ValueError, match="хранилища"):
        list(storage.read("../../etc/passwd"))
