"""Правка материала по блокам: какой блок чем проверяется и как модель
переписывает один блок, не трогая соседние (TEACHER_AI_BRIEF, раздел 4.1,
правило 3).

Схема блока — поле схемы вида, вынутое поодиночке: ответ модели на «перепиши
середину урока» проверяется тем же описанием, что и при генерации всего плана,
и ручная правка учителя — тоже. Два описания одного поля разошлись бы.

Модели показывают весь текущий документ, кроме блоков сервера: новый блок
обязан согласовываться с соседними, а цель обучения с кодом модель не видит
ни здесь, ни при генерации — её урок приходит из `LessonBrief`.
"""

import json
import logging
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, ValidationError, create_model
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.models import LessonKit
from app.application.ports import AiMessage, AiPort
from app.domain.lesson_ai import (
    BlockOutputInvalidError,
    objective_codes_in,
    strip_objective_codes,
)
from app.lesson_ai.context import LessonBrief, describe, prepare, strip_codes_deep
from app.lesson_ai.generate import KindSpec
from app.lesson_ai.kinds import SPECS
from app.lesson_ai.schemas import strict_schema

log = logging.getLogger(__name__)

# Кривой ответ модели на один блок переспрашивается один раз, прямо в запросе:
# учитель ждёт у экрана, а третья попытка — ещё десяток секунд ожидания.
BLOCK_ATTEMPTS = 2


@dataclass(frozen=True)
class Rewritten:
    value: object
    tokens_in: int
    tokens_out: int


class BlockRewriter:
    def __init__(self, ai: AiPort, specs: tuple[KindSpec, ...] = SPECS):
        self.ai = ai
        self.specs = {spec.kind: spec for spec in specs}
        # Схемы блоков не меняются от урока к уроку — собираются один раз
        self._models: dict[tuple[str, str], type[BaseModel]] = {}
        self._schemas: dict[tuple[str, str], dict] = {}
        for spec in specs:
            for name, field in spec.draft.model_fields.items():
                model = create_model(
                    f"{spec.draft.__name__}_{name}",
                    __config__=ConfigDict(extra="forbid"),
                    **{name: (field.annotation, field)},
                )
                self._models[spec.kind, name] = model
                self._schemas[spec.kind, name] = strict_schema(model)

    def is_server_block(self, kind: str, block: str) -> bool:
        # Вид без схемы модели (ключи с ответами) собирает сервер целиком:
        # правке и перегенерации у него не подлежит ни один блок
        spec = self.specs.get(kind)
        return spec is None or block in spec.server_blocks

    def validate(self, kind: str, block: str, value) -> object:
        """Ручная правка — по той же схеме, что ответ модели: пустой блок
        или блок не той формы в документ не попадает. Возвращает значение
        в том виде, в каком его пишет генерация (строки без краевых пробелов).
        """
        model = self._models[kind, block]
        checked = model.model_validate({block: value})
        return checked.model_dump(mode="json")[block]

    def prepare(self, db: DbSession, kit: LessonKit, kind: str) -> LessonBrief:
        # Соседний вид видит план урока и при перегенерации блока: новый
        # блок обязан соответствовать ходу урока так же, как весь материал
        return prepare(db, kit, with_plan=self.specs[kind].with_plan).brief

    def rewrite(
        self, kind: str, block: str, brief: LessonBrief, document: dict, instruction: str
    ) -> Rewritten:
        spec = self.specs[kind]
        message = _message(spec, block, brief, document, instruction)
        tokens_in = tokens_out = 0
        for attempt in range(1, BLOCK_ATTEMPTS + 1):
            # Отказ провайдера (`AiUnavailableError`) не переспрашивается:
            # клиент уже повторил запрос сам, дальше решает учитель
            reply = self.ai.complete_json(
                # Системный промпт — тот же, что у генерации всего вида:
                # начало запроса совпадает, и провайдер берёт его из кэша
                system=spec.system,
                messages=[AiMessage(role="user", content=message)],
                name=f"{kind}_{block}",
                schema=self._schemas[kind, block],
            )
            tokens_in += reply.tokens_in
            tokens_out += reply.tokens_out
            value = self._checked(kind, block, reply.text)
            if value is not None:
                return Rewritten(value=value, tokens_in=tokens_in, tokens_out=tokens_out)
            log.warning("%s.%s: блок не по форме, попытка %s", kind, block, attempt)
        raise BlockOutputInvalidError(tokens_in=tokens_in, tokens_out=tokens_out)

    def _checked(self, kind: str, block: str, text: str):
        try:
            data = self._models[kind, block].model_validate_json(text).model_dump(mode="json")
        except ValidationError:
            return None
        # Код цели в ответе — сочинён: модели его не давали
        if objective_codes_in(data):
            return None
        return data[block]


def _message(
    spec: KindSpec, block: str, brief: LessonBrief, document: dict, instruction: str
) -> str:
    # Блоки сервера не показываются: в них цель обучения с кодом. Коды
    # вырезаются и из остального — учитель мог вписать код в блок руками
    shown = {
        name: strip_codes_deep(value)
        for name, value in document.items()
        if name not in spec.server_blocks
    }
    description = spec.draft.model_fields[block].description or ""
    lang = "казахском" if brief.lang == "kz" else "русском"
    parts = [
        describe(brief, levels=spec.levels),
        "Текущий документ (JSON):\n" + json.dumps(shown, ensure_ascii=False, indent=1),
        f"Перепиши только блок «{block}». Что в нём должно быть: {description}"
        "\nОстальные блоки документа остаются как есть — новый блок должен"
        " с ними согласовываться.",
    ]
    instruction = strip_objective_codes(instruction)
    if instruction:
        parts.append(f"Просьба учителя: {instruction}")
    parts.append(f"Весь текст блока — на {lang} языке.")
    return "\n\n".join(parts)
