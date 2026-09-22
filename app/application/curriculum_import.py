"""Импорт справочника учебной программы из приказа.

Две языковые редакции одного приказа загружаются вместе и только вместе: главная
проверка импорта — сверка кодов целей между ними (TEACHER_AI_BRIEF, раздел 3.1),
а сверять нечего, если вторая редакция приедет отдельным заходом.

Импорт кладёт программу в состояние `loaded`. Генерация видит только `published`,
и перевести программу туда можно, пока в ней остались неразобранные ошибки.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.adapters.db.models import CurriculumProgram
from app.adapters.db.repos import CurriculumRepo
from app.application.curriculum_checks import check_all, check_pair, check_program
from app.domain.errors import ConflictError, ValidationAppError
from app.domain.plural import plural

# Порядок фиксирован: русская редакция идёт первой, потому что по ней заводится
# предмет, а её название — то, что админ увидит в списке.
LANGS = ("ru", "kz")


@dataclass(frozen=True)
class ImportResult:
    """Итог одного импорта — то, что видит человек сразу после загрузки."""

    subject_code: str
    program_ids: dict[str, int]
    objectives: dict[str, int]
    issues_total: int
    errors: int
    warnings: int


class CurriculumImportService:
    def __init__(self, curriculum: CurriculumRepo, commit=None):
        self.curriculum = curriculum
        self.commit = commit

    def load(
        self, subject_code: str, parsed: dict[str, dict], *, force: bool = False
    ) -> ImportResult:
        """Загрузить обе редакции разобранной программы.

        `parsed` — словарь вида {"ru": <разбор>, "kz": <разбор>} из
        `app.adapters.curriculum.parser`.

        `force` — согласие потерять ручную вычитку: без него импорт поверх
        программы с правленными целями отбивается. Спрашивается явно, потому
        что стирается чужая работа, а не данные разбора.
        """
        missing = [lang for lang in LANGS if lang not in parsed]
        if missing:
            raise ValidationAppError(
                f"Нужны обе редакции программы, не хватает: {', '.join(missing)}"
            )

        ru, kz = parsed["ru"], parsed["kz"]
        subject = self.curriculum.subject_by_code(subject_code)
        if subject is None:
            title = ru.get("subject") or subject_code
            subject = self.curriculum.create_subject(subject_code, title)

        # Подозрения считаются до записи: импорт не должен «починить» данные по дороге,
        # а потом отчитаться о чистом справочнике.
        pair_issues = check_pair(ru, kz)
        per_lang = {lang: check_program(parsed[lang], lang) for lang in LANGS}

        program_ids: dict[str, int] = {}
        objectives: dict[str, int] = {}
        for lang in LANGS:
            program = self._replace_program(subject.id, lang, parsed[lang], force=force)
            program_ids[lang] = program.id
            objectives[lang] = self._fill(program, parsed[lang])
            # Межъязыковые подозрения пишутся в обе программы: человек открывает
            # одну программу и должен увидеть расхождение, не догадываясь про вторую.
            self._save_issues(program, per_lang[lang] + pair_issues)

        if self.commit:
            self.commit()

        all_issues = check_all(ru, kz)
        errors = sum(1 for i in all_issues if i.severity == "error")
        return ImportResult(
            subject_code=subject_code,
            program_ids=program_ids,
            objectives=objectives,
            issues_total=len(all_issues),
            errors=errors,
            warnings=len(all_issues) - errors,
        )

    def publish(self, program: CurriculumProgram) -> None:
        """Открыть программу генерации.

        Пока в программе есть неразобранные ошибки, публикации нет: справочник с кривой
        целью хуже пустого — по нему сгенерируется правдоподобный план урока, который
        завернёт завуч.
        """
        left = self.curriculum.unresolved_errors(program.id)
        if left:
            raise ConflictError(
                f"В программе осталось {left} неразобранных ошибок — сначала разобрать их"
            )
        program.status = "published"
        program.published_at = datetime.now(UTC)
        if self.commit:
            self.commit()

    def _replace_program(
        self, subject_id: int, lang: str, data: dict, *, force: bool = False
    ) -> CurriculumProgram:
        source = data.get("source") or {}
        order_number = str(source.get("order_number") or "")
        existing = self.curriculum.program(subject_id, lang, order_number)
        if existing is not None:
            if existing.status == "published":
                raise ConflictError(
                    "Программа уже опубликована: на её цели могут ссылаться планы уроков. "
                    "Снять публикацию и повторить импорт."
                )
            # Снятая с публикации программа тоже могла успеть поработать: по её
            # целям собраны комплекты к урокам, и снести цели из-под них нельзя
            # никаким флагом — документы учителей не должны задним числом
            # ссылаться на несуществующую цель (TEACHER_AI_BRIEF, раздел 3)
            kits = self.curriculum.kits_on_program(existing.id)
            if kits:
                raise ConflictError(
                    f"По целям программы уже собрано {kits} "
                    f"{plural(kits, 'комплект', 'комплекта', 'комплектов')} к урокам —"
                    " повторный импорт оставил бы их без цели обучения."
                )
            # Вторая защита — уже не за данные, а за человека: владелец вычитывает
            # справочник руками, проставляя знаки вместо [img], и эта работа
            # уходит вместе со снесённой программой. Молча её терять нельзя.
            edited = self.curriculum.edited_objectives(existing.id)
            if edited and not force:
                raise ConflictError(
                    f"В программе {edited} "
                    f"{plural(edited, 'цель поправлена', 'цели поправлены', 'целей поправлено')}"
                    " руками — повторный импорт сотрёт эту работу."
                    " Повторить с --force, если правки больше не нужны."
                )
            self.curriculum.drop_program(existing)

        grades = data.get("grades") or []
        return self.curriculum.add_program(
            subject_id=subject_id,
            lang=lang,
            grade_from=min(grades) if grades else 0,
            grade_to=max(grades) if grades else 0,
            order_number=order_number,
            order_date=_as_date(source.get("order_date")),
            order_date_text=source.get("order_date_text") or "",
            source_document_id=source.get("document_id") or "",
            source_url=_adilet_url(source.get("group_number"), lang),
            revision_date=_as_date(source.get("revision_date")),
            title=data.get("program_title") or "",
            status="loaded",
            imported_at=datetime.now(UTC),
        )

    def _fill(self, program: CurriculumProgram, data: dict) -> int:
        """Записать разделы, подразделы и цели. Возвращает число целей."""
        # Ключ подраздела -> id строки, чтобы цель нашла свой подраздел без второго запроса
        subsection_ids: dict[str, int] = {}
        for order, section in enumerate(data.get("sections") or [], start=1):
            parent = self.curriculum.add_section(
                program_id=program.id,
                parent_id=None,
                code=str(section.get("no")),
                title=section.get("title") or "",
                order_index=order,
            )
            for sub_order, sub in enumerate(section.get("subsections") or [], start=1):
                child = self.curriculum.add_section(
                    program_id=program.id,
                    parent_id=parent.id,
                    code=sub.get("code") or "",
                    title=sub.get("title") or "",
                    order_index=sub_order,
                )
                subsection_ids[child.code] = child.id

        rows = []
        for obj in data.get("objectives") or []:
            section_id = subsection_ids.get(obj.get("subsection_code") or "")
            if section_id is None:
                # Цель без подраздела — это ошибка разбора, а не данные: молча положить
                # её «никуда» значит потерять её из справочника навсегда
                raise ValidationAppError(
                    f"Цель {obj.get('code')} ссылается на подраздел "
                    f"{obj.get('subsection_code')!r}, которого нет в программе"
                )
            text = obj.get("text") or ""
            rows.append(
                {
                    "program_id": program.id,
                    "section_id": section_id,
                    "code": obj["code"],
                    "grade": obj["grade"],
                    "index": obj["index"],
                    "marker": obj.get("marker") or "",
                    "text": text,
                    "quarters": obj.get("quarters") or [],
                    "has_image": "[img]" in text,
                }
            )
        self.curriculum.add_objectives(rows)
        return len(rows)

    def _save_issues(self, program: CurriculumProgram, issues: list) -> None:
        self.curriculum.add_issues(
            [
                {
                    "program_id": program.id,
                    "kind": issue.kind,
                    "severity": issue.severity,
                    "objective_code": issue.objective_code,
                    "message": issue.message,
                }
                for issue in issues
            ]
        )


def _adilet_url(group_number: str | None, lang: str) -> str:
    """Ссылка на приказ в «Әділет». В разборе её нет — собираем из НГР.

    Нужна на экране сверки: человек должен открыть первоисточник в одно нажатие.
    """
    if not group_number:
        return ""
    section = "rus" if lang == "ru" else "kaz"
    return f"https://adilet.zan.kz/{section}/docs/{group_number}"


def _as_date(value):
    """Дата из разбора приходит строкой ISO либо не приходит вовсе."""
    if not value:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value).date()
    return value
