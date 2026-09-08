"""Реестр действий ассистента — механика и только она.

Закрытый список именованных операций вместо свободного `http_request`
(`ASSISTANT_BRIEF`, раздел 1). Из одного списка получаются три вещи:

| Кому | Что берёт |
|---|---|
| GPT | JSON-описания в поле `tools` запроса |
| исполнитель | соответствие «имя → функция» и проверка аргументов |
| MCP, когда понадобится | тот же список, завёрнутый в протокол (раздел 15) |

Здесь же **механика предложений**: инструмент с пометкой `confirm`
не исполняется вовсе — он заводит строку `assistant_proposal`, а ручка
`execute` берёт аргументы из строки. Обещание модели «я спрошу перед
удалением» механизмом не является; исполнение опасного инструмента
физически недоступно циклу вызовов.

Сами инструменты живут в `tools/*.py` и сюда не складываются: их пишут
несколько человек сразу, а двое в одном файле дают потерянные куски,
которых не видит ни один тест.

**Реестр не предполагает, что вызывающий — наш собственный чат** (раздел 15):
`ctx` собирается из админа и сессии базы, а не из куки, и `thread_id`
в нём может быть пустым — тогда предложения просто негде заводить.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session as DbSession

from app.adapters.db.models import AssistantPdGrant, AssistantProposal, User
from app.domain.platform import PLATFORMS

log = logging.getLogger(__name__)

# Три пометки опасности (ASSISTANT_BRIEF, раздел 6).
DANGER_LEVELS = ("read", "write", "confirm")

# Предложение одноразовое и живёт час: столько админ читает карточку
# и решается. Дольше — и «Выполнить» нажимается по забытому тексту.
PROPOSAL_TTL_MIN = 60

# Строка, которая дописывается к описанию `confirm`-инструмента. Модель
# не должна обещать «сейчас удалю»: инструмент вернёт ей предложение,
# а не результат, и говорить об этом надо заранее.
CONFIRM_NOTE = (
    " ОПАСНОЕ ДЕЙСТВИЕ: сразу не выполняется. Вызов создаёт предложение"
    " с кнопкой «Выполнить», которую нажимает администратор."
)

# То же для `write`: изменение произойдёт молча, и модель обязана об этом
# сказать вслух, а не отчитаться постфактум.
WRITE_NOTE = " Изменяет данные."


def json_safe(value: Any) -> Any:
    """Ответ инструмента в том виде, в каком его примет JSONB.

    Сценарии отдают то, что удобно им: `date` у даты старта курса, `Decimal`
    у цены, `datetime` у отметки времени. Модели это уходит через `json.dumps`
    с `default=str` и не мешает, а вот запись в `assistant_message.tool_result`
    падает — и вместе с ней падает весь ход. Один инструмент, забывший привести
    дату к строке, ронял бы чат целиком, поэтому приведение стоит здесь, а не
    в каждом из шестидесяти инструментов.
    """
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


# Что стоит в ленте на месте персональных данных. Не пустая строка: видно,
# что поле правилось, — просто чем именно, в истории не остаётся.
HIDDEN = "скрыто"


def mask_args(spec: "ToolSpec", args: dict) -> dict:
    """Аргументы для хранения: значения персональных полей заменены пометкой.

    Модель их получила и инструмент их применил — а вот карточка вызова живёт
    в ленте вечно, и новый телефон учителя в ней копился бы ровно так же, как
    копилась бы карточка (`ASSISTANT_BRIEF`, раздел 5).
    """
    if not spec.pd_args:
        return args
    return {
        name: (HIDDEN if name in spec.pd_args else value) for name, value in args.items()
    }


class ToolError(Exception):
    """Отказ инструмента, который **видит модель**, а не фронт.

    Это не ошибка HTTP: сам запрос чата удался. Текст уходит в ленту
    результатом вызова, и правило разговора «ошибку показывает как есть»
    держится именно на нём.
    """

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_result(self) -> dict:
        result: dict = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            result["error"]["details"] = self.details
        return result


@dataclass(frozen=True)
class Arg:
    """Один аргумент инструмента.

    Тип и `required` записаны явно, а не выведены из строки вида
    `"draft|published"`: у половины инструментов аргумент — это `course_id`,
    то есть целое и обязательное, а из шорткада ни того, ни другого
    не получится. Схема отсюда уходит в поле `tools` запроса как есть.
    """

    type: str
    description: str
    required: bool = False
    enum: list[str] | None = None
    # Тип элементов у массива: без него JSON Schema невалидна.
    items: str = "string"

    TYPES = ("string", "integer", "number", "boolean", "array")

    def schema(self) -> dict:
        out: dict = {"type": self.type, "description": self.description}
        if self.enum:
            out["enum"] = self.enum
        if self.type == "array":
            out["items"] = {"type": self.items}
        return out


def platform_arg(
    description: str = (
        "Код учебной площадки. Не задан — считаются обе, и в ответе это видно"
        " отдельными числами."
    ),
) -> Arg:
    """Площадка приходит аргументом, а не из `Origin`: админка одна на обе
    (`ASSISTANT_BRIEF`, решение 11). Один конструктор на все инструменты —
    чтобы список кодов не расползся по файлам субагентов."""
    return Arg("string", description, enum=list(PLATFORMS))


@dataclass
class ToolSpec:
    name: str
    danger: str
    description: str
    args: dict[str, Arg]
    func: Callable[..., Any]
    # Ответ содержит персональные данные. Такой результат не ложится в ленту
    # чата вовсе — вместо него остаётся пометка (`ASSISTANT_BRIEF`, раздел 5).
    pd: bool = False
    # Аргументы, значения которых нельзя хранить: правка чужих данных приходит
    # новыми ФИО и телефоном, а карточка вызова живёт в ленте вечно. В ленте
    # от них остаются имена полей, а из строки предложения значения стираются
    # сразу после выполнения — держать их дольше незачем.
    pd_args: tuple[str, ...] = ()
    # Человекочитаемое «что произойдёт» для `confirm`. Заводится отдельным
    # декоратором `@preview`, потому что текст считается по данным:
    # не «удалить модуль», а «удалится модуль „Оценивание“ и 4 урока в нём».
    preview: Callable[..., str] | None = None

    def openai_schema(self) -> dict:
        description = self.description
        if self.danger == "confirm":
            description += CONFIRM_NOTE
        elif self.danger == "write":
            description += WRITE_NOTE
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {name: arg.schema() for name, arg in self.args.items()},
                    "required": [name for name, arg in self.args.items() if arg.required],
                    "additionalProperties": False,
                },
            },
        }


_TOOLS: dict[str, ToolSpec] = {}


def tool(
    *,
    name: str,
    danger: str,
    description: str,
    args: dict[str, Arg] | None = None,
    pd: bool = False,
    pd_args: tuple[str, ...] = (),
):
    """Регистрирует функцию как действие ассистента.

    Опечатка в пометке опасности или в типе аргумента роняет импорт, а не
    первый вызов: это ошибка программиста, и цена ей — модель, которая
    удаляет курс без подтверждения.
    """

    def wrap(func: Callable[..., Any]) -> Callable[..., Any]:
        if danger not in DANGER_LEVELS:
            raise RuntimeError(f"{name}: danger={danger!r}, а бывает {DANGER_LEVELS}")
        if name in _TOOLS:
            raise RuntimeError(f"Инструмент {name} уже зарегистрирован — имена уникальны")
        if not description.strip():
            raise RuntimeError(f"{name}: описание пустое, а его читает модель")
        for arg_name, arg in (args or {}).items():
            if not isinstance(arg, Arg):
                raise RuntimeError(f"{name}.{arg_name}: аргумент описывается через Arg(...)")
            if arg.type not in Arg.TYPES:
                raise RuntimeError(f"{name}.{arg_name}: тип {arg.type!r}, а бывает {Arg.TYPES}")
        unknown_pd = sorted(set(pd_args) - set(args or {}))
        if unknown_pd:
            raise RuntimeError(f"{name}: в pd_args названы несуществующие аргументы: {unknown_pd}")
        _TOOLS[name] = ToolSpec(
            name=name,
            danger=danger,
            description=description,
            args=args or {},
            func=func,
            pd=pd,
            pd_args=tuple(pd_args),
        )
        return func

    return wrap


def preview(tool_name: str):
    """Текст «что произойдёт» для опасного инструмента.

    Отдельной функцией, а не строкой в декораторе: последствия считаются
    по данным и с числами — «удалится модуль „Оценивание“ и 4 урока в нём».
    Сигнатура та же, что у самого инструмента.
    """

    def wrap(func: Callable[..., str]) -> Callable[..., str]:
        spec = _TOOLS.get(tool_name)
        if spec is None:
            raise RuntimeError(f"@preview({tool_name!r}): такого инструмента нет")
        if spec.danger != "confirm":
            raise RuntimeError(f"@preview({tool_name!r}): предложения бывают только у confirm")
        spec.preview = func
        return func

    return wrap


def get(name: str) -> ToolSpec:
    spec = _TOOLS.get(name)
    if spec is None:
        raise ToolError("unknown_tool", f"Инструмента {name} нет")
    return spec


def all_tools() -> dict[str, ToolSpec]:
    return dict(_TOOLS)


def openai_tools() -> list[dict]:
    """Список для поля `tools` запроса — в порядке имён, чтобы он не менялся
    от прогона к прогону и кэш промпта провайдера работал."""
    return [_TOOLS[name].openai_schema() for name in sorted(_TOOLS)]


def check_registry() -> list[str]:
    """Что в реестре не так. Зовётся тестом, а не стартом сервиса: забытый
    `@preview` — ошибка кода, и ронять из-за неё живой API незачем."""
    problems = []
    for name, spec in sorted(_TOOLS.items()):
        if spec.danger == "confirm" and spec.preview is None:
            problems.append(f"{name}: confirm без @preview — последствий админ не увидит")
    return problems


# -- контекст вызова -----------------------------------------------------


@dataclass
class ToolCtx:
    """Что инструмент знает о том, кто и откуда его позвал.

    Приходит не из куки (раздел 15): собирается из админа и сессии базы,
    поэтому тот же реестр ляжет под MCP без переписывания. Права проверены
    выше — `get_current_admin`, там же, где у обычных админских ручек;
    своей копии проверок у реестра нет.
    """

    db: DbSession
    admin: User
    now: datetime
    # Чат, из которого пришёл вызов. Пусто — предложения заводить негде:
    # так ассистента зовут черновики ответов, где инструментов нет вовсе.
    thread_id: int | None = None
    # Закреплённый у чата курс: инструменты получают его как course_id
    # по умолчанию (раздел 8).
    course_id: int | None = None
    # Допуски этого чата: subject_user_id → 'read' | 'write'.
    grants: dict[int, str] = field(default_factory=dict)

    def grant_level(self, user_id: int) -> str | None:
        return self.grants.get(user_id)

    def require_pd(self, user_id: int, level: str = "read") -> str:
        """Допуск к данным одного человека или отказ, который увидит модель.

        Отказ говорит по существу, а не «мне не разрешено»: админ должен
        понять, что нужна кнопка, и на кого именно (раздел 3).
        """
        have = self.grants.get(user_id)
        if have is None:
            raise ToolError(
                "pd_grant_required",
                f"Нужен допуск к данным этого человека (учитель #{user_id})."
                " Администратор выдаёт его кнопкой в чате.",
                subject_user_id=user_id,
                need_level=level,
            )
        if level == "write" and have != "write":
            raise ToolError(
                "pd_grant_level",
                f"Допуск к данным учителя #{user_id} выдан только на чтение —"
                " для правки нужен допуск с правкой.",
                subject_user_id=user_id,
                need_level="write",
                have_level=have,
            )
        return have


def load_grants(db: DbSession, thread_id: int, now: datetime) -> dict[int, str]:
    """Действующие допуски чата. Просроченные и отозванные не возвращаются
    вовсе — так «допуск кончился» и «допуска не было» для инструмента
    выглядят одинаково, и второй ветки в каждом инструменте не нужно."""
    rows = (
        db.query(AssistantPdGrant)
        .filter(
            AssistantPdGrant.thread_id == thread_id,
            AssistantPdGrant.revoked_at.is_(None),
            AssistantPdGrant.expires_at > now,
        )
        .all()
    )
    return {row.subject_user_id: row.level for row in rows}


# -- проверка аргументов -------------------------------------------------


def check_args(spec: ToolSpec, args: dict) -> dict:
    """Аргументы модели — до вызова функции.

    Модель ошибается предсказуемо: присылает лишний ключ, забывает
    обязательный, отдаёт `"12"` вместо `12`. Первые два — отказ, который
    она прочитает и исправит; третий — приводится молча, потому что число
    в строке это то же самое число.
    """
    unknown = sorted(set(args) - set(spec.args))
    if unknown:
        raise ToolError(
            "unknown_argument",
            f"У {spec.name} нет аргументов: {', '.join(unknown)}."
            f" Есть: {', '.join(spec.args) or 'ни одного'}",
        )
    clean: dict = {}
    for name, arg in spec.args.items():
        if name not in args or args[name] is None:
            if arg.required:
                raise ToolError(
                    "missing_argument", f"{spec.name}: не задан {name} — {arg.description}"
                )
            continue
        clean[name] = _coerce(spec.name, name, arg, args[name])
    return clean


def _coerce(tool_name: str, name: str, arg: Arg, value: Any) -> Any:
    where = f"{tool_name}.{name}"
    if arg.type == "integer":
        value = _as_int(where, value)
    elif arg.type == "number":
        value = _as_float(where, value)
    elif arg.type == "boolean":
        value = _as_bool(where, value)
    elif arg.type == "string":
        if not isinstance(value, str):
            raise ToolError("bad_argument", f"{where}: ожидалась строка")
    elif arg.type == "array":
        if not isinstance(value, list):
            raise ToolError("bad_argument", f"{where}: ожидался список")
        value = [
            _coerce(tool_name, name, Arg(arg.items, arg.description), item) for item in value
        ]
    if arg.enum and value not in arg.enum:
        raise ToolError(
            "bad_argument", f"{where}: {value!r} — бывает только {', '.join(arg.enum)}"
        )
    return value


def _as_int(where: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ToolError("bad_argument", f"{where}: ожидалось целое число")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    raise ToolError("bad_argument", f"{where}: ожидалось целое число, пришло {value!r}")


def _as_float(where: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ToolError("bad_argument", f"{where}: ожидалось число")
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ToolError("bad_argument", f"{where}: ожидалось число, пришло {value!r}") from None


def _as_bool(where: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in ("true", "false"):
        return value == "true"
    raise ToolError("bad_argument", f"{where}: ожидалось true или false, пришло {value!r}")


# -- вызов ---------------------------------------------------------------


def call(name: str, args: dict, ctx: ToolCtx) -> dict:
    """Один вызов инструмента из цикла.

    `confirm` здесь **не исполняется**: вместо результата заводится
    предложение. Это и есть механизм — обещание модели механизмом не является.
    """
    spec = get(name)
    clean = check_args(spec, args)
    if spec.danger == "confirm":
        return propose(spec, clean, ctx)
    return spec.func(ctx, **clean)


def propose(spec: ToolSpec, args: dict, ctx: ToolCtx) -> dict:
    """Заводит предложение и возвращает модели то, что она скажет вслух."""
    if ctx.thread_id is None:
        raise ToolError(
            "no_thread", f"{spec.name} — опасное действие, а подтверждать его здесь негде"
        )
    what = _what_happens(spec, args, ctx)
    row = AssistantProposal(
        thread_id=ctx.thread_id,
        tool_name=spec.name,
        tool_args=args,
        what_happens=what,
        expires_at=ctx.now + timedelta(minutes=PROPOSAL_TTL_MIN),
    )
    ctx.db.add(row)
    ctx.db.flush()
    return {
        "status": "waiting_confirmation",
        "proposal_id": row.id,
        "what_happens": what,
        "expires_in_min": PROPOSAL_TTL_MIN,
    }


def _what_happens(spec: ToolSpec, args: dict, ctx: ToolCtx) -> str:
    """Последствия своими словами. Забытый `@preview` не роняет сервис —
    его ловит тест (`check_registry`), а здесь остаётся честный, пусть
    и бледный, текст: молчания в карточке подтверждения быть не должно."""
    if spec.preview is None:
        log.warning("У инструмента %s нет @preview — админ увидит общий текст", spec.name)
        return f"{spec.description} Аргументы: {args}"
    return spec.preview(ctx, **args)


def run_proposal(proposal: AssistantProposal, ctx: ToolCtx) -> dict:
    """Выполнить подтверждённое действие.

    Аргументы берутся **из строки**, а не из тела запроса: иначе
    подтверждение подтверждает не то, что было показано (раздел 6).
    """
    if proposal.executed_at is not None:
        raise ToolError("already_executed", "Это действие уже выполнено")
    if proposal.cancelled_at is not None:
        raise ToolError("cancelled", "Предложение отменено")
    if proposal.expires_at <= ctx.now:
        raise ToolError(
            "expired", "Предложение устарело — попросите ассистента предложить заново"
        )
    spec = get(proposal.tool_name)
    clean = check_args(spec, dict(proposal.tool_args or {}))
    result = spec.func(ctx, **clean)
    proposal.executed_at = ctx.now
    proposal.executed_by = ctx.admin.id
    proposal.result = json_safe(result)
    # Значения персональных полей из строки стираются сразу: подтверждать
    # больше нечего, а предложение остаётся в базе навсегда
    proposal.tool_args = mask_args(spec, proposal.tool_args or {})
    return result
