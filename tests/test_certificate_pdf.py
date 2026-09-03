"""GET /certificates/{id}/pdf — сертификат бумагой.

Содержимое документа глазами тут никто не читает: текст в PDF лежит
глифами подмножества шрифта, и обратно в строку он не разбирается. Поэтому
проверяем то, что проверяемо: право на документ, валидность байтов, влияние
картинок бренда и дату по Алматы — её видно сравнением двух сборок.

Картинки берутся у площадки самого документа: бумагу открывает браузер прямой
ссылкой, `Origin` в такой запрос не приходит, и площадки у запроса нет вовсе.
Лежат они файлами в `app/assets/brands/<площадка>/`, но самих файлов
в репозитории нет — их кладёт владелец, — поэтому тест подставляет свою
директорию фикстурой `brand_file`.
"""

import re
import struct
import zlib
from datetime import UTC, datetime

import pytest

from app.adapters.db.models import Certificate
from app.adapters.db.repos import now_utc
from app.adapters.pdf.certificate import _verify_host, render_certificate
from app.application import settings as settings_module
from app.application.certificate_pdf import CertificatePdfService
from app.config import get_settings
from app.domain import brands
from app.domain.brands import CERT_SLOTS
from tests.conftest import (
    login_admin,
    login_named,
    make_certificate,
    make_course,
    make_user,
    user_id,
)

# Отметка времени сборки — единственное, чем различаются два одинаковых
# документа, собранных в разные секунды.
CREATION_DATE = re.compile(rb"/CreationDate \(D:[^)]*\)")

# Байты из test_settings: заголовок PNG есть, картинки за ним нет. Проверка
# настроек такое пропускает (там смотрят на сигнатуру начала файла, а она тут
# настоящая), а fpdf2 на этом спотыкается — то самое «сломанная картинка».
BROKEN_PNG = b"\x89PNG\r\n\x1a\n" + b"fake" * 64


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Настоящий однотонный PNG средствами stdlib: fpdf2 берёт только
    разбираемую картинку, а тащить ради теста новую зависимость нельзя."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def own_certificate(client, sms, **kw):
    """Учитель с выданным сертификатом; возвращает его id."""
    login_named(client, sms)
    course = make_course()
    return make_certificate(user_id(client), course.id, **kw).id


def get_pdf(client, certificate_id):
    return client.get(f"/certificates/{certificate_id}/pdf")


@pytest.fixture
def brand_file(tmp_path, monkeypatch):
    """Кладёт картинку площадке. Директория подменяется на временную: класть
    файлы в `app/assets/brands/` значило бы оставлять их в репозитории."""
    monkeypatch.setattr(settings_module, "BRANDS_DIR", tmp_path)

    def put(platform: str, slot: str, content: bytes) -> None:
        directory = tmp_path / platform
        directory.mkdir(exist_ok=True)
        (directory / brands.BRANDS[platform].images[slot]).write_bytes(content)

    return put


# Картинки в трёх слотах разные: одинаковую fpdf2 кладёт в документ один раз,
# и по весу бумаги было бы не видно, попали все три или только одна.
CERT_PICTURES = {
    "cert_logo": (120, 60, (31, 56, 100)),
    "cert_sign": (160, 60, (20, 20, 60)),
    "cert_stamp": (120, 120, (150, 30, 30)),
}


def put_stamps(brand_file, platform: str, *slots: str) -> None:
    """Картинки сертификата у площадки: логотип, подпись и печать."""
    for slot in slots or tuple(CERT_SLOTS.values()):
        brand_file(platform, slot, png(*CERT_PICTURES[slot]))


# -- права ---------------------------------------------------------------


def test_pdf_requires_login(client, sms, storage):
    certificate_id = own_certificate(client, sms)
    client.post("/auth/logout")

    assert get_pdf(client, certificate_id).status_code == 401


def test_own_certificate_comes_as_pdf(client, sms, storage):
    certificate_id = own_certificate(client, sms)

    resp = get_pdf(client, certificate_id)

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    # Номер из ASCII, поэтому имя файла обычное, без filename*
    assert resp.headers["content-disposition"] == 'attachment; filename="KZ-2026-XB7K2M.pdf"'
    assert resp.content.startswith(b"%PDF")


def test_the_file_name_carries_only_what_a_number_is_made_of(client, sms, storage):
    """Номер собирает сервер из своего алфавита, и лишних знаков в нём
    не бывает. Но заголовок склеивается подстановкой в кавычки, и держать
    его разбор на инварианте из другого файла не стоит."""
    certificate_id = own_certificate(client, sms, number='KZ-2026-A"B; ы')

    resp = get_pdf(client, certificate_id)

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] == 'attachment; filename="KZ-2026-AB.pdf"'


def test_foreign_certificate_is_refused(client, sms, storage):
    """Чужой документ учителю не отдаётся: на бумаге чужие ФИО."""
    login_named(client, sms)
    course = make_course()
    stranger = make_user("+77010000001")
    certificate_id = make_certificate(stranger.id, course.id).id

    resp = get_pdf(client, certificate_id)

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_admin_takes_any_certificate(client, sms, storage):
    """Админ печатает любой документ: заявка на бумажную копию приходит ему."""
    login_admin(client, sms)
    course = make_course()
    stranger = make_user("+77010000002")
    certificate_id = make_certificate(stranger.id, course.id).id

    resp = get_pdf(client, certificate_id)

    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"%PDF")


def test_missing_certificate_is_not_found(client, sms, storage):
    login_named(client, sms)

    resp = get_pdf(client, 9_999_999)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# -- отозванный ------------------------------------------------------------


def test_revoked_is_not_printed_to_its_holder(client, sms, storage):
    """Отозванная бумага не должна печататься заново — даже владельцем."""
    certificate_id = own_certificate(client, sms, revoked_at=now_utc())

    resp = get_pdf(client, certificate_id)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_revoked_is_not_printed_to_admin(client, client2, sms, storage):
    """И админом тоже: отзыв — про сам документ, а не про того, кто его просит."""
    certificate_id = own_certificate(client, sms, revoked_at=now_utc())
    login_admin(client2, sms)

    assert get_pdf(client2, certificate_id).status_code == 404


def test_revoked_is_found_and_refused_by_decision(client, sms, storage):
    """404 у отозванного — решение, а не случайность: поиск по id его находит,
    иначе «нет такого документа» и «отозван» слились бы по недосмотру.

    Разница видна на чужом отозванном: его находят и отвечают отказом в праве,
    а не «не найден».
    """
    login_named(client, sms)
    course = make_course()
    stranger = make_user("+77010000003")
    certificate_id = make_certificate(stranger.id, course.id, revoked_at=now_utc()).id

    assert get_pdf(client, certificate_id).status_code == 403


# -- сам документ ----------------------------------------------------------


def test_kazakh_document_is_built(client, sms, storage):
    """Казахская версия собирается: буквы ә ғ қ ң ө ұ ү һ і шрифт не роняют."""
    certificate_id = own_certificate(
        client,
        sms,
        lang="kz",
        holder_name="Әбдіғұлова Гүлназ Қуанышқызы",
        course_title="Бастауыш сыныптағы қалыптастырушы бағалау",
    )

    resp = get_pdf(client, certificate_id)

    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"%PDF")


def test_document_without_images_is_still_issued(client, sms, storage):
    """Настройки пустые: логотипа, подписи и печати нет, места остаются
    пустыми — отсутствие печати не повод отказать человеку в сертификате."""
    certificate_id = own_certificate(client, sms)

    resp = get_pdf(client, certificate_id)

    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"%PDF")


def test_brand_images_get_into_document(client, sms, storage, brand_file):
    """Три картинки бренда попадают на бумагу: документ заметно тяжелеет."""
    certificate_id = own_certificate(client, sms)
    plain = get_pdf(client, certificate_id).content

    put_stamps(brand_file, "p1")

    resp = get_pdf(client, certificate_id)

    assert resp.status_code == 200, resp.text
    assert len(resp.content) > len(plain) + 1000


def test_the_document_takes_the_images_of_its_own_platform(client, sms, storage, brand_file):
    """Сертификат выдан на второй площадке, а запрос за бумагой приходит без
    `Origin`: картинки берутся у документа, а не у площадки запроса. Печать
    соседней площадки на чужом сертификате — это чужая организация на бумаге.
    """
    certificate_id = own_certificate(client, sms, platform="p2")
    plain = get_pdf(client, certificate_id).content

    # Картинки лежат только у первой площадки — документ второй их не берёт
    put_stamps(brand_file, "p1")
    assert same_paper(get_pdf(client, certificate_id).content, plain)

    put_stamps(brand_file, "p2")
    assert len(get_pdf(client, certificate_id).content) > len(plain) + 1000


def test_broken_image_does_not_stop_the_document(client, sms, storage, brand_file):
    """В слоте печати лежит не картинка: место остаётся пустым, а документ
    всё равно выдаётся."""
    certificate_id = own_certificate(client, sms)
    brand_file("p1", "cert_stamp", BROKEN_PNG)

    resp = get_pdf(client, certificate_id)

    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"%PDF")


def test_a_missing_file_leaves_only_its_own_place_empty(client, sms, storage, brand_file):
    """Файла подписи и печати у площадки нет, а логотип есть: пустой слот —
    не ошибка, и остальные картинки на бумагу всё равно попадают."""
    certificate_id = own_certificate(client, sms)
    plain = get_pdf(client, certificate_id).content
    put_stamps(brand_file, "p1", "cert_logo")

    resp = get_pdf(client, certificate_id)

    assert resp.status_code == 200, resp.text
    assert not same_paper(resp.content, plain)


# -- дата на бумаге --------------------------------------------------------


# Идентификатор файла fpdf2 считает вместе с отметкой времени сборки: две
# сборки одного и того же различаются и им тоже.
FILE_ID = re.compile(rb"/ID \[<[0-9A-F]+><[0-9A-F]+>\]")


def same_paper(first: bytes, second: bytes) -> bool:
    """Два документа — один и тот же лист, если различаются только отметкой
    времени сборки и посчитанным по ней идентификатором файла."""

    def stable(document: bytes) -> bytes:
        return FILE_ID.sub(b"", CREATION_DATE.sub(b"", document))

    return stable(first) == stable(second)


def build(issued_at: datetime) -> bytes:
    """Один и тот же документ с разной отметкой выдачи, без отметки времени
    сборки: сборки, отличающиеся только ею, сравниваются побайтно."""
    document = {
        "holder_name": "Смагулова Гульмира Токтарбековна",
        "course_title": "Формирующее оценивание",
        "hours": 72,
        "issued_at": issued_at,
        "number": "KZ-2026-XB7K2M",
        "lang": "ru",
    }
    return CREATION_DATE.sub(b"", render_certificate(document, {}))


def test_date_on_paper_is_almaty_not_utc():
    """Выдан в 20:00 UTC — на бумаге уже следующий день: в 01:00 по Алматы
    документ, выданный час назад, не должен выглядеть вчерашним."""
    evening_utc = build(datetime(2026, 8, 18, 20, 0, tzinfo=UTC))

    # Та же дата по Алматы, другая по UTC — документы совпадают
    assert evening_utc == build(datetime(2026, 8, 19, 3, 0, tzinfo=UTC))
    # Та же дата по UTC, другая по Алматы — документы разные
    assert evening_utc != build(datetime(2026, 8, 18, 18, 0, tzinfo=UTC))


# -- QR на страницу проверки -----------------------------------------------


def qr_rects(pdf_bytes: bytes) -> int:
    """Сколько закрашенных прямоугольников в документе: QR рисуется ими,
    и по их числу видно, попал он на лист или нет."""
    return len(re.findall(rb"\bre\b", zlib.decompress(_page_stream(pdf_bytes))))


def _page_stream(pdf_bytes: bytes) -> bytes:
    """Сжатый поток страницы. fpdf2 кладёт содержимое одним объектом,
    и разбирать весь PDF ради этого незачем."""
    start = pdf_bytes.index(b"stream\n", pdf_bytes.index(b"/Contents")) + len(b"stream\n")
    return pdf_bytes[start : pdf_bytes.index(b"\nendstream", start)]


def build_qr(verify_url, number="KZ-2026-XB7K2M", lang="ru") -> bytes:
    document = {
        "holder_name": "Смагулова Гульмира Токтарбековна",
        "course_title": "Формирующее оценивание",
        "hours": 72,
        "issued_at": datetime(2026, 8, 19, 6, 0, tzinfo=UTC),
        "number": number,
        "lang": lang,
    }
    return CREATION_DATE.sub(b"", render_certificate(document, {}, verify_url))


def test_qr_gets_onto_the_paper():
    with_qr = build_qr("https://domain.kz/verify/KZ-2026-XB7K2M")
    without = build_qr(None)

    assert len(with_qr) > len(without)
    # Прямоугольников заметно больше: рамка есть у обоих, модули — только у QR
    assert qr_rects(with_qr) > qr_rects(without) + 50


def test_a_document_without_a_verify_address_is_still_issued(client, sms, monkeypatch):
    """Пустая настройка — кода нет, а документ выдаётся: QR, ведущий
    в никуда, с бумаги уже не исправить."""
    monkeypatch.setattr(get_settings(), "verify_base_url", "")
    certificate_id = own_certificate(client, sms)

    resp = get_pdf(client, certificate_id)

    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"%PDF")
    assert qr_rects(resp.content) == qr_rects(build_qr(None))


def test_the_code_carries_the_address_of_this_very_certificate():
    """Матрица в документе — ровно та, что кодирует ссылку на этот номер.
    Так проверяется наша сборка ссылки, а не библиотека."""
    number = "KZ-2026-QQ11WW"
    drawn = build_qr(f"https://domain.kz/verify/{number}", number=number)

    assert drawn == build_qr(f"https://domain.kz/verify/{number}", number=number)
    # Номер другой — символ другой, то есть в код уходит именно он
    assert drawn != build_qr("https://domain.kz/verify/KZ-2026-XB7K2M", number=number)


def verify_url_for(base: str, number: str) -> str | None:
    """Ссылка так, как её собирает сценарий. Репозиторий для этого не нужен —
    только конфигурация и номер."""
    service = CertificatePdfService(certificates=None, cfg=get_settings())
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(get_settings(), "verify_base_url", base)
        return service._verify_url(Certificate(number=number))


def test_the_address_in_the_code_is_assembled_exactly():
    assert (
        verify_url_for("https://domain.kz", "KZ-2026-XB7K2M")
        == "https://domain.kz/verify/KZ-2026-XB7K2M"
    )
    # Хвостовой слэш в настройке не должен давать «//verify»
    assert (
        verify_url_for("https://domain.kz/", "KZ-2026-XB7K2M")
        == "https://domain.kz/verify/KZ-2026-XB7K2M"
    )
    # Пусто и пробелы — кода нет вовсе
    assert verify_url_for("", "KZ-2026-XB7K2M") is None
    assert verify_url_for("   ", "KZ-2026-XB7K2M") is None


def test_the_printed_address_carries_no_number():
    """Под кодом стоит короткий адрес без протокола и без номера — ровно
    тот же, что на экране сертификата."""
    assert _verify_host("https://domain.kz/verify/KZ-2026-XB7K2M") == "domain.kz/verify"
    assert _verify_host("http://localhost:3000/verify/KZ-2026-XB7K2M") == "localhost:3000/verify"


def test_kazakh_document_with_a_code_is_built():
    document = build_qr("https://domain.kz/verify/KZ-2026-XB7K2M", lang="kz")

    assert document.startswith(b"%PDF")
    assert qr_rects(document) > 50
