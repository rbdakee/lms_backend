"""Банк готовых материалов «Жоспара» (TEACHER_AI_BRIEF, раздел 7).

Отдельным файлом, а не в `lesson_ai_repos.py`: у банка своя сторона — он
общий на всех учителей и ни одному не принадлежит, — и свои правила: строка
банка не знает ни учителя, ни комплекта, ни материала, из которого выросла.
Кто что получил, видно только с другой стороны — по `lesson_material.bank_id`.
"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.lesson_ai_repos import save_blocks
from app.adapters.db.models import LessonKit, LessonMaterial, LessonMaterialBank


class LessonBankRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def unseen(
        self,
        user_id: int,
        *,
        objective_id: int,
        kind: str,
        lang: str,
        engagement_level: str,
        difficulty_level: str,
        plan_bank_id: int | None,
    ) -> LessonMaterialBank | None:
        """Самый старый вариант на ключ, которого у учителя ещё нет.

        У одной цели бывает несколько уроков: учитель, ведущий второй урок
        по той же цели, иначе получил бы тот же документ второй раз. Самый
        старый — чтобы выдача была предсказуемой и её можно было проверить.
        `plan_bank_id` пустой — вариант, который делали без плана урока.
        """
        received = (
            select(LessonMaterial.id)
            .join(LessonKit, LessonKit.id == LessonMaterial.kit_id)
            .where(LessonMaterial.bank_id == LessonMaterialBank.id, LessonKit.user_id == user_id)
            .exists()
        )
        plan = (
            LessonMaterialBank.plan_bank_id.is_(None)
            if plan_bank_id is None
            else LessonMaterialBank.plan_bank_id == plan_bank_id
        )
        return self.db.scalar(
            select(LessonMaterialBank)
            .where(
                LessonMaterialBank.objective_id == objective_id,
                LessonMaterialBank.kind == kind,
                LessonMaterialBank.lang == lang,
                LessonMaterialBank.engagement_level == engagement_level,
                LessonMaterialBank.difficulty_level == difficulty_level,
                plan,
                ~received,
            )
            .order_by(LessonMaterialBank.id)
            .limit(1)
        )

    def issue(self, kit_id: int, variant: LessonMaterialBank, *, user_id: int) -> None:
        """Материал комплекта — сразу готовым, из варианта банка, без задания.

        Блоки и первые версии пишутся так же, как при генерации: дальше
        учитель правит выданное точно так же, как сгенерированное.
        """
        content = document(variant)
        material = LessonMaterial(
            kit_id=kit_id,
            kind=variant.kind,
            status="ready",
            content=content,
            mechanics=list(variant.mechanics),
            bank_id=variant.id,
        )
        self.db.add(material)
        self.db.flush()
        save_blocks(self.db, material.id, content, user_id=user_id)

    def variant(self, bank_id: int) -> LessonMaterialBank | None:
        return self.db.get(LessonMaterialBank, bank_id)

    def kit_plan(self, kit_id: int) -> LessonMaterial | None:
        """План урока комплекта: по его `bank_id` решается, откуда соседи
        берут план — из банка или из материала учителя."""
        return self.db.scalar(
            select(LessonMaterial).where(
                LessonMaterial.kit_id == kit_id, LessonMaterial.kind == "lesson_plan"
            )
        )

    def add(
        self,
        *,
        content: dict,
        mechanics: tuple[str, ...],
        model: str | None,
        objective_id: int,
        kind: str,
        lang: str,
        engagement_level: str,
        difficulty_level: str,
        plan_bank_id: int | None,
    ) -> int:
        """Новый вариант — всегда новой строкой: вариантов на ключ несколько.

        Порядок блоков — порядок словаря ответа, то есть форма документа:
        JSONB его не сохранит, а выданный материал обязан получить блоки
        в порядке формы.
        """
        row = LessonMaterialBank(
            objective_id=objective_id,
            kind=kind,
            lang=lang,
            engagement_level=engagement_level,
            difficulty_level=difficulty_level,
            plan_bank_id=plan_bank_id,
            content=content,
            block_order=list(content),
            mechanics=list(mechanics),
            model=model,
        )
        self.db.add(row)
        self.db.flush()
        return row.id

    def link(self, kit_id: int, kind: str, bank_id: int) -> None:
        """Материал учителя помечен вариантом, который он сам и породил:
        второй раз этот вариант ему не выдаётся."""
        self.db.execute(
            update(LessonMaterial)
            .where(LessonMaterial.kit_id == kit_id, LessonMaterial.kind == kind)
            .values(bank_id=bank_id)
        )


def document(variant: LessonMaterialBank) -> dict:
    """Документ варианта в порядке формы, а не в порядке ключей JSONB."""
    return {name: variant.content[name] for name in variant.block_order}
