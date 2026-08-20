"""Хранилище в S3-совместимом бакете.

На месте локального каталога в бою стоит этот адаптер, и порт от этого
не меняется: домену по-прежнему нужны ровно три действия, а про бакеты,
подписи запросов и составные выгрузки знает только этот файл.

Публичного адреса у объекта нет и здесь: бакет приватный, наружу уходит
своя подписанная ссылка на 15 минут, а байты сервис отдаёт сам
(`app/application/files.py`). Пресайн-ссылки самого хранилища для этого
не годятся — они ведут на чужой домен и живут по чужим правилам.

Выгрузка собрана руками, а не через `upload_fileobj`: тот на потоке,
который нельзя перемотать, отправляет тело без `Content-Length`, и PS Cloud
отвечает `MissingContentLength`. Здесь каждый запрос уходит с готовым
куском байтов, и длина известна всегда.
"""

from collections.abc import Iterable, Iterator
from contextlib import suppress

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import Settings

CHUNK_SIZE = 1024 * 1024
# Кусок составной выгрузки. Меньше 5 МБ хранилище не примет (кроме
# последнего), а больше незачем: файл и так ограничен 20 МБ (UPLOAD_MAX_MB),
# и это же потолок памяти на одну загрузку.
PART_SIZE = 8 * 1024 * 1024

# Хранилища отвечают по-разному: у head_object тела нет и botocore называет
# отсутствие объекта кодом «404», у get_object — «NoSuchKey».
_MISSING = ("404", "NoSuchKey")

# botocore с версии 1.36 по умолчанию считает контрольную сумму загрузки
# и шлёт тело кодировкой `aws-chunked`: вместо Content-Length уходит
# потоковая кодировка, и не-AWS хранилища отвечают `MissingContentLength`.
# `when_required` возвращает прежнее поведение — сумма считается там, где
# её просит сама операция.
_CHECKSUMS = Config(
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required",
)

_client = None


def s3_client(cfg: Settings):
    """Клиент один на процесс: его создание — это разбор моделей botocore,
    десятки миллисекунд, и на каждый запрос это заметно."""
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=cfg.s3_endpoint_url,
            region_name=cfg.s3_region,
            aws_access_key_id=cfg.s3_access_key,
            aws_secret_access_key=cfg.s3_secret_key,
            config=_CHECKSUMS,
        )
    return _client


class _StreamReader:
    """Куски порта как поток, из которого можно взять ровно сколько нужно:
    `save` получает куски чужой длины, а в запрос уходит кусок нашей.

    Копить всё в памяти нельзя — это те самые 20 МБ на каждую загрузку,
    ради которых порт и отдаёт куски.
    """

    def __init__(self, chunks: Iterable[bytes]):
        self._chunks = iter(chunks)
        self._buffer = bytearray()
        # Сколько байт ушло в хранилище — тот самый размер, который порт
        # обещает вернуть. Считаем по дороге: второй проход по итератору
        # сделать уже нечем.
        self.size = 0

    def read(self, amount: int) -> bytes:
        """Ровно `amount` байт; меньше — только если поток кончился."""
        while len(self._buffer) < amount:
            chunk = next(self._chunks, None)
            if chunk is None:
                break
            self._buffer += chunk
        take = min(amount, len(self._buffer))
        data = bytes(self._buffer[:take])
        del self._buffer[:take]
        self.size += len(data)
        return data


class S3Storage:
    def __init__(self, client, bucket: str):
        self.client = client
        self.bucket = bucket

    def save(self, key: str, chunks: Iterable[bytes]) -> int:
        reader = _StreamReader(chunks)
        first = reader.read(PART_SIZE)
        if len(first) < PART_SIZE:
            # Обычный случай: памятка, обложка, сданная работа. Один запрос,
            # и он атомарен — недописанному объекту взяться неоткуда
            self.client.put_object(Bucket=self.bucket, Key=key, Body=first)
        else:
            self._save_parts(key, reader, first)
        return reader.size

    def _save_parts(self, key: str, reader: _StreamReader, first: bytes) -> None:
        """Составная выгрузка. Незавершённая наружу не видна, но место
        занимает — поэтому обрыв обязан её отменить."""
        upload_id = self.client.create_multipart_upload(Bucket=self.bucket, Key=key)["UploadId"]
        parts: list[dict] = []
        try:
            part = first
            while part:
                number = len(parts) + 1
                done = self.client.upload_part(
                    Bucket=self.bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=number,
                    Body=part,
                )
                parts.append({"ETag": done["ETag"], "PartNumber": number})
                part = reader.read(PART_SIZE)
            self.client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            # Превышен лимит или оборвалась загрузка: объекта под ключом
            # не появится, а куски убираем за собой
            self._abort(key, upload_id)
            raise

    def size(self, key: str) -> int | None:
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError as failed:
            # Ключ приходит снаружи — из тела PATCH: «такого объекта нет» для
            # него значит контрактное 404, а вот отказ доступа или неверный
            # бакет молчать не должны, это поломка настройки.
            if failed.response.get("Error", {}).get("Code") in _MISSING:
                return None
            raise

    def read(self, key: str) -> Iterator[bytes]:
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            yield from body.iter_chunks(CHUNK_SIZE)
        finally:
            # Соединение возвращается в пул, даже если скачивающий ушёл
            # на середине файла
            body.close()

    def _abort(self, key: str, upload_id: str) -> None:
        # Уборка за неудачной загрузкой не важнее самой неудачи: наружу
        # должна уйти исходная ошибка, а не отказ хранилища на отмене
        with suppress(ClientError):
            self.client.abort_multipart_upload(
                Bucket=self.bucket, Key=key, UploadId=upload_id
            )
