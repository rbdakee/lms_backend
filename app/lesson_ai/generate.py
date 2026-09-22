"""Генерация одного материала по схеме его вида.

Устройство общее на все виды: вид описывается схемой ответа, промптом
и списком блоков, которые кладёт сервер (`KindSpec`), а вызов модели,
разбор ответа и подстановка цели обучения — здесь, одни на всех. L5
размножает виды описаниями, а не копиями этого кода.

Ответ модели проверяется трижды, и каждый отказ — повод спросить ещё раз
(`model_output_invalid`, с повтором): это JSON; он лёг в схему, и пустых
полей нет; в нём нет кода цели обучения. Код модели не давали — значит,
найденный код она сочинила, а сочинённый код проваливает проверку у завуча.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session as DbSession

from app.adapters.ai.openai_chat import AiUnavailableError
from app.adapters.db.models import LessonKit
from app.application.lesson_ai_queue import Generated
from app.application.ports import AiMessage, AiPort
from app.domain.lesson_ai import JobError, objective_codes_in
from app.lesson_ai.context import LessonBrief, Prepared, prepare
from app.lesson_ai.schemas import strict_schema

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KindSpec:
    """Вид материала: что пишет модель и что кладёт сервер."""

    kind: str
    draft: type[BaseModel]
    system: str
    user_message: Callable[[LessonBrief], str]
    # Блоки сервера, в порядке формы, — впереди блоков модели
    server_blocks: tuple[str, ...]


class SchemaGenerator:
    def __init__(self, ai: AiPort, model: str, spec: KindSpec):
        self.ai = ai
        self.model = model
        self.spec = spec
        # Схема не меняется от урока к уроку — собирается один раз
        self.schema = strict_schema(spec.draft)

    def prepare(self, db: DbSession, kit: LessonKit) -> Prepared:
        return prepare(db, kit)

    def generate(self, prepared: Prepared) -> Generated:
        try:
            reply = self.ai.complete_json(
                system=self.spec.system,
                messages=[
                    AiMessage(role="user", content=self.spec.user_message(prepared.brief))
                ],
                name=self.spec.kind,
                schema=self.schema,
            )
        except AiUnavailableError:
            raise JobError("ai_failed", retryable=True) from None
        tokens = {"tokens_in": reply.tokens_in, "tokens_out": reply.tokens_out}
        draft = self._checked(reply.text, tokens)
        server = {"section": prepared.section, "learning_objectives": prepared.objectives}
        content = {name: server[name] for name in self.spec.server_blocks}
        content.update(draft)
        return Generated(content=content, **tokens)

    def _checked(self, text: str, tokens: dict) -> dict:
        try:
            draft = self.spec.draft.model_validate_json(text)
        except ValidationError as exc:
            # В лог — где и что не так, но не само содержимое: в нём тема урока
            # и пожелания учителя
            where = [(".".join(map(str, e["loc"])), e["type"]) for e in exc.errors()[:10]]
            log.warning("%s: ответ модели не по схеме: %s", self.spec.kind, where)
            raise JobError("model_output_invalid", retryable=True, **tokens) from None
        data = draft.model_dump(mode="json")
        codes = objective_codes_in(data)
        if codes:
            log.warning("%s: модель вписала коды целей: %s шт.", self.spec.kind, len(codes))
            raise JobError("model_output_invalid", retryable=True, **tokens)
        return data
