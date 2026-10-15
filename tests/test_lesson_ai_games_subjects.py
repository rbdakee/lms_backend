"""«Жоспар»: каталог игр под все предметы начальной школы.

Каталог писался под математику — механики с примерами и счётом. С дополнением
16.10.2026 у него есть предметные блоки и для остальных предметов реестра:
языки, литературное чтение, познание мира, естествознание, музыка, труд, ИЗО,
физкультура. Отсюда три проверки. Темы `math_topics` живут только на
математических карточках — у универсальных и предметных не-математики их нет,
и в ранжировании поле не участвует вовсе. Отбор держит минимум две механики
для каждого предмета на каждом уровне вовлечённости и в каждом своём классе,
на обоих языках обучения, и математическая карточка в нематематический урок
не проходит. И карточка с кодом предмета в `subjects` стоит в ранжировании
выше универсальной «any» — иначе предметные карточки не имели бы смысла.

Реестр предметов и их классы — у парсера программы (`parser._SUBJECTS`):
он и есть источник справочника, дублировать его здесь нельзя.
"""

import pytest

from app.adapters.curriculum.parser import _SUBJECTS
from app.lesson_ai.context import LessonBrief
from app.lesson_ai.games.mechanic import Mechanic, load_catalog
from app.lesson_ai.games.select import pick

LEVELS = ("calm", "active", "full_game")
LANGS = ("ru", "kz")
SEEDS = range(6)


def brief(**fields) -> LessonBrief:
    """Заказ глазами отбора: второй класс, познание мира, уровни по умолчанию."""
    base = {
        "lang": "ru",
        "grade": 2,
        "subject": "Познание мира",
        "section": "Раздел программы",
        "subsection": "",
        "objectives": ("различать живое и неживое",),
        "topic": "Живое и неживое",
        "class_size": 24,
        "notes": "",
        "subject_code": "world_around",
    }
    return LessonBrief(**(base | fields))


@pytest.fixture(scope="module")
def cards() -> tuple[Mechanic, ...]:
    return load_catalog()


def test_math_topics_live_only_on_math_cards(cards):
    """math_topics — данные математических карточек: у универсальной или
    предметной не-математики их нет. Ранжирование поле не читает —
    нематематический урок за его отсутствие не штрафуется."""
    for card in cards:
        if card.math_topics:
            assert "math" in card.subjects, card.id


def subject_cases():
    for code, spec in _SUBJECTS.items():
        for grade in spec.grades:
            for level in LEVELS:
                for lang in LANGS:
                    yield code, spec.title, grade, level, lang


@pytest.mark.parametrize(("code", "title", "grade", "level", "lang"), list(subject_cases()))
def test_every_subject_has_two_mechanics_at_every_level(cards, code, title, grade, level, lang):
    """Покрытие предмет×уровень×класс: не меньше двух механик на любом зерне.
    Математическая карточка (subjects ровно ["math"]) в нематематическом
    уроке не встречается вовсе."""
    order = brief(
        subject=title,
        grade=grade,
        engagement_level=level,
        lang=lang,
        subject_code=code,
    )
    for seed in SEEDS:
        picked = pick(cards, order, seed)

        assert len(picked) >= 2, (code, grade, level, lang, seed)
        for card in picked:
            assert card.subjects != ["math"] or code == "math", (card.id, code)


def test_subject_card_ranks_above_universal(cards):
    """Карточка с кодом предмета в subjects стоит выше универсальной при
    равных прочих: это единственное, что отличает парные синтетические
    карточки, — значит, во главе отбора предметная, а «any» не отобрана.
    Та же карточка с «any» фильтр проходит, то есть дело в ранжировании,
    а не в фильтре."""
    base = next(card for card in cards if card.id == "math_lotto")
    common = {
        "id": "synthetic_subject_card",
        "title": "Предметная синтетическая",
        "family": "synthetic_subject",
        "status": "active",
        "subjects": ["world_around"],
        "engagement_levels": ["active"],
        "lesson_phases": ["middle"],
        "combines_with": [],
        "math_topics": [],
    }
    subject_card = base.model_copy(update=common)
    universal_card = subject_card.model_copy(
        update={"id": "synthetic_universal_card", "family": "synthetic_universal",
                "subjects": ["any"]}
    )
    order = brief(subject_code="world_around", engagement_level="active")

    for seed in SEEDS:
        # Парные карточки скрещиваются в один отбор — предметная во главе
        assert pick([subject_card, universal_card], order, seed)[0] is subject_card, seed
        assert pick([universal_card], order, seed)[0] is universal_card, seed
