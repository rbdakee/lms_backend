"""«Жоспар»: каталог игровых механик и отбор строительных блоков для игры.

Каталог — данные, а не код: по файлу `.toml` на механику, и добавить или
убрать механику — значит добавить или убрать файл (решение владельца,
22.09.2026; `L5_games_db_design.md`, раздел 5.1). Поэтому здесь проверяется
три вещи. Настоящий каталог собран честно: поля строгие, ссылки живые, уровни
согласованы (разделы 1, 2.1). Загрузчик отбивает кривую карточку с именем
файла, а новую подхватывает без правки кода. Отбор под заказ держит жёсткий
фильтр и разнообразие (раздел 4.3), а в сообщение модели уходят блоки,
а не меню, — и нигде в коде и промптах нет списка игр.
"""

import re
import shutil
from pathlib import Path

import pytest

from app.lesson_ai.context import LessonBrief
from app.lesson_ai.games.mechanic import (
    CATALOG_DIR,
    CatalogError,
    LangNotes,
    Mechanic,
    load_catalog,
)
from app.lesson_ai.games.select import FRAME, _fits, pick, references

# Доступ к «Жоспару» (L7) в этих тестах открыт: они проверяют продукт,
# а сама проверка доступа — в `test_lesson_ai_access.py`
pytestmark = pytest.mark.usefixtures("lesson_ai_open")

APP = Path(__file__).resolve().parent.parent / "app"
OBJECTIVE_CODE = re.compile(r"\d+\.\d+\.\d+\.\d+")
LEVELS = ("calm", "active", "full_game")
DIFFICULTIES = ("basic", "standard", "advanced")
SEEDS = range(12)
# Соседний уровень — своей копией из дизайна (раздел 4.3, шаг 3)
NEIGHBOUR = {"calm": "active", "active": "calm", "full_game": "active"}


def brief(**fields) -> LessonBrief:
    """Заказ глазами отбора: второй класс, математика, уровни по умолчанию."""
    base = {
        "lang": "ru",
        "grade": 2,
        "subject": "Математика",
        "section": "Числа и величины",
        "subsection": "Операции над числами",
        "objectives": ("выполнять устно сложение и вычитание двузначных чисел",),
        "topic": "Сложение и вычитание двузначных чисел",
        "class_size": 24,
        "notes": "",
        "subject_code": "math",
    }
    return LessonBrief(**(base | fields))


def all_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for part in value.values() for item in all_strings(part)]
    if isinstance(value, list | tuple):
        return [item for part in value for item in all_strings(part)]
    return []


@pytest.fixture(scope="module")
def cards() -> tuple[Mechanic, ...]:
    return load_catalog()


# -- настоящий каталог --------------------------------------------------------


def test_catalog_cards_are_named_by_their_files(cards):
    """Слаг — вечный ключ: он пишется в материал и в статистику. Файл
    и `id` совпадают, повторов нет."""
    files = sorted(path.stem for path in CATALOG_DIR.glob("*.toml"))

    assert len(cards) == 38
    assert [card.id for card in cards] == files
    assert len({card.id for card in cards}) == 38


def test_combines_with_points_to_existing_cards(cards):
    ids = {card.id for card in cards}

    for card in cards:
        assert set(card.combines_with) <= ids, card.id
        assert card.id not in card.combines_with, card.id


def test_every_card_scales_to_all_three_class_levels(cards):
    """Уровень класса — не фильтр, а масштаб (дизайн, раздел 2.1): модели
    уходит строка уровня заказа, и пустой строки быть не может."""
    for card in cards:
        for level in DIFFICULTIES:
            assert getattr(card.difficulty_scaling, level).strip(), (card.id, level)


def test_language_sensitive_cards_have_kazakh_notes(cards):
    """Механика на словах языка без казахских заметок казахскому классу
    не достанется вовсе — такая карточка в каталоге бесполезна наполовину."""
    for card in cards:
        if card.lang_sensitive:
            assert card.lang_notes.kz.strip(), card.id


def test_no_calm_native_card_runs_around_the_class(cards):
    """Учитель, выбравший «спокойно», беготни по классу не ждёт."""
    for card in cards:
        assert not (card.engagement_levels[0] == "calm" and card.movement == "around_class"), (
            card.id
        )


def test_no_objective_codes_anywhere_in_the_catalog(cards):
    """Код цели обучения модели не дают ни в одном виде (TEACHER_AI_BRIEF,
    раздел 4.1, правило 2): карточка уходит модели, и код в ней — дыра."""
    for card in cards:
        found = [text for text in all_strings(card.model_dump()) if OBJECTIVE_CODE.search(text)]
        assert found == [], card.id


# -- загрузчик на временной папке ---------------------------------------------


@pytest.fixture
def folder(tmp_path) -> Path:
    """Копия настоящего каталога: портим один файл, остальные честные."""
    target = tmp_path / "catalog"
    shutil.copytree(CATALOG_DIR, target)
    return target


def rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


BROKEN = {
    "extra_field": ("math_lotto", 'status = "draft"', 'status = "draft"\ncolor = "red"'),
    "outside_enum": ("math_lotto", 'noise = "quiet"', 'noise = "silent"'),
    "missing_scaling": (
        "math_lotto",
        'basic = "Ответы в пределах 10',
        'basik = "Ответы в пределах 10',
    ),
    "combines_with_unknown": (
        "math_lotto",
        'combines_with = ["story_journey", "station_quest"]',
        'combines_with = ["story_journey", "no_such_game"]',
    ),
    "grades_reversed": (
        "math_lotto",
        "grade_min = 1\ngrade_max = 4",
        "grade_min = 4\ngrade_max = 1",
    ),
    "calm_around_class": ("math_lotto", 'movement = "seated"', 'movement = "around_class"'),
    "broken_toml": ("math_lotto", 'title = "Математическое лото"', 'title = "Математическое лото'),
}


@pytest.mark.parametrize("case", list(BROKEN))
def test_broken_card_fails_loading_with_its_file_name(folder, case):
    """Кривая карточка — ошибка при старте воркеров, а не тихо кривая игра,
    и по сообщению сразу видно, какой файл чинить. У ссылки на несуществующую
    механику виновата ссылающаяся карточка."""
    name, old, new = BROKEN[case]
    rewrite(folder / f"{name}.toml", old, new)

    with pytest.raises(CatalogError) as error:
        load_catalog(folder)

    assert name in str(error.value)


def test_id_other_than_file_name_fails(folder):
    """Переименованный файл молча дал бы вторую механику под старым слагом."""
    (folder / "math_lotto.toml").rename(folder / "lotto.toml")

    with pytest.raises(CatalogError) as error:
        load_catalog(folder)

    assert "lotto.toml" in str(error.value)


def test_adding_and_removing_a_file_is_all_it_takes(folder):
    """Главное требование владельца: каталог — только данные. Новая
    механика — новый файл, убранная — удалённый; код не трогается."""
    text = (folder / "tangram.toml").read_text(encoding="utf-8")
    text = text.replace('id = "tangram"', 'id = "shape_hunt"', 1)
    text = re.sub(r'(?m)^title = ".*"$', 'title = "Охота за фигурами"', text, count=1)
    (folder / "shape_hunt.toml").write_text(text, encoding="utf-8")

    added = {card.id: card for card in load_catalog(folder)}
    assert len(added) == 39
    assert added["shape_hunt"].title == "Охота за фигурами"

    (folder / "shape_hunt.toml").unlink()
    ids = {card.id for card in load_catalog(folder)}
    assert len(ids) == 38
    assert "shape_hunt" not in ids


def test_removed_card_still_referenced_fails(folder):
    """Убрать карточку, на которую ссылаются, нельзя молча: `retired`
    вместо удаления (дизайн, раздел 1.1)."""
    (folder / "station_quest.toml").unlink()

    with pytest.raises(CatalogError) as error:
        load_catalog(folder)

    assert "station_quest" in str(error.value)


# -- отбор на настоящем каталоге ----------------------------------------------


def cases():
    for grade in (1, 2, 3, 4):
        for level in LEVELS:
            for lang in ("ru", "kz"):
                yield grade, level, lang


@pytest.mark.parametrize(("grade", "level", "lang"), list(cases()))
def test_pick_respects_the_hard_filter(cards, grade, level, lang):
    """Две-три механики из разных семейств, все по классу и уровню
    (дизайн, раздел 4.3, шаги 1 и 3)."""
    for difficulty in DIFFICULTIES:
        for seed in SEEDS:
            order = brief(
                grade=grade, engagement_level=level, difficulty_level=difficulty, lang=lang
            )
            picked = pick(cards, order, seed)

            assert 2 <= len(picked) <= 3, (grade, level, lang, seed)
            families = [card.family for card in picked]
            assert len(set(families)) == len(families)
            for card in picked:
                assert card.grade_min <= grade <= card.grade_max, card.id
                assert card.status != "retired"
                if grade == 1:
                    assert card.student_reading != "sentences", card.id
                    assert card.rules_complexity != "complex", card.id
                if lang == "kz" and card.lang_sensitive:
                    assert card.lang_notes.kz, card.id
            # Основная и вторая — с уровня заказа (у игры на весь урок ещё
            # станции квеста); третья может прийти с соседнего уровня как
            # чужой ход (шаг 3). Поэтому «каждая держит full_game» — только
            # про первые две
            station = level == "full_game"
            for card in picked[:2]:
                assert level in card.engagement_levels or (station and card.station_ready), card.id
            for card in picked[2:]:
                assert (
                    level in card.engagement_levels
                    or (station and card.station_ready)
                    or NEIGHBOUR[level] in card.engagement_levels
                ), card.id


def test_same_seed_same_pick_different_seeds_vary(cards):
    """Зерно — номер комплекта: жалобу разбирают, повторив отбор, а разные
    комплекты получают разное."""
    variety = set()
    for level in LEVELS:
        order = brief(engagement_level=level)
        for seed in SEEDS:
            first = [card.id for card in pick(cards, order, seed)]
            assert [card.id for card in pick(cards, order, seed)] == first
            variety.add((level, tuple(first)))
    assert len(variety) > len(LEVELS)


def test_retired_is_never_picked(cards):
    """`retired` вместо удаления: на механику ссылаются готовые игры,
    но в новые она не идёт."""
    retired = {"math_lotto", "dice_board", "station_quest", "math_domino", "track_race"}
    patched = [
        card.model_copy(update={"status": "retired"}) if card.id in retired else card
        for card in cards
    ]
    for grade in (1, 2, 3, 4):
        for level in LEVELS:
            for seed in SEEDS:
                order = brief(grade=grade, engagement_level=level)
                assert not {card.id for card in pick(patched, order, seed)} & retired
    # Отсечь могла именно отметка: без неё каждая проходит чей-то фильтр,
    # а не отсеяна классом или уровнем заранее
    by_id = {card.id: card for card in cards}
    for card_id in retired:
        card = by_id[card_id]
        assert any(
            _fits(card, brief(grade=grade, engagement_level=level), level)
            for grade in (1, 2, 3, 4)
            for level in LEVELS
        ), card_id


def test_all_retired_gives_nothing(cards):
    """Пустой фильтр — не отказ: игра генерируется и без опоры."""
    patched = [card.model_copy(update={"status": "retired"}) for card in cards]

    assert pick(patched, brief(), 1) == []


def synthetic(cards, **update) -> Mechanic:
    """Карточка, которая заведомо выигрывает ранжирование у остальных,
    если проходит фильтр: иначе её отсутствие ничего бы не доказывало."""
    base = next(card for card in cards if card.id == "math_lotto")
    fields = {
        "id": "synthetic_card",
        "title": "Синтетическая",
        "family": "synthetic_family",
        "status": "active",
        "engagement_levels": ["active"],
        "lesson_phases": ["middle"],
        "combines_with": [],
    }
    return base.model_copy(update=fields | update)


def test_kazakh_order_skips_language_cards_without_kazakh_notes(cards):
    card = synthetic(cards, lang_sensitive=True, lang_notes=LangNotes())
    catalogue = [*cards, card]

    ru = [pick(catalogue, brief(lang="ru"), seed) for seed in SEEDS]
    kz = [pick(catalogue, brief(lang="kz"), seed) for seed in SEEDS]

    # Русскому заказу она достаётся — значит, казахскому отказал именно язык
    assert all(card in picked for picked in ru)
    assert not any(card in picked for picked in kz)


def test_other_subject_does_not_get_math_only_cards(cards):
    math_only = [card for card in cards if card.subjects == ["math"]]
    assert math_only
    card = synthetic(cards, subjects=["math"])
    catalogue = [*cards, card]

    assert all(card in pick(catalogue, brief(), seed) for seed in SEEDS)
    for grade in (1, 2, 3, 4):
        for level in LEVELS:
            for seed in SEEDS:
                order = brief(grade=grade, engagement_level=level, subject_code="science")
                picked = pick(catalogue, order, seed)
                assert all(item.subjects != ["math"] for item in picked)


# -- что уходит модели --------------------------------------------------------


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_references_are_blocks_not_a_menu(cards, difficulty):
    """Рамка «блоки, не меню» — первой; из карточки — заголовок и строка
    уровня заказа; служебного и кодов целей — нет (дизайн, раздел 4.3,
    шаги 5 и 6)."""
    order = brief(difficulty_level=difficulty)

    text, slugs = references(order, 7)

    picked = pick(cards, order, 7)
    assert slugs == tuple(card.id for card in picked)
    assert text.startswith(FRAME)
    assert text.startswith("Строительные блоки для игры")
    assert "Это не меню" in text.split("\n\n")[0]
    for card in picked:
        assert f"### {card.title}" in text
        assert getattr(card.difficulty_scaling, difficulty) in text
        for other in DIFFICULTIES:
            if other != difficulty:
                assert getattr(card.difficulty_scaling, other) not in text
        assert card.source not in text
        if card.export_shape != "none":
            assert card.export_shape not in text
    assert not OBJECTIVE_CODE.search(text)
    assert "export_shape" not in text
    assert "source" not in text


# -- никакого хардкода списка игр ----------------------------------------------


def test_no_card_id_or_title_in_code_or_prompts(cards):
    """Требование владельца: список игр — только в каталоге. Имя механики
    в `.py` приложения (включая промпты `app/lesson_ai/prompts/`) — это уже
    меню, и добавленная файлом механика окажется второго сорта."""
    sources = {path: path.read_text(encoding="utf-8") for path in APP.rglob("*.py")}
    assert APP / "lesson_ai" / "prompts" / "game.py" in sources

    found = []
    for card in cards:
        for path, text in sources.items():
            if re.search(rf"\b{re.escape(card.id)}\b", text) or card.title in text:
                found.append((card.id, str(path.relative_to(APP))))
    assert found == []


# -- самопроверка: поломки ловятся --------------------------------------------


def test_filter_checks_are_not_vacuous(cards):
    """Фильтр отбора действительно режет: без проверки класса первому
    классу достались бы механики 3–4 классов. Если бы каталог таких не
    держал, проверка класса выше была бы пустой."""
    assert any(card.grade_min > 1 for card in cards)
    assert any(card.student_reading == "sentences" for card in cards)
    assert any(
        not card.station_ready and "full_game" not in card.engagement_levels for card in cards
    )
