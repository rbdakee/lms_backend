"""Сборка программы курса из базы — одна на три места: оглавление страницы
курса, сайдбар экрана урока и счётчики прогресса.
"""

from app.adapters.db.models import Course
from app.adapters.db.repos import CourseRepo, ProgressRepo
from app.domain.program import apply_statuses, progress_of


def build_program(courses: CourseRepo, course_id: int) -> list[dict]:
    """Оглавление: уроки, тесты и задания модуля вперемешку по order_index.
    Скрытые уроки и скрытые вопросы не показываются и не считаются."""
    by_module: dict[int, list[tuple[int, dict]]] = {}
    for lesson in courses.lessons(course_id):
        by_module.setdefault(lesson.module_id, []).append(
            (
                lesson.order_index,
                {
                    "kind": lesson.kind,
                    "id": lesson.id,
                    "title": lesson.title,
                    "time_required_min": lesson.time_required_min,
                    "duration_label": lesson.duration_label,
                },
            )
        )
    quizzes = courses.quizzes(course_id)
    questions = courses.questions_count([q.id for q in quizzes])
    for quiz in quizzes:
        by_module.setdefault(quiz.module_id, []).append(
            (
                quiz.order_index,
                {
                    "kind": "quiz",
                    "id": quiz.id,
                    "title": quiz.title,
                    "time_required_min": quiz.time_required_min,
                    "questions_count": questions.get(quiz.id, 0),
                    "time_limit_min": quiz.time_limit_min,
                    "pass_score": quiz.pass_score,
                    "is_final": quiz.is_final,
                },
            )
        )
    for task in courses.tasks(course_id):
        by_module.setdefault(task.module_id, []).append(
            (
                task.order_index,
                {
                    "kind": "task",
                    "id": task.id,
                    "title": task.title,
                    "time_required_min": task.time_required_min,
                    "submit_format": task.submit_format,
                },
            )
        )
    return [
        {
            "id": module.id,
            "title": module.title,
            "items": [
                item
                for _, item in sorted(by_module.get(module.id, []), key=lambda pair: pair[0])
            ],
        }
        for module in courses.modules(course_id)
    ]


def with_statuses(
    progress: ProgressRepo, course: Course, user_id: int, program: list[dict]
) -> list[dict]:
    """Та же программа глазами учителя: что пройдено, что закрыто."""
    return apply_statuses(
        program,
        progress.done_keys(user_id, course.id),
        strict_order=course.strict_order,
    )


def course_progress(
    courses: CourseRepo, progress: ProgressRepo, course: Course, user_id: int
) -> dict:
    """Блок прогресса курса: агрегат по статусам той же программы."""
    program = with_statuses(progress, course, user_id, build_program(courses, course.id))
    return progress_of(program)
