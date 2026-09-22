"""«Жоспар»: правка готового ҚМЖ по блокам — перегенерация одного блока,
ручная правка, история версий и откат.

Формы ответов и отказов сверяются целиком с `CONTRACT.md`, раздел «Жоспар:
файл и правка по блокам (сессия L4)»: редактор по блокам пишется от этих
примеров.

Главное в файле — «правка одного блока не трогает соседние» (TEACHER_AI_BRIEF,
раздел 4.1, правило 3). Поэтому после каждой правки сравнивается не один блок,
а снимок материала целиком: документ, все строки блоков и вся история. Второе
по важности — код цели: модели его не показывают и при перегенерации, даже
если учитель сам вписал его в соседний блок, и сочинённый ею код отбивается.

Фоновых воркеров в тестах нет: план собирается руками через `run_next(ai)`,
а перегенерацию блока обслуживает тот же подставной ИИ в ручке.
"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import (
    LessonAiJob,
    LessonKit,
    LessonMaterial,
    LessonMaterialBlock,
    LessonMaterialVersion,
)
from app.domain.lesson_ai import objective_codes_in
from tests.conftest import FakeAi, login
from tests.test_lesson_ai_kits import (
    CODE,
    DRAFT,
    OBJECTIVE_TEXT,
    OTHER_PHONE,
    Scene,
    generated,
    invalid,
    order,
    sent_message,
    set_program_status,
)

# Доступ к «Жоспару» (L7) в этих тестах открыт: они проверяют продукт,
# а сама проверка доступа — в `test_lesson_ai_access.py`
pytestmark = pytest.mark.usefixtures("lesson_ai_open")

# Порядок формы ҚМЖ — он же `order_index`. Выписан здесь, а не взят из схемы:
# иначе тест сверял бы схему саму с собой
BLOCK_ORDER = [
    "section",
    "learning_objectives",
    "lesson_goals",
    "assessment_criteria",
    "language_goals",
    "values",
    "cross_curricular_links",
    "prior_knowledge",
    "stage_beginning",
    "stage_middle",
    "stage_end",
    "differentiation",
    "assessment",
    "health_and_safety",
    "reflection",
]
SERVER_BLOCKS = {"section", "learning_objectives"}
# Середина урока — десятый блок формы, `order_index` 9: счёт с нуля, как
# в примере GET …/blocks (в примере ответа перегенерации контракта стоит 10)
MIDDLE_INDEX = BLOCK_ORDER.index("stage_middle")

# Документ сразу после генерации: блоки сервера из справочника плюс черновик модели
ORIGINAL = {
    "section": {"title": "Числа и величины", "subsection": "Операции над числами"},
    "learning_objectives": [{"code": CODE, "text": OBJECTIVE_TEXT, "marker": ""}],
    **DRAFT,
}

INSTRUCTION = "сделай середину урока подвижнее, с игрой по рядам"

# Середина урока, переписанная моделью по просьбе учителя
NEW_MIDDLE = [
    {
        "teacher": "Проводит игру «Передай пример»: первый ряд — 23 + 14, второй — 41 + 26.",
        "students": "Решают по цепочке и передают карточку соседу по ряду.",
        "resources": ["карточки с примерами"],
    }
]
# Середина урока, которую учитель переписал сам
TEACHER_MIDDLE = [
    {
        "teacher": "Разбирает 53 + 14 на доске, затем даёт пары примеров по вариантам.",
        "students": "Решают свой вариант и проверяют друг друга.",
        "resources": [],
    }
]

MATERIAL_NOT_FOUND = {"error": {"code": "not_found", "message": "Материал не найден"}}
BLOCK_NOT_FOUND = {"error": {"code": "not_found", "message": "Блок не найден"}}
VERSION_NOT_FOUND = {"error": {"code": "not_found", "message": "Версия не найдена"}}
MATERIAL_NOT_READY = {"error": {"code": "material_not_ready", "message": "Материал ещё не готов"}}
BLOCK_LOCKED = {
    "error": {
        "code": "block_locked",
        "message": "Этот блок берётся из учебной программы и не правится",
    }
}
BLOCK_CHANGED = {
    "error": {
        "code": "block_changed",
        "message": "Блок изменился, пока шла перегенерация — обновите и повторите",
    }
}
PROGRAM_NOT_PUBLISHED = {
    "error": {
        "code": "program_not_published",
        "message": "Программа ещё не опубликована — готовить материалы по ней нельзя",
    }
}
AI_SILENT = {"error": {"code": "ai_failed", "message": "Модель не ответила — попробуйте ещё раз"}}
AI_MISSHAPED = {
    "error": {
        "code": "ai_failed",
        "message": "Модель вернула блок не по форме — попробуйте ещё раз",
    }
}


@pytest.fixture
def scene() -> Scene:
    return Scene()


def ready(client, sms, ai, scene: Scene, **kw) -> int:
    """Готовый план своего учителя: номер его материала."""
    body = generated(client, sms, ai, scene, **kw)
    [material] = body["materials"]
    assert material["status"] == "ready"
    return material["id"]


def url(material_id: int, *parts) -> str:
    return "/".join([f"/lesson_ai/materials/{material_id}/blocks", *map(str, parts)])


def regenerate(client, material_id: int, block: str, **body):
    return client.post(url(material_id, block, "regenerate"), json=body)


def edit(client, material_id: int, block: str, content):
    return client.put(url(material_id, block), json={"content": content})


def versions(client, material_id: int, block: str) -> list[dict]:
    resp = client.get(url(material_id, block, "versions"))
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def restore(client, material_id: int, block: str, version_id: int):
    return client.post(url(material_id, block, "versions", version_id, "restore"))


def snapshot(material_id: int) -> dict:
    """Всё, что правка блока могла задеть, прямо из базы: документ, строки
    блоков, вся история и очередь. Сравнение снимков «до» и «после» ловит
    и лишний тронутый блок, и переписанную задним числом версию."""
    with OrmSession(get_engine()) as db:
        material = db.get(LessonMaterial, material_id)
        kit = db.get(LessonKit, material.kit_id)
        blocks = db.scalars(
            select(LessonMaterialBlock).where(LessonMaterialBlock.material_id == material_id)
        ).all()
        kinds = {block.id: block.kind for block in blocks}
        rows = db.scalars(
            select(LessonMaterialVersion)
            .where(LessonMaterialVersion.block_id.in_(kinds))
            .order_by(LessonMaterialVersion.id)
        ).all()
        jobs = db.scalars(select(LessonAiJob).order_by(LessonAiJob.id)).all()
        return {
            "status": (material.status, kit.status),
            "content": material.content,
            "blocks": {
                block.kind: (block.order_index, block.content, block.updated_at) for block in blocks
            },
            "versions": [
                (
                    row.id,
                    kinds[row.block_id],
                    row.source,
                    row.instruction,
                    row.content,
                    row.user_id,
                    row.created_at,
                )
                for row in rows
            ],
            "jobs": [(job.id, job.kind, job.status, job.attempts) for job in jobs],
        }


def assert_invariant(snap: dict) -> None:
    """Инвариант L3: ключ документа и строка блока — одно и то же."""
    assert set(snap["blocks"]) == set(snap["content"]) == set(BLOCK_ORDER)
    for kind, (_order, content, _updated) in snap["blocks"].items():
        assert snap["content"][kind] == content, kind


def added_versions(before: dict, after: dict) -> list[tuple]:
    """Новые версии — без id, автора и метки времени; заодно проверяет,
    что прежние остались как были: история не переписывается."""
    assert after["versions"][: len(before["versions"])] == before["versions"]
    return [row[1:5] for row in after["versions"][len(before["versions"]) :]]


def shown_document(message: str) -> dict:
    """Документ, который модель видит в сообщении, — разобранный JSON."""
    tail = message.split("Текущий документ (JSON):\n", 1)[1]
    return json.loads(tail.split("\n\nПерепиши", 1)[0])


# -- GET /lesson_ai/materials/{id}/blocks -----------------------------------


def test_blocks_come_in_form_order(client, sms, ai, scene):
    """Редактор рисует блоки сверху вниз прямо по списку: порядок — порядок
    формы ҚМЖ, а закрыты от правки ровно два блока сервера. Содержимое
    блока — ровно ключ документа: экран и выгрузка не расходятся."""
    material_id = ready(client, sms, ai, scene)

    resp = client.get(url(material_id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["material_id"], body["kind"]) == (material_id, "lesson_plan")
    items = body["items"]
    assert [item["kind"] for item in items] == BLOCK_ORDER
    for number, item in enumerate(items):
        assert item == {
            "kind": BLOCK_ORDER[number],
            "order_index": number,
            "editable": item["kind"] not in SERVER_BLOCKS,
            "content": ORIGINAL[item["kind"]],
            "updated_at": item["updated_at"],
        }
        assert item["updated_at"].endswith("Z")
    assert [item["kind"] for item in items if not item["editable"]] == [
        "section",
        "learning_objectives",
    ]
    assert_invariant(snapshot(material_id))


# -- POST …/blocks/{block}/regenerate ---------------------------------------


def test_regenerate_rewrites_exactly_one_block(client, sms, ai, scene):
    """«Перепиши середину урока» меняет одно поле: остальные четырнадцать
    блоков, их история и документ вокруг — байт в байт как были. Материал
    целиком не пересобирается: ни нового задания в очереди, ни второго
    вызова модели."""
    material_id = ready(client, sms, ai, scene)
    before = snapshot(material_id)
    calls = len(ai.calls)
    ai.returns_json({"stage_middle": NEW_MIDDLE})

    resp = regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "kind": "stage_middle",
        "order_index": MIDDLE_INDEX,
        "editable": True,
        "content": NEW_MIDDLE,
        "updated_at": body["updated_at"],
    }
    assert body["updated_at"].endswith("Z")
    assert len(ai.calls) == calls + 1

    after = snapshot(material_id)
    assert after["jobs"] == before["jobs"]
    assert after["status"] == before["status"] == ("ready", "ready")
    assert after["content"] == {**before["content"], "stage_middle": NEW_MIDDLE}
    untouched = {kind: row for kind, row in before["blocks"].items() if kind != "stage_middle"}
    assert {kind: after["blocks"][kind] for kind in untouched} == untouched
    assert after["blocks"]["stage_middle"][:2] == (MIDDLE_INDEX, NEW_MIDDLE)
    assert_invariant(after)
    assert added_versions(before, after) == [
        ("stage_middle", "regenerated", INSTRUCTION, NEW_MIDDLE)
    ]
    # Список блоков после правки — то же самое глазами экрана
    listed = {
        item["kind"]: item["content"] for item in client.get(url(material_id)).json()["items"]
    }
    assert listed == {**ORIGINAL, "stage_middle": NEW_MIDDLE}


@pytest.mark.parametrize("body", [{}, {"instruction": "  \n\t "}], ids=["absent", "blank"])
def test_regenerate_without_instruction_stores_null(client, sms, ai, scene, body):
    """Кнопка «переписать заново» без просьбы — законный запрос. В истории
    просьбы нет (`null`, а не пустая строка), и в сообщение модели не уходит
    пустая строка «Просьба учителя:» — её модель прочла бы как просьбу."""
    material_id = ready(client, sms, ai, scene)
    before = snapshot(material_id)
    ai.returns_json({"values": "Взаимопомощь и терпение, когда сосед по парте ошибся."})

    resp = client.post(url(material_id, "values", "regenerate"), json=body)

    assert resp.status_code == 200, resp.text
    assert "Просьба учителя" not in sent_message(ai)
    after = snapshot(material_id)
    assert added_versions(before, after) == [
        (
            "values",
            "regenerated",
            None,
            "Взаимопомощь и терпение, когда сосед по парте ошибся.",
        )
    ]
    assert versions(client, material_id, "values")[0]["instruction"] is None


def test_instruction_over_500_chars_is_refused(client, sms, ai, scene):
    """Просьба длиннее 500 знаков — не просьба, а попытка переписать документ
    через блок. Отказ до модели: вызов стоил бы денег."""
    material_id = ready(client, sms, ai, scene)
    before = snapshot(material_id)
    calls = len(ai.calls)

    resp = regenerate(client, material_id, "stage_middle", instruction="я" * 501)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert len(ai.calls) == calls
    assert snapshot(material_id) == before


# -- что уходит модели при перегенерации ------------------------------------


def test_model_gets_the_document_without_server_blocks_and_codes(client, sms, ai):
    """Модели нужен весь документ, чтобы новый блок согласовался с соседними, —
    но без блоков сервера и без единого кода цели: ни из справочника, ни из
    темы и пожеланий, ни из блока, куда учитель вписал код руками, ни из самой
    просьбы. Ответить она может только одним полем: других в схеме нет.
    Системный промпт — тот же, что у генерации плана: кэш провайдера общий."""
    glued = "выполнять устно сложение 3.1.2.4 и вычитание двузначных чисел"
    scene = Scene(text=glued)
    material_id = ready(
        client,
        sms,
        ai,
        scene,
        topic="2.1.2.6 Сложение двузначных чисел",
        notes="повторить 2.1.2.5, путали разряды",
    )
    plan_system = ai.calls[-1]["system"]
    # Ручная правка кода не проверяет — это документ учителя (решение 12)
    typed = edit(client, material_id, "values", "Взаимоуважение, как в цели 1.2.3.4.")
    assert typed.status_code == 200, typed.text
    ai.returns_json({"stage_middle": NEW_MIDDLE})

    resp = regenerate(
        client, material_id, "stage_middle", instruction="как в цели 2.1.2.6 — с игрой"
    )

    assert resp.status_code == 200, resp.text
    call = ai.calls[-1]
    message = sent_message(ai)
    schema = call["schema"]
    for code in (CODE, "2.1.2.5", "3.1.2.4", "1.2.3.4"):
        for part in (call["system"], message, json.dumps(schema, ensure_ascii=False)):
            assert code not in part
    assert objective_codes_in([call["system"], message, schema]) == []

    document = shown_document(message)
    assert set(document) == set(DRAFT)
    assert document["values"] == "Взаимоуважение, как в цели."
    assert document["stage_middle"] == DRAFT["stage_middle"]
    assert '"section"' not in message
    assert '"learning_objectives"' not in message

    lines = message.splitlines()
    assert "Тема урока: Сложение двузначных чисел" in lines
    assert "Цель обучения по программе: выполнять устно сложение и вычитание двузначных чисел" in (
        lines
    )
    assert "Перепиши только блок «stage_middle»." in message
    assert "Середина урока, 5–35 минут" in message
    assert "Просьба учителя: как в цели — с игрой" in lines
    assert "Весь текст блока — на русском языке." in lines

    assert call["name"] == "lesson_plan_stage_middle"
    assert schema["type"] == "object"
    assert list(schema["properties"]) == ["stage_middle"]
    assert schema["required"] == ["stage_middle"]
    assert schema["additionalProperties"] is False
    assert call["system"] == plan_system

    # Просьба в истории — как её написал учитель: вырезается она только
    # на пути к модели
    assert versions(client, material_id, "stage_middle")[0]["instruction"] == (
        "как в цели 2.1.2.6 — с игрой"
    )


def test_kazakh_kit_asks_for_a_kazakh_block(client, sms, ai):
    """Язык блока — язык обучения комплекта: казахский ҚМЖ, в котором одна
    середина урока вдруг по-русски, завуч не примет."""
    kz = Scene(lang="kz", code="3.1.2.4", text="екі таңбалы сандарды ауызша қосу")
    material_id = ready(client, sms, ai, kz)
    ai.returns_json({"reflection": ["Барлық оқушы шешу жолын түсіндіре алды ма?"]})

    resp = regenerate(client, material_id, "reflection")

    assert resp.status_code == 200, resp.text
    message = sent_message(ai)
    assert "Весь текст блока — на казахском языке." in message.splitlines()
    assert "3.1.2.4" not in message


# -- кривой ответ модели ----------------------------------------------------


@pytest.mark.parametrize(
    "first",
    [
        "это не JSON",
        {"stage_middle": []},
        {"stage_middle": [{"teacher": "Объясняет.", "resources": []}]},
        {"stage_middle": NEW_MIDDLE, "values": "Дописала соседний блок."},
        {"values": "Не тот блок."},
        {"stage_middle": [{**NEW_MIDDLE[0], "teacher": "Работаем по цели 2.1.2.6."}]},
    ],
    ids=["not-json", "empty-list", "missing-field", "extra-block", "wrong-block", "code"],
)
def test_misshaped_answer_is_asked_again_once(client, sms, ai, scene, first):
    """Кривой ответ переспрашивается один раз в том же запросе: учитель ждёт
    у экрана, и второй ровный ответ — обычный успех. Ответ, дописавший
    соседний блок, — тоже кривой: иначе модель правила бы то, о чём не просили."""
    material_id = ready(client, sms, ai, scene)
    before = snapshot(material_id)
    calls = len(ai.calls)
    ai.returns_json(first).returns_json({"stage_middle": NEW_MIDDLE})

    resp = regenerate(client, material_id, "stage_middle")

    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == NEW_MIDDLE
    assert len(ai.calls) == calls + 2
    after = snapshot(material_id)
    assert after["content"] == {**ORIGINAL, "stage_middle": NEW_MIDDLE}
    assert added_versions(before, after) == [("stage_middle", "regenerated", None, NEW_MIDDLE)]


@pytest.mark.parametrize(
    "answer",
    [
        '{"stage_middle": ',
        {"stage_middle": [{**NEW_MIDDLE[0], "teacher": "Работаем по цели 2.1.2.6."}]},
        {"stage_middle": [{**NEW_MIDDLE[0], "resources": ["карточка 2.1.2.6"]}]},
    ],
    ids=["broken-json", "code-in-teacher", "code-in-resources"],
)
def test_twice_misshaped_answer_is_ai_failed(client, sms, ai, scene, answer):
    """Дважды кривой ответ — отказ с внятной причиной, а не третья попытка:
    ещё десяток секунд ожидания. Сочинённый код цели в любой строке блока —
    такой же брак, как ответ не по форме. Блок и история остаются как были."""
    material_id = ready(client, sms, ai, scene)
    before = snapshot(material_id)
    calls = len(ai.calls)
    ai.returns_json(answer).returns_json(answer)

    resp = regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION)

    assert resp.status_code == 502
    assert resp.json() == AI_MISSHAPED
    assert len(ai.calls) == calls + 2
    assert snapshot(material_id) == before


def test_silent_provider_is_ai_failed_without_retry(client, sms, ai, scene):
    """Провайдер не ответил — клиент уже повторял сам, дальше решает учитель
    кнопкой. Записано ничего не должно быть."""
    material_id = ready(client, sms, ai, scene)
    before = snapshot(material_id)
    calls = len(ai.calls)
    ai.fails()

    resp = regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION)

    assert resp.status_code == 502
    assert resp.json() == AI_SILENT
    assert len(ai.calls) == calls + 1
    assert snapshot(material_id) == before


# -- гонка: блок поправили, пока модель думала -------------------------------


@pytest.mark.parametrize("meanwhile", ["edit", "restore"])
def test_late_answer_does_not_overwrite_a_fresh_change(
    client, client2, sms, ai, scene, monkeypatch, meanwhile
):
    """Учитель открыл план во второй вкладке и поправил (или откатил) тот же
    блок, пока модель думала. Запоздалый ответ модели свежую правку не
    перетирает: отказ `block_changed`, и в истории нет его следа."""
    material_id = ready(client, sms, ai, scene)
    login(client2, sms)
    generated_id = versions(client, material_id, "stage_middle")[0]["id"]
    ai.returns_json({"stage_middle": NEW_MIDDLE})
    second_tab = []
    real = FakeAi.complete_json

    def thinking(self, **kw):
        reply = real(self, **kw)
        # Вторая вкладка успевает раньше, чем ответ модели дошёл до записи
        if meanwhile == "edit":
            second_tab.append(edit(client2, material_id, "stage_middle", TEACHER_MIDDLE))
        else:
            second_tab.append(restore(client2, material_id, "stage_middle", generated_id))
        return reply

    monkeypatch.setattr(FakeAi, "complete_json", thinking)
    before = snapshot(material_id)

    resp = regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION)

    [tab] = second_tab
    assert tab.status_code == 200, tab.text
    assert resp.status_code == 409
    assert resp.json() == BLOCK_CHANGED
    after = snapshot(material_id)
    expected = TEACHER_MIDDLE if meanwhile == "edit" else DRAFT["stage_middle"]
    source = "edited" if meanwhile == "edit" else "restored"
    assert after["content"] == {**ORIGINAL, "stage_middle": expected}
    assert_invariant(after)
    assert added_versions(before, after) == [("stage_middle", source, None, expected)]


def test_edit_of_another_block_meanwhile_is_kept(client, client2, sms, ai, scene, monkeypatch):
    """Пока модель переписывала середину урока, во второй вкладке поправили
    соседний блок. Это не конфликт: перегенерация проходит, и правка соседа
    не теряется — документ пишется под замком материала поверх свежего,
    а не того, что было прочитано до вызова модели."""
    material_id = ready(client, sms, ai, scene)
    login(client2, sms)
    ai.returns_json({"stage_middle": NEW_MIDDLE})
    real = FakeAi.complete_json
    second_tab = []

    def thinking(self, **kw):
        reply = real(self, **kw)
        second_tab.append(edit(client2, material_id, "values", "Дружба при работе в парах."))
        return reply

    monkeypatch.setattr(FakeAi, "complete_json", thinking)

    resp = regenerate(client, material_id, "stage_middle")

    [tab] = second_tab
    assert tab.status_code == 200, tab.text
    assert resp.status_code == 200, resp.text
    after = snapshot(material_id)
    assert after["content"] == {
        **ORIGINAL,
        "stage_middle": NEW_MIDDLE,
        "values": "Дружба при работе в парах.",
    }
    assert_invariant(after)


# -- PUT …/blocks/{block} ---------------------------------------------------


def test_edit_string_block_trims_edges(client, sms, ai, scene):
    """Учитель правит блок руками: краевые пробелы и перевод строки из поля
    ввода срезаются — так же, как у ответа модели. Версия — `edited`, без
    просьбы, соседние блоки не тронуты."""
    material_id = ready(client, sms, ai, scene)
    before = snapshot(material_id)
    calls = len(ai.calls)

    resp = edit(client, material_id, "values", "  Дружба и взаимопомощь при работе в парах.\n ")

    assert resp.status_code == 200, resp.text
    value = "Дружба и взаимопомощь при работе в парах."
    body = resp.json()
    assert body == {
        "kind": "values",
        "order_index": 5,
        "editable": True,
        "content": value,
        "updated_at": body["updated_at"],
    }
    assert len(ai.calls) == calls
    after = snapshot(material_id)
    assert after["content"] == {**ORIGINAL, "values": value}
    untouched = {kind: row for kind, row in before["blocks"].items() if kind != "values"}
    assert {kind: after["blocks"][kind] for kind in untouched} == untouched
    assert_invariant(after)
    assert added_versions(before, after) == [("values", "edited", None, value)]


def test_edit_step_list_block(client, sms, ai, scene):
    """Блок хода урока — список шагов; пустой список ресурсов законен,
    а пробелы по краям строк внутри шагов срезаются так же."""
    material_id = ready(client, sms, ai, scene)
    before = snapshot(material_id)
    padded = [
        {
            "teacher": " " + TEACHER_MIDDLE[0]["teacher"] + "  ",
            "students": TEACHER_MIDDLE[0]["students"] + "\n",
            "resources": [],
        }
    ]

    resp = edit(client, material_id, "stage_middle", padded)

    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == TEACHER_MIDDLE
    after = snapshot(material_id)
    assert after["content"] == {**ORIGINAL, "stage_middle": TEACHER_MIDDLE}
    assert_invariant(after)
    assert added_versions(before, after) == [("stage_middle", "edited", None, TEACHER_MIDDLE)]


@pytest.mark.parametrize(
    ("block", "content", "message"),
    [
        ("values", "   \n ", "Поле не может быть пустым"),
        ("reflection", [], "Нужен хотя бы один пункт"),
        (
            "stage_middle",
            [{"teacher": "Объясняет на примере 41 + 25.", "resources": []}],
            "Не хватает поля: students",
        ),
        (
            "stage_middle",
            [{**TEACHER_MIDDLE[0], "minutes": 10}],
            "Лишнее поле: minutes",
        ),
        ("stage_middle", "Игра по рядам.", "Блок не той формы"),
    ],
    ids=["blank-string", "empty-list", "missing-field", "extra-field", "wrong-shape"],
)
def test_edit_is_validated_in_russian(client, sms, ai, scene, block, content, message):
    """Кривая правка отбивается той же схемой, что ответ модели, и причиной
    по-русски: экран показывает её под блоком (решение 15). Поле ошибки —
    `content`, по имени поля запроса. В базу не попадает ничего."""
    material_id = ready(client, sms, ai, scene)
    before = snapshot(material_id)

    resp = edit(client, material_id, block, content)

    assert resp.status_code == 422
    assert resp.json() == invalid("content", message)
    assert snapshot(material_id) == before


# -- GET …/blocks/{block}/versions ------------------------------------------


def test_every_block_starts_with_one_generated_version(client, sms, ai, scene):
    """Первая версия пишется при генерации — поэтому откат «к тому, что
    сгенерировалось» обычный, а не особый случай. У блоков сервера история
    тоже читается: экран истории один на все блоки."""
    material_id = ready(client, sms, ai, scene)

    for kind in BLOCK_ORDER:
        [version] = versions(client, material_id, kind)
        assert version == {
            "id": version["id"],
            "source": "generated",
            "instruction": None,
            "content": ORIGINAL[kind],
            "created_at": version["created_at"],
            "current": True,
        }
        assert version["created_at"].endswith("Z")


def test_versions_are_newest_first_and_only_the_first_is_current(client, sms, ai, scene):
    """История свежими сверху, и текущая — всегда первая: правка пишет новую
    версию, а не переставляет указатель."""
    material_id = ready(client, sms, ai, scene)
    ai.returns_json({"stage_middle": NEW_MIDDLE})
    assert (
        regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION).status_code == 200
    )
    assert edit(client, material_id, "stage_middle", TEACHER_MIDDLE).status_code == 200

    resp = client.get(url(material_id, "stage_middle", "versions"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["block"] == "stage_middle"
    items = body["items"]
    assert [
        (item["source"], item["instruction"], item["content"], item["current"]) for item in items
    ] == [
        ("edited", None, TEACHER_MIDDLE, True),
        ("regenerated", INSTRUCTION, NEW_MIDDLE, False),
        ("generated", None, DRAFT["stage_middle"], False),
    ]
    ids = [item["id"] for item in items]
    assert ids == sorted(ids, reverse=True)
    # У соседнего блока история своя и не выросла
    assert len(versions(client, material_id, "stage_end")) == 1


# -- POST …/versions/{version_id}/restore -----------------------------------


def test_restore_brings_back_generated_and_can_be_undone(client, sms, ai, scene):
    """Откат к сгенерированному после перегенерации и ручной правки возвращает
    документ ровно к исходному. Откат — новая версия `restored`, прежние
    на месте, поэтому откатить можно и сам откат."""
    material_id = ready(client, sms, ai, scene)
    ai.returns_json({"stage_middle": NEW_MIDDLE})
    assert (
        regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION).status_code == 200
    )
    assert edit(client, material_id, "stage_middle", TEACHER_MIDDLE).status_code == 200
    history = versions(client, material_id, "stage_middle")
    edited_id, _regenerated_id, generated_id = (item["id"] for item in history)
    before = snapshot(material_id)

    resp = restore(client, material_id, "stage_middle", generated_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "kind": "stage_middle",
        "order_index": MIDDLE_INDEX,
        "editable": True,
        "content": DRAFT["stage_middle"],
        "updated_at": body["updated_at"],
    }
    after = snapshot(material_id)
    assert after["content"] == ORIGINAL
    assert_invariant(after)
    assert added_versions(before, after) == [
        ("stage_middle", "restored", None, DRAFT["stage_middle"])
    ]
    restored = versions(client, material_id, "stage_middle")
    assert [item["source"] for item in restored] == [
        "restored",
        "edited",
        "regenerated",
        "generated",
    ]
    assert [item["current"] for item in restored] == [True, False, False, False]
    assert restored[1:] == [{**item, "current": False} for item in history]

    # Откат отката — к ручной правке учителя
    undo = restore(client, material_id, "stage_middle", edited_id)

    assert undo.status_code == 200, undo.text
    assert undo.json()["content"] == TEACHER_MIDDLE
    final = snapshot(material_id)
    assert final["content"] == {**ORIGINAL, "stage_middle": TEACHER_MIDDLE}
    assert_invariant(final)
    assert [item["source"] for item in versions(client, material_id, "stage_middle")] == [
        "restored",
        "restored",
        "edited",
        "regenerated",
        "generated",
    ]


def test_restore_to_a_version_of_another_block_is_404(client, sms, ai, scene):
    """Номер версии приходит от клиента: версия соседнего блока в этот блок
    не откатывается — иначе в середину урока встала бы рефлексия."""
    material_id = ready(client, sms, ai, scene)
    [foreign] = versions(client, material_id, "reflection")
    before = snapshot(material_id)

    wrong = restore(client, material_id, "stage_middle", foreign["id"])
    unknown = restore(client, material_id, "stage_middle", foreign["id"] + 1000)

    assert (wrong.status_code, unknown.status_code) == (404, 404)
    assert wrong.json() == VERSION_NOT_FOUND
    assert unknown.json() == VERSION_NOT_FOUND
    assert snapshot(material_id) == before


def test_unknown_block_is_404_everywhere(client, sms, ai, scene):
    """Блок — по имени в пути, и имя приходит от клиента: чужое имя —
    внятный отказ, а не падение на словаре схем."""
    material_id = ready(client, sms, ai, scene)
    [version] = versions(client, material_id, "stage_middle")
    before = snapshot(material_id)
    calls = len(ai.calls)

    answers = [
        regenerate(client, material_id, "homework", instruction=INSTRUCTION),
        edit(client, material_id, "homework", "Решить № 5."),
        client.get(url(material_id, "homework", "versions")),
        restore(client, material_id, "homework", version["id"]),
    ]

    for resp in answers:
        assert resp.status_code == 404, resp.request.url
        assert resp.json() == BLOCK_NOT_FOUND
    assert len(ai.calls) == calls
    assert snapshot(material_id) == before


# -- блоки сервера ------------------------------------------------------------


@pytest.mark.parametrize("block", ["section", "learning_objectives"])
def test_server_blocks_are_locked(client, sms, ai, scene, block):
    """Раздел программы и цель обучения с кодом — строка приказа, её сверяет
    завуч. Ни модель, ни учитель, ни откат её не меняют, и модель по такой
    просьбе даже не зовут. История при этом читается."""
    material_id = ready(client, sms, ai, scene)
    [version] = versions(client, material_id, block)
    before = snapshot(material_id)
    calls = len(ai.calls)
    ai.returns_json({block: ORIGINAL[block]})

    answers = [
        regenerate(client, material_id, block, instruction="поменяй код на 2.1.2.7"),
        edit(client, material_id, block, ORIGINAL[block]),
        restore(client, material_id, block, version["id"]),
    ]

    for resp in answers:
        assert resp.status_code == 409, resp.request.url
        assert resp.json() == BLOCK_LOCKED
    assert len(ai.calls) == calls
    assert snapshot(material_id) == before
    assert len(versions(client, material_id, block)) == 1


# -- права и состояния --------------------------------------------------------


def all_routes(client, material_id: int, version_id: int = 1) -> list:
    """Все ручки правки по блокам — одним списком ответов."""
    return [
        client.get(url(material_id)),
        regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION),
        edit(client, material_id, "stage_middle", TEACHER_MIDDLE),
        client.get(url(material_id, "stage_middle", "versions")),
        restore(client, material_id, "stage_middle", version_id),
    ]


def test_foreign_and_unknown_material_is_404(client, client2, sms, ai, scene):
    """Чужой материал — 404, а не 403, и тот же, что у несуществующего:
    по номерам не перебрать, чьи планы существуют. Модель не зовут,
    записано ничего не должно быть."""
    material_id = ready(client, sms, ai, scene)
    [version] = versions(client, material_id, "stage_middle")
    before = snapshot(material_id)
    calls = len(ai.calls)
    ai.returns_json({"stage_middle": NEW_MIDDLE})
    login(client2, sms, phone=OTHER_PHONE)

    for resp in all_routes(client2, material_id, version["id"]) + all_routes(
        client, material_id + 1000, version["id"]
    ):
        assert resp.status_code == 404, resp.request.url
        assert resp.json() == MATERIAL_NOT_FOUND
    assert len(ai.calls) == calls
    assert snapshot(material_id) == before


def test_material_in_queue_is_not_ready(client, sms, ai, scene):
    """Пока план собирается, править нечего: правка посреди генерации была
    бы перетёрта её результатом. Отказ, а не ожидание."""
    login(client, sms)
    [material] = order(client, scene.objective.id).json()["materials"]
    ai.returns_json({"stage_middle": NEW_MIDDLE})

    for resp in all_routes(client, material["id"]):
        assert resp.status_code == 409, resp.request.url
        assert resp.json() == MATERIAL_NOT_READY
    assert ai.calls == []


def test_unpublished_program_closes_regeneration(client, sms, ai, scene):
    """Программу сняли с публикации после генерации: переписывать блок
    моделью по ней нельзя, как и собирать новый план. Отказ — до вызова
    модели, то есть без денег."""
    material_id = ready(client, sms, ai, scene)
    set_program_status(scene.program.id, "loaded")
    before = snapshot(material_id)
    calls = len(ai.calls)
    ai.returns_json({"stage_middle": NEW_MIDDLE})

    resp = regenerate(client, material_id, "stage_middle", instruction=INSTRUCTION)

    assert resp.status_code == 409
    assert resp.json() == PROGRAM_NOT_PUBLISHED
    assert len(ai.calls) == calls
    assert snapshot(material_id) == before


def test_block_routes_require_login(client, client2, sms, ai, scene):
    """Без входа — 401 на каждой ручке: перегенерация стоит денег, а история
    блока — текст учителя."""
    material_id = ready(client, sms, ai, scene)
    before = snapshot(material_id)
    calls = len(ai.calls)

    for resp in all_routes(client2, material_id):
        assert resp.status_code == 401, resp.request.url
        assert resp.json()["error"]["code"] == "unauthorized"
    assert len(ai.calls) == calls
    assert snapshot(material_id) == before
