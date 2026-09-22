"""Репозитории «Жоспара» — отдельным файлом, а не в `repos.py`.

Причина та же, что у схем справочника и ассистента: `repos.py` — файл на четыре
тысячи строк, и дописывать в его конец очередной продукт значит гарантированно
столкнуться в нём с параллельной сессией.

Два репозитория по двум сторонам одной таблицы заданий: учитель заказывает
комплект и смотрит на него (`LessonKitRepo`), воркер берёт задания и закрывает
их (`LessonAiJobRepo`).
"""

from datetime import timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import aliased

from app.adapters.db.models import (
    CurriculumProgram,
    CurriculumSection,
    CurriculumSubject,
    LearningObjective,
    LessonAiJob,
    LessonKit,
    LessonMaterial,
    LessonMaterialBlock,
    LessonMaterialVersion,
)
from app.domain.lesson_ai import MATERIAL_KINDS, kit_status


class LessonKitRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def objective_card(
        self, objective_id: int
    ) -> tuple[LearningObjective, CurriculumProgram, CurriculumSubject] | None:
        """Цель вместе с программой и предметом: по программе решается,
        открыта ли генерация, по ней же — язык комплекта."""
        row = self.db.execute(
            select(LearningObjective, CurriculumProgram, CurriculumSubject)
            .join(CurriculumProgram, CurriculumProgram.id == LearningObjective.program_id)
            .join(CurriculumSubject, CurriculumSubject.id == CurriculumProgram.subject_id)
            .where(LearningObjective.id == objective_id)
        ).first()
        return tuple(row) if row is not None else None

    def create_kit(self, **fields) -> LessonKit:
        kit = LessonKit(**fields)
        self.db.add(kit)
        self.db.flush()
        return kit

    def add_material(self, kit_id: int, kind: str) -> None:
        """Материал и задание на него заводятся вместе: материал без задания
        навсегда остался бы в очереди, задание без материала некуда сохранить."""
        self.db.add(LessonMaterial(kit_id=kit_id, kind=kind))
        self.db.add(LessonAiJob(kit_id=kit_id, kind=kind))
        self.db.flush()

    def kit(self, kit_id: int) -> LessonKit | None:
        return self.db.get(LessonKit, kit_id)

    def materials(self, kit_id: int) -> list[LessonMaterial]:
        """Материалы комплекта в порядке видов из брифа: план урока первым."""
        rows = self.db.scalars(select(LessonMaterial).where(LessonMaterial.kit_id == kit_id))
        return sorted(rows, key=lambda material: MATERIAL_KINDS.index(material.kind))

    def last_errors(self, kit_id: int) -> dict[str, str]:
        """Код отказа последнего задания по каждому виду — то, что увидит
        учитель у упавшего материала."""
        rows = self.db.execute(
            select(LessonAiJob.kind, LessonAiJob.error)
            .where(LessonAiJob.kit_id == kit_id, LessonAiJob.error.isnot(None))
            .order_by(LessonAiJob.id)
        )
        return {kind: error for kind, error in rows}


class LessonAiJobRepo:
    """Очередь генерации. Все метки времени — `now()` базы: аренду ставят
    и проверяют одни и те же часы, а часы контейнера Postgres и хоста в проекте
    уже расходились на секунды (`db-clock-flaky-tests`)."""

    def __init__(self, db: DbSession):
        self.db = db

    def claim(self, lease_sec: int) -> LessonAiJob | None:
        """Взять следующее задание и отметить его взятым — одним запросом.

        `SKIP LOCKED`: два воркера не возьмут одно задание и не будут ждать
        друг друга. Берётся и ждущее своего часа, и взятое, чья аренда
        истекла, — процесс умер посреди генерации, и задание не должно
        висеть «в работе» вечно. Такой подбор — новая попытка, со счётом.
        """
        pending = (
            select(LessonAiJob.id)
            .where(
                or_(
                    and_(LessonAiJob.status == "queued", LessonAiJob.run_after <= func.now()),
                    and_(LessonAiJob.status == "running", LessonAiJob.lease_until < func.now()),
                )
            )
            .order_by(LessonAiJob.run_after, LessonAiJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        job_id = self.db.scalar(
            update(LessonAiJob)
            .where(LessonAiJob.id == pending)
            .values(
                status="running",
                attempts=LessonAiJob.attempts + 1,
                started_at=func.now(),
                lease_until=func.now() + timedelta(seconds=lease_sec),
            )
            .returning(LessonAiJob.id)
        )
        if job_id is None:
            return None
        job = self.db.get(LessonAiJob, job_id)
        self._material_status(job, "generating")
        return job

    def owned(self, job_id: int, attempt: int) -> LessonAiJob | None:
        """Задание под замком — если оно всё ещё наше.

        Номер попытки работает жетоном: генерация затянулась дольше аренды,
        задание подобрал другой воркер, и счёт попыток ушёл вперёд. Тогда
        наш результат опоздал и не пишется — иначе два ответа модели
        перетирали бы друг друга.
        """
        return self.db.scalar(
            select(LessonAiJob)
            .where(
                LessonAiJob.id == job_id,
                LessonAiJob.status == "running",
                LessonAiJob.attempts == attempt,
            )
            .with_for_update()
        )

    def done(self, job: LessonAiJob, content: dict, *, user_id: int) -> None:
        """Материал готов: документ, блоки и первые версии блоков — вместе."""
        material = self._material(job)
        material.content = content
        material.status = "ready"
        material.updated_at = func.now()
        self._save_blocks(material.id, content, user_id=user_id)
        job.status = "done"
        job.error = None
        self._close(job)

    def retry(self, job: LessonAiJob, error: str, delay_sec: int) -> None:
        """Попытка не удалась, но не последняя: задание снова ждёт,
        материал снова в очереди."""
        job.status = "queued"
        job.error = error
        job.lease_until = None
        job.run_after = func.now() + timedelta(seconds=delay_sec)
        self._material_status(job, "queued")

    def fail(self, job: LessonAiJob, error: str) -> None:
        job.status = "failed"
        job.error = error
        self._material_status(job, "failed")
        self._close(job)

    def add_usage(self, job: LessonAiJob, *, tokens_in: int, tokens_out: int, model: str) -> None:
        job.tokens_in += tokens_in
        job.tokens_out += tokens_out
        job.model = model

    def _close(self, job: LessonAiJob) -> None:
        """Задание закрыто — комплект пересчитывается.

        Строка комплекта берётся под замок: материалы одного комплекта
        закрываются параллельно (с L5), и без замка оба пересчёта видят
        соседа ещё недоделанным — комплект навсегда остаётся «собирается».
        """
        job.finished_at = func.now()
        job.lease_until = None
        self.db.flush()
        kit = self.db.scalar(select(LessonKit).where(LessonKit.id == job.kit_id).with_for_update())
        statuses = self.db.scalars(
            select(LessonMaterial.status).where(LessonMaterial.kit_id == kit.id)
        )
        kit.status = kit_status(statuses)
        kit.updated_at = func.now()

    def _material(self, job: LessonAiJob) -> LessonMaterial:
        return self.db.scalar(
            select(LessonMaterial).where(
                LessonMaterial.kit_id == job.kit_id, LessonMaterial.kind == job.kind
            )
        )

    def _material_status(self, job: LessonAiJob, status: str) -> None:
        self.db.execute(
            update(LessonMaterial)
            .where(LessonMaterial.kit_id == job.kit_id, LessonMaterial.kind == job.kind)
            .values(status=status, updated_at=func.now())
        )

    def _save_blocks(self, material_id: int, content: dict, *, user_id: int) -> None:
        """Блок на каждое поле верхнего уровня и версия на каждый блок.

        Блок, уже существующий, переписывается, а не заводится заново:
        снести его — значит снести каскадом и историю правок. Блок, которого
        в новом документе нет, удаляется: иначе он всплывал бы в документе
        из прошлой генерации.
        """
        existing = {
            block.kind: block
            for block in self.db.scalars(
                select(LessonMaterialBlock).where(LessonMaterialBlock.material_id == material_id)
            )
        }
        for order_index, (kind, value) in enumerate(content.items()):
            block = existing.pop(kind, None)
            if block is None:
                block = LessonMaterialBlock(material_id=material_id, kind=kind)
                self.db.add(block)
            block.order_index = order_index
            block.content = value
            block.updated_at = func.now()
            self.db.flush()
            self.db.add(
                LessonMaterialVersion(
                    block_id=block.id, content=value, source="generated", user_id=user_id
                )
            )
        if existing:
            self.db.execute(
                delete(LessonMaterialBlock).where(
                    LessonMaterialBlock.id.in_([block.id for block in existing.values()])
                )
            )
        self.db.flush()


def objective_sections(db: DbSession, objective: LearningObjective) -> tuple[str, str]:
    """Раздел и подраздел программы, к которым относится цель, — названиями.

    Цель ссылается на подраздел; раздел достаётся по его `parent_id`.
    Коды разделов (`1`, `1.2`) не нужны ни модели, ни документу: в ҚМЖ
    пишется название раздела.
    """
    parent = aliased(CurriculumSection)
    row = db.execute(
        select(parent.title, CurriculumSection.title)
        .select_from(CurriculumSection)
        .outerjoin(parent, parent.id == CurriculumSection.parent_id)
        .where(CurriculumSection.id == objective.section_id)
    ).first()
    if row is None:
        return "", ""
    section, subsection = row
    # Цель прямо в разделе, без подраздела: тогда её раздел — он сам
    return (section or subsection or ""), (subsection if section else "")

