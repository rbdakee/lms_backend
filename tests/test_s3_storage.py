"""Адаптер объектного хранилища.

Настоящий бакет тестам не нужен и вреден: сеть, ключи и мусор в чужом
хранилище. Клиент здесь — заглушка с теми же вызовами, какие делает
адаптер; проверяется наше — выбор между одним запросом и составной
выгрузкой, подсчёт размера на лету, уборка за оборвавшейся загрузкой
и «объекта нет» вместо исключения.

Чего заглушка не проверяет и проверить не может — что настоящее хранилище
принимает эти запросы. Это сверено руками против `lms-main` в PS Cloud
(сессия 9, список изменений), и там же выяснилось, ради чего в клиенте
стоит `request_checksum_calculation`.
"""

import pytest
from botocore.exceptions import ClientError

from app.adapters.storage.s3_storage import S3Storage, _StreamReader
from app.domain.errors import FileTooLargeError

KEY = "uploads/2026/08/21/9f3c1a7e4b2d8c05.pdf"


def missing(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, operation)


class FakeBody:
    def __init__(self, data: bytes):
        self.data = data
        self.closed = False

    def iter_chunks(self, size: int):
        for start in range(0, len(self.data), size):
            yield self.data[start : start + size]

    def close(self) -> None:
        self.closed = True


class FakeS3Client:
    """Имена аргументов — как у boto3, иначе заглушка проверяла бы не тот
    вызов. Незавершённая составная выгрузка живёт отдельно от объектов:
    наружу её не видно, а место она занимает — как и в настоящем бакете."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, list[bytes]] = {}
        self.bodies: list[FakeBody] = []
        self.aborted: list[str] = []

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.objects[Key] = Body

    def create_multipart_upload(self, Bucket, Key):  # noqa: N803
        upload_id = f"upload-{len(self.uploads) + 1}"
        self.uploads[upload_id] = []
        return {"UploadId": upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):  # noqa: N803
        assert len(self.uploads[UploadId]) == PartNumber - 1, "куски идут по порядку"
        self.uploads[UploadId].append(Body)
        return {"ETag": f'"etag-{PartNumber}"'}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):  # noqa: N803
        parts = MultipartUpload["Parts"]
        assert [p["PartNumber"] for p in parts] == list(range(1, len(parts) + 1))
        self.objects[Key] = b"".join(self.uploads.pop(UploadId))

    def abort_multipart_upload(self, Bucket, Key, UploadId):  # noqa: N803
        self.uploads.pop(UploadId, None)
        self.aborted.append(UploadId)

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise missing("HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise missing("GetObject")
        body = FakeBody(self.objects[Key])
        self.bodies.append(body)
        return {"Body": body}


def storage() -> tuple[S3Storage, FakeS3Client]:
    client = FakeS3Client()
    return S3Storage(client, "lms-main"), client


def small_parts(monkeypatch, size: int = 8) -> None:
    """Порог составной выгрузки — 8 МБ, и держать их в тесте незачем:
    те же ветки видны на восьми байтах."""
    monkeypatch.setattr("app.adapters.storage.s3_storage.PART_SIZE", size)


def torn(*chunks: bytes):
    """Загрузка, оборвавшаяся на середине: так выглядит превышение лимита
    в 20 МБ и так же — оборванное соединение."""

    def stream():
        yield from chunks
        raise FileTooLargeError(20)

    return stream()


# -- Один запрос: обычный случай ----------------------------------------


def test_writes_and_reads():
    s3, client = storage()

    assert s3.save(KEY, [b"%PDF", b"-1.4 fake content"]) == 21
    assert client.objects[KEY] == b"%PDF-1.4 fake content"
    assert client.uploads == {}, "составная выгрузка на 21 байт не нужна"
    assert s3.size(KEY) == 21
    assert b"".join(s3.read(KEY)) == b"%PDF-1.4 fake content"


def test_a_torn_upload_never_reaches_the_bucket():
    """Обрыв до первого запроса: объекта не появится, потому что запроса
    не было вовсе. Один PUT атомарен — недописанному объекту взяться
    неоткуда."""
    s3, client = storage()

    with pytest.raises(FileTooLargeError):
        s3.save(KEY, torn(b"start"))
    assert client.objects == {}


# -- Составная выгрузка -------------------------------------------------


def test_a_large_object_goes_in_parts(monkeypatch):
    small_parts(monkeypatch)
    s3, client = storage()

    assert s3.save(KEY, [b"abcdefghij", b"klmnopqrst", b"uv"]) == 22
    assert client.objects[KEY] == b"abcdefghijklmnopqrstuv"
    assert client.uploads == {}, "завершённая выгрузка не остаётся висеть"
    assert client.aborted == []


def test_a_torn_large_upload_is_aborted(monkeypatch):
    """Незавершённая составная выгрузка наружу не видна, но место занимает:
    обрыв обязан её отменить, иначе бакет копит невидимый мусор."""
    small_parts(monkeypatch)
    s3, client = storage()

    with pytest.raises(FileTooLargeError):
        s3.save(KEY, torn(b"abcdefghij", b"klmnopqrst"))

    assert client.objects == {}
    assert client.uploads == {}
    assert client.aborted == ["upload-1"]


def test_a_failed_abort_does_not_replace_the_real_error(monkeypatch):
    """Уборка не важнее самой неудачи: наружу уходит исходная ошибка, иначе
    ручка вместо понятного «файл больше 20 МБ» отдаёт 500 про хранилище."""
    small_parts(monkeypatch)
    s3, client = storage()

    def abort_multipart_upload(Bucket, Key, UploadId):  # noqa: N803
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "AbortMultipartUpload")

    client.abort_multipart_upload = abort_multipart_upload
    with pytest.raises(FileTooLargeError):
        s3.save(KEY, torn(b"abcdefghij"))


# -- Чтение и «объекта нет» ---------------------------------------------


def test_missing_object_has_no_size():
    """`size` — дешёвая проверка перед раздачей, и «объекта нет» для неё
    обычный ответ: строка в базе есть, байтов нет — скачивающий получит
    контрактное 404, а не 500."""
    s3, _ = storage()
    assert s3.size("uploads/2026/08/21/нет.pdf") is None


def test_a_refusal_that_is_not_a_missing_object_is_not_silenced():
    """А вот отказ доступа или неверный бакет молчать не должны: это
    поломка настройки, и выглядеть она обязана поломкой, а не пустым
    хранилищем, в котором «просто ничего нет»."""
    s3, client = storage()

    def head_object(Bucket, Key):  # noqa: N803
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")

    client.head_object = head_object
    with pytest.raises(ClientError):
        s3.size(KEY)


def test_read_closes_the_body():
    """Соединение возвращается в пул, даже если скачивающий ушёл на
    середине файла."""
    s3, client = storage()
    s3.save(KEY, [b"x" * 100])

    chunks = s3.read(KEY)
    next(chunks)
    chunks.close()

    assert client.bodies[-1].closed


# -- Куски порта как поток ----------------------------------------------


def test_reader_gives_out_exactly_what_was_asked():
    """В запрос уходит кусок нашей длины, а порт отдаёт куски своей:
    границы не совпадают, и склеивать их — работа читателя."""
    reader = _StreamReader([b"abc", b"defgh", b"ij"])

    assert reader.read(4) == b"abcd"
    assert reader.read(4) == b"efgh"
    assert reader.read(4) == b"ij"
    assert reader.read(4) == b""
    assert reader.size == 10


def test_reader_counts_what_it_gave_out():
    """Размер порт обещает вернуть, а второго прохода по итератору сделать
    уже нечем — значит считаем по дороге."""
    reader = _StreamReader([b"x" * 1000, b"y" * 500])

    while reader.read(256):
        pass
    assert reader.size == 1500
