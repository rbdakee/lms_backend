"""Значки предметов: реестр разбора ссылается на каталог картинок презентаций,
а картинка отдаётся открытой ручкой — `<img>` не несёт заголовков."""

import pytest

from app.adapters.curriculum.parser import SUBJECT_CODES, subject_icon
from app.lesson_ai import images


def test_every_subject_has_icon_from_catalog():
    assert len(SUBJECT_CODES) == 19
    for code in SUBJECT_CODES:
        assert subject_icon(code) in images.catalog(), code


def test_unknown_subject_has_no_icon():
    assert subject_icon("no_such_subject") == ""


def test_kazakh_and_russian_are_distinguishable():
    assert subject_icon("kazakh_lang_l1") != subject_icon("russian_lang_l1")


def test_image_is_public_png_with_long_cache(client):
    resp = client.get("/lesson_ai/images/numbers")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert resp.content.startswith(b"\x89PNG")


@pytest.mark.parametrize("key", ["nope", "..%2Fcatalog.toml", "NUMBERS.png", "numbers.png"])
def test_image_only_by_catalog_key(client, key):
    assert client.get(f"/lesson_ai/images/{key}").status_code == 404
