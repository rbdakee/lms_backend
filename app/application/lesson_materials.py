"""Готовый материал глазами учителя: скачать файлом, переписать блок, откатить.

Правка по блокам (TEACHER_AI_BRIEF, раздел 4.1, правило 3): «перепиши
середину урока» переписывает одно поле, остальные остаются как были,
и каждое состояние блока сохраняется версией. Три способа поменять блок —
модель по просьбе учителя, сам учитель руками и откат к прежней версии, —
и все три пишут блок, ключ документа и версию одной транзакцией.

Блоки сервера — раздел программы и цель обучения с кодом — не меняются
ни одним из трёх: это строка приказа, а не текст модели.

Материал виден только своему учителю: чужой — 404, как у комплекта.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy.orm import Session as DbSession

from app.adapters.ai.openai_chat import AiUnavailableError
from app.adapters.db.lesson_ai_repos import LessonMaterialRepo, LessonUsageRepo
from app.adapters.db.models import LessonKit, LessonMaterial, LessonMaterialBlock, User
from app.domain.errors import (
    AiFailedError,
    BlockChangedError,
    BlockLockedError,
    FieldError,
    MaterialNotReadyError,
    NotFoundError,
    ProgramNotPublishedError,
)
from app.domain.lesson_ai import (
    ANSWER_SOURCES,
    FILE_FORMATS,
    BlockOutputInvalidError,
    JobError,
    assemble_answer_key,
)

# Шапка ключей с ответами — блоки сервера из справочника, впереди ответов
KEY_HEAD = ("section", "learning_objectives")


class BlockRewriterPort(Protocol):
    """Что сценарию нужно от генерации: знать блоки вида, проверить блок
    и переписать один блок моделью (`app/lesson_ai/blocks.py`)."""

    def is_server_block(self, kind: str, block: str) -> bool: ...

    def validate(self, kind: str, block: str, value: Any) -> Any: ...

    def prepare(self, db: DbSession, kit: LessonKit, kind: str) -> Any: ...

    def rewrite(
        self, kind: str, block: str, brief: Any, document: dict, instruction: str
    ) -> Any: ...


@dataclass(frozen=True)
class MaterialFile:
    """Всё, из чего роутер собирает файл: содержимое и шапка документа."""

    kind: str
    format: str
    content: dict
    lang: str
    grade: int
    topic: str
    teacher_name: str
    kit_id: int


class LessonMaterialsService:
    def __init__(
        self,
        materials: LessonMaterialRepo,
        rewriter: BlockRewriterPort,
        *,
        db: DbSession,
        commit: Callable[[], None],
        model: str,
    ):
        self.materials = materials
        self.rewriter = rewriter
        self.db = db
        self.commit = commit
        # Для учёта расхода — имя из настройки, как у чата
        self.model = model

    # -- GET /lesson_ai/materials/{id}/file ---------------------------------

    def file(self, user: User, material_id: int) -> MaterialFile:
        material, kit = self._ready(user, material_id)
        fmt = FILE_FORMATS.get(material.kind)
        if fmt is None:
            raise NotFoundError("У этого материала нет файла")
        return MaterialFile(
            kind=material.kind,
            format=fmt,
            content=material.content,
            lang=kit.lang,
            grade=kit.grade,
            topic=kit.topic,
            # ФИО — из профиля в момент скачивания, а не из материала: в
            # содержимое оно не входит нарочно (банк готовых материалов, L7)
            teacher_name=" ".join(
                part for part in (user.last_name, user.first_name, user.middle_name) if part
            ),
            kit_id=kit.id,
        )

    # -- GET /lesson_ai/materials/{id}/blocks -------------------------------

    def blocks(self, user: User, material_id: int) -> dict:
        material, _kit = self._ready(user, material_id)
        return {
            "material_id": material.id,
            "kind": material.kind,
            "items": [
                self._block_out(material, block) for block in self.materials.blocks(material.id)
            ],
        }

    # -- POST /lesson_ai/materials/{id}/blocks/{block}/regenerate -----------

    def regenerate(self, user: User, material_id: int, block_kind: str, instruction: str) -> dict:
        material, kit = self._ready(user, material_id)
        block = self._editable(material, block_kind)
        kind, document = material.kind, dict(material.content)
        token = self.materials.last_version_id(block.id)
        try:
            brief = self.rewriter.prepare(self.db, kit, kind)
        except JobError as exc:
            if exc.code == "program_not_published":
                raise ProgramNotPublishedError() from None
            raise
        # Читающая транзакция закрывается до вызова модели: он идёт секунды,
        # и держать всё это время соединение с открытой транзакцией незачем
        self.commit()

        instruction = instruction.strip()
        try:
            result = self.rewriter.rewrite(kind, block_kind, brief, document, instruction)
        except AiUnavailableError:
            raise AiFailedError() from None
        except BlockOutputInvalidError as exc:
            # Кривые ответы оплачены так же, как ровный
            self._spent(user, kit, kind, exc.tokens_in, exc.tokens_out)
            raise AiFailedError("Модель вернула блок не по форме — попробуйте ещё раз") from None
        # Расход — в учёт сразу и своей транзакцией: модель уже ответила,
        # и заплачено, даже если блок ниже не запишется (его успели поправить)
        self._spent(user, kit, kind, result.tokens_in, result.tokens_out)

        material = self.materials.lock_material(material_id)
        block = self.materials.block(material_id, block_kind)
        if material.status != "ready" or block is None:
            raise MaterialNotReadyError()
        if self.materials.last_version_id(block.id) != token:
            raise BlockChangedError()
        self.materials.write_block(
            material,
            block,
            result.value,
            source="regenerated",
            user_id=user.id,
            instruction=instruction or None,
        )
        self._sync_answer_key(material, user)
        return self._block_out(material, block)

    def _spent(
        self, user: User, kit: LessonKit, kind: str, tokens_in: int, tokens_out: int
    ) -> None:
        """Строка учёта — и сразу коммит: отказ дальше откатил бы транзакцию
        запроса вместе с ней, а деньги уже потрачены."""
        LessonUsageRepo(self.db).add(
            user.id,
            "block",
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            kit_id=kit.id,
            kind=kind,
        )
        self.commit()

    # -- PUT /lesson_ai/materials/{id}/blocks/{block} -----------------------

    def edit(self, user: User, material_id: int, block_kind: str, value: Any) -> dict:
        material, _kit = self._ready(user, material_id)
        self._editable(material, block_kind)
        try:
            value = self.rewriter.validate(material.kind, block_kind, value)
        except ValidationError as exc:
            raise FieldError("content", _first_error(exc)) from None
        return self._write(user, material_id, block_kind, value, source="edited")

    # -- GET /lesson_ai/materials/{id}/blocks/{block}/versions --------------

    def versions(self, user: User, material_id: int, block_kind: str) -> dict:
        material, _kit = self._ready(user, material_id)
        block = self._block(material, block_kind)
        rows = self.materials.versions(block.id)
        return {
            "block": block_kind,
            "items": [
                {
                    "id": version.id,
                    "source": version.source,
                    "instruction": version.instruction,
                    "content": version.content,
                    "created_at": version.created_at,
                    # Текущая — последняя: любая правка, и откат тоже, пишет
                    # новую версию, а не переставляет указатель
                    "current": number == 0,
                }
                for number, version in enumerate(rows)
            ],
        }

    # -- POST /lesson_ai/materials/{id}/blocks/{block}/versions/{v}/restore -

    def restore(self, user: User, material_id: int, block_kind: str, version_id: int) -> dict:
        material, _kit = self._ready(user, material_id)
        block = self._editable(material, block_kind)
        version = self.materials.version(block.id, version_id)
        if version is None:
            raise NotFoundError("Версия не найдена")
        return self._write(user, material_id, block_kind, version.content, source="restored")

    # -- общее --------------------------------------------------------------

    def _ready(self, user: User, material_id: int) -> tuple[LessonMaterial, LessonKit]:
        row = self.materials.material(material_id)
        if row is None or row[1].user_id != user.id:
            raise NotFoundError("Материал не найден")
        material, kit = row
        if material.status != "ready" or material.content is None:
            raise MaterialNotReadyError()
        return material, kit

    def _block(self, material: LessonMaterial, block_kind: str) -> LessonMaterialBlock:
        block = self.materials.block(material.id, block_kind)
        if block is None:
            raise NotFoundError("Блок не найден")
        return block

    def _editable(self, material: LessonMaterial, block_kind: str) -> LessonMaterialBlock:
        block = self._block(material, block_kind)
        if self.rewriter.is_server_block(material.kind, block_kind):
            raise BlockLockedError()
        return block

    def _write(self, user: User, material_id: int, block_kind: str, value, *, source: str) -> dict:
        material = self.materials.lock_material(material_id)
        if material.status != "ready":
            raise MaterialNotReadyError()
        block = self.materials.block(material_id, block_kind)
        self.materials.write_block(material, block, value, source=source, user_id=user.id)
        self._sync_answer_key(material, user)
        return self._block_out(material, block)

    def _sync_answer_key(self, material: LessonMaterial, user: User) -> None:
        """Правка задачи — правка ключей: ответ живёт в той же схеме, что
        задача, и ключи — его сводка (раздел 4.2). Иначе учитель, поправивший
        пример на карточке, проверял бы тетради по старому ответу.

        Пересборка — той же транзакцией, что правка, и ключи под замком
        после материала: порядок замков один у всех правок, взаимной
        блокировки нет.
        """
        if material.kind not in ANSWER_SOURCES:
            return
        key = self.materials.sibling(material.kit_id, "answer_key")
        if key is None or key.status != "ready" or key.content is None:
            return
        # Раздел и цель обучения остаются как были — это строка справочника.
        # Порядок — формы, а не `content`: JSONB раскладывает ключи по-своему
        content = {name: key.content[name] for name in KEY_HEAD if name in key.content}
        content |= assemble_answer_key(self.materials.ready_blocks(material.kit_id, ANSWER_SOURCES))
        self.materials.replace_content(key, content, user_id=user.id)

    def _block_out(self, material: LessonMaterial, block: LessonMaterialBlock) -> dict:
        return {
            "kind": block.kind,
            "order_index": block.order_index,
            "editable": not self.rewriter.is_server_block(material.kind, block.kind),
            "content": block.content,
            "updated_at": block.updated_at,
        }


def _first_error(exc: ValidationError) -> str:
    """Что не так с правкой — по-русски, одной строкой: учитель видит её
    под блоком. Английский текст pydantic ему ничего не скажет."""
    error = exc.errors()[0]
    kind = error["type"]
    if kind == "string_too_short":
        return "Поле не может быть пустым"
    if kind == "too_short":
        return "Нужен хотя бы один пункт"
    if kind == "missing":
        return "Не хватает поля: " + str(error["loc"][-1])
    if kind == "extra_forbidden":
        return "Лишнее поле: " + str(error["loc"][-1])
    return "Блок не той формы"
