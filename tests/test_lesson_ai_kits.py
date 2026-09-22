"""«Жоспар»: заказ комплекта к уроку, опрос его и граница между справочником
и моделью.

Ручек две — `POST /lesson_ai/kits` и `GET /lesson_ai/kits/{id}`, — и их формы
сверяются целиком с `CONTRACT.md`, раздел «Жоспар: комплект к уроку»: экран
ожидания пишется от этих примеров.

Главное в файле — цель обучения. Её код модели не дают вовсе, а в документ
его кладёт сервер из справочника дословно: сочинённый моделью код — главный
способ провалить проверку у завуча (TEACHER_AI_BRIEF, раздел 4.1). Поэтому
проверяется не чтение кода генератора, а то, что реально ушло подставному ИИ.

Фоновых воркеров в тестах нет (`LESSON_AI_WORKERS=0`): задания берутся руками
через `JobRunner.run_next()`, генераторы — настоящие, ИИ — подставной.
"""

import json

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as OrmSession

from app.adapters.db.base import get_engine
from app.adapters.db.models import (
    CurriculumProgram,
    LearningObjective,
    LessonAiJob,
    LessonKit,
    LessonMaterial,
)
from app.application.lesson_ai_queue import JobRunner
from app.config import get_settings
from app.domain.errors import ConflictError
from app.domain.lesson_ai import objective_codes_in
from app.lesson_ai.kinds import generators
from tests.conftest import login
from tests.test_admin_curriculum import (
    import_program,
    make_objective,
    make_program,
    make_section,
    make_subject,
)
from tests.test_curriculum_import import make_parsed

# Доступ к «Жоспару» (L7) в этих тестах открыт: они проверяют продукт,
# а сама проверка доступа — в `test_lesson_ai_access.py`
pytestmark = pytest.mark.usefixtures("lesson_ai_open")

OTHER_PHONE = "+7 (707) 555-01-02"

CODE = "2.1.2.6"
OBJECTIVE_TEXT = (
    "выполнять устно сложение и вычитание двузначных чисел без перехода через десяток"
    " в таких случаях, как 40+17, 57-40, 57-17, 35±12"
)
TOPIC = "Сложение и вычитание двузначных чисел"
NOTES = "в прошлый раз путали разряды"

# Черновик ҚМЖ от модели — все тринадцать блоков, которые она пишет; раздела
# и целей обучения в нём нет, их кладёт сервер. Сокращённый живой ответ gpt-4.1.
DRAFT = {
    "lesson_goals": [
        "Учащиеся научатся устно складывать двузначные числа без перехода через десяток."
    ],
    "assessment_criteria": ["Учащийся верно складывает и вычитает двузначные числа."],
    "language_goals": {
        "vocabulary": ["десятки", "единицы", "разряд"],
        "phrases": ["В числе 46 — 4 десятка и 6 единиц."],
    },
    "values": "Взаимоуважение при работе в парах.",
    "cross_curricular_links": "Русский язык: объяснение хода вычислений.",
    "prior_knowledge": "Сложение и вычитание однозначных чисел, понятие разряда.",
    "stage_beginning": [
        {
            "teacher": "Пишет на доске 34, 52, 70 и спрашивает, где десятки и где единицы.",
            "students": "Называют десятки и единицы.",
            "resources": ["доска"],
        }
    ],
    "stage_middle": [
        {
            "teacher": "Разбирает 41 + 25: десятки 4 + 2 = 6, единицы 1 + 5 = 6, вместе 66.",
            "students": "Проговаривают разрядный состав вместе с учителем.",
            "resources": ["доска", "счётные палочки"],
        }
    ],
    "stage_end": [
        {
            "teacher": "Самооценка цветными фишками. Домашнее задание: 36 + 23, 81 – 40.",
            "students": "Показывают фишку, записывают задание.",
            "resources": [],
        }
    ],
    "differentiation": "Тем, кому трудно, — счётные палочки; сильным — объяснить соседу.",
    "assessment": "Уточняющие вопросы в парах, объяснение шагов решения.",
    "health_and_safety": "Осанка при письме, физкультминутка.",
    "reflection": ["Все ли ученики научились объяснять ход решения?"],
}

KIT_NOT_FOUND = {"error": {"code": "not_found", "message": "Комплект не найден"}}
LESSON_AI_OFF = {"error": {"code": "lesson_ai_off", "message": "Помощник учителя недоступен"}}


class Scene:
    """Кусок справочника из контракта: математика, раздел «Числа и величины»,
    подраздел «Операции над числами», одна цель второго класса.

    Программа по умолчанию опубликована — генерация видит только такие.
    """

    def __init__(self, *, lang="ru", status="published", **objective):
        self.subject = make_subject()
        self.program = make_program(self.subject.id, lang=lang, status=status)
        self.section = make_section(self.program.id, "1", "Числа и величины")
        self.subsection = make_section(
            self.program.id, "1.2", "Операции над числами", parent_id=self.section.id
        )
        fields = {"text": OBJECTIVE_TEXT}
        fields.update(objective)
        self.objective = make_objective(
            self.program.id, self.subsection.id, fields.pop("code", CODE), **fields
        )


@pytest.fixture
def scene() -> Scene:
    return Scene()


def order(client, objective_id: int, **kw):
    body = {"objective_id": objective_id, "topic": TOPIC, "kinds": ["lesson_plan"]}
    body.update(kw)
    return client.post("/lesson_ai/kits", json=body)


def run_next(ai) -> bool:
    """Одно задание очереди — так же, как его берёт воркер, но руками."""
    runner = JobRunner(
        lambda: OrmSession(get_engine()), generators(ai, "gpt-test"), lease_sec=600
    )
    return runner.run_next()


def hurry_retries() -> None:
    """Пауза между попытками — 30 с и 2 мин по часам базы: тест их не ждёт,
    а сдвигает срок в прошлое теми же часами."""
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE lesson_ai_job SET run_after = now() - interval '1 second'"
                " WHERE status = 'queued'"
            )
        )


def set_program_status(program_id: int, status: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE curriculum_program SET status = :status WHERE id = :id"),
            {"status": status, "id": program_id},
        )


def count(model) -> int:
    with OrmSession(get_engine()) as db:
        return db.scalar(select(func.count()).select_from(model))


def invalid(field: str, message: str) -> dict:
    return {
        "error": {
            "code": "validation_error",
            "message": "Проверьте заполнение полей",
            "details": {"fields": [{"field": field, "message": message}]},
        }
    }


def sent_message(ai) -> str:
    """Текст, который модель получила от сервера, — все сообщения запроса."""
    return "\n".join(message.content for message in ai.calls[-1]["messages"])


def generated(client, sms, ai, scene: Scene, draft=None, **kw) -> dict:
    """Заказ и одна успешная генерация: комплект глазами учителя после неё."""
    login(client, sms)
    kit_id = order(client, scene.objective.id, **kw).json()["id"]
    ai.returns_json(draft or DRAFT)
    assert run_next(ai)
    return client.get(f"/lesson_ai/kits/{kit_id}").json()


# -- POST /lesson_ai/kits -------------------------------------------------


def test_order_answers_with_the_contract_shape(client, sms, scene):
    """Экран ожидания рисуется прямо из ответа на заказ: комплект собирается,
    материал в очереди, а цель — ровно та строка справочника, которую учитель
    подтвердил, с кодом и пометкой."""
    login(client, sms)

    resp = order(client, scene.objective.id, class_size=24, notes=NOTES)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    [material] = body["materials"]
    assert body == {
        "id": body["id"],
        "objective": {
            "id": scene.objective.id,
            "code": CODE,
            "text": OBJECTIVE_TEXT,
            "marker": "",
        },
        "subject_code": "math",
        "grade": 2,
        "lang": "ru",
        "topic": TOPIC,
        "class_size": 24,
        "notes": NOTES,
        # Уровни не присланы — середина по умолчанию (L5, раздел 4.3 брифа)
        "engagement_level": "active",
        "difficulty_level": "standard",
        "status": "generating",
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
        "materials": [
            {
                "id": material["id"],
                "kind": "lesson_plan",
                "status": "queued",
                "error": None,
                "content": None,
                "updated_at": material["updated_at"],
            }
        ],
    }
    # Метки времени — UTC с `Z`, как в примере контракта; всё заведено
    # одной транзакцией, поэтому метки совпадают
    assert body["created_at"].endswith("Z")
    assert body["created_at"] == body["updated_at"] == material["updated_at"]

    # Ответ — не пересказ: комплект, материал и задание на него лежат в базе
    with OrmSession(get_engine()) as db:
        kit = db.get(LessonKit, body["id"])
        assert (kit.grade, kit.lang, kit.status) == (2, "ru", "generating")
        assert db.get(LessonMaterial, material["id"]).kit_id == kit.id
        jobs = db.scalars(select(LessonAiJob)).all()
        assert [(job.kit_id, job.kind, job.status) for job in jobs] == [
            (kit.id, "lesson_plan", "queued")
        ]


def test_kazakh_objective_makes_a_kazakh_kit(client, sms):
    """Язык документов — язык обучения, и его знает цель: она взята из
    казахской редакции приказа. Языка в заказе нет, и прислать казахский
    класс с русской целью нечем (L3, решение 6)."""
    kz = Scene(
        lang="kz",
        code="3.1.2.4",
        text="екі таңбалы сандарды ондықтан аттамай ауызша қосу және азайту",
    )
    login(client, sms)

    resp = order(client, kz.objective.id)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["lang"], body["grade"]) == ("kz", 3)
    assert body["objective"]["code"] == "3.1.2.4"


def test_objective_of_an_unpublished_program_is_refused(client, sms):
    """Загруженная программа ещё не вычитана владельцем, и план по ней завуч
    может не принять. Отказ — до того, как что-нибудь заведено: иначе
    в очереди остался бы материал, который никогда не соберётся."""
    scene = Scene(status="loaded")
    login(client, sms)

    resp = order(client, scene.objective.id)

    assert resp.status_code == 409
    assert resp.json() == {
        "error": {
            "code": "program_not_published",
            "message": "Программа ещё не опубликована — готовить материалы по ней нельзя",
        }
    }
    assert (count(LessonKit), count(LessonMaterial), count(LessonAiJob)) == (0, 0, 0)


def test_unknown_objective_is_404(client, sms, scene):
    """Цель выбирается из справочника, но номер приходит от клиента —
    несуществующий получает внятный отказ, а не падение на внешнем ключе."""
    login(client, sms)

    resp = order(client, scene.objective.id + 1000)

    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "Цель обучения не найдена"}}
    assert count(LessonKit) == 0


def test_contract_validation_errors(client, sms, scene):
    """Два отказа из контракта — дословно: вид, которого нет среди семи,
    и тема из одних пробелов. Экран подсвечивает поле по имени."""
    login(client, sms)

    unknown = order(client, scene.objective.id, kinds=["quiz"])
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"]["fields"][0]["field"] == "kinds.0"

    blank = order(client, scene.objective.id, topic="   \t ")
    assert blank.status_code == 422
    assert blank.json() == invalid("topic", "Тема урока не может быть пустой")
    assert count(LessonKit) == 0


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"kinds": []}, "kinds"),
        ({"topic": ""}, "topic"),
        ({"class_size": 0}, "class_size"),
        ({"class_size": 61}, "class_size"),
        ({"notes": "я" * 1001}, "notes"),
    ],
    ids=["no-kinds", "empty-topic", "class-size-0", "class-size-61", "long-notes"],
)
def test_order_field_is_validated(client, sms, scene, patch, field):
    """Кривой заказ отбивается до базы: заведённый по нему материал стоил бы
    вызова модели, а учитель увидел бы ошибку уже после ожидания."""
    login(client, sms)

    resp = order(client, scene.objective.id, **patch)

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert [item["field"] for item in error["details"]["fields"]] == [field]
    assert count(LessonKit) == 0


def test_repeated_kind_makes_one_material(client, sms, scene):
    """Повтор вида в заказе — двойной клик по галочке, а не просьба о двух
    планах: второй материал того же вида стоил бы второго вызова модели."""
    login(client, sms)

    resp = order(client, scene.objective.id, kinds=["lesson_plan", "lesson_plan"])

    assert resp.status_code == 201, resp.text
    assert [material["kind"] for material in resp.json()["materials"]] == ["lesson_plan"]
    assert (count(LessonMaterial), count(LessonAiJob)) == (1, 1)


# -- доступ ---------------------------------------------------------------


def test_kits_require_login(client, client2, sms, scene):
    """Генерация стоит денег: без входа не заказать и не подсмотреть чужой
    комплект по номеру."""
    login(client2, sms, phone=OTHER_PHONE)
    kit_id = order(client2, scene.objective.id).json()["id"]

    post = order(client, scene.objective.id)
    get = client.get(f"/lesson_ai/kits/{kit_id}")

    assert (post.status_code, get.status_code) == (401, 401)
    assert post.json()["error"]["code"] == "unauthorized"
    assert get.json()["error"]["code"] == "unauthorized"
    assert count(LessonKit) == 1


@pytest.mark.parametrize(
    ("setting", "value"),
    [("lesson_ai_enabled", False), ("openai_api_key", "")],
    ids=["switched-off", "no-key"],
)
def test_switched_off_product_answers_lesson_ai_off(
    client, sms, scene, monkeypatch, setting, value
):
    """До проверки доступа (L7) продукт закрыт выключателем, иначе любой
    вошедший учитель тратил бы деньги на генерацию. Без ключа он так же мёртв.
    Фронт по коду прячет вход в продукт — поэтому код один на обе ручки."""
    login(client, sms)
    kit_id = order(client, scene.objective.id).json()["id"]

    # Настройки кэшированы — патчится сам объект, monkeypatch вернёт как было
    monkeypatch.setattr(get_settings(), setting, value)
    post = order(client, scene.objective.id)
    get = client.get(f"/lesson_ai/kits/{kit_id}")

    assert (post.status_code, get.status_code) == (503, 503)
    assert post.json() == LESSON_AI_OFF
    assert get.json() == LESSON_AI_OFF
    assert count(LessonKit) == 1


# -- GET /lesson_ai/kits/{id} ---------------------------------------------


def test_own_kit_is_polled_in_the_same_shape(client, sms, scene):
    """Экран ожидания перерисовывается одним и тем же ответом: опрос
    до генерации — слово в слово ответ на заказ."""
    login(client, sms)
    created = order(client, scene.objective.id, class_size=24, notes=NOTES).json()

    resp = client.get(f"/lesson_ai/kits/{created['id']}")

    assert resp.status_code == 200
    assert resp.json() == created


def test_foreign_and_unknown_kits_are_404(client, client2, sms, scene):
    """Чужой комплект — 404, а не 403: по номерам нельзя перебрать, чьи
    комплекты существуют. И отказ тот же, что у несуществующего."""
    login(client, sms)
    kit_id = order(client, scene.objective.id).json()["id"]
    login(client2, sms, phone=OTHER_PHONE)

    foreign = client2.get(f"/lesson_ai/kits/{kit_id}")
    unknown = client.get(f"/lesson_ai/kits/{kit_id + 1000}")

    assert (foreign.status_code, unknown.status_code) == (404, 404)
    assert foreign.json() == KIT_NOT_FOUND
    assert unknown.json() == KIT_NOT_FOUND


def test_generated_kit_is_ready(client, sms, ai, scene):
    """Очередь собрала план — опрос показывает готовый документ, и комплект
    перестаёт «собираться»: по этому экран ожидания и останавливается."""
    body = generated(client, sms, ai, scene)

    assert body["status"] == "ready"
    [material] = body["materials"]
    assert (material["status"], material["error"]) == ("ready", None)
    assert material["content"] == {
        "section": {"title": "Числа и величины", "subsection": "Операции над числами"},
        "learning_objectives": [{"code": CODE, "text": OBJECTIVE_TEXT, "marker": ""}],
        **DRAFT,
    }


def test_provider_failures_end_in_a_failed_kit(client, sms, ai, scene):
    """Провайдер не ответил трижды — материал упал с внятной причиной.
    Пока впереди повтор, отказ ещё не приговор, и учитель его не видит."""
    login(client, sms)
    kit_id = order(client, scene.objective.id).json()["id"]
    ai.fails().fails().fails()

    assert run_next(ai)
    waiting = client.get(f"/lesson_ai/kits/{kit_id}").json()
    assert waiting["status"] == "generating"
    assert [(m["status"], m["error"]) for m in waiting["materials"]] == [("queued", None)]

    hurry_retries()
    assert run_next(ai)
    hurry_retries()
    assert run_next(ai)

    body = client.get(f"/lesson_ai/kits/{kit_id}").json()
    assert body["status"] == "failed"
    [material] = body["materials"]
    assert material == {
        "id": material["id"],
        "kind": "lesson_plan",
        "status": "failed",
        "error": {"code": "ai_failed", "message": "Модель не ответила"},
        "content": None,
        "updated_at": material["updated_at"],
    }
    assert len(ai.calls) == 3


# -- цель обучения: её кладёт сервер, модель её кода не видит --------------


def test_learning_objectives_come_from_the_dictionary_verbatim(client, sms, ai):
    """Цель в документе — строка справочника дословно, с кодом и пометкой
    приказа, что бы модель ни написала в своих блоках: завуч сверяет план
    именно с этой строкой."""
    scene = Scene(marker="**")
    # Модель увела цели урока в сторону — её текст остаётся её текстом,
    # а цель обучения от этого не меняется
    draft = {**DRAFT, "lesson_goals": ["Учащиеся научатся умножать на 2 и на 3."]}

    body = generated(client, sms, ai, scene, draft=draft)

    content = body["materials"][0]["content"]
    assert content["learning_objectives"] == [
        {"code": CODE, "text": OBJECTIVE_TEXT, "marker": "**"}
    ]
    assert content["section"] == {
        "title": "Числа и величины",
        "subsection": "Операции над числами",
    }
    assert content["lesson_goals"] == draft["lesson_goals"]
    assert body["objective"]["marker"] == "**"


def test_model_never_sees_the_objective_code(client, sms, ai, scene):
    """Код цели модели не дают вовсе — ни в системном промпте, ни в сообщении,
    ни в схеме ответа: списать неоткуда, а сочинённый код ловит проверка
    ответа. Проверяется то, что реально ушло провайдеру."""
    generated(client, sms, ai, scene, class_size=24, notes=NOTES)

    call = ai.calls[-1]
    message = sent_message(ai)
    schema = json.dumps(call["schema"], ensure_ascii=False)
    assert call["name"] == "lesson_plan"
    # Сообщение не пустое: цель в нём есть — текстом, без кода
    assert OBJECTIVE_TEXT in message
    for part in (call["system"], message, schema):
        assert CODE not in part
    assert objective_codes_in([call["system"], message, call["schema"]]) == []


def test_codes_are_cut_from_topic_notes_and_objective_text(client, sms, ai):
    """Учитель мог вписать код в тему или пожелания руками, а в формулировке
    цели он мог остаться от склейки ячеек приказа. До модели не доходит ни
    один — а в ответе ручки и в документе цель остаётся строкой справочника,
    с кодом внутри: это не пересказ, а то, что подтвердил учитель."""
    glued = "выполнять устно сложение 3.1.2.4 и вычитание двузначных чисел"
    scene = Scene(text=glued)
    login(client, sms)

    created = order(
        client,
        scene.objective.id,
        topic="2.1.2.6 Сложение двузначных чисел",
        notes="повторить 2.1.2.5, путали разряды",
    ).json()
    ai.returns_json(DRAFT)
    assert run_next(ai)

    message = sent_message(ai)
    for code in ("2.1.2.6", "2.1.2.5", "3.1.2.4"):
        assert code not in message
    lines = message.splitlines()
    assert "Тема урока: Сложение двузначных чисел" in lines
    assert "Пожелания учителя: повторить, путали разряды" in lines
    assert (
        "Цель обучения по программе: выполнять устно сложение и вычитание двузначных чисел"
        in lines
    )

    assert created["objective"]["text"] == glued
    # Тему учитель написал сам, и она хранится как написана
    assert created["topic"] == "2.1.2.6 Сложение двузначных чисел"
    body = client.get(f"/lesson_ai/kits/{created['id']}").json()
    assert body["objective"]["text"] == glued
    content = body["materials"][0]["content"]
    assert content["learning_objectives"][0]["text"] == glued


@pytest.mark.parametrize(
    ("lang", "language", "tail", "other"),
    [
        ("ru", "русский", "на русском языке", "казахск"),
        ("kz", "казахский", "на казахском языке", "русск"),
    ],
)
def test_message_names_the_language_of_instruction(client, sms, ai, lang, language, tail, other):
    """Язык плана — язык обучения, а не интерфейса: учитель с русским
    интерфейсом ведёт казахский класс. Системный промпт у языков общий
    (ради кэша провайдера), поэтому язык обязан быть в сообщении."""
    scene = Scene(lang=lang)

    generated(client, sms, ai, scene)

    message = sent_message(ai)
    assert f"Язык обучения: {language}" in message.splitlines()
    assert tail in message
    assert other not in message


def test_message_carries_the_lesson_and_optional_lines_only_when_set(client, sms, ai, scene):
    """Модель пишет план по сообщению: класс, раздел и цель в нём есть
    всегда. Необязательное — только если задано: пустую строку «Пожелания
    учителя:» модель читает как пожелание."""
    login(client, sms)
    bare = order(client, scene.objective.id).json()["id"]
    full = order(client, scene.objective.id, class_size=24, notes=NOTES).json()["id"]
    ai.returns_json(DRAFT).returns_json(DRAFT)

    assert run_next(ai)
    bare_lines = sent_message(ai).splitlines()
    assert run_next(ai)
    full_lines = sent_message(ai).splitlines()

    for lines in (bare_lines, full_lines):
        assert "Предмет: Математика" in lines
        assert "Класс: 2" in lines
        assert "Раздел программы: Числа и величины" in lines
        assert "Подраздел: Операции над числами" in lines
        assert f"Цель обучения по программе: {OBJECTIVE_TEXT}" in lines
    # Комплект без пожеланий идёт через банк (L7): его генерация обезличена,
    # и темы учителя модель не видит — тема есть только у личного
    assert f"Тема урока: {TOPIC}" in full_lines
    assert not [line for line in bare_lines if line.startswith(("Тема", "Детей", "Пожелания"))]
    assert "Детей в классе: 24" in full_lines
    assert f"Пожелания учителя: {NOTES}" in full_lines
    # Оба комплекта собраны — порядок очереди совпал с порядком заказа
    for kit_id in (bare, full):
        assert client.get(f"/lesson_ai/kits/{kit_id}").json()["status"] == "ready"


def test_unpublished_between_order_and_generation_fails_without_the_model(
    client, sms, ai, scene
):
    """Программу сняли с публикации, пока материал ждал очереди: генерация
    по ней закрыта так же, как для нового заказа. Повтор не поможет, поэтому
    отказ — с первой попытки и без вызова модели, то есть без денег."""
    login(client, sms)
    kit_id = order(client, scene.objective.id).json()["id"]
    set_program_status(scene.program.id, "loaded")

    assert run_next(ai)

    assert ai.calls == []
    body = client.get(f"/lesson_ai/kits/{kit_id}").json()
    assert body["status"] == "failed"
    [material] = body["materials"]
    assert material["status"] == "failed"
    assert material["content"] is None
    assert material["error"] == {
        "code": "program_not_published",
        "message": "Программа снята с публикации — генерация по ней закрыта",
    }
    with OrmSession(get_engine()) as db:
        job = db.scalars(select(LessonAiJob)).one()
        assert (job.status, job.attempts, job.error) == ("failed", 1, "program_not_published")
    # Повторной попытки нет — очередь пуста
    assert not run_next(ai)


# -- повторный импорт программы, по которой собраны комплекты ---------------


@pytest.mark.parametrize("lang", ["ru", "kz"])
def test_reimport_under_kits_is_refused(client, sms, lang):
    """Повторный импорт сносит программу вместе с целями, а комплекты
    учителей на них ссылаются. Снятая публикация этого не меняет, и никакой
    флаг не разрешает: документ не должен задним числом ссылаться на
    несуществующую цель (L3, решение 13). Отказ называет число комплектов.

    Казахская редакция идёт в импорте второй — русскую он к тому времени уже
    снёс в своей транзакции, и отказ обязан откатить и её."""
    first = import_program({"ru": make_parsed(), "kz": make_parsed()})
    program_id = first.program_ids[lang]
    set_program_status(program_id, "published")
    with OrmSession(get_engine()) as db:
        objective_id = db.scalar(
            select(LearningObjective.id).where(
                LearningObjective.program_id == program_id, LearningObjective.code == "1.1.1.1"
            )
        )
    login(client, sms)
    kit_ids = [order(client, objective_id, topic=topic).json()["id"] for topic in ("А", "Б")]
    set_program_status(program_id, "loaded")

    for force in (False, True):
        with pytest.raises(ConflictError) as refused:
            import_program({"ru": make_parsed(), "kz": make_parsed()}, force=force)
        assert "собрано 2 комплекта" in refused.value.message

    # Отказ ничего не снёс по дороге: обе редакции, цель и комплекты на месте
    with OrmSession(get_engine()) as db:
        for program in first.program_ids.values():
            assert db.get(CurriculumProgram, program) is not None
        assert db.get(LearningObjective, objective_id) is not None
    for kit_id in kit_ids:
        resp = client.get(f"/lesson_ai/kits/{kit_id}")
        assert resp.status_code == 200
        assert resp.json()["objective"]["code"] == "1.1.1.1"
