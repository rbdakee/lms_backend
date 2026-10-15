"""Автопроверки импортированного справочника учебной программы.

Справочник вычитывает владелец руками (TEACHER_AI_BRIEF, раздел 3.1), и читать ровные
строки в поисках одной кривой — не работа человека. Всё, что ловится без знания предмета,
ловится здесь и попадает в `curriculum_issue`; человеку остаётся смысл.

Модуль чистый: на вход — разобранные парсером словари, на выходе — список подозрений.
Ни базы, ни фреймворка.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

# Код цели: класс.раздел.подраздел.номер. Номер бывает двузначным (3.1.2.14),
# и жёсткое \d потеряло бы половину подраздела.
CODE_RE = re.compile(r"\b\d{1,2}\.\d+\.\d+\.\d{1,2}\b")

# Обрубок кода в тексте: «3.1.4. использовать…» — в приказе у цели потеряна последняя
# цифра кода, и парсер приклеил её к предыдущей цели. Живые случаи 12.10.2026: «3.1.4.»
# в литературном чтении (рус.) и «1.1. 5.» в «Ана тілі» (каз.).
CODE_STUB_RE = re.compile(r"(?<![\d.])\d\s*\.\s*\d\s*\.\s*\d\s*\.(?=\s*[^\d\s])")

# Формулировка цели короче этого — почти наверняка обрубок ячейки, а не цель.
MIN_TEXT_LEN = 15

# Обрыв формулировки на середине перечисления. Цели пишутся со строчной буквы и без
# точки в конце, поэтому «начинается не с заглавной» и «нет точки» тут не признаки —
# а вот висящая запятая или открытая скобка означают, что текст оборвался.
TRUNCATED_TAIL_RE = re.compile(r"[,;(]\s*$")

# Знаки, которым в формулировке математической цели взяться неоткуда. Появляются
# из-за шрифта Symbol: в нём ∈ кодируется той же позицией, что € в обычной кодировке,
# и при выгрузке приказа в HTML знак принадлежности молча превращается в валюту.
# Найдено на живых данных 19.09.2026 в цели 2.5.2.2: «знаки € и ∉».
SUSPICIOUS_SYMBOLS = "€£¥¤§¶†‡"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    """Одно подозрение. `kind` — машинный код проверки, по нему группируется список."""

    kind: str
    severity: str
    message: str
    objective_code: str | None = None


def _objectives(program: dict) -> list[dict]:
    return program.get("objectives") or []


def _by_code(program: dict) -> dict[str, dict]:
    # Дубликаты отлавливает отдельная проверка; здесь побеждает последний, чтобы
    # остальные проверки не падали на сборке словаря.
    return {o["code"]: o for o in _objectives(program)}


def check_program(program: dict, lang: str) -> list[Issue]:
    """Проверки внутри одной языковой редакции."""
    issues: list[Issue] = []
    objectives = _objectives(program)

    seen: dict[str, int] = {}
    for obj in objectives:
        code = obj["code"]
        seen[code] = seen.get(code, 0) + 1

    for code, count in seen.items():
        if count > 1:
            issues.append(
                Issue(
                    kind="duplicate_code",
                    severity=SEVERITY_ERROR,
                    objective_code=code,
                    message=(
                        f"[{lang}] код встречается {count} раза — "
                        f"в справочнике он должен быть один"
                    ),
                )
            )

    for obj in objectives:
        code = obj["code"]
        text = (obj.get("text") or "").strip()

        # Класс берётся из первой цифры кода, а колонка таблицы — только сверка.
        # Расхождение значит, что цель уехала в чужой класс при разборе.
        code_grade = int(code.split(".")[0])
        if obj.get("grade") != code_grade:
            issues.append(
                Issue(
                    kind="grade_mismatch",
                    severity=SEVERITY_ERROR,
                    objective_code=code,
                    message=(
                        f"[{lang}] класс {obj.get('grade')} не совпадает с первой цифрой кода "
                        f"({code_grade})"
                    ),
                )
            )

        if not text:
            issues.append(
                Issue(
                    kind="empty_text",
                    severity=SEVERITY_ERROR,
                    objective_code=code,
                    message=f"[{lang}] у цели пустая формулировка",
                )
            )
        elif len(text) < MIN_TEXT_LEN:
            issues.append(
                Issue(
                    kind="short_text",
                    severity=SEVERITY_WARNING,
                    objective_code=code,
                    message=f"[{lang}] формулировка короче {MIN_TEXT_LEN} знаков: «{text}»",
                )
            )

        # Чужой код внутри формулировки означает, что в одну ячейку слиплись две цели.
        foreign = [c for c in CODE_RE.findall(text) if c != code]
        if foreign:
            issues.append(
                Issue(
                    kind="code_inside_text",
                    severity=SEVERITY_ERROR,
                    objective_code=code,
                    message=(
                        f"[{lang}] внутри формулировки встретился чужой код: "
                        f"{', '.join(foreign)}"
                    ),
                )
            )

        stub = CODE_STUB_RE.search(text)
        if stub:
            issues.append(
                Issue(
                    kind="broken_code_in_text",
                    severity=SEVERITY_ERROR,
                    objective_code=code,
                    message=(
                        f"[{lang}] в формулировке обрубок кода «{stub.group(0).strip()}» — "
                        f"в приказе потеряна цифра, и под ней слиплись две цели"
                    ),
                )
            )

        if TRUNCATED_TAIL_RE.search(text):
            issues.append(
                Issue(
                    kind="truncated_text",
                    severity=SEVERITY_WARNING,
                    objective_code=code,
                    message=f"[{lang}] формулировка обрывается на «{text[-24:]}»",
                )
            )

        # Математические знаки (≠, ∩, ∪, ∅, ≥) вставлены в приказ картинками. Снять тег
        # молча значит потерять знак, поэтому парсер оставляет метку — а человек обязан
        # проставить знак руками.
        if "[img]" in text:
            issues.append(
                Issue(
                    kind="image_in_text",
                    severity=SEVERITY_ERROR,
                    objective_code=code,
                    message=(
                        f"[{lang}] вместо математического знака стоит [img] — "
                        f"проставить руками"
                    ),
                )
            )

        wrong = sorted({c for c in text if c in SUSPICIOUS_SYMBOLS})
        if wrong:
            issues.append(
                Issue(
                    kind="suspicious_symbol",
                    severity=SEVERITY_ERROR,
                    objective_code=code,
                    message=(
                        f"[{lang}] в формулировке стоит знак {' '.join(wrong)} — "
                        f"похоже на подменённый математический символ, сверить с приказом"
                    ),
                )
            )

        if not obj.get("quarters"):
            issues.append(
                Issue(
                    kind="no_quarter",
                    severity=SEVERITY_WARNING,
                    objective_code=code,
                    message=f"[{lang}] цель не привязана ни к одной четверти",
                )
            )

    issues.extend(_check_numbering(objectives, lang))
    issues.extend(_check_empty_sections(program, objectives, lang))
    return issues


def _check_numbering(objectives: Iterable[dict], lang: str) -> list[Issue]:
    """Разрыв в нумерации внутри подраздела одного класса.

    Это подозрение, а не отказ: цель могли законно исключить приказом об изменениях.
    Решает человек, поэтому severity — предупреждение.
    """
    groups: dict[tuple[int, str], list[int]] = {}
    for obj in objectives:
        key = (obj["grade"], obj.get("subsection_code") or "")
        groups.setdefault(key, []).append(obj["index"])

    issues: list[Issue] = []
    for (grade, subsection), indexes in sorted(groups.items()):
        ordered = sorted(indexes)
        missing = [n for n in range(1, ordered[-1]) if n not in ordered]
        if missing:
            gaps = ", ".join(str(n) for n in missing)
            issues.append(
                Issue(
                    kind="numbering_gap",
                    severity=SEVERITY_WARNING,
                    objective_code=f"{grade}.{subsection}",
                    message=(
                        f"[{lang}] в подразделе {subsection} у {grade} класса "
                        f"пропущены номера: {gaps}"
                    ),
                )
            )
    return issues


def _check_empty_sections(program: dict, objectives: Iterable[dict], lang: str) -> list[Issue]:
    """Раздел или подраздел, в который не попало ни одной цели."""
    used_subsections = {o.get("subsection_code") for o in objectives}
    used_sections = {o.get("section_no") for o in objectives}

    issues: list[Issue] = []
    for section in program.get("sections") or []:
        if section.get("no") not in used_sections:
            issues.append(
                Issue(
                    kind="empty_section",
                    severity=SEVERITY_WARNING,
                    message=f"[{lang}] в разделе «{section.get('title')}» нет ни одной цели",
                )
            )
        for sub in section.get("subsections") or []:
            if sub.get("code") not in used_subsections:
                issues.append(
                    Issue(
                        kind="empty_subsection",
                        severity=SEVERITY_WARNING,
                        message=(
                            f"[{lang}] в подразделе {sub.get('code')} «{sub.get('title')}» "
                            f"нет ни одной цели"
                        ),
                    )
                )
    return issues


def check_pair(ru: dict, kz: dict) -> list[Issue]:
    """Сверка двух языковых редакций одного приказа.

    Главная проверка импорта: коды целей в русской и казахской редакциях обязаны
    совпадать — это один и тот же приказ. Любое расхождение означает потерю при разборе,
    и ловится оно без знания предмета.
    """
    issues: list[Issue] = []
    ru_by_code = _by_code(ru)
    kz_by_code = _by_code(kz)

    for code in sorted(set(ru_by_code) - set(kz_by_code)):
        issues.append(
            Issue(
                kind="missing_in_kz",
                severity=SEVERITY_ERROR,
                objective_code=code,
                message="код есть в русской редакции и отсутствует в казахской",
            )
        )
    for code in sorted(set(kz_by_code) - set(ru_by_code)):
        issues.append(
            Issue(
                kind="missing_in_ru",
                severity=SEVERITY_ERROR,
                objective_code=code,
                message="код есть в казахской редакции и отсутствует в русской",
            )
        )

    for code in sorted(set(ru_by_code) & set(kz_by_code)):
        left, right = ru_by_code[code], kz_by_code[code]

        if left.get("subsection_code") != right.get("subsection_code"):
            issues.append(
                Issue(
                    kind="subsection_mismatch",
                    severity=SEVERITY_ERROR,
                    objective_code=code,
                    message=(
                        f"цель отнесена к разным подразделам: "
                        f"ru {left.get('subsection_code')}, kz {right.get('subsection_code')}"
                    ),
                )
            )

        if sorted(left.get("quarters") or []) != sorted(right.get("quarters") or []):
            issues.append(
                Issue(
                    kind="quarters_mismatch",
                    severity=SEVERITY_WARNING,
                    objective_code=code,
                    message=(
                        f"четверти разошлись: ru {left.get('quarters')}, kz {right.get('quarters')}"
                    ),
                )
            )

    ru_count, kz_count = len(ru_by_code), len(kz_by_code)
    if ru_count != kz_count:
        issues.append(
            Issue(
                kind="count_mismatch",
                severity=SEVERITY_ERROR,
                message=f"число целей разошлось: ru {ru_count}, kz {kz_count}",
            )
        )
    return issues


def check_all(ru: dict, kz: dict) -> list[Issue]:
    """Полный прогон: обе редакции по отдельности и сверка между ними."""
    return check_program(ru, "ru") + check_program(kz, "kz") + check_pair(ru, kz)
