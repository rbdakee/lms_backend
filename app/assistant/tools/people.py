"""Учителя, заявки, доступы, работы на проверку — и допуск к персданным.

Самый чувствительный файл реестра: за этими инструментами лежат ФИО,
телефоны, школы и ИИН живых людей, а контекст ассистента целиком уходит
во внешнюю модель клиента. Отсюда четыре правила, которые здесь не обсуждаются
(`ASSISTANT_BRIEF`, раздел 5; `MCP_BRIEF`, решение 5):

1. **Списки маскированы всегда, без исключений.** `teachers_list`,
   `teachers_find`, `leads_list`, `submissions_list` и всё остальное, что
   отдаёт больше одного человека, показывает `masked_teacher(user)`:
   `user_id`, «Учитель #418», регион и школа. Ни ФИО, ни телефона, ни ИИН,
   ни почты — ни при каком аргументе.
2. **Человека видно ровно в одном месте** — `teacher_card`, и только после
   допуска подключения (флаг `pd_allowed`, его админ ставит галочкой на экране
   «Подключения ИИ»). Инструмент помечен `pd=True`.
3. **Массовой выгрузки персданных нет вовсе.** Карточка отдаётся по одному
   человеку, поэтому инструмента, отдающего список карточек, здесь нет
   ни под каким видом: «дай телефоны всех из Алматы» не выполняется
   ни с каким допуском (раздел 12).
4. **Допуск и пометка опасности — две разные вещи.** Допуск говорит
   «подключению можно видеть и править данные людей», пометка `confirm` —
   «клиент спросит человека перед вызовом». Правка данных и выдача доступа
   требуют и `require_pd`, и пометки `confirm`.

Отдельно про даты: результат вызова уходит в JSON, а `datetime` туда
не сериализуется вовсе — поэтому наружу они уходят строкой (`_at`).
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime

from sqlalchemy import select

from app.adapters.db.models import User
from app.adapters.db.repos import (
    AttemptRepo,
    CertificateRepo,
    CourseRepo,
    EnrollmentRepo,
    LeadRepo,
    NotificationRepo,
    QuizAdminRepo,
    SessionRepo,
    SubmissionRepo,
    TeacherAdminRepo,
    UserRepo,
)
from app.api import deps
from app.application.leads import LeadsService
from app.application.submissions_admin import SubmissionsAdminService
from app.application.teachers_admin import TeachersAdminService
from app.application.users import UsersService
from app.assistant.masking import masked_teacher, teacher_ref
from app.assistant.registry import Arg, ToolCtx, ToolError, platform_arg, preview, tool
from app.config import get_settings
from app.domain.platform import PLATFORMS

# Потолок строк в ответе, тот же, что у курсов: ассистенту нужен вывод,
# а не страница — за второй он сходит с более узким фильтром.
LIST_LIMIT = 50

# Карточка выбора человека — это несколько строк под вопросом «кто нужен?»,
# а не список: админ читает её глазами и кликает одну (раздел 5).
FIND_LIMIT = 10

LEAD_STATUSES = ["new", "contacted", "paid", "granted", "declined", "open"]
# `granted` руками не ставится: доступ заводит enrollment_grant, он же
# и закрывает заявку — статус без доступа означал бы учёбу, которой нет.
LEAD_EDIT_STATUSES = ["new", "contacted", "paid", "declined"]

SUBMISSION_STATUSES = ["pending", "accepted", "rework"]

# Поля карточки человека, которые админ правит, и как они называются
# в тексте предложения. Порядок — тот, в котором они стоят на экране:
# по нему собирается перечисление «фамилия, ИИН» над кнопкой.
EDITABLE_FIELDS = {
    "last_name": "фамилия",
    "first_name": "имя",
    "middle_name": "отчество",
    "iin": "ИИН",
    "phone": "телефон",
    "email": "почта",
    "school": "школа",
    "position": "должность",
    "region": "регион",
    "city": "город",
    "subject": "предмет",
    "experience": "стаж",
}

EXPERIENCE_MAX = 70


# -- сервисы -------------------------------------------------------------


def teachers_service(ctx: ToolCtx) -> TeachersAdminService:
    """Тот же сервис, что стоит за `/admin/teachers`.

    Профиль правится тем же сценарием, что и `PATCH /me`: правила ИИН
    и пустых полей одни на все двери, и третьей их копии здесь не заводится.
    """
    return TeachersAdminService(
        users=UserRepo(ctx.db),
        teachers=TeacherAdminRepo(ctx.db),
        quizzes=QuizAdminRepo(ctx.db),
        enrollments=EnrollmentRepo(ctx.db),
        attempts=AttemptRepo(ctx.db),
        certificates=CertificateRepo(ctx.db),
        sessions=SessionRepo(ctx.db),
        notifications=NotificationRepo(ctx.db),
        profiles=UsersService(sessions=SessionRepo(ctx.db), users=UserRepo(ctx.db)),
    )


def leads_service(ctx: ToolCtx) -> LeadsService:
    """Тот же сервис, что стоит за `/admin/leads` и `POST /admin/enrollments`.

    Предпросмотром ассистент не пользуется вовсе (раздел 12), поэтому
    репозитории настоящие, а `preview_course_id` пуст. Бот берётся у `deps`
    по той же причине, что хранилище в `courses.py`: выбор адаптера делает
    конфигурация. В сам бот отсюда не уходит ничего — уведомление шлёт
    только `create_lead`, а заявки заводит учитель, и в реестре её нет.
    """
    return LeadsService(
        users=UserRepo(ctx.db),
        courses=CourseRepo(ctx.db),
        leads=LeadRepo(ctx.db),
        enrollments=EnrollmentRepo(ctx.db),
        notifications=NotificationRepo(ctx.db),
        telegram=deps.get_admin_notifier(ctx.db, deps.get_telegram(ctx.db)),
        # Ход коммитится целиком в конце (`assistant_chat.say`): провайдер
        # не ответил — хода не было, и половина работы в базе не остаётся
        commit=ctx.db.flush,
        preview_course_id=None,
        admin_base_url=get_settings().admin_base_url,
    )


def submissions_service(ctx: ToolCtx) -> SubmissionsAdminService:
    return SubmissionsAdminService(
        submissions=SubmissionRepo(ctx.db),
        notifications=NotificationRepo(ctx.db),
        storage=deps.get_storage(),
        cfg=get_settings(),
    )


# -- общее ---------------------------------------------------------------


def _at(value: datetime | date | None) -> str | None:
    """Дата строкой: ответ инструмента ложится в ленту в JSONB, а `datetime`
    туда не сериализуется — падал бы весь ход чата, а не один вызов."""
    return value.isoformat() if value is not None else None


def _masked(ctx: ToolCtx, user_ids: list[int]) -> dict[int, dict]:
    """Маска на всю страницу одним запросом: `user_id` → «Учитель #418».

    Маска у всех списков одна и берётся из `masking.py`, а не собирается
    на месте по полям сценария: «тут же только регион» — ровно тот способ,
    каким телефон однажды окажется в ленте чата.
    """
    if not user_ids:
        return {}
    rows = ctx.db.scalars(select(User).where(User.id.in_(set(user_ids))))
    return {user.id: masked_teacher(user) for user in rows}


def _by_platform(total: int, platform: str | None, count_one: Callable[[str], int]) -> dict:
    """Числа по площадкам — всегда, даже когда фильтра не было: «12 заявок»
    без площадки это неверный ответ (`ASSISTANT_BRIEF`, раздел 4).

    Фильтр задан — считать нечего: строк другой площадки в ответе нет.
    """
    if platform is not None:
        return {code: total if code == platform else 0 for code in PLATFORMS}
    return {code: count_one(code) for code in PLATFORMS}


def _list_out(total: int, rows: list[dict], by_platform: dict | None = None) -> dict:
    """Хвост любого списка: сколько всего, сколько показано и срезан ли он."""
    cut = rows[:LIST_LIMIT]
    out = {"total": total, "shown": len(cut), "truncated": total > len(cut)}
    if by_platform is not None:
        out["by_platform"] = by_platform
    out["items"] = cut
    return out


# -- учителя -------------------------------------------------------------


@tool(
    name="teachers_list",
    danger="read",
    description=(
        "Список учителей с фильтрами: регион, школа, курс, поиск по ФИО"
        " или телефону. Ищет сервер, а в ответе имён и телефонов нет вовсе —"
        " «Учитель #418», регион, школа и счётчики курсов и сертификатов."
        " Человека показывает только teacher_card и только при допуске подключения."
        " Площадки у учителя нет: она есть у доступа — teacher_courses."
    ),
    args={
        "region": Arg("string", "Регион целиком, как он записан в профиле"),
        "school": Arg("string", "Школа целиком, как она записана в профиле"),
        "course_id": Arg("integer", "Только те, у кого действует доступ к этому курсу"),
        "q": Arg("string", "Поиск по ФИО или телефону; имена в ответ не возвращаются"),
    },
)
def teachers_list(
    ctx: ToolCtx,
    region: str | None = None,
    school: str | None = None,
    course_id: int | None = None,
    q: str | None = None,
) -> dict:
    data = teachers_service(ctx).admin_list(
        region=region, school=school, course_id=course_id, q=q, offset=0, limit=LIST_LIMIT
    )
    masks = _masked(ctx, [item["id"] for item in data["items"]])
    rows = [
        {
            **masks[item["id"]],
            "courses_count": item["courses_count"],
            "completed_count": item["completed_count"],
            "certificates_count": item["certificates_count"],
            "is_blocked": item["is_blocked"],
        }
        for item in data["items"]
    ]
    return _list_out(data["total"], rows)


@tool(
    name="teachers_find",
    danger="read",
    description=(
        "Найти человека, которого админ назвал словами. Возвращает кандидатов"
        " без имён: user_id, регион, школа и курсы, по которым их различают."
        " Показав кандидатов, спроси, кто нужен; дальше идёт teacher_card"
        " (если подключение допущено к персональным данным)."
    ),
    args={"query": Arg("string", "ФИО или телефон, как их назвал админ", required=True)},
)
def teachers_find(ctx: ToolCtx, query: str) -> dict:
    data = teachers_service(ctx).admin_list(
        region=None, school=None, course_id=None, q=query, offset=0, limit=FIND_LIMIT
    )
    masks = _masked(ctx, [item["id"] for item in data["items"]])
    teachers = TeacherAdminRepo(ctx.db)
    rows = []
    for item in data["items"]:
        # Курсы — то, чем админ различает двух однофамильцев. Запрос на
        # кандидата, и потому список короткий: карточка выбора это несколько
        # строк под вопросом «кто нужен?», а не выгрузка
        courses = [
            {
                "course_id": course.id,
                "title": course.title,
                "platform": enrollment.platform,
            }
            for enrollment, course in teachers.enrollments(item["id"])
            if enrollment.revoked_at is None
        ]
        rows.append({**masks[item["id"]], "courses": courses})
    return _list_out(data["total"], rows)


@tool(
    name="teacher_card",
    danger="read",
    pd=True,
    description=(
        "Полная карточка человека: ФИО, телефон, почта, школа, должность,"
        " регион, город, предмет, стаж и ИИН. Единственное место, где виден"
        " человек, а не «Учитель #418», и работает только если подключение"
        " допущено к персональным данным (галочка на экране «Подключения ИИ»)."
        " Доступы,"
        " прогресс и тесты лежат отдельно: teacher_courses и teacher_quizzes."
    ),
    args={"user_id": Arg("integer", "Идентификатор учителя", required=True)},
)
def teacher_card(ctx: ToolCtx, user_id: int) -> dict:
    # Отказ звучит по существу — «нужен допуск к данным этого человека»,
    # и текст его написан в require_pd: оборачивать его своим значило бы
    # ссылаться на инструкции как на аргумент (раздел 3)
    ctx.require_pd(user_id)
    card = teachers_service(ctx).card(user_id)
    return {
        "user_id": card["id"],
        "last_name": card["last_name"],
        "first_name": card["first_name"],
        "middle_name": card["middle_name"],
        "phone": card["phone"],
        "email": card["email"],
        "school": card["school"],
        "position": card["position"],
        "region": card["region"],
        "city": card["city"],
        "subject": card["subject"],
        "experience": card["experience"],
        # ИИН последним полем: он чувствительнее всего остального, что мы
        # храним (CERTIFICATES_BRIEF, 1). Заглушка из нулей означает
        # «не заполнен», а не «ИИН такой»
        "iin": card["iin"],
    }


@tool(
    name="teacher_courses",
    danger="read",
    description=(
        "Доступы одного человека: какие курсы открыты, на какой площадке,"
        " когда выдан доступ, получена ли оплата, что пройдено и завершён ли"
        " курс. Персданных здесь нет, допуск не нужен. Отсюда берут"
        " enrollment_id для отзыва доступа."
    ),
    args={"user_id": Arg("integer", "Идентификатор учителя", required=True)},
)
def teacher_courses(ctx: ToolCtx, user_id: int) -> dict:
    rows = teachers_service(ctx).card(user_id)["courses"]
    active = [row for row in rows if row["revoked_at"] is None]
    return {
        "user_id": user_id,
        "ref": teacher_ref(user_id),
        # Доступ свой у каждой площадки: тот же курс на второй — вторая учёба
        "by_platform": {
            code: sum(1 for row in active if row["platform"] == code) for code in PLATFORMS
        },
        **_list_out(len(rows), [_enrollment_row(row) for row in rows]),
    }


@tool(
    name="teacher_quizzes",
    danger="read",
    description=(
        "Тесты одного человека по площадкам: попытки, баллы, зачтено или нет"
        " и можно ли разрешить пересдачу. Строка на тест и площадку: у купившего"
        " курс дважды один тест живёт двумя жизнями. Отсюда берут quiz_id"
        " и площадку для quiz_retake_allow, а retake_blocker говорит, почему"
        " пересдача закрыта."
    ),
    args={"user_id": Arg("integer", "Идентификатор учителя", required=True)},
)
def teacher_quizzes(ctx: ToolCtx, user_id: int) -> dict:
    rows = teachers_service(ctx).card(user_id)["quizzes"]
    return {
        "user_id": user_id,
        "ref": teacher_ref(user_id),
        "by_platform": {
            code: sum(1 for row in rows if row["platform"] == code) for code in PLATFORMS
        },
        **_list_out(len(rows), [_quiz_row(row) for row in rows]),
    }


@tool(
    name="teacher_update",
    danger="confirm",
    description=(
        "Исправить данные человека: ФИО, ИИН, телефон, почту, школу,"
        " должность, регион, город, предмет, стаж. Нужен допуск"
        " подключения к персональным данным. Меняются только"
        " названные поля; пустая строка стирает поле, ИИН не стирается вовсе."
        " Смена телефона завершает все сессии человека: входит он по номеру."
    ),
    args={
        "user_id": Arg("integer", "Идентификатор учителя", required=True),
        "last_name": Arg("string", "Фамилия"),
        "first_name": Arg("string", "Имя"),
        "middle_name": Arg("string", "Отчество"),
        "iin": Arg("string", "ИИН — ровно 12 цифр, у каждого человека свой"),
        "phone": Arg("string", "Телефон: казахстанский, 10 цифр после +7"),
        "email": Arg("string", "Почта"),
        "school": Arg("string", "Школа"),
        "position": Arg("string", "Должность"),
        "region": Arg("string", "Регион"),
        "city": Arg("string", "Город"),
        "subject": Arg("string", "Предмет"),
        "experience": Arg("integer", f"Стаж в годах, от 0 до {EXPERIENCE_MAX}"),
    },
    # Новые значения инструмент применяет, но в журнале вызовов они не остаются:
    # он живёт вечно, и от значений там остаются только имена полей (`mask_args`).
    pd_args=(
        "last_name", "first_name", "middle_name", "iin", "phone", "email",
        "school", "position", "region", "city", "subject", "experience",
    ),
)
def teacher_update(ctx: ToolCtx, user_id: int, **fields) -> dict:
    """Допуск открывает дверь, а пометка `confirm` просит клиента спросить
    человека — нужны оба."""
    ctx.require_pd(user_id)
    changed = _changed(fields)
    teachers_service(ctx).patch(ctx.admin, user_id, changed)
    # В ответе имена полей, а не значения: результат вызова ложится в ленту
    # как есть, и новые ФИО с ИИН копились бы в базе чатов
    return {"user_id": user_id, "changed": list(changed)}


@preview("teacher_update")
def teacher_update_preview(ctx: ToolCtx, user_id: int, **fields) -> str:
    # Проверка допуска стоит и здесь, и в самом инструменте: описание
    # последствий не должно показываться тому, кому сама правка откажет
    ctx.require_pd(user_id)
    changed = _changed(fields)
    listed = ", ".join(EDITABLE_FIELDS[name] for name in changed)
    text = (
        f"{teacher_ref(user_id)}: изменится полей — {len(changed)} ({listed})."
        " Новые значения видны в карточке вызова. Данные человека одни"
        " на обе площадки, и правка задевает обе."
    )
    if "phone" in changed:
        text += (
            " Смена номера завершит все его сессии: входить он будет"
            " по новому номеру."
        )
    return text


def _changed(fields: dict) -> dict:
    """Только названные поля и в порядке экрана. Пусто — правки нет вовсе,
    и предложение с кнопкой заводить не за чем."""
    changed = {name: fields[name] for name in EDITABLE_FIELDS if name in fields}
    if not changed:
        raise ToolError("no_fields", "Не сказано, что менять: назови хотя бы одно поле")
    experience = changed.get("experience")
    # Границу стажа проверяет схема запроса, а у инструмента своей схемы нет:
    # 900 лет стажа в карточке живого человека — это не правка, а опечатка
    if experience is not None and not 0 <= experience <= EXPERIENCE_MAX:
        raise ToolError("bad_argument", f"Стаж — от 0 до {EXPERIENCE_MAX} лет")
    return changed


def _enrollment_row(row: dict) -> dict:
    return {
        "enrollment_id": row["enrollment_id"],
        "course_id": row["course"]["id"],
        "course_title": row["course"]["title"],
        "platform": row["platform"],
        "granted_at": _at(row["granted_at"]),
        # «Оплата получена» — отметка админа в момент выдачи, а не статус
        # у учителя. Сам комментарий не отдаём: админ пишет туда свободный
        # текст, и в нём заводится то, чего в ленте чата быть не должно
        "paid": row["paid_note"] is not None,
        "revoked_at": _at(row["revoked_at"]),
        "completed_at": _at(row["completed_at"]),
        "lessons_done": row["lessons_done"],
        "lessons_total": row["lessons_total"],
        "progress_percent": row["progress_percent"],
    }


def _quiz_row(row: dict) -> dict:
    return {
        "quiz_id": row["quiz_id"],
        "title": row["title"],
        "course_id": row["course_id"],
        "course_title": row["course_title"],
        "platform": row["platform"],
        "retakable": row["retakable"],
        "pass_score": row["pass_score"],
        "can_allow_retake": row["can_allow_retake"],
        "retake_blocker": row["retake_blocker"],
        "attempts": [
            {
                "started_at": _at(attempt["started_at"]),
                "finished_at": _at(attempt["finished_at"]),
                "score": attempt["score"],
                "passed": attempt["passed"],
                "is_counted": attempt["is_counted"],
                "uncounted_reason": attempt["uncounted_reason"],
            }
            for attempt in row["attempts"]
        ],
    }


# -- заявки --------------------------------------------------------------


@tool(
    name="leads_list",
    danger="read",
    description=(
        "Очередь заявок: кто на какой курс просится и сколько дней ждёт."
        " Заявка — просьба связаться, а не оплата: деньги проходят вне"
        " платформы, доступ выдаёт админ через enrollment_grant. Имён"
        " и телефонов в ответе нет. Без площадки считаются обе, и в ответе"
        " видно, сколько на каждой."
    ),
    args={
        "status": Arg(
            "string",
            "Статус заявки: new — новая, contacted — связались,"
            " paid — оплата получена, granted — доступ выдан, declined — отказ,"
            " open — любая в работе (new, contacted, paid).",
            enum=LEAD_STATUSES,
        ),
        "course_id": Arg("integer", "Только заявки на этот курс"),
        "platform": platform_arg(
            "Заявки этой площадки. Не задана — обе, и числа видно по каждой."
        ),
    },
)
def leads_list(
    ctx: ToolCtx,
    status: str | None = None,
    course_id: int | None = None,
    platform: str | None = None,
) -> dict:
    service = leads_service(ctx)

    def page(where: str | None, limit: int) -> dict:
        return service.admin_list(
            statuses=[status] if status else None,
            course_ids=[course_id] if course_id else None,
            q=None,
            platform=where,
            offset=0,
            limit=limit,
        )

    data = page(platform, LIST_LIMIT)
    masks = _masked(ctx, [item["teacher"]["id"] for item in data["items"]])
    rows = [
        {
            "lead_id": item["id"],
            "platform": item["platform"],
            "status": item["status"],
            # «Ждёт 4 дня», а не SLA: закрытая заявка стоит на нуле
            "waiting_days": item["waiting_days"],
            "created_at": _at(item["created_at"]),
            "price_snapshot": item["price_snapshot"],
            # Текст заметки не отдаём: админ пишет её свободно, и в ней
            # заводятся чужие имена и номера. Что заметка есть — видно
            "has_note": bool(item["note"]),
            "course": {"id": item["course"]["id"], "title": item["course"]["title"]},
            "teacher": masks[item["teacher"]["id"]],
        }
        for item in data["items"]
    ]
    # Счёт по площадке — тот же сценарий с другим фильтром: второй способ
    # считать заявки однажды разошёлся бы с первым
    return _list_out(
        data["total"],
        rows,
        _by_platform(data["total"], platform, lambda code: page(code, 1)["total"]),
    )


@tool(
    name="lead_update",
    danger="write",
    description=(
        "Отметка о работе по заявке: сменить статус (связались, оплата"
        " получена, отказ) и записать заметку. Доступ этим не выдаётся —"
        " для него enrollment_grant, он же закрывает заявку статусом granted."
        " Заявку, по которой доступ уже выдан, менять нельзя."
    ),
    args={
        "lead_id": Arg("integer", "Идентификатор заявки", required=True),
        "status": Arg("string", "Новый статус заявки", enum=LEAD_EDIT_STATUSES),
        "note": Arg("string", "Заметка админа по заявке; перезаписывает прежнюю"),
    },
)
def lead_update(
    ctx: ToolCtx, lead_id: int, status: str | None = None, note: str | None = None
) -> dict:
    fields: dict = {}
    if status is not None:
        fields["status"] = status
    if note is not None:
        fields["note"] = note
    if not fields:
        raise ToolError("no_fields", "Не сказано, что менять: статус или заметку")
    lead = leads_service(ctx).admin_patch(lead_id, fields)
    # Ответ сценария несёт ФИО и телефон заявителя — наружу уходят только
    # поля самой заявки
    return {
        "lead_id": lead["id"],
        "platform": lead["platform"],
        "status": lead["status"],
        "waiting_days": lead["waiting_days"],
        "has_note": bool(lead["note"]),
    }


# -- доступ к курсу ------------------------------------------------------


@tool(
    name="enrollment_grant",
    danger="confirm",
    description=(
        "Открыть человеку курс на названной площадке и отметить, получена ли"
        " оплата. Оплата проходит вне платформы, система её не видит — отметку"
        " ставит админ. Площадка обязательна: доступ, выданный на одной,"
        " курса на другой не открывает. Открытая заявка на этот курс"
        " закрывается сама. Нужен допуск подключения к персональным данным."
    ),
    args={
        "user_id": Arg("integer", "Кому открыть курс", required=True),
        "course_id": Arg("integer", "Какой курс открыть", required=True),
        "platform": replace(
            platform_arg(
                "Площадка, на которой откроется курс. Обязательна: у каждой"
                " площадки свой доступ, свой прогресс и свой сертификат."
            ),
            required=True,
        ),
        "paid": Arg(
            "boolean",
            "Оплата получена. Не угадывай: спроси админа, если он не сказал.",
            required=True,
        ),
        "note": Arg("string", "Комментарий к оплате: чем и когда заплатили"),
    },
)
def enrollment_grant(
    ctx: ToolCtx,
    user_id: int,
    course_id: int,
    platform: str,
    paid: bool,
    note: str | None = None,
) -> dict:
    ctx.require_pd(user_id)
    granted = leads_service(ctx).grant(ctx.admin, user_id, course_id, paid, note, platform)
    return {
        "enrollment_id": granted["id"],
        "user_id": granted["user_id"],
        "course_id": granted["course_id"],
        "platform": granted["platform"],
        "granted_at": _at(granted["granted_at"]),
        "paid": granted["paid"],
    }


@preview("enrollment_grant")
def enrollment_grant_preview(
    ctx: ToolCtx,
    user_id: int,
    course_id: int,
    platform: str,
    paid: bool,
    note: str | None = None,
) -> str:
    ctx.require_pd(user_id)
    course = CourseRepo(ctx.db).by_id(course_id)
    if course is None:
        raise ToolError("not_found", f"Курса #{course_id} нет")
    price = CourseRepo(ctx.db).platforms.prices([course_id], platform).get(course_id)
    text = (
        f"{teacher_ref(user_id)} получит доступ к курсу «{course.title}»"
        f" ({course.lang}) на {platform}"
    )
    if price is not None:
        text += f", цена площадки {price} ₸"
    text += (
        ". Оплата получена — отметка админа."
        if paid
        else ". Отметки об оплате не будет."
    )
    text += (
        " Открытая заявка на этот курс закроется. Отменяется это только"
        " удалением его учёбы."
    )
    return text


@tool(
    name="enrollment_revoke",
    danger="confirm",
    description=(
        "Закрыть человеку доступ к курсу на той площадке, где он выдан."
        " Ничего не удаляет: прогресс, попытки, работы и выданные сертификаты"
        " остаются, а строка доступа — в карточке с отметкой об отзыве."
        " enrollment_id берут в teacher_courses."
    ),
    args={"enrollment_id": Arg("integer", "Идентификатор доступа", required=True)},
)
def enrollment_revoke(ctx: ToolCtx, enrollment_id: int) -> dict:
    enrollment = EnrollmentRepo(ctx.db).by_id(enrollment_id)
    # Строка взята до отзыва, а прочитана после: удалённый доступ отобьёт
    # сам сценарий («Доступ не найден»), и до полей дело не дойдёт
    teachers_service(ctx).revoke(enrollment_id)
    return {
        "enrollment_id": enrollment_id,
        "user_id": enrollment.user_id,
        "course_id": enrollment.course_id,
        "platform": enrollment.platform,
        "revoked": True,
    }


@preview("enrollment_revoke")
def enrollment_revoke_preview(ctx: ToolCtx, enrollment_id: int) -> str:
    enrollment = EnrollmentRepo(ctx.db).by_id(enrollment_id)
    if enrollment is None:
        raise ToolError("not_found", f"Доступа #{enrollment_id} нет")
    course = CourseRepo(ctx.db).by_id(enrollment.course_id)
    title = f"«{course.title}»" if course is not None else f"#{enrollment.course_id}"
    return (
        f"{teacher_ref(enrollment.user_id)} потеряет доступ к курсу {title}"
        f" на {enrollment.platform}: курс пропадёт у него из кабинета."
        " Прогресс, попытки, работы и выданные сертификаты остаются на месте."
        " Доступ на второй площадке это не задевает."
    )


@tool(
    name="quiz_retake_allow",
    danger="confirm",
    description=(
        "Разрешить человеку пересдать тест на названной площадке: снимает"
        " зачёт с его зачётной попытки и открывает новую. Ответы и баллы"
        " прежней попытки остаются в истории. Тест, который и так пересдаётся,"
        " и курс с выданным сертификатом или заявкой на него — отказ."
        " quiz_id, площадку и причину отказа берут в teacher_quizzes."
    ),
    args={
        "user_id": Arg("integer", "Кому разрешить пересдачу", required=True),
        "quiz_id": Arg("integer", "Тест", required=True),
        "platform": replace(
            platform_arg(
                "Площадка попытки. Обязательна: у купившего курс дважды"
                " зачётных попыток две, и снять зачёт не с той — необратимо."
            ),
            required=True,
        ),
        "reason": Arg(
            "string", "Причина: она остаётся в истории попытки", required=True
        ),
    },
)
def quiz_retake_allow(
    ctx: ToolCtx, user_id: int, quiz_id: int, platform: str, reason: str
) -> dict:
    # Ответ сценария — карточка учителя целиком, с ФИО и ИИН: наружу уходит
    # только то, что произошло
    teachers_service(ctx).allow_retake(ctx.admin, user_id, quiz_id, reason, platform)
    return {
        "user_id": user_id,
        "quiz_id": quiz_id,
        "platform": platform,
        "retake_allowed": True,
    }


@preview("quiz_retake_allow")
def quiz_retake_allow_preview(
    ctx: ToolCtx, user_id: int, quiz_id: int, platform: str, reason: str
) -> str:
    found = QuizAdminRepo(ctx.db).with_course_and_module(quiz_id)
    if found is None:
        raise ToolError("not_found", f"Теста #{quiz_id} нет")
    quiz, _, course = found
    return (
        f"{teacher_ref(user_id)} сможет пересдать тест «{quiz.title}» курса"
        f" «{course.title}» на {platform}: зачёт с его нынешней попытки"
        f" снимется, ответы и баллы останутся в истории. Проходной балл"
        f" {quiz.pass_score}%. Причина: «{reason}»."
    )


# -- работы на проверку --------------------------------------------------


@tool(
    name="submissions_list",
    danger="read",
    description=(
        "Очередь работ на проверку: чья работа, по какому заданию и сколько"
        " дней ждёт. Без статуса — все, pending — то, что ждёт вердикта."
        " Имён и телефонов в ответе нет. Без площадки считаются обе, и в ответе"
        " видно, сколько на каждой. Текст работы — в submission_card."
    ),
    args={
        "status": Arg(
            "string",
            "Статус работы: pending — ждёт проверки, accepted — зачтено,"
            " rework — отправлена на доработку.",
            enum=SUBMISSION_STATUSES,
        ),
        "course_id": Arg("integer", "Только работы по этому курсу"),
        "platform": platform_arg(
            "Работы этой площадки. Не задана — обе, и числа видно по каждой."
        ),
    },
)
def submissions_list(
    ctx: ToolCtx,
    status: str | None = None,
    course_id: int | None = None,
    platform: str | None = None,
) -> dict:
    service = submissions_service(ctx)

    def page(where: str | None, limit: int) -> dict:
        return service.admin_list(
            status=status, course_id=course_id, platform=where, offset=0, limit=limit
        )

    data = page(platform, LIST_LIMIT)
    masks = _masked(ctx, [item["teacher"]["id"] for item in data["items"]])
    rows = [
        {
            "submission_id": item["id"],
            "platform": item["platform"],
            "status": item["status"],
            "attempt_number": item["attempt_number"],
            # «Ждёт 4 дня»: у проверенной работы счётчик стоит на нуле
            "waiting_days": item["waiting_days"],
            "created_at": _at(item["created_at"]),
            "task": item["task"],
            "course": {"id": item["course"]["id"], "title": item["course"]["title"]},
            "teacher": masks[item["teacher"]["id"]],
        }
        for item in data["items"]
    ]
    return _list_out(
        data["total"],
        rows,
        _by_platform(data["total"], platform, lambda code: page(code, 1)["total"]),
    )


@tool(
    name="submission_card",
    danger="read",
    description=(
        "Одна работа целиком: условие задания, текст работы, комментарий"
        " проверяющего и что человек присылал по этому заданию раньше."
        " Имени автора здесь нет — «Учитель #418». Прочитать приложенные файлы"
        " нельзя, видно только сколько их. Отсюда идут в submission_review."
    ),
    args={"submission_id": Arg("integer", "Идентификатор работы", required=True)},
)
def submission_card(ctx: ToolCtx, submission_id: int) -> dict:
    card = submissions_service(ctx).card(submission_id)
    masks = _masked(ctx, [card["teacher"]["id"]])
    return {
        "submission_id": card["id"],
        "platform": card["platform"],
        "status": card["status"],
        "attempt_number": card["attempt_number"],
        "waiting_days": card["waiting_days"],
        "created_at": _at(card["created_at"]),
        "reviewed_at": _at(card["reviewed_at"]),
        "teacher": masks[card["teacher"]["id"]],
        "course": {"id": card["course"]["id"], "title": card["course"]["title"]},
        "task": {
            "id": card["task"]["id"],
            "title": card["task"]["title"],
            "statement": card["task"]["statement"],
        },
        "text": card["text"],
        # Имена файлов учителя сплошь и рядом его фамилия, а прочитать файл
        # ассистент всё равно не может — наружу уходит только счёт
        "files_count": len(card["files"]),
        "comment": card["comment"],
        "history": [
            {
                "status": earlier["status"],
                "created_at": _at(earlier["created_at"]),
                "reviewed_at": _at(earlier["reviewed_at"]),
                "comment": earlier["comment"],
            }
            for earlier in card["history"]
        ],
    }


@tool(
    name="submission_review",
    danger="confirm",
    description=(
        "Вердикт по работе: зачесть или вернуть на доработку. Это ответ живому"
        " человеку — он придёт учителю уведомлением, и переменить вердикт"
        " потом нельзя. При доработке комментарий обязателен: это единственная"
        " подсказка, что доделать. Текст комментария — черновик, пока админ"
        " не нажал «Выполнить»."
    ),
    args={
        "submission_id": Arg("integer", "Идентификатор работы", required=True),
        "verdict": Arg(
            "string",
            "accepted — зачтено, rework — на доработку",
            required=True,
            enum=["accepted", "rework"],
        ),
        "comment": Arg("string", "Что написать учителю; при доработке обязателен"),
    },
)
def submission_review(
    ctx: ToolCtx, submission_id: int, verdict: str, comment: str | None = None
) -> dict:
    # Ответ сценария — карточка с ФИО автора: наружу уходит только вердикт
    card = submissions_service(ctx).review(ctx.admin, submission_id, verdict, comment)
    return {
        "submission_id": card["id"],
        "platform": card["platform"],
        "status": card["status"],
        "reviewed_at": _at(card["reviewed_at"]),
    }


@preview("submission_review")
def submission_review_preview(
    ctx: ToolCtx, submission_id: int, verdict: str, comment: str | None = None
) -> str:
    card = submissions_service(ctx).card(submission_id)
    what = "зачтена" if verdict == "accepted" else "возвращена на доработку"
    text = (
        f"{teacher_ref(card['teacher']['id'])}: работа по заданию"
        f" «{card['task']['title']}» курса «{card['course']['title']}»"
        f" на {card['platform']} будет {what}, попытка"
        f" {card['attempt_number']}. Учитель получит уведомление,"
        " переменить вердикт потом нельзя."
    )
    if comment:
        text += f" Комментарий ему: «{comment}»"
    return text
