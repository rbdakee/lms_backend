"""Экран сверки справочника в админке: чтение, правка формулировок,
разбор подозрений и публикация программы (CONTRACT.md, раздел «Справочник
учебной программы»).

Сценарий отдаёт `dict`, роутер оборачивает его в Out-схему: ORM-объекты
наружу не отдаются нигде в проекте.

Площадки у справочника нет ни в одном методе: государственная программа
одна на обе (TEACHER_AI_BRIEF, раздел 8).
"""

from app.adapters.db.models import (
    CurriculumIssue,
    CurriculumProgram,
    CurriculumSection,
    LearningObjective,
    User,
)
from app.adapters.db.repos import CurriculumRepo, now_utc
from app.application.curriculum_import import CurriculumImportService
from app.domain.errors import ConflictError, FieldError, NotFoundError

# Метка парсера на месте математического знака, вставленного в приказ
# картинкой (≠, ∩, ∪, ∅, ≥). Правка цели ровно ради неё и существует.
IMAGE_MARK = "[img]"

# Формулировка цели длинная, но не безразмерная: в приказе самая длинная
# укладывается в несколько строк, а поле правки — обычный textarea.
TEXT_MAX = 2000

EMPTY_TEXT = "Формулировка цели не может быть пустой"


class CurriculumAdminService:
    def __init__(self, curriculum: CurriculumRepo, imports: CurriculumImportService):
        # Публикация живёт в сценарии импорта и остаётся там: правило у неё одно
        # («неразобранные ошибки закрывают публикацию»), и второй его копии,
        # которая однажды разойдётся с первой, быть не должно
        self.curriculum = curriculum
        self.imports = imports

    # -- GET /admin/curriculum/programs -----------------------------------

    def programs(
        self,
        *,
        subject_code: str | None,
        lang: str | None,
        status: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        rows, total = self.curriculum.programs_page(
            subject_code=subject_code,
            lang=lang,
            status=status,
            offset=offset,
            limit=limit,
        )
        return {"items": [self._program_out(*row) for row in rows], "total": total}

    # -- GET /admin/curriculum/programs/{id} ------------------------------

    def program(self, program_id: int) -> dict:
        """Программа с деревом разделов: экран открывается с неё."""
        row = self._card(program_id)
        return {**self._program_out(*row), "sections": self._sections_out(program_id)}

    # -- GET /admin/curriculum/programs/{id}/objectives -------------------

    def objectives(
        self,
        program_id: int,
        *,
        grade: int | None,
        subsection: str | None,
        q: str | None,
        has_image: bool | None,
        edited: bool | None,
        offset: int,
        limit: int,
    ) -> dict:
        # Несуществующая программа — это 404, а не пустой список: пустой
        # список на экране читается как «всё разобрано», и это неправда
        self._found(program_id)
        rows, total = self.curriculum.objectives_page(
            program_id=program_id,
            grade=grade,
            subsection=subsection,
            q=q,
            has_image=has_image,
            edited=edited,
            offset=offset,
            limit=limit,
        )
        return {"items": [self._objective_out(*row) for row in rows], "total": total}

    # -- PATCH /admin/curriculum/objectives/{id} --------------------------

    def edit_objective(self, objective_id: int, text: str) -> dict:
        """Правка формулировки: меняется только текст.

        Код, класс и подраздел заданы приказом — их правка означала бы, что
        цель переехала, а по коду цели завуч сверяет план урока.
        """
        found = self.curriculum.objective(objective_id)
        if found is None:
            raise NotFoundError("Цель не найдена")
        objective, program, section_code, section_title = found
        if program.status == "published":
            raise ConflictError("Программа опубликована: сначала снять публикацию")

        text = text.strip()
        if not text:
            raise FieldError("text", EMPTY_TEXT)
        if len(text) > TEXT_MAX:
            raise FieldError("text", f"Не длиннее {TEXT_MAX} символов")

        objective.text = text
        # По тому же признаку, что и на импорте: человек убрал метку — цель
        # ушла из списка «посмотреть в первую очередь»
        objective.has_image = IMAGE_MARK in text
        objective.edited_at = now_utc()
        return self._objective_out(objective, section_code, section_title)

    # -- GET /admin/curriculum/programs/{id}/issues -----------------------

    def issues(
        self,
        program_id: int,
        *,
        severity: str | None,
        resolved: bool | None,
        offset: int,
        limit: int,
    ) -> dict:
        self._found(program_id)
        rows, total = self.curriculum.issues_page(
            program_id=program_id,
            severity=severity,
            resolved=resolved,
            offset=offset,
            limit=limit,
        )
        return {"items": [self._issue_out(*row) for row in rows], "total": total}

    # -- POST | DELETE /admin/curriculum/issues/{id}/resolve --------------

    def resolve_issue(self, admin: User, issue_id: int) -> dict:
        """«Я посмотрел, так и должно быть». Данные не исправляются.

        Повторный вызов на уже разобранном отдаёт ту же строку: двойной клик
        не должен ни пугать отказом, ни переписывать, кто именно решил.
        """
        issue, resolver = self._issue(issue_id)
        if issue.resolved_at is None:
            issue.resolved_at = now_utc()
            issue.resolved_by = admin.id
            resolver = admin
        return self._issue_out(issue, resolver)

    def unresolve_issue(self, issue_id: int) -> dict:
        issue, _ = self._issue(issue_id)
        issue.resolved_at = None
        issue.resolved_by = None
        return self._issue_out(issue, None)

    # -- POST /admin/curriculum/programs/{id}/publish | unpublish ---------

    def publish(self, program_id: int) -> dict:
        row = self._card(program_id)
        self.imports.publish(row[0])
        return self._program_out(*row)

    def unpublish(self, program_id: int) -> dict:
        """Снятие публикации не удаляет ничего: планы уроков, уже сделанные
        по этой программе, остаются. Программа перестаёт предлагаться в новых."""
        row = self._card(program_id)
        program = row[0]
        program.status = "loaded"
        program.published_at = None
        return self._program_out(*row)

    # -- поиск строк -------------------------------------------------------

    def _card(self, program_id: int) -> tuple:
        row = self.curriculum.program_card(program_id)
        if row is None:
            raise NotFoundError("Программа не найдена")
        return row

    def _found(self, program_id: int) -> CurriculumProgram:
        program = self.curriculum.program_by_id(program_id)
        if program is None:
            raise NotFoundError("Программа не найдена")
        return program

    def _issue(self, issue_id: int) -> tuple[CurriculumIssue, User | None]:
        found = self.curriculum.issue(issue_id)
        if found is None:
            raise NotFoundError("Подозрение не найдено")
        return found

    # -- сборка ответа -----------------------------------------------------

    @staticmethod
    def _program_out(
        program: CurriculumProgram,
        subject_code: str,
        subject_title: str,
        objectives_count: int,
        issues_open: int,
        errors_open: int,
    ) -> dict:
        return {
            "id": program.id,
            "subject_code": subject_code,
            "subject_title": subject_title,
            "lang": program.lang,
            "grade_from": program.grade_from,
            "grade_to": program.grade_to,
            "order_number": program.order_number,
            "order_date": program.order_date,
            # Сырая строка из шапки приказа рядом с разобранной датой:
            # на сверке по ней видно, что именно разобрал импорт
            "order_date_text": program.order_date_text,
            "source_url": program.source_url,
            "title": program.title,
            "status": program.status,
            "imported_at": program.imported_at,
            "published_at": program.published_at,
            "objectives_count": objectives_count,
            "issues_open": issues_open,
            "errors_open": errors_open,
        }

    def _sections_out(self, program_id: int) -> list[dict]:
        """Дерево из плоского списка: уровня всего два, и собирается оно
        одним проходом — раздел приходит раньше своих подразделов."""
        roots: dict[int, dict] = {}
        children: list[tuple[CurriculumSection, int]] = []
        for section, objectives_count in self.curriculum.sections(program_id):
            if section.parent_id is None:
                roots[section.id] = {
                    "id": section.id,
                    "code": section.code,
                    "title": section.title,
                    "subsections": [],
                }
            else:
                children.append((section, objectives_count))

        for section, objectives_count in children:
            parent = roots.get(section.parent_id)
            if parent is None:
                # Подраздел без раздела в базе невозможен (внешний ключ),
                # но и молча удваивать уровень дерева тут не из чего
                continue
            parent["subsections"].append(
                {
                    "id": section.id,
                    "code": section.code,
                    "title": section.title,
                    "objectives_count": objectives_count,
                }
            )
        return list(roots.values())

    @staticmethod
    def _objective_out(
        objective: LearningObjective, subsection_code: str, subsection_title: str
    ) -> dict:
        return {
            "id": objective.id,
            "code": objective.code,
            "grade": objective.grade,
            "index": objective.index,
            "marker": objective.marker,
            "text": objective.text,
            # Пустой список — цели нет в долгосрочном плане приказа; так бывает
            "quarters": list(objective.quarters),
            "has_image": objective.has_image,
            "edited_at": objective.edited_at,
            "subsection_code": subsection_code,
            "subsection_title": subsection_title,
        }

    @staticmethod
    def _issue_out(issue: CurriculumIssue, resolver: User | None) -> dict:
        return {
            "id": issue.id,
            "kind": issue.kind,
            "severity": issue.severity,
            "objective_code": issue.objective_code,
            "message": issue.message,
            "resolved_at": issue.resolved_at,
            "resolved_by": _admin_name(resolver),
        }


def _admin_name(user: User | None) -> str | None:
    """Фамилия и инициал: «Нурланова А.».

    Через месяц важно, кто принял решение, а не полное ФИО — строка стоит
    в списке подозрений, и место в ней одно. Профиль админа бывает пуст
    (первый админ заводится по телефону), тогда имени нет вовсе.
    """
    if user is None:
        return None
    parts = [user.last_name, f"{user.first_name[0]}." if user.first_name else ""]
    return " ".join(part for part in parts if part) or None
