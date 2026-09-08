"""Черновики ответов: «Ответить с помощью ИИ» у вопроса и у отзыва.

Здесь проверяется то, что легко потерять: это не чат, контекст фиксирован
сервером, бренд площадки свой, правильные ответы теста модель видит, а строка
«из чего собрано» говорит вслух, чего в курсе не было.
"""

from app.config import get_settings
from app.domain.brands import brand
from tests.conftest import (
    login_admin,
    make_course,
    make_lesson,
    make_module,
    make_option,
    make_question,
    make_quiz,
    make_review,
    make_thread_message,
    make_user,
)


def scene(**course_kw):
    """Курс с одним модулем, уроком и тестом — общая сцена обоих черновиков."""
    course = make_course(title="Критериальное оценивание", **course_kw)
    module = make_module(course.id, title="Оценивание")
    lesson = make_lesson(
        module.id,
        title="Критерии и дескрипторы",
        kind="text",
        video_url=None,
        body={"blocks": [{"type": "p", "text": "Дескриптор описывает наблюдаемое действие."}]},
    )
    return course, module, lesson


def with_quiz(module):
    quiz = make_quiz(module.id, title="Проверка по модулю")
    question = make_question(quiz.id, text="Что описывает дескриптор?", explanation="См. урок 1.")
    make_option(question.id, text="Наблюдаемое действие", is_correct=True)
    make_option(question.id, text="Оценку в журнале", is_correct=False)
    return quiz


def user_text(ai) -> str:
    """Контекст, который сервер собрал и отдал модели одним сообщением."""
    return ai.calls[-1]["messages"][0].content


def test_question_draft_is_one_call_without_tools(client, sms, ai):
    """Это не чат: один вызов, без истории, без инструментов, без цикла."""
    login_admin(client, sms)
    course, module, lesson = scene()
    teacher = make_user("+77010000020")
    question = make_thread_message(lesson.id, course.id, teacher.id, text="Не понял дескрипторы")
    ai.says("Дескриптор — это наблюдаемое действие. Посмотрите урок «Критерии и дескрипторы».")

    resp = client.post(f"/admin/thread_messages/{question.id}/draft_reply")
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"].startswith("Дескриптор")
    assert len(ai.calls) == 1
    assert ai.calls[0]["tools"] is None
    assert len(ai.calls[0]["messages"]) == 1


def test_question_draft_carries_the_fixed_context(client, sms, ai):
    login_admin(client, sms)
    course, module, lesson = scene(short="Курс про критериальное оценивание")
    teacher = make_user("+77010000021")
    question = make_thread_message(
        lesson.id, course.id, teacher.id, text="Не понял, что писать в дескрипторе"
    )
    client.post(f"/admin/thread_messages/{question.id}/draft_reply")

    context = user_text(ai)
    assert "Критериальное оценивание" in context          # курс
    assert "Курс про критериальное" in context            # описание
    assert "Критерии и дескрипторы" in context            # урок в программе и целиком
    assert "наблюдаемое действие" in context.lower()      # текст урока
    assert "Не понял, что писать" in context              # ветка вопроса


def test_author_is_masked_in_the_context(client, sms, ai):
    """Персданных в контексте нет, поэтому и допуск кнопке не нужен."""
    login_admin(client, sms)
    course, module, lesson = scene()
    teacher = make_user("+77010000022", last_name="Сериккызы", first_name="Айгерим")
    question = make_thread_message(lesson.id, course.id, teacher.id, text="Вопрос по уроку")
    client.post(f"/admin/thread_messages/{question.id}/draft_reply")

    context = user_text(ai)
    assert f"Учитель #{teacher.id}" in context
    assert "Сериккызы" not in context
    assert "77010000022" not in context


def test_quiz_goes_into_the_context_only_when_the_question_is_about_it(client, sms, ai):
    """Тест с ответами — самая дорогая часть контекста: класть его в каждый
    вопрос про видео значит платить за него всегда."""
    login_admin(client, sms)
    course, module, lesson = scene()
    with_quiz(module)
    teacher = make_user("+77010000023")

    about_video = make_thread_message(lesson.id, course.id, teacher.id, text="Видео не грузится")
    client.post(f"/admin/thread_messages/{about_video.id}/draft_reply")
    assert "Тест этого модуля" not in user_text(ai)

    about_quiz = make_thread_message(
        lesson.id, course.id, teacher.id, text="Не набрал балл в тесте, почему?"
    )
    client.post(f"/admin/thread_messages/{about_quiz.id}/draft_reply")
    context = user_text(ai)
    assert "Тест этого модуля" in context
    # Правильные ответы и разъяснения модель видит
    assert "(верный) Наблюдаемое действие" in context
    assert "См. урок 1." in context
    # ...и в промпте ей прямо сказано их не переносить
    assert "в ответ учителю они не переносятся" in context
    assert "верный вариант не называй" in ai.calls[-1]["system"]


def test_built_from_says_what_was_missing(client, sms, ai):
    """Чего в курсе не хватало — сервер говорит вслух. Это не требование
    что-то заполнить: видно, на чём стоит текст."""
    login_admin(client, sms)
    course, module, lesson = scene()  # без описания курса
    teacher = make_user("+77010000024")
    question = make_thread_message(lesson.id, course.id, teacher.id, text="Вопрос")

    built = client.post(f"/admin/thread_messages/{question.id}/draft_reply").json()["built_from"]
    assert "программы курса" in built
    assert "у курса нет описания" in built.lower()


def test_answer_is_drafted_in_the_voice_of_its_own_platform(client, sms, ai):
    """Черновик пишется голосом своей площадки: безликий ответ от академии —
    тоже брак, а чужой бренд — брак вдвойне."""
    login_admin(client, sms)
    course, module, lesson = scene(platforms={"p1": 45000, "p2": 60000})
    teacher = make_user("+77010000025")
    question = make_thread_message(
        lesson.id, course.id, teacher.id, text="Вопрос", platform="p2"
    )
    client.post(f"/admin/thread_messages/{question.id}/draft_reply")

    system = ai.calls[-1]["system"]
    assert brand("p2").platform_name in system
    # Наружу второй площадки не существует — и первой в тексте для p2 тоже
    assert brand("p1").platform_name not in system


def test_review_draft_has_the_rating_and_the_text(client, sms, ai):
    login_admin(client, sms)
    course, module, lesson = scene(short="Курс про оценивание")
    teacher = make_user("+77010000026", last_name="Сериккызы")
    review = make_review(
        teacher.id, course.id, rating=2, text="Слишком много теории, мало примеров."
    )
    ai.says("Спасибо за отзыв — примеры добавим.")

    resp = client.post(f"/admin/reviews/{review.id}/draft_reply")
    assert resp.status_code == 200, resp.text
    context = user_text(ai)
    assert "Оценка: 2 из 5" in context
    assert "Слишком много теории" in context
    assert f"Учитель #{teacher.id}" in context
    assert "Сериккызы" not in context
    # Переписки под отзывом не бывает, и урока у него нет
    assert "Урок, к которому задан вопрос" not in context


def test_draft_changes_nothing(client, sms, ai):
    """Обе ручки только возвращают текст: отправляет админ той же кнопкой,
    что была там и раньше."""
    login_admin(client, sms)
    course, module, lesson = scene()
    teacher = make_user("+77010000027")
    review = make_review(teacher.id, course.id)
    question = make_thread_message(lesson.id, course.id, teacher.id, text="Вопрос")

    client.post(f"/admin/reviews/{review.id}/draft_reply")
    client.post(f"/admin/thread_messages/{question.id}/draft_reply")

    assert client.get("/admin/reviews").json()["items"][0]["reply"] is None
    assert client.get("/admin/questions").json()["items"][0]["replies"] == []


def test_draft_answers_the_root_of_the_branch(client, sms, ai):
    """Отвечают на вопрос, а не на ответ под ним: ветка одна, корень один."""
    login_admin(client, sms)
    course, module, lesson = scene()
    teacher = make_user("+77010000028")
    root = make_thread_message(lesson.id, course.id, teacher.id, text="Первый вопрос")
    reply = make_thread_message(
        lesson.id, course.id, teacher.id, text="И ещё уточнение", parent_id=root.id
    )
    client.post(f"/admin/thread_messages/{reply.id}/draft_reply")
    context = user_text(ai)
    assert "Первый вопрос" in context
    assert "И ещё уточнение" in context


def test_no_key_means_no_button(client, sms, ai, monkeypatch):
    """Пустой OPENAI_API_KEY — кнопки нет, ровно как нет и страницы ассистента."""
    login_admin(client, sms)
    course, module, lesson = scene()
    teacher = make_user("+77010000029")
    review = make_review(teacher.id, course.id)
    monkeypatch.setattr(get_settings(), "openai_api_key", "")

    resp = client.post(f"/admin/reviews/{review.id}/draft_reply")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "assistant_off"
    # А обычный ответ на отзыв руками работает как работал
    assert client.post(
        f"/admin/reviews/{review.id}/reply", json={"text": "Спасибо!"}
    ).status_code == 200


def test_drafts_share_the_chat_limit(client, sms, ai, monkeypatch):
    """Лимит тот же, что у чата: черновики и разговор тратят один счёт."""
    monkeypatch.setattr(get_settings(), "assistant_messages_per_hour", 60)
    login_admin(client, sms)
    course, module, lesson = scene()
    teacher = make_user("+77010000030")
    review = make_review(teacher.id, course.id)
    url = f"/admin/reviews/{review.id}/draft_reply"
    for _ in range(get_settings().assistant_messages_per_hour):
        assert client.post(url).status_code == 200
    assert client.post(url).status_code == 429


def test_teacher_cannot_draft(client, sms, ai):
    from tests.conftest import login

    login(client, sms)
    assert client.post("/admin/reviews/1/draft_reply").status_code == 403
