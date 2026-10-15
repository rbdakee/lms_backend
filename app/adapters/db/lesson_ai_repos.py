"""Репозитории «Жоспара» — отдельным файлом, а не в `repos.py`.

Причина та же, что у схем справочника и ассистента: `repos.py` — файл на четыре
тысячи строк, и дописывать в его конец очередной продукт значит гарантированно
столкнуться в нём с параллельной сессией.

Два репозитория по двум сторонам одной таблицы заданий: учитель заказывает
комплект и смотрит на него (`LessonKitRepo`), воркер берёт задания и закрывает
их (`LessonAiJobRepo`). Третий — чат сбора заказа и поиск цели по справочнику
(`LessonChatRepo`, сессия L6). С L7 — доступ к продукту (`LessonAccessRepo`)
и учёт расхода (`LessonUsageRepo`).
"""

from datetime import date, timedelta

from sqlalchemy import Integer, and_, case, delete, func, or_, select, true, update
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import aliased

from app.adapters.db.models import (
    CurriculumProgram,
    CurriculumSection,
    CurriculumSubject,
    LearningObjective,
    LessonAiAccess,
    LessonAiChat,
    LessonAiChatMessage,
    LessonAiJob,
    LessonAiUsage,
    LessonKit,
    LessonMaterial,
    LessonMaterialBlock,
    LessonMaterialVersion,
    User,
)
from app.adapters.db.repos import name_or_phone_filter
from app.domain.lesson_ai import MATERIAL_KINDS, WAITS_FOR, kit_status, usage_cost


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

    def user_kits(
        self, user_id: int, *, q: str | None, offset: int, limit: int
    ) -> tuple[list[tuple[LessonKit, LearningObjective, CurriculumSubject]], int]:
        """Комплекты учителя для «Моих уроков» — новые сверху.

        Комплект из банка — такая же строка `lesson_kit` с `user_id` получателя
        (банк отдаёт материалы, а не комплекты), поэтому выбор по `user_id`
        находит и его. Порядок и отбор идут по `ix_lesson_kit_user_id_id`.
        `q` ищет по теме, коду и тексту цели без учёта регистра.
        """
        where = [LessonKit.user_id == user_id]
        if q:
            # Спецсимволы LIKE экранируются: «50%» ищется как есть
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                or_(
                    LessonKit.topic.ilike(like, escape="\\"),
                    LearningObjective.code.ilike(like, escape="\\"),
                    LearningObjective.text.ilike(like, escape="\\"),
                )
            )
        base = (
            select(LessonKit, LearningObjective, CurriculumSubject)
            .join(LearningObjective, LearningObjective.id == LessonKit.objective_id)
            .join(CurriculumProgram, CurriculumProgram.id == LearningObjective.program_id)
            .join(CurriculumSubject, CurriculumSubject.id == CurriculumProgram.subject_id)
            .where(*where)
        )
        total = self.db.execute(
            select(func.count()).select_from(base.with_only_columns(LessonKit.id).subquery())
        ).scalar_one()
        rows = self.db.execute(base.order_by(LessonKit.id.desc()).offset(offset).limit(limit)).all()
        return [tuple(row) for row in rows], total

    def materials_of(self, kit_ids: list[int]) -> dict[int, list[LessonMaterial]]:
        """Материалы страницы комплектов одним запросом, в порядке видов из брифа."""
        found: dict[int, list[LessonMaterial]] = {kit_id: [] for kit_id in kit_ids}
        if kit_ids:
            for material in self.db.scalars(
                select(LessonMaterial).where(LessonMaterial.kit_id.in_(kit_ids))
            ):
                found[material.kit_id].append(material)
        for rows in found.values():
            rows.sort(key=lambda material: MATERIAL_KINDS.index(material.kind))
        return found

    def ready_blocks(self, kit_id: int, kinds: tuple[str, ...]) -> list[tuple[str, list]]:
        return ready_blocks(self.db, kit_id, kinds)

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
                ),
                _not_waiting(),
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

    def done(
        self, job: LessonAiJob, content: dict, *, user_id: int, mechanics: tuple[str, ...] = ()
    ) -> None:
        """Материал готов: документ, блоки и первые версии блоков — вместе."""
        material = self._material(job)
        material.content = content
        material.mechanics = list(mechanics)
        material.status = "ready"
        material.updated_at = func.now()
        save_blocks(self.db, material.id, content, user_id=user_id)
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
        """Расход попытки — на задание и строкой учёта по учителю (L7).
        Учитель — владелец комплекта: генерацию заказал он."""
        job.tokens_in += tokens_in
        job.tokens_out += tokens_out
        job.model = model
        user_id = self.db.scalar(select(LessonKit.user_id).where(LessonKit.id == job.kit_id))
        LessonUsageRepo(self.db).add(
            user_id,
            "material",
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            kit_id=job.kit_id,
            kind=job.kind,
        )

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


def ready_blocks(db: DbSession, kit_id: int, kinds: tuple[str, ...]) -> list[tuple[str, list]]:
    """Готовые материалы комплекта из `kinds` — блоками в порядке формы.

    Порядок блоков берётся из `order_index`, а не из `content`: JSONB
    раскладывает ключи по-своему (короткие первыми), и план урока пришёл
    бы модели с концом урока впереди начала. Не готовые — пропускаются:
    упавший план не повод оставлять без материалов остальной комплект.
    """
    rows = db.execute(
        select(LessonMaterial.kind, LessonMaterialBlock.kind, LessonMaterialBlock.content)
        .join(LessonMaterialBlock, LessonMaterialBlock.material_id == LessonMaterial.id)
        .where(
            LessonMaterial.kit_id == kit_id,
            LessonMaterial.kind.in_(kinds),
            LessonMaterial.status == "ready",
        )
        .order_by(LessonMaterialBlock.order_index)
    )
    found: dict[str, list] = {}
    for kind, block, content in rows:
        found.setdefault(kind, []).append((block, content))
    return [(kind, found[kind]) for kind in kinds if kind in found]


def save_blocks(db: DbSession, material_id: int, content: dict, *, user_id: int) -> None:
    """Блок на каждое поле верхнего уровня и версия на каждый блок.

    Пишут двое: очередь, закрывая задание, и пересборка ключей с ответами
    после правки задачи (`LessonMaterialRepo.replace_content`).

    Блок, уже существующий, переписывается, а не заводится заново:
    снести его — значит снести каскадом и историю правок. Блок, которого
    в новом документе нет, удаляется: иначе он всплывал бы в документе
    из прошлой генерации.
    """
    existing = {
        block.kind: block
        for block in db.scalars(
            select(LessonMaterialBlock).where(LessonMaterialBlock.material_id == material_id)
        )
    }
    for order_index, (kind, value) in enumerate(content.items()):
        block = existing.pop(kind, None)
        if block is None:
            block = LessonMaterialBlock(material_id=material_id, kind=kind)
            db.add(block)
        block.order_index = order_index
        block.content = value
        block.updated_at = func.now()
        db.flush()
        db.add(
            LessonMaterialVersion(
                block_id=block.id, content=value, source="generated", user_id=user_id
            )
        )
    if existing:
        db.execute(
            delete(LessonMaterialBlock).where(
                LessonMaterialBlock.id.in_([block.id for block in existing.values()])
            )
        )
    db.flush()


def _not_waiting():
    """Задание не ждёт предшественника (`WAITS_FOR`): план урока идёт первым,
    ключи собираются после задач. Предшественник, упавший окончательно,
    зависимых отпускает — один сбойный вид не оставляет учителя без комплекта
    (TEACHER_AI_BRIEF, раздел 7)."""
    ahead = aliased(LessonAiJob)
    conditions = [
        or_(
            LessonAiJob.kind != kind,
            ~select(ahead.id)
            .where(
                ahead.kit_id == LessonAiJob.kit_id,
                ahead.kind.in_(before),
                ahead.status.in_(("queued", "running")),
            )
            .exists(),
        )
        for kind, before in WAITS_FOR.items()
    ]
    return and_(*conditions)


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


class LessonMaterialRepo:
    """Готовый материал глазами учителя: файл, блоки и их история.

    Правка блока пишет три места одной транзакцией — блок, ключ в
    `material.content` и версию: инвариант `content[block.kind] == block.content`
    держится здесь, а не у вызывающего.
    """

    def __init__(self, db: DbSession):
        self.db = db

    def material(self, material_id: int) -> tuple[LessonMaterial, LessonKit] | None:
        row = self.db.execute(
            select(LessonMaterial, LessonKit)
            .join(LessonKit, LessonKit.id == LessonMaterial.kit_id)
            .where(LessonMaterial.id == material_id)
        ).first()
        return tuple(row) if row is not None else None

    def blocks(self, material_id: int) -> list[LessonMaterialBlock]:
        return list(
            self.db.scalars(
                select(LessonMaterialBlock)
                .where(LessonMaterialBlock.material_id == material_id)
                .order_by(LessonMaterialBlock.order_index)
            )
        )

    def block(self, material_id: int, kind: str) -> LessonMaterialBlock | None:
        return self.db.scalar(
            select(LessonMaterialBlock).where(
                LessonMaterialBlock.material_id == material_id, LessonMaterialBlock.kind == kind
            )
        )

    def lock_material(self, material_id: int) -> LessonMaterial:
        """Материал под замок перед любой записью в его блоки.

        Замок на материал, а не на блок: `content` у материала один на все
        блоки, и две правки разных блоков иначе перетёрли бы его друг другу.
        Заодно все правки одного материала встают в очередь в одном
        порядке замков — взаимной блокировки не бывает.
        """
        # populate_existing: материал уже прочитан этой сессией до замка,
        # и без него проверка статуса шла бы по снимку, а не по строке под замком
        return self.db.scalar(
            select(LessonMaterial)
            .where(LessonMaterial.id == material_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def last_version_id(self, block_id: int) -> int | None:
        """Номер последней версии — жетон правки: блок переписали, пока
        модель думала, — номер ушёл вперёд, и запоздалый ответ не пишется."""
        return self.db.scalar(
            select(func.max(LessonMaterialVersion.id)).where(
                LessonMaterialVersion.block_id == block_id
            )
        )

    def versions(self, block_id: int) -> list[LessonMaterialVersion]:
        """История блока, свежие сверху."""
        return list(
            self.db.scalars(
                select(LessonMaterialVersion)
                .where(LessonMaterialVersion.block_id == block_id)
                .order_by(LessonMaterialVersion.id.desc())
            )
        )

    def version(self, block_id: int, version_id: int) -> LessonMaterialVersion | None:
        return self.db.scalar(
            select(LessonMaterialVersion).where(
                LessonMaterialVersion.id == version_id,
                LessonMaterialVersion.block_id == block_id,
            )
        )

    def ready_blocks(self, kit_id: int, kinds: tuple[str, ...]) -> list[tuple[str, list]]:
        return ready_blocks(self.db, kit_id, kinds)

    def sibling(self, kit_id: int, kind: str) -> LessonMaterial | None:
        """Соседний материал того же комплекта — под замок: ключи с ответами
        пересобираются после правки задачи, и две правки разом не должны
        перетереть ключи друг другу."""
        return self.db.scalar(
            select(LessonMaterial)
            .where(LessonMaterial.kit_id == kit_id, LessonMaterial.kind == kind)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def replace_content(self, material: LessonMaterial, content: dict, *, user_id: int) -> None:
        """Документ целиком — заново, как при генерации: блоки и по версии
        на каждый. Так пересобираются ключи с ответами — сервером, не моделью."""
        material.content = content
        material.updated_at = func.now()
        save_blocks(self.db, material.id, content, user_id=user_id)

    def write_block(
        self,
        material: LessonMaterial,
        block: LessonMaterialBlock,
        value,
        *,
        source: str,
        user_id: int,
        instruction: str | None = None,
    ) -> None:
        """Новое содержимое блока — в блок, в документ и в историю.

        Материал — взятый `lock_material`. Документ переписывается новым
        словарём, а не правкой ключа на месте: изменение внутри JSONB
        SQLAlchemy не замечает и в базу не пишет.
        """
        material.content = {**(material.content or {}), block.kind: value}
        material.updated_at = func.now()
        block.content = value
        block.updated_at = func.now()
        self.db.add(
            LessonMaterialVersion(
                block_id=block.id,
                content=value,
                source=source,
                user_id=user_id,
                instruction=instruction,
            )
        )
        self.db.flush()


# Цель вместе со всем, что о ней показывают учителю: программа — язык,
# предмет — код предмета, раздел и подраздел — названиями
ObjectiveRow = tuple[LearningObjective, CurriculumProgram, CurriculumSubject, str, str]


class LessonChatRepo:
    """Чат сбора заказа и справочник глазами этого чата.

    Справочник здесь виден **только опубликованным**: загруженная программа
    ещё не вычитана владельцем, и цель из неё учитель не должен даже увидеть
    (TEACHER_AI_BRIEF, раздел 3.1). Фильтр стоит в каждом запросе, а не
    у вызывающего — забыть его негде.
    """

    def __init__(self, db: DbSession):
        self.db = db

    # -- чат --------------------------------------------------------------

    def create_chat(self, user_id: int, now) -> LessonAiChat:
        # Обе метки одними часами, как у чатов ассистента
        chat = LessonAiChat(user_id=user_id, created_at=now, updated_at=now)
        self.db.add(chat)
        self.db.flush()
        return chat

    def chat(self, chat_id: int) -> LessonAiChat | None:
        return self.db.get(LessonAiChat, chat_id)

    def lock_chat(self, chat_id: int) -> LessonAiChat | None:
        """Чат под замком строки: два «Готовить» подряд не заводят два комплекта."""
        return self.db.execute(
            select(LessonAiChat).where(LessonAiChat.id == chat_id).with_for_update()
        ).scalar_one_or_none()

    def messages(self, chat_id: int) -> list[LessonAiChatMessage]:
        return list(
            self.db.execute(
                select(LessonAiChatMessage)
                .where(LessonAiChatMessage.chat_id == chat_id)
                .order_by(LessonAiChatMessage.id)
            ).scalars()
        )

    def add_message(self, chat_id: int, **fields) -> LessonAiChatMessage:
        row = LessonAiChatMessage(chat_id=chat_id, **fields)
        self.db.add(row)
        return row

    def user_chats(
        self, user_id: int, *, offset: int, limit: int
    ) -> tuple[list[LessonAiChat], int]:
        """Чаты учителя для истории — свежие по последней активности сверху:
        `updated_at` двигают ход, правка заказа и подтверждение."""
        where = LessonAiChat.user_id == user_id
        total = self.db.execute(
            select(func.count()).select_from(LessonAiChat).where(where)
        ).scalar_one()
        rows = self.db.execute(
            select(LessonAiChat)
            .where(where)
            .order_by(LessonAiChat.updated_at.desc(), LessonAiChat.id.desc())
            .offset(offset)
            .limit(limit)
        ).scalars()
        return list(rows), total

    def first_user_messages(self, chat_ids: list[int]) -> dict[int, str]:
        """Первая реплика учителя в каждом чате — одним запросом на страницу."""
        if not chat_ids:
            return {}
        rows = self.db.execute(
            select(LessonAiChatMessage.chat_id, LessonAiChatMessage.content)
            .where(
                LessonAiChatMessage.chat_id.in_(chat_ids),
                LessonAiChatMessage.role == "user",
            )
            .order_by(LessonAiChatMessage.chat_id, LessonAiChatMessage.id)
            .distinct(LessonAiChatMessage.chat_id)
        ).all()
        return {chat_id: content for chat_id, content in rows}

    # -- справочник -------------------------------------------------------

    def _objectives(self):
        parent = aliased(CurriculumSection)
        return (
            select(
                LearningObjective,
                CurriculumProgram,
                CurriculumSubject,
                func.coalesce(parent.title, CurriculumSection.title),
                case((parent.id.is_(None), ""), else_=CurriculumSection.title),
            )
            .join(CurriculumProgram, CurriculumProgram.id == LearningObjective.program_id)
            .join(CurriculumSubject, CurriculumSubject.id == CurriculumProgram.subject_id)
            .join(CurriculumSection, CurriculumSection.id == LearningObjective.section_id)
            .outerjoin(parent, parent.id == CurriculumSection.parent_id)
            .where(CurriculumProgram.status == "published")
        )

    def find_objectives(
        self,
        stems: list[str],
        *,
        code: str | None,
        grade: int | None,
        lang: str | None,
        limit: int,
        subject_code: str | None = None,
    ) -> list[ObjectiveRow]:
        """Цели, в тексте которых больше всего основ запроса, — сверху.

        Счёт — сколько разных основ нашлось в тексте; названный код цели
        перевешивает любой счёт. Ни одного совпадения — цели в выдаче нет.
        """
        text = func.replace(func.lower(LearningObjective.text), "ё", "е")
        score = sum(
            (case((text.contains(stem, autoescape=True), 1), else_=0) for stem in stems),
            start=func.cast(0, Integer),
        )
        if code:
            score = score + case((LearningObjective.code == code, 100), else_=0)
        query = self._objectives().where(score > 0)
        if grade is not None:
            query = query.where(LearningObjective.grade == grade)
        if lang is not None:
            query = query.where(CurriculumProgram.lang == lang)
        if subject_code is not None:
            query = query.where(CurriculumSubject.code == subject_code)
        rows = self.db.execute(
            query.order_by(
                score.desc(), LearningObjective.grade, CurriculumProgram.lang, LearningObjective.id
            ).limit(limit)
        ).all()
        return [tuple(row) for row in rows]

    def grade_objectives(self, grade: int, lang: str) -> list[ObjectiveRow]:
        """Все цели класса в одной редакции — в порядке приказа: учитель
        открывает список и выбирает сам (раздел 1). Порядок строк импорта
        и есть порядок документа."""
        rows = self.db.execute(
            self._objectives()
            .where(LearningObjective.grade == grade, CurriculumProgram.lang == lang)
            .order_by(CurriculumProgram.id, LearningObjective.id)
        ).all()
        return [tuple(row) for row in rows]

    def subject_known(self, subject_code: str) -> bool:
        """Есть ли такой предмет в справочнике — чтобы опечатку модели в коде
        предмета отличить от предмета, у которого просто нет такой цели."""
        return (
            self.db.scalar(
                select(CurriculumSubject.id).where(CurriculumSubject.code == subject_code)
            )
            is not None
        )

    def objectives_by_code(
        self, code: str, lang: str | None, subject_code: str | None = None
    ) -> list[ObjectiveRow]:
        query = self._objectives().where(LearningObjective.code == code)
        if lang is not None:
            query = query.where(CurriculumProgram.lang == lang)
        if subject_code is not None:
            query = query.where(CurriculumSubject.code == subject_code)
        rows = self.db.execute(query.order_by(CurriculumProgram.lang, LearningObjective.id)).all()
        return [tuple(row) for row in rows]

    def objective(self, objective_id: int) -> ObjectiveRow | None:
        row = self.db.execute(
            self._objectives().where(LearningObjective.id == objective_id)
        ).first()
        return tuple(row) if row is not None else None

    def objectives_by_ids(self, objective_ids: list[int]) -> dict[int, ObjectiveRow]:
        """Цели страницы чатов одним запросом — с тем же фильтром публикации,
        что у `objective`: снятая цель в истории не числится, как и в заказе."""
        if not objective_ids:
            return {}
        rows = self.db.execute(
            self._objectives().where(LearningObjective.id.in_(objective_ids))
        ).all()
        return {row[0].id: tuple(row) for row in rows}

    def objective_lang(self, objective_id: int) -> tuple[str, str] | None:
        """Код цели и язык её редакции — **без фильтра публикации**: заказ
        уже ссылается на эту строку, и сменить язык надо, даже если её
        программу сейчас сняли на правку."""
        row = self.db.execute(
            select(LearningObjective.code, CurriculumProgram.lang)
            .join(CurriculumProgram, CurriculumProgram.id == LearningObjective.program_id)
            .where(LearningObjective.id == objective_id)
        ).first()
        return tuple(row) if row is not None else None

    def counterpart(self, objective_id: int, lang: str) -> ObjectiveRow | None:
        """Та же цель в другой языковой редакции приказа — по коду.

        Коды целей в русской и казахской редакциях одни и те же (раздел 3.1),
        а тексты — два официальных корпуса: казахский комплект берёт казахскую
        строку, а не перевод русской. Сначала ищется редакция того же приказа,
        потом любая опубликованная по тому же предмету.
        """
        source = self.db.execute(
            select(LearningObjective, CurriculumProgram)
            .join(CurriculumProgram, CurriculumProgram.id == LearningObjective.program_id)
            .where(LearningObjective.id == objective_id)
        ).first()
        if source is None:
            return None
        objective, program = source
        if program.lang == lang:
            return self.objective(objective.id)
        row = self.db.execute(
            self._objectives()
            .where(
                LearningObjective.code == objective.code,
                CurriculumProgram.subject_id == program.subject_id,
                CurriculumProgram.lang == lang,
            )
            .order_by(
                (CurriculumProgram.order_number != program.order_number),
                CurriculumProgram.id.desc(),
            )
            .limit(1)
        ).first()
        return tuple(row) if row is not None else None


class LessonAccessRepo:
    """Доступ к «Жоспару» — строка на учителя (`lesson_ai_access`)."""

    def __init__(self, db: DbSession):
        self.db = db

    def by_user(self, user_id: int) -> LessonAiAccess | None:
        return self.db.scalar(select(LessonAiAccess).where(LessonAiAccess.user_id == user_id))

    def lock_by_user(self, user_id: int) -> LessonAiAccess | None:
        """Под замком: два админа, выдающие доступ одному учителю одновременно,
        иначе оба не нашли бы строки и второй упал бы на уникальности."""
        return self.db.scalar(
            select(LessonAiAccess).where(LessonAiAccess.user_id == user_id).with_for_update()
        )

    def create(self, **fields) -> LessonAiAccess:
        access = LessonAiAccess(**fields)
        self.db.add(access)
        self.db.flush()
        return access

    def page(
        self,
        *,
        status: str | None,
        q: str | None,
        today: date,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple], int]:
        """Страница списка доступов, свежие выдачи сверху, с расходом каждого
        учителя за всё время — одним запросом.

        Строка — `(доступ, учитель, комплектов, токенов на вход, на выход,
        стоимость)`. Расход считается подзапросами на строку страницы, а не
        группировкой всей таблицы учёта: она растёт с каждым вызовом модели,
        а на странице двадцать человек. Кто выдал — тем же запросом: админов
        несколько, и `db.get` при сборке ответа находит его уже загруженным.

        Админы в списке остаются: строка доступа у админа — такая же выдача
        (решение L7, запись 2), и спрятанная она выглядела бы как пропавшая.
        """
        conds = []
        if status == "revoked":
            conds.append(LessonAiAccess.revoked_at.is_not(None))
        elif status == "active":
            conds += [
                LessonAiAccess.revoked_at.is_(None),
                or_(LessonAiAccess.expires_on.is_(None), LessonAiAccess.expires_on >= today),
            ]
        elif status == "expired":
            conds += [LessonAiAccess.revoked_at.is_(None), LessonAiAccess.expires_on < today]
        if q is not None and q.strip():
            conds.append(name_or_phone_filter(q))

        total = (
            self.db.scalar(
                select(func.count())
                .select_from(LessonAiAccess)
                .join(User, User.id == LessonAiAccess.user_id)
                .where(*conds)
            )
            or 0
        )
        kits = (
            select(func.count())
            .where(LessonKit.user_id == LessonAiAccess.user_id)
            .correlate(LessonAiAccess)
            .scalar_subquery()
        )
        # SUM по пустому набору — NULL: токены превращаются в 0, а стоимость
        # остаётся NULL — «неизвестно», если ни у одной строки её нет
        usage = (
            select(
                func.coalesce(func.sum(LessonAiUsage.tokens_in), 0).label("tokens_in"),
                func.coalesce(func.sum(LessonAiUsage.tokens_out), 0).label("tokens_out"),
                func.sum(LessonAiUsage.cost_usd).label("cost_usd"),
            )
            .where(LessonAiUsage.user_id == LessonAiAccess.user_id)
            .lateral()
        )
        granter = aliased(User)
        rows = self.db.execute(
            select(
                LessonAiAccess,
                User,
                granter,
                kits,
                usage.c.tokens_in,
                usage.c.tokens_out,
                usage.c.cost_usd,
            )
            .join(User, User.id == LessonAiAccess.user_id)
            .outerjoin(granter, granter.id == LessonAiAccess.granted_by)
            .join(usage, true())
            .where(*conds)
            .order_by(LessonAiAccess.granted_at.desc(), LessonAiAccess.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [
            (access, teacher, kits_count, tokens_in, tokens_out, cost)
            for access, teacher, _, kits_count, tokens_in, tokens_out, cost in rows
        ], total


class LessonUsageRepo:
    """Учёт расхода по учителю. Стоимость считается здесь, в момент записи,
    и дальше не пересчитывается (`usage_cost`)."""

    def __init__(self, db: DbSession):
        self.db = db

    def add(
        self,
        user_id: int,
        source: str,
        *,
        model: str | None,
        tokens_in: int,
        tokens_out: int,
        kit_id: int | None = None,
        chat_id: int | None = None,
        kind: str | None = None,
    ) -> None:
        # Провайдер не ответил вовсе — платить не за что, строка была бы шумом
        if not model or not (tokens_in or tokens_out):
            return
        self.db.add(
            LessonAiUsage(
                user_id=user_id,
                source=source,
                kit_id=kit_id,
                chat_id=chat_id,
                kind=kind,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=usage_cost(model, tokens_in, tokens_out),
            )
        )
        self.db.flush()
