"""Очередь генерации «Жоспара»: взять задание, собрать материал, закрыть задание.

Очередь — своя таблица в своей базе (TEACHER_AI_BRIEF, раздел 7), и всё, что
обычно обещает брокер, здесь обещает SQL: одно задание не берут двое, брошенное
задание не висит «в работе» вечно, опоздавший ответ модели не перетирает свежий.
Эти обещания и проверяются — на настоящем Postgres, потому что `SKIP LOCKED`,
аренда по `now()` базы и частичный уникальный индекс в памяти не подделать.

Фоновых воркеров в тестах нет (`LESSON_AI_WORKERS=0`): задания берутся руками
через `JobRunner.run_next()`, а вместо модели — генератор-заглушка, наружу
не ходит никто. Время сравнивается и сдвигается только по часам базы: часы
контейнера Postgres и хоста расходятся на секунды (`db-clock-flaky-tests`).
"""

from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.lesson_ai_repos import LessonAiJobRepo, LessonKitRepo
from app.adapters.db.models import LessonAiJob, LessonMaterial, User
from app.application.lesson_ai_queue import Generated, JobRunner
from app.application.lesson_kits import LessonKitsService
from app.application.ratelimit import SlidingWindowLimiter
from app.domain.lesson_ai import (
    MAX_ATTEMPTS,
    RETRY_DELAYS_SEC,
    JobError,
    kit_status,
    retry_delay,
)
from tests.conftest import make_user, seed
from tests.test_admin_curriculum import (
    make_objective,
    make_program,
    make_section,
    make_subject,
)

LEASE_SEC = 600

# Порядок ключей нарочно не совпадает с тем, как их разложит JSONB (короткие
# первыми): порядок блоков обязан идти от генератора, а не от базы.
CONTENT = {
    "section": "Числа и величины",
    "learning_objectives": [
        {"code": "1.1.1.1", "text": "считать в пределах десяти", "marker": ""}
    ],
    "stage_start": {"minutes": 5, "text": "Разминка: счёт хором до десяти"},
    "aim": "Научиться считать предметы до десяти",
}


class StubGenerator:
    """Генератор вида без модели: исход каждой попытки задаёт тест.

    Исход — `Generated`, исключение или функция, которую надо вызвать посреди
    генерации (так моделируется соседний воркер). Кончились исходы — успех.
    `refuse` — отказ ещё на чтении базы, как у снятой с публикации программы.
    """

    model = "stub-model"

    def __init__(self, *outcomes, refuse: JobError | None = None):
        self.outcomes = list(outcomes)
        self.refuse = refuse
        self.prepared: list[int] = []
        self.calls = 0

    def prepare(self, db, kit):
        self.prepared.append(kit.id)
        if self.refuse is not None:
            raise self.refuse
        return kit.id

    def generate(self, prepared):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else Generated(dict(CONTENT), 900, 1400)
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass(frozen=True)
class Kit:
    id: int
    user_id: int
    objective_id: int


def order_kit(user_id: int, objective_id: int) -> int:
    """Комплект из одного ҚМЖ — тем же сервисом, что и ручка заказа:
    материал и задание на него заводятся вместе, как в бою."""
    with OrmSession(get_engine()) as db:
        out = LessonKitsService(LessonKitRepo(db), limiter=SlidingWindowLimiter(100, 86400)).create(
            db.get(User, user_id),
            objective_id=objective_id,
            topic="Счёт до десяти",
            kinds=["lesson_plan"],
            class_size=24,
            notes="",
        )
        db.commit()
    return out["id"]


@pytest.fixture
def kit() -> Kit:
    """Учитель, цель опубликованной программы и заказанный по ней комплект."""
    teacher = make_user("+77075550101")
    subject = make_subject()
    program = make_program(subject.id, status="published")
    section = make_section(program.id, "1", "Числа и величины")
    subsection = make_section(
        program.id, "1.1", "Натуральные числа и число 0", parent_id=section.id
    )
    objective = make_objective(program.id, subsection.id, "1.1.1.1")
    return Kit(order_kit(teacher.id, objective.id), teacher.id, objective.id)


def runner(generators) -> JobRunner:
    return JobRunner(lambda: OrmSession(get_engine()), generators, lease_sec=LEASE_SEC)


def fetch(sql: str, **params) -> list[dict]:
    with get_engine().connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings()]


def execute(sql: str, **params) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(sql), params)


def job_of(kit_id: int, kind: str = "lesson_plan") -> dict:
    """Последнее задание вида. Отсрочка и аренда — в секундах от `now()` базы:
    сравнивать метки с часами хоста нельзя."""
    row = fetch(
        "SELECT id, status, attempts, error, tokens_in, tokens_out, model,"
        " lease_until, started_at, finished_at,"
        " EXTRACT(EPOCH FROM run_after - now()) AS wait_sec,"
        " EXTRACT(EPOCH FROM lease_until - now()) AS lease_left_sec"
        " FROM lesson_ai_job WHERE kit_id = :kit_id AND kind = :kind"
        " ORDER BY id DESC LIMIT 1",
        kit_id=kit_id,
        kind=kind,
    )[0]
    for key in ("wait_sec", "lease_left_sec"):
        if row[key] is not None:
            row[key] = float(row[key])
    return row


def material_of(kit_id: int, kind: str = "lesson_plan") -> dict:
    return fetch(
        "SELECT id, status, content FROM lesson_material WHERE kit_id = :kit_id AND kind = :kind",
        kit_id=kit_id,
        kind=kind,
    )[0]


def kit_status_of(kit_id: int) -> str:
    return fetch("SELECT status FROM lesson_kit WHERE id = :id", id=kit_id)[0]["status"]


def blocks_of(material_id: int) -> list[dict]:
    return fetch(
        "SELECT id, kind, order_index, content FROM lesson_material_block"
        " WHERE material_id = :material_id ORDER BY order_index",
        material_id=material_id,
    )


def versions_of(block_id: int) -> list[dict]:
    return fetch(
        "SELECT source, user_id, content, instruction FROM lesson_material_version"
        " WHERE block_id = :block_id ORDER BY id",
        block_id=block_id,
    )


def make_due() -> None:
    """Пауза между попытками истекла — сдвигом по часам базы, а не ожиданием."""
    execute(
        "UPDATE lesson_ai_job SET run_after = now() - interval '1 second'"
        " WHERE status = 'queued'"
    )


def expire_lease() -> None:
    """Воркер, взявший задание, «умер»: его аренда в прошлом по часам базы."""
    execute(
        "UPDATE lesson_ai_job SET lease_until = now() - interval '1 second'"
        " WHERE status = 'running'"
    )


def claim_and_commit() -> None:
    """Задание взял соседний воркер: взятие закоммичено, воркер больше не вернётся."""
    with OrmSession(get_engine()) as db:
        assert LessonAiJobRepo(db).claim(LEASE_SEC) is not None
        db.commit()


# -- удачная генерация ------------------------------------------------------


def test_fresh_job_is_taken_and_material_becomes_ready(kit):
    """Главный путь: заказанный материал доходит до учителя.

    Проверяется всё, что закрытое задание оставляет после себя: токены
    и модель — это деньги (учёт L7), пустая аренда и `finished_at` — то, что
    задание больше никто не подберёт, `ready` у комплекта — то, что учитель
    перестанет опрашивать его и увидит документ.
    """
    generator = StubGenerator(Generated(dict(CONTENT), tokens_in=900, tokens_out=1400))

    assert runner({"lesson_plan": generator}).run_next() is True

    job = job_of(kit.id)
    assert job["status"] == "done"
    assert job["attempts"] == 1
    assert job["error"] is None
    assert job["lease_until"] is None
    assert job["started_at"] is not None
    assert job["finished_at"] is not None
    assert (job["tokens_in"], job["tokens_out"]) == (900, 1400)
    assert job["model"] == "stub-model"
    material = material_of(kit.id)
    assert material["status"] == "ready"
    assert material["content"] == CONTENT
    assert kit_status_of(kit.id) == "ready"
    # Генератору достался комплект именно этого задания
    assert generator.prepared == [kit.id]
    assert generator.calls == 1


def test_generated_content_is_mirrored_into_blocks_with_first_version(kit):
    """Блоки — единица правки в L4, версии — её история (решение L3 №12).

    Блок на каждое поле верхнего уровня в порядке генератора, иначе документ
    на экране соберётся в порядке, который выбрал JSONB. Первая версия пишется
    сразу, иначе откат «к тому, что сгенерировалось» станет особым случаем.
    Инвариант `content[block.kind] == block.content` — на нём держится
    правка по блокам: разойдутся — экран и выгрузка покажут разное.
    """
    assert runner({"lesson_plan": StubGenerator()}).run_next() is True

    material = material_of(kit.id)
    blocks = blocks_of(material["id"])
    assert [block["kind"] for block in blocks] == list(CONTENT)
    assert [block["order_index"] for block in blocks] == list(range(len(CONTENT)))
    for block in blocks:
        assert material["content"][block["kind"]] == block["content"]
        assert versions_of(block["id"]) == [
            {
                "source": "generated",
                "user_id": kit.user_id,
                "content": block["content"],
                "instruction": None,
            }
        ]


def test_empty_queue_gives_nothing(kit):
    """Воркер без работы должен уснуть, а не крутиться: `False` — сигнал
    на паузу. Комплект есть, но его задание уже закрыто."""
    run = runner({"lesson_plan": StubGenerator()})
    assert run.run_next() is True

    assert run.run_next() is False


def test_no_jobs_at_all_gives_nothing():
    """Пустая таблица заданий — тоже законное состояние сразу после выката."""
    assert runner({"lesson_plan": StubGenerator()}).run_next() is False


# -- отказы -----------------------------------------------------------------


def test_retryable_refusal_waits_between_attempts_and_gives_up_after_last(kit):
    """Модель не ответила — повтор, но не сразу: провайдер, отказавший
    по потолку токенов в минуту, через секунду откажет снова (раздел 7).

    Между попытками материал снова «в очереди», а комплект «собирается»:
    отказ, у которого ещё будет повтор, учителю не приговор. После третьей
    попытки задание закрывается окончательно — иначе оно жгло бы токены вечно.
    """
    generator = StubGenerator(*(JobError("ai_failed", retryable=True) for _ in range(MAX_ATTEMPTS)))
    run = runner({"lesson_plan": generator})

    for attempt, delay in enumerate(RETRY_DELAYS_SEC, start=1):
        assert run.run_next() is True
        job = job_of(kit.id)
        assert job["status"] == "queued"
        assert job["attempts"] == attempt
        assert job["error"] == "ai_failed"
        assert job["lease_until"] is None
        assert job["finished_at"] is None
        assert job["wait_sec"] == pytest.approx(delay, abs=5)
        assert material_of(kit.id)["status"] == "queued"
        assert kit_status_of(kit.id) == "generating"
        # До срока задание не берётся
        assert run.run_next() is False
        make_due()

    assert run.run_next() is True
    job = job_of(kit.id)
    assert job["status"] == "failed"
    assert job["attempts"] == MAX_ATTEMPTS
    assert job["error"] == "ai_failed"
    assert job["lease_until"] is None
    assert job["finished_at"] is not None
    assert material_of(kit.id)["status"] == "failed"
    assert kit_status_of(kit.id) == "failed"
    assert generator.calls == MAX_ATTEMPTS
    # Закрытое задание очередь больше не отдаёт
    make_due()
    assert run.run_next() is False


def test_final_refusal_fails_on_first_attempt(kit):
    """Снятая с публикации программа повтором не лечится (решение L3 №4):
    вторая и третья попытки стоили бы времени учителя, а ответ тот же."""
    generator = StubGenerator(refuse=JobError("program_not_published", retryable=False))
    run = runner({"lesson_plan": generator})

    assert run.run_next() is True

    job = job_of(kit.id)
    assert job["status"] == "failed"
    assert job["attempts"] == 1
    assert job["error"] == "program_not_published"
    assert job["finished_at"] is not None
    assert material_of(kit.id)["status"] == "failed"
    assert kit_status_of(kit.id) == "failed"
    # Отказ на чтении базы — до модели дело не доходит
    assert generator.calls == 0
    assert run.run_next() is False


def test_unexpected_exception_fails_as_internal_error_without_retry(kit):
    """Собственный баг, повторённый трижды, — втрое больше токенов за тот же
    отказ. А текст исключения наружу не идёт: в нём бывает тема урока."""
    generator = StubGenerator(RuntimeError("Тема урока: счёт до десяти"))
    run = runner({"lesson_plan": generator})

    assert run.run_next() is True

    job = job_of(kit.id)
    assert job["status"] == "failed"
    assert job["attempts"] == 1
    assert job["error"] == "internal_error"
    assert material_of(kit.id)["status"] == "failed"
    assert kit_status_of(kit.id) == "failed"
    make_due()
    assert run.run_next() is False
    assert generator.calls == 1


def test_kind_without_generator_fails_as_internal_error(kit):
    """Вид, которого очередь не умеет, не должен вечно висеть в очереди:
    материал без генератора закрывается отказом, а не пропускается."""
    other = StubGenerator()

    assert runner({"tasks": other}).run_next() is True

    job = job_of(kit.id)
    assert job["status"] == "failed"
    assert job["error"] == "internal_error"
    assert material_of(kit.id)["status"] == "failed"
    assert kit_status_of(kit.id) == "failed"
    assert other.prepared == []
    assert other.calls == 0


def test_tokens_add_up_across_attempts(kit):
    """Кривой ответ модели стоит тех же денег, что и ровный: в учёт идут
    все попытки, а не только удачная."""
    generator = StubGenerator(
        JobError("model_output_invalid", retryable=True, tokens_in=100, tokens_out=50),
        Generated(dict(CONTENT), tokens_in=900, tokens_out=1400),
    )
    run = runner({"lesson_plan": generator})

    assert run.run_next() is True
    job = job_of(kit.id)
    assert (job["tokens_in"], job["tokens_out"]) == (100, 50)
    make_due()
    assert run.run_next() is True

    job = job_of(kit.id)
    assert job["status"] == "done"
    assert job["attempts"] == 2
    # Удачная попытка стирает код прошлого отказа: у готового материала его нет
    assert job["error"] is None
    assert (job["tokens_in"], job["tokens_out"]) == (1000, 1450)
    assert job["model"] == "stub-model"


# -- аренда и жетон попытки -------------------------------------------------


def test_abandoned_job_is_picked_up_only_after_lease_expires(kit):
    """Процесс умер посреди генерации (выкат, падение) — задание не должно
    висеть «в работе» вечно. Но и живое задание, пока его аренда не истекла,
    второй воркер брать не должен: за один материал заплатили бы дважды."""
    claim_and_commit()
    generator = StubGenerator()
    run = runner({"lesson_plan": generator})

    assert run.run_next() is False
    job = job_of(kit.id)
    assert job["status"] == "running"
    assert job["attempts"] == 1
    assert job["lease_left_sec"] == pytest.approx(LEASE_SEC, abs=5)
    assert material_of(kit.id)["status"] == "generating"
    assert generator.calls == 0

    expire_lease()
    assert run.run_next() is True

    job = job_of(kit.id)
    assert job["status"] == "done"
    assert job["attempts"] == 2
    assert job["lease_until"] is None
    assert material_of(kit.id)["status"] == "ready"
    assert kit_status_of(kit.id) == "ready"
    assert generator.calls == 1


def test_expired_lease_on_last_attempt_closes_job_as_interrupted(kit):
    """Задание, умиравшее вместе с процессом на каждой попытке, должно
    когда-то закончиться: после последней попытки подбор по истёкшей аренде
    закрывает его `interrupted`, и модель за него больше не платится."""
    execute(
        "UPDATE lesson_ai_job SET status = 'running', attempts = :attempts,"
        " started_at = now(), lease_until = now() - interval '1 second'",
        attempts=MAX_ATTEMPTS,
    )
    execute("UPDATE lesson_material SET status = 'generating'")
    generator = StubGenerator()

    assert runner({"lesson_plan": generator}).run_next() is True

    job = job_of(kit.id)
    assert job["status"] == "failed"
    assert job["error"] == "interrupted"
    assert job["lease_until"] is None
    assert job["finished_at"] is not None
    assert material_of(kit.id)["status"] == "failed"
    assert kit_status_of(kit.id) == "failed"
    assert generator.prepared == []
    assert generator.calls == 0


def overtaken(outcome):
    """Исход попытки, пока которой задание успел подобрать соседний воркер:
    генерация затянулась дольше аренды, и счёт попыток ушёл вперёд."""

    def run():
        expire_lease()
        claim_and_commit()
        return outcome

    return run


@pytest.mark.parametrize(
    "outcome",
    [
        Generated(dict(CONTENT), tokens_in=900, tokens_out=1400),
        JobError("ai_failed", retryable=True, tokens_in=100, tokens_out=50),
    ],
    ids=["result", "refusal"],
)
def test_late_attempt_does_not_overwrite_job_taken_by_another_worker(kit, outcome):
    """Номер попытки — жетон: опоздавший ответ модели не пишется, иначе два
    ответа перетирали бы друг друга, а материал стал бы «готов» посреди
    чужой генерации. Опоздавший отказ тоже не пишется: он вернул бы в очередь
    задание, которое прямо сейчас собирает другой воркер."""
    generator = StubGenerator(overtaken(outcome))

    assert runner({"lesson_plan": generator}).run_next() is True

    job = job_of(kit.id)
    # Задание принадлежит второму воркеру и нетронуто первым
    assert job["status"] == "running"
    assert job["attempts"] == 2
    assert job["error"] is None
    assert job["lease_left_sec"] == pytest.approx(LEASE_SEC, abs=5)
    assert (job["tokens_in"], job["tokens_out"]) == (0, 0)
    assert job["model"] is None
    material = material_of(kit.id)
    assert material["status"] == "generating"
    assert material["content"] is None
    assert blocks_of(material["id"]) == []
    assert kit_status_of(kit.id) == "generating"


def test_parallel_claims_skip_locked_jobs_instead_of_waiting(kit):
    """Два воркера (или два процесса API) не должны ни взять одно задание,
    ни ждать друг друга: `SKIP LOCKED`. Сессии держат транзакции открытыми,
    как воркер между взятием и коммитом; `lock_timeout` превращает ожидание
    замка в ошибку — без него сломанный `SKIP LOCKED` повесил бы прогон."""
    order_kit(kit.user_id, kit.objective_id)
    sessions = [OrmSession(get_engine()) for _ in range(3)]
    try:
        taken = []
        for db in sessions:
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            job = LessonAiJobRepo(db).claim(LEASE_SEC)
            taken.append(job.id if job is not None else None)
        first, second, third = taken
        assert first is not None
        assert second is not None
        assert first != second
        # Оба задания под замком у соседей — третьему достаётся ничего
        assert third is None
    finally:
        for db in sessions:
            db.rollback()
            db.close()


# -- комплект из нескольких материалов --------------------------------------


@pytest.mark.parametrize(
    ("plan_ok", "tasks_ok", "expected"),
    [
        (True, True, "ready"),
        (True, False, "partial"),
        (False, True, "partial"),
        (False, False, "failed"),
    ],
)
def test_kit_status_summarises_its_materials(kit, plan_ok, tasks_ok, expected):
    """Статус комплекта — сводка по материалам (решение L3 №5). Один сбойный
    вид не должен выглядеть в списке уроков как пропавший комплект, а пока
    хоть один материал ждёт — комплект собирается, что бы ни случилось
    с соседом. Второй материал заведён мимо ручки: она пока принимает
    только ҚМЖ, а ограничение базы — все семь видов."""
    seed(LessonMaterial(kit_id=kit.id, kind="tasks"))
    seed(LessonAiJob(kit_id=kit.id, kind="tasks"))

    def outcome(ok: bool):
        return Generated({"items": ["Посчитай яблоки"]}) if ok else RuntimeError("сбой")

    run = runner(
        {
            "lesson_plan": StubGenerator(outcome(plan_ok)),
            "tasks": StubGenerator(outcome(tasks_ok)),
        }
    )

    assert run.run_next() is True
    statuses = sorted(
        (material_of(kit.id, kind)["status"] for kind in ("lesson_plan", "tasks")),
        key=lambda status: status != "queued",
    )
    assert statuses[0] == "queued"
    assert statuses[1] in ("ready", "failed")
    assert kit_status_of(kit.id) == "generating"

    assert run.run_next() is True
    assert kit_status_of(kit.id) == expected
    assert run.run_next() is False


# -- одно живое задание и перегенерация -------------------------------------


def test_only_one_live_job_per_material(kit):
    """Двойной клик «перегенерировать» не должен запускать две генерации
    одного материала: живое задание — ждущее или взятое — одно на вид.
    Закрытые задания в индекс не попадают, и после них новое заводится."""
    with pytest.raises(IntegrityError):
        seed(LessonAiJob(kit_id=kit.id, kind="lesson_plan"))

    claim_and_commit()
    with pytest.raises(IntegrityError):
        seed(LessonAiJob(kit_id=kit.id, kind="lesson_plan"))

    expire_lease()
    assert runner({"lesson_plan": StubGenerator()}).run_next() is True
    assert job_of(kit.id)["status"] == "done"
    seed(LessonAiJob(kit_id=kit.id, kind="lesson_plan"))
    assert job_of(kit.id)["status"] == "queued"


def test_regeneration_rewrites_blocks_and_keeps_their_history(kit):
    """Повторная генерация переписывает те же блоки, а не заводит новые:
    снести блок — значит каскадом снести историю правок учителя."""
    first = {"aim": "Считать до десяти", "stage_start": "Разминка хором"}
    second = {"aim": "Считать до десяти и обратно", "stage_start": "Разминка с мячом"}
    run = runner({"lesson_plan": StubGenerator(Generated(first), Generated(second))})
    assert run.run_next() is True
    material_id = material_of(kit.id)["id"]
    before = blocks_of(material_id)

    seed(LessonAiJob(kit_id=kit.id, kind="lesson_plan"))
    assert run.run_next() is True

    material = material_of(kit.id)
    assert material["id"] == material_id
    assert material["status"] == "ready"
    assert material["content"] == second
    after = blocks_of(material_id)
    assert [block["id"] for block in after] == [block["id"] for block in before]
    for block in after:
        assert block["content"] == second[block["kind"]]
        history = versions_of(block["id"])
        assert [version["content"] for version in history] == [
            first[block["kind"]],
            second[block["kind"]],
        ]
    assert kit_status_of(kit.id) == "ready"


def test_regeneration_drops_block_missing_from_new_content(kit):
    """Блок, которого нет в новом документе, удаляется вместе с историей:
    иначе он всплыл бы в документе из прошлой генерации. Порядок оставшихся
    блоков — новый, по новому документу."""
    first = {"aim": "Цель", "stage_start": "Начало", "stage_end": "Рефлексия"}
    second = {"stage_start": "Начало с мячом", "aim": "Цель и обратный счёт"}
    run = runner({"lesson_plan": StubGenerator(Generated(first), Generated(second))})
    assert run.run_next() is True
    material_id = material_of(kit.id)["id"]
    before = {block["kind"]: block["id"] for block in blocks_of(material_id)}

    seed(LessonAiJob(kit_id=kit.id, kind="lesson_plan"))
    assert run.run_next() is True

    after = blocks_of(material_id)
    assert [block["kind"] for block in after] == ["stage_start", "aim"]
    assert [block["order_index"] for block in after] == [0, 1]
    assert {block["kind"]: block["id"] for block in after} == {
        "stage_start": before["stage_start"],
        "aim": before["aim"],
    }
    assert versions_of(before["stage_end"]) == []
    assert material_of(kit.id)["content"] == second


# -- чистые правила домена --------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], "generating"),
        (["queued"], "generating"),
        (["generating"], "generating"),
        (["ready", "queued"], "generating"),
        (["failed", "generating"], "generating"),
        (["ready", "failed", "queued"], "generating"),
        (["ready"], "ready"),
        (["ready", "ready"], "ready"),
        (["ready", "failed"], "partial"),
        (["failed", "ready", "ready"], "partial"),
        (["failed"], "failed"),
        (["failed", "failed"], "failed"),
    ],
)
def test_kit_status(statuses, expected):
    """Сводка комплекта: пустой комплект — ещё собирается (материалы
    заводятся после него), ждущий материал перевешивает всё остальное.
    Итератор, а не список: репозиторий отдаёт сюда результат запроса."""
    assert kit_status(iter(statuses)) == expected


def test_retry_delays():
    """Три попытки, паузы 30 с и 2 мин (решение L3 №3); после последней
    попытки и после подбора сверх неё повтора нет."""
    assert MAX_ATTEMPTS == 3
    assert retry_delay(1) == 30
    assert retry_delay(2) == 120
    assert retry_delay(MAX_ATTEMPTS) is None
    assert retry_delay(MAX_ATTEMPTS + 1) is None
