"""Комплект к уроку глазами учителя: заказать и следить, как он собирается.

Генерации здесь нет: ручка заводит комплект, материалы и задания на них,
а собирает их очередь (`lesson_ai_queue.py`). Учитель опрашивает комплект,
пока у всех материалов не закончится ожидание (TEACHER_AI_BRIEF, раздел 7).

Комплект виден только своему учителю: чужой — 404, а не 403, чтобы по
номерам нельзя было перебрать, чьи комплекты существуют.

Суточный лимит на учителя (L7) считается здесь, в комплектах: сюда приходит
и прямой заказ, и «Готовить» в чате, — один счётчик на оба пути.

Здесь же — выдача из банка готовых материалов (L7, раздел 7): у комплекта
без пожеланий материал, который уже есть в банке, заводится сразу готовым,
без задания в очереди и без вызова модели.
"""

from app.adapters.db.lesson_ai_repos import LessonKitRepo
from app.adapters.db.lesson_bank_repo import LessonBankRepo
from app.adapters.db.models import LessonKit, LessonMaterialBank, User
from app.application.ratelimit import SlidingWindowLimiter
from app.domain.errors import (
    FieldError,
    NotFoundError,
    ProgramNotPublishedError,
    RateLimitedError,
)
from app.domain.lesson_ai import (
    ANSWER_SOURCES,
    BANK_KINDS,
    DEFAULT_DIFFICULTY,
    DEFAULT_ENGAGEMENT,
    JOB_ERROR_MESSAGES,
    MATERIAL_KINDS,
    WAITS_FOR,
    bankable,
    kit_status,
)


class LessonKitsService:
    def __init__(self, kits: LessonKitRepo, *, limiter: SlidingWindowLimiter):
        self.kits = kits
        # Обязательный аргумент нарочно: сервис без лимита — это очередь,
        # которую один учитель занимает на всех (раздел 7)
        self.limiter = limiter
        # Банк — в той же транзакции, что и заказ: выданный материал
        # и комплект появляются вместе или не появляются вовсе
        self.bank = LessonBankRepo(kits.db)

    # -- POST /lesson_ai/kits ----------------------------------------------

    def create(
        self,
        user: User,
        *,
        objective_id: int,
        topic: str,
        kinds: list[str],
        class_size: int | None,
        notes: str,
        engagement_level: str = DEFAULT_ENGAGEMENT,
        difficulty_level: str = DEFAULT_DIFFICULTY,
    ) -> dict:
        topic = topic.strip()
        if not topic:
            raise FieldError("topic", "Тема урока не может быть пустой")
        # Ключи собираются из ответов к заданиям, а не генерируются: без
        # заданий, домашней или проверочной им не из чего взяться (раздел 4.2)
        if "answer_key" in kinds and not set(kinds) & set(ANSWER_SOURCES):
            raise FieldError(
                "kinds",
                "Ключи с ответами собираются из заданий, домашнего задания"
                " или проверочной — отметьте хотя бы одно из них",
            )
        card = self.kits.objective_card(objective_id)
        if card is None:
            raise NotFoundError("Цель обучения не найдена")
        objective, program, _subject = card
        # Генерация видит только опубликованное: загруженная программа ещё
        # не вычитана владельцем (раздел 3.1)
        if program.status != "published":
            raise ProgramNotPublishedError()
        # Лимит — после всех проверок заказа: отбитый заказ не съедает
        # комплект из суточных
        wait = self.limiter.hit(str(user.id))
        if wait:
            raise RateLimitedError(
                f"За сутки можно заказать не больше {self.limiter.limit} комплектов"
                " — следующий станет доступен позже",
                retry_after_sec=wait,
            )
        kit = self.kits.create_kit(
            user_id=user.id,
            objective_id=objective.id,
            topic=topic,
            # Класс и язык — из цели: язык документов равен языку обучения,
            # а цель взята из редакции приказа на этом языке (раздел 1)
            grade=objective.grade,
            lang=program.lang,
            class_size=class_size,
            notes=notes.strip(),
            engagement_level=engagement_level,
            difficulty_level=difficulty_level,
        )
        # Порядок видов — из брифа, а не из запроса: план урока идёт первым,
        # остальные материалы получат его на вход (раздел 4.2). Он же нужен
        # банку: соседей плана можно выдать, только решив, откуда сам план
        ordered = sorted(set(kinds), key=MATERIAL_KINDS.index)
        banked = bankable(kit.notes)
        plan: LessonMaterialBank | None = None
        for kind in ordered:
            variant = self._variant(kit, kind, ordered, plan) if banked else None
            if variant is None:
                self.kits.add_material(kit.id, kind)
                continue
            self.bank.issue(kit.id, variant, user_id=user.id)
            if kind == "lesson_plan":
                plan = variant
        # Статус пересчитывает закрытие задания, а у комплекта, собранного
        # из банка целиком, заданий нет — без пересчёта он остался бы
        # «собирается» навсегда
        kit.status = kit_status(material.status for material in self.kits.materials(kit.id))
        return self._out(kit)

    def _variant(
        self,
        kit: LessonKit,
        kind: str,
        ordered: list[str],
        plan: LessonMaterialBank | None,
    ) -> LessonMaterialBank | None:
        """Вариант банка для вида или None — генерировать.

        Соседи плана выдаются только цепочкой (записи 6–7 журнала L7):
        к плану из банка — построенные по этому же плану; план заказан,
        но генерируется заново — соседи тоже генерируются, по нему;
        план не заказан — варианты, которые делали без плана.
        """
        if kind not in BANK_KINDS:
            return None
        plan_bank_id = None
        if "lesson_plan" in WAITS_FOR.get(kind, ()) and "lesson_plan" in ordered:
            if plan is None:
                return None
            plan_bank_id = plan.id
        return self.bank.unseen(
            kit.user_id,
            objective_id=kit.objective_id,
            kind=kind,
            lang=kit.lang,
            engagement_level=kit.engagement_level,
            difficulty_level=kit.difficulty_level,
            plan_bank_id=plan_bank_id,
        )

    # -- GET /lesson_ai/kits/{id} ------------------------------------------

    def kit(self, user: User, kit_id: int) -> dict:
        kit = self.kits.kit(kit_id)
        if kit is None or kit.user_id != user.id:
            raise NotFoundError("Комплект не найден")
        return self._out(kit)

    # -- GET /lesson_ai/kits -----------------------------------------------

    def my_kits(self, user: User, *, q: str | None, offset: int, limit: int) -> dict:
        """Свои комплекты, новые сверху, — карточки «Моих уроков».

        Без `content`, `notes` и `class_size`: список не тянет документы,
        страница собирается тремя запросами при любом размере.
        """
        rows, total = self.kits.user_kits(
            user.id, q=(q or "").strip() or None, offset=offset, limit=limit
        )
        materials = self.kits.materials_of([kit.id for kit, _, _ in rows])
        items = [
            {
                "id": kit.id,
                "objective": {
                    "id": objective.id,
                    "code": objective.code,
                    "text": objective.text,
                    "marker": objective.marker,
                },
                "subject_code": subject.code,
                "grade": kit.grade,
                "lang": kit.lang,
                "topic": kit.topic,
                "engagement_level": kit.engagement_level,
                "difficulty_level": kit.difficulty_level,
                "status": kit.status,
                "created_at": kit.created_at,
                "updated_at": kit.updated_at,
                "materials": [
                    {"id": m.id, "kind": m.kind, "status": m.status} for m in materials[kit.id]
                ],
            }
            for kit, objective, subject in rows
        ]
        return {"items": items, "total": total}

    def _out(self, kit: LessonKit) -> dict:
        objective, _program, subject = self.kits.objective_card(kit.objective_id)
        errors = self.kits.last_errors(kit.id)
        return {
            "id": kit.id,
            "objective": {
                "id": objective.id,
                "code": objective.code,
                "text": objective.text,
                "marker": objective.marker,
            },
            "subject_code": subject.code,
            "grade": kit.grade,
            "lang": kit.lang,
            "topic": kit.topic,
            "class_size": kit.class_size,
            "notes": kit.notes,
            "engagement_level": kit.engagement_level,
            "difficulty_level": kit.difficulty_level,
            "status": kit.status,
            "created_at": kit.created_at,
            "updated_at": kit.updated_at,
            "materials": [
                {
                    "id": material.id,
                    "kind": material.kind,
                    "status": material.status,
                    # Отказ показывается только у упавшего материала: у ждущего
                    # повтора он ещё не приговор
                    "error": _error(errors.get(material.kind))
                    if material.status == "failed"
                    else None,
                    "content": material.content,
                    "updated_at": material.updated_at,
                }
                for material in self.kits.materials(kit.id)
            ],
        }


def _error(code: str | None) -> dict | None:
    if code is None:
        return None
    message = JOB_ERROR_MESSAGES.get(code, JOB_ERROR_MESSAGES["internal_error"])
    return {"code": code, "message": message}
