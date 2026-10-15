"""Вложения в чат «Жоспара» (решение владельца 16.10.2026).

Проверяется то, на чём держится продукт **в коде, а не в промпте**:

- вид файла определяется по сигнатуре байтов: переименование расширением
  не обманывает, а HEIC получает просьбу «сохраните как JPG»;
- лимиты — размер, число файлов на чат, страницы PDF — отбивают файл до
  записи в хранилище;
- выжимка снимается с файла **один раз** (одним вызовом модели или вообще
  без неё у docx/pptx), ложится в `summary` и в учёт расхода;
- выжимки идут в служебное сообщение чата и в контекст генерации, а сам
  файл — никогда;
- комплект, собранный по файлам учителя, личный: в банк не идёт.
"""

import io
import zipfile

import pytest
from sqlalchemy.orm import Session as OrmSession

from app.adapters.ai.openai_chat import AiUnavailableError, MediaUnsupportedError
from app.adapters.ai.zai_chat import ZaiChat
from app.adapters.db.base import get_engine
from app.adapters.db.models import LessonAiAttachment, LessonAiUsage, LessonKit
from app.application.lesson_ai_attachments import AttachmentRunner
from app.config import get_settings
from app.domain.lesson_ai import ATTACHMENT_SUMMARY_MAX
from app.lesson_ai.context import describe, prepare
from app.lesson_ai.prompts.attachment import materials_block
from tests.conftest import login
from tests.test_lesson_ai_chat import LEVELS, NOTHING, Scene, ready_chat

pytestmark = pytest.mark.usefixtures("lesson_ai_open")

# -- байты настоящих форматов: сигнатуры, а не фейковые строки ---------------

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
WEBP = b"RIFF\x18\x00\x00\x00WEBPVP8 \x0c\x00\x00\x00" + b"\x00" * 16
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1heic" + b"\x00" * 16
PDF = (
    b"%PDF-1.7\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R 4 0 R 5 0 R]/Count 3>>endobj\n"
    + b"".join(
        f"{n} 0 obj<</Type/Page/Parent 2 0 R>>endobj\n".encode() for n in (3, 4, 5)
    )
    + b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def pdf_with_pages(count: int) -> bytes:
    head = b"%PDF-1.7\n2 0 obj<</Type/Pages/Kids[" + b",".join(
        f"{n} 0 R".encode() for n in range(3, 3 + count)
    ) + f"]/Count {count}>>endobj\n".encode()
    body = b"".join(
        f"{n} 0 obj<</Type/Page/Parent 2 0 R>>endobj\n".encode() for n in range(3, 3 + count)
    )
    return head + body + b"trailer<</Root 1 0 R>>\n%%EOF\n"


def zip_with(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "<xml/>")
    return buffer.getvalue()


DOCX = zip_with("word/document.xml", "[Content_Types].xml")
PPTX = zip_with("ppt/presentation.xml", "[Content_Types].xml")
PLAIN_ZIP = zip_with("photos/1.jpg")


def real_docx() -> bytes:
    """Настоящий .docx от python-docx: разбор должен прочитать абзац и ячейку."""
    import docx

    buffer = io.BytesIO()
    document = docx.Document()
    document.add_paragraph("Тема урока: сложение двузначных чисел")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Задание"
    table.rows[0].cells[1].text = "40+17"
    document.save(buffer)
    return buffer.getvalue()


def real_pptx() -> bytes:
    import pptx

    buffer = io.BytesIO()
    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(0, 0, 100, 100).text_frame.text = "Сегодня на уроке"
    presentation.save(buffer)
    return buffer.getvalue()


# -- хелперы ---------------------------------------------------------------


def upload(client, chat_id: int, data: bytes, name: str = "file.pdf"):
    return client.post(
        f"/lesson_ai/chats/{chat_id}/attachments",
        files={"file": (name, data, "application/octet-stream")},
    )


def new_chat(client) -> dict:
    return client.post("/lesson_ai/chats").json()


def runner(ai, storage) -> AttachmentRunner:
    """Фоновый разбор руками, как `JobRunner.run_next` в тестах генерации:
    воркеры в тестах не поднимаются (`LESSON_AI_WORKERS=0`)."""
    return AttachmentRunner(
        lambda: OrmSession(get_engine()), lambda: storage, ai, model="gpt-4.1"
    )


@pytest.fixture
def scene() -> Scene:
    return Scene()


@pytest.fixture(autouse=True)
def teacher(client, sms):
    """Ручки вложений — за входом и доступом «Жоспара» (последний открыт
    фикстурой `lesson_ai_open`): в каждом тесте уже вошедший учитель.
    Второму «устройству» тест входит сам, тем же клиентом SMS."""
    login(client, sms)


# -- вид по сигнатуре ------------------------------------------------------


def test_kind_by_signature_not_by_extension(client, scene):
    """PNG, названный .pdf, — картинка, а не PDF: вид читается из байтов."""
    chat = new_chat(client)
    resp = upload(client, chat["id"], PNG, name="page.pdf")
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["kind"] == "image"
    assert row["mime"] == "image/png"
    assert row["status"] == "processing"
    assert row["filename"] == "page.pdf"


def test_kind_variants(client):
    chat = new_chat(client)
    kinds = [
        (JPEG, "image"),
        (WEBP, "image"),
        (PDF, "pdf"),
        (DOCX, "docx"),
        (PPTX, "pptx"),
    ]
    for data, kind in kinds:
        resp = upload(client, chat["id"], data)
        assert resp.status_code == 201, resp.text
        assert resp.json()["kind"] == kind


def test_unknown_bytes_rejected(client):
    chat = new_chat(client)
    resp = upload(client, chat["id"], b"\x00\x01\x02\x03\x04not-a-format")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "attachment_type"


def test_plain_zip_rejected(client):
    """Архив с картинками, переименованный в .docx, — не Word: обязательной
    части в ZIP нет."""
    chat = new_chat(client)
    resp = upload(client, chat["id"], PLAIN_ZIP, name="file.docx")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "attachment_type"


def test_heic_asks_for_jpg(client):
    chat = new_chat(client)
    resp = upload(client, chat["id"], HEIC, name="photo.heic")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "attachment_type"
    assert "JPG" in body["error"]["message"]


def test_pdf_pages_counted(client):
    chat = new_chat(client)
    resp = upload(client, chat["id"], PDF)
    assert resp.json()["pages"] == 3


# -- лимиты ----------------------------------------------------------------


def test_pdf_too_many_pages(client):
    chat = new_chat(client)
    resp = upload(client, chat["id"], pdf_with_pages(21))
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "attachment_pages"
    assert body["details"]["pages"] == 21


def test_file_too_big_rejected(client):
    chat = new_chat(client)
    # Больше лимита на байт: ровно на лимите должно приниматься
    limit = get_settings().lesson_ai_attach_max_mb * 1024 * 1024
    resp = upload(client, chat["id"], PNG + b"\x00" * limit, name="big.png")
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "attachment_too_big"


def test_empty_file_rejected(client):
    chat = new_chat(client)
    resp = upload(client, chat["id"], b"")
    assert resp.status_code == 422


def test_five_files_per_chat(client):
    limit = get_settings().lesson_ai_attach_max_files
    chat = new_chat(client)
    for _ in range(limit):
        assert upload(client, chat["id"], PNG).status_code == 201
    resp = upload(client, chat["id"], PNG)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "attachment_limit"


# -- чужое -----------------------------------------------------------------


def test_other_chat_is_404(client, client2, sms, scene):
    chat = new_chat(client)
    login(client2, sms, phone="+7 (707) 555-01-02")
    assert upload(client2, chat["id"], PNG).status_code == 404
    assert client2.get(f"/lesson_ai/chats/{chat['id']}/attachments").status_code == 404


def test_attachment_of_other_chat_is_404(client, client2, sms, storage):
    chat = new_chat(client)
    attachment = upload(client, chat["id"], PNG).json()
    login(client2, sms, phone="+7 (707) 555-01-02")
    other = new_chat(client2)
    resp = client2.delete(f"/lesson_ai/chats/{other['id']}/attachments/{attachment['id']}")
    assert resp.status_code == 404
    # И в свой чат файл чужого чата приложить нельзя
    resp = client2.post(
        f"/lesson_ai/chats/{other['id']}/messages",
        json={"text": "возьми файл", "attachment_ids": [attachment["id"]]},
    )
    assert resp.status_code == 404


# -- разбор: один раз, в учёт, со статусом ---------------------------------


def _last_attachment(chat_id: int) -> int:
    with OrmSession(get_engine()) as db:
        row = (
            db.query(LessonAiAttachment)
            .filter_by(chat_id=chat_id)
            .order_by(LessonAiAttachment.id.desc())
            .first()
        )
        assert row is not None
        return row.id


def _row(attachment_id: int) -> LessonAiAttachment:
    """Строка вложения после разбора — отсоединённая от сессии."""
    with OrmSession(get_engine()) as db:
        row = db.get(LessonAiAttachment, attachment_id)
        assert row is not None
        db.expunge(row)
        return row


def _ready(ai, storage, attachment_id: int) -> LessonAiAttachment:
    """Один шаг фонового разбора, затем строка вложения."""
    assert runner(ai, storage).run_next() is True
    return _row(attachment_id)


def test_image_summary_calls_model_once(client, ai, storage):
    chat = new_chat(client)
    attachment = upload(client, chat["id"], PNG).json()
    ai.describes("Текст задания: 40+17. Ниже рисунок — числовой луч.", tokens_in=50, tokens_out=20)
    row = _ready(ai, storage, attachment["id"])
    assert row.status == "ready"
    assert row.summary.startswith("Текст задания")
    # Один файл — один вызов модели, и в нём тот самый файл
    assert len(ai.media_calls) == 1
    call = ai.media_calls[0]
    assert call["media"].data == PNG
    assert call["media"].mime == "image/png"
    # Разбор платит: строка учёта с новым источником
    with OrmSession(get_engine()) as db:
        usage = db.query(LessonAiUsage).filter_by(source="attachment").one()
        assert usage.tokens_in == 50
        assert usage.chat_id == chat["id"]
        assert usage.kind == "image"


def test_pdf_summary_gets_the_whole_file(client, ai, storage):
    chat = new_chat(client)
    attachment = upload(client, chat["id"], PDF, name="учебник.pdf").json()
    ai.describes("Глава 1. Сложение…")
    row = _ready(ai, storage, attachment["id"])
    assert row.status == "ready"
    assert ai.media_calls[0]["media"].data == PDF
    assert ai.media_calls[0]["media"].mime == "application/pdf"
    assert ai.media_calls[0]["media"].filename == "учебник.pdf"


def test_docx_and_pptx_without_model(client, ai, storage):
    """Текст Word и PowerPoint лежит в самом файле — модель не зовётся,
    и в учёте от разбора таких файлов строки нет."""
    chat = new_chat(client)
    upload(client, chat["id"], real_docx(), name="конспект.docx")
    upload(client, chat["id"], real_pptx(), name="слайды.pptx")
    while runner(ai, storage).run_next():
        pass
    assert ai.media_calls == []
    with OrmSession(get_engine()) as db:
        from app.adapters.db.models import LessonAiAttachment

        rows = db.query(LessonAiAttachment).filter_by(chat_id=chat["id"]).all()
        by_kind = {row.kind: row for row in rows}
    assert "сложение двузначных" in by_kind["docx"].summary
    assert "Задание\t40+17" in by_kind["docx"].summary
    assert "Сегодня на уроке" in by_kind["pptx"].summary
    assert by_kind["docx"].status == "ready"


def test_broken_docx_fails_without_crash(client, ai, storage):
    """ZIP с обязательной частью, но битым содержимым: разбор ставит failed,
    а не валит воркер."""
    chat = new_chat(client)
    attachment = upload(client, chat["id"], DOCX, name="битый.docx").json()
    row = _ready(ai, storage, attachment["id"])
    assert row.status == "failed"
    assert row.error_code == "extract_failed"


def test_provider_down_marks_failed(client, ai, storage):
    chat = new_chat(client)
    attachment = upload(client, chat["id"], PNG).json()
    ai.media_fails(AiUnavailableError("Модель не ответила"))
    row = _ready(ai, storage, attachment["id"])
    assert row.status == "failed"
    assert row.error_code == "ai_failed"


def test_summary_trimmed_with_note(client, ai, storage):
    chat = new_chat(client)
    attachment = upload(client, chat["id"], PNG).json()
    long_text = "слово " * (ATTACHMENT_SUMMARY_MAX // 3)
    ai.describes(long_text)
    row = _ready(ai, storage, attachment["id"])
    assert len(row.summary) < len(long_text)
    assert "обрезан" in row.summary


def test_runner_idle_when_nothing_to_do(ai, storage):
    assert runner(ai, storage).run_next() is False


def test_zai_cannot_read_files():
    """GLM файлы не читает — отказ кодом, а не «модель не ответила»:
    повторной попыткой этот случай не лечится."""
    cfg = get_settings()
    adapter = ZaiChat(cfg, model="glm-5")
    with pytest.raises(MediaUnsupportedError):
        adapter.describe_media(
            system="s",
            media=type("M", (), {"mime": "image/png", "data": PNG, "filename": "x"})(),
            instruction="i",
        )


def test_openai_adapter_builds_data_uri():
    """Файл уходит `data:`-URI в content part: у провайдера нет доступа
    к нашему хранилищу, байты едут в запросе."""
    from app.adapters.ai.openai_chat import _data_uri
    from app.application.ports import AiMedia

    uri = _data_uri(AiMedia(mime="application/pdf", data=PDF, filename="u.pdf"))
    assert uri.startswith("data:application/pdf;base64,")


# -- реплика с вложениями ---------------------------------------------------


def test_message_binds_attachments(client, ai, scene):
    chat = new_chat(client)
    first = upload(client, chat["id"], PNG).json()
    second = upload(client, chat["id"], PDF).json()
    ai.returns_json(NOTHING)
    ai.says("Взял материалы в работу")
    resp = client.post(
        f"/lesson_ai/chats/{chat['id']}/messages",
        json={"text": "вот страница учебника", "attachment_ids": [first["id"], second["id"]]},
    )
    assert resp.status_code == 200, resp.text
    feed = client.get(f"/lesson_ai/chats/{chat['id']}/messages").json()
    user_rows = [item for item in feed["items"] if item["role"] == "user"]
    attached = user_rows[-1]["attachments"]
    assert [a["id"] for a in attached] == [first["id"], second["id"]]
    assert attached[0]["kind"] == "image"
    with OrmSession(get_engine()) as db:
        from app.adapters.db.models import LessonAiAttachment

        rows = db.query(LessonAiAttachment).filter_by(chat_id=chat["id"]).all()
        assert {row.message_id for row in rows} == {user_rows[-1]["id"]}


def test_double_bind_rejected(client, ai, scene):
    chat = new_chat(client)
    attachment = upload(client, chat["id"], PNG).json()
    body = {"text": "раз", "attachment_ids": [attachment["id"]]}
    ai.returns_json(NOTHING)
    ai.says("Хорошо")
    assert client.post(
        f"/lesson_ai/chats/{chat['id']}/messages", json=body
    ).status_code == 200
    ai.returns_json(NOTHING)
    ai.says("Хорошо")
    resp = client.post(f"/lesson_ai/chats/{chat['id']}/messages", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


# -- выжимки в состоянии чата ----------------------------------------------


def test_materials_in_chat_state(client, ai, storage, scene):
    """Готовая выжимка идёт в служебное сообщение хода; ещё читающийся файл —
    строкой «файл ещё читается»."""
    chat = ready_chat(client, scene)
    upload(client, chat["id"], PNG)
    upload(client, chat["id"], PDF)
    ai.describes("На фото страница 34 учебника: задания 1–5.")
    assert runner(ai, storage).run_next() is True
    ai.returns_json(NOTHING)
    ai.says("Хорошо")
    resp = client.post(f"/lesson_ai/chats/{chat['id']}/messages", json={"text": "готовь"})
    assert resp.status_code == 200, resp.text
    state = [m.content for m in ai.calls[-1]["messages"] if m.role == "system"][0]
    assert "Материалы учителя" in state
    assert "страница 34" in state
    assert "PDF-документ" in state
    assert "файл ещё читается" in state


def test_materials_budget_trims_evenly(client, ai, storage):
    """Общий бюджет на ход: длинные выжимки урезаются равномерно, а первый
    файл не съедает бюджет целиком."""
    rows = [
        {"kind": "pdf", "pages": 3, "summary": "А" * 9000},
        {"kind": "image", "pages": None, "summary": "Б" * 9000},
    ]
    block = materials_block(rows, budget=8000)
    first = block.index("А")
    second = block.index("Б")
    assert block.count("А") < 9000
    assert block.count("Б") < 9000
    # Обе доли равные: никого не оставили за бортом
    assert abs(block.count("А") - block.count("Б")) <= 1
    assert first < second


def test_empty_materials_block():
    assert materials_block([], budget=8000) == ""


# -- генерация: выжимки в контекст, комплект личный -------------------------


def _confirm_with_attachment(client, ai, storage, scene):
    chat = ready_chat(client, scene)
    upload(client, chat["id"], PNG, name="учебник.png")
    ai.describes("На фото страница 34: задания на сложение.", tokens_in=50, tokens_out=20)
    assert runner(ai, storage).run_next() is True
    resp = client.post(
        f"/lesson_ai/chats/{chat['id']}/confirm",
        json={"kinds": ["lesson_plan"], **LEVELS},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_kit_with_attachments_is_personal(client, ai, storage, scene):
    """Урок, собранный по файлам учителя, в банк не идёт — даже без пожеланий."""
    result = _confirm_with_attachment(client, ai, storage, scene)
    kit_id = result["kit"]["id"]
    with OrmSession(get_engine()) as db:
        kit = db.get(LessonKit, kit_id)
        assert kit.personal is True
        # И на уровне prepare: банк не получает слот
        prepared = prepare(db, kit, bank=True)
    assert prepared.bank is None
    text = describe(prepared.brief)
    assert "Материалы учителя" in text
    assert "страница 34" in text
    # Сам файл модели не уходит — только выжимка
    assert "PNG" not in text


def test_materials_survive_regeneration(client, ai, storage, scene):
    """Пересборка (кнопка «Пересобрать») тоже видит выжимки: prepare читает
    вложения по комплекту, а не по моменту заказа."""
    result = _confirm_with_attachment(client, ai, storage, scene)
    kit_id = result["kit"]["id"]
    # Задание плана закрываем руками — воркеров в тестах нет, а пересборка
    # идёт только с готовым комплектом
    with OrmSession(get_engine()) as db:
        from app.adapters.db.lesson_ai_repos import LessonAiJobRepo
        from app.adapters.db.models import LessonAiJob

        job = db.query(LessonAiJob).filter_by(kit_id=kit_id, kind="lesson_plan").one()
        LessonAiJobRepo(db).done(
            job, {"section": {}, "learning_objectives": []}, user_id=1
        )
        db.commit()
    resp = client.post(f"/lesson_ai/kits/{kit_id}/regenerate", json={})
    assert resp.status_code == 200, resp.text
    with OrmSession(get_engine()) as db:
        prepared = prepare(db, db.get(LessonKit, kit_id), with_plan=False, bank=True)
    assert "Материалы учителя" in describe(prepared.brief)
    assert "страница 34" in describe(prepared.brief)


# -- удаление ---------------------------------------------------------------


def test_delete_removes_row_and_object(client, storage):
    chat = new_chat(client)
    attachment = upload(client, chat["id"], PNG).json()
    assert storage.objects, "файл должен лежать в хранилище до удаления"
    resp = client.delete(f"/lesson_ai/chats/{chat['id']}/attachments/{attachment['id']}")
    assert resp.status_code == 204
    assert not storage.objects
    feed = client.get(f"/lesson_ai/chats/{chat['id']}/attachments").json()
    assert feed["items"] == []
    resp = client.delete(f"/lesson_ai/chats/{chat['id']}/attachments/{attachment['id']}")
    assert resp.status_code == 404
