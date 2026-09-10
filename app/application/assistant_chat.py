"""Чаты админа с ассистентом: лента, ход, допуски и подтверждения.

Чат принадлежит админу, который его создал: чужие не видны — проверкой
на сервере, а не скрытием в интерфейсе (`ASSISTANT_BRIEF`, раздел 8).

Ход устроен так: сохраняем сообщение админа, собираем системный промпт
заново, отдаём модели хвост истории и список инструментов, крутим цикл
до текстового ответа, складываем всё в ленту. Наружу ход идёт **событиями**
(`say_stream`): куски текста по мере генерации и карточка вызова в момент
вызова.

**Персданные в ленту не ложатся даже с допуском.** Инструмент, помеченный
`pd=True`, отдаёт модели настоящую карточку, а в базу вместо неё пишется
пометка «карточка учителя #418, показана 09.09 в 14:20». Отсюда два
следствия: персданные лежат в контексте OpenAI ровно те ходы, где идёт
работа, и не копятся в базе чатов.
"""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session as DbSession

from app.adapters.ai.openai_chat import AiUnavailableError
from app.adapters.db.models import (
    AssistantMessage,
    AssistantPdGrant,
    AssistantProposal,
    AssistantThread,
    Course,
    User,
)
from app.adapters.db.repos import now_utc
from app.application.assistant_memory import MemoryService
from app.application.ports import AiMessage, AiPort, AiToolCall
from app.application.ratelimit import SlidingWindowLimiter
from app.assistant import registry
from app.assistant.loop import AiStreamBrokenError, AssistantLoop, LoopResult, Step
from app.assistant.masking import card_placeholder
from app.assistant.prompt import build_state, build_system
from app.assistant.registry import ToolCtx, ToolError
from app.domain.errors import (
    AiFailedError,
    AssistantActionError,
    FieldError,
    NotFoundError,
    RateLimitedError,
)
from app.domain.kz_time import ALMATY

log = logging.getLogger(__name__)

MESSAGE_MAX = 4000
TITLE_MAX = 120

# Запасной заголовок — начало первого сообщения: пустая строка в списке
# не даёт узнать чат вовсе. Он же остаётся, если модель за заголовком
# не дошла: чат без подписи хуже чата с грубой подписью.
TITLE_FALLBACK = 60

# Заголовок ассистент пишет сам после первого ответа (ASSISTANT_BRIEF,
# раздел 8). Отдельным дешёвым запросом, а не тем же ходом: системный промпт
# хода — это описания 68 инструментов и двадцать шесть тысяч токенов, и
# спрашивать в нём про заголовок значило бы платить за них второй раз.
TITLE_PROMPT = (
    "Ты подписываешь чат администратора с ассистентом. Придумай заголовок"
    " из двух-четырёх слов по сути разговора: по нему чат находят в списке."
    " Ответь одним заголовком без кавычек, без точки и без пояснений."
    " Персональных данных в заголовок не переноси: ни имён, ни телефонов."
)
# Столько текста хватает, чтобы понять, о чём разговор, и не платить за хвост
TITLE_SOURCE_MAX = 400


class AssistantChatService:
    def __init__(
        self,
        db: DbSession,
        ai: AiPort,
        *,
        memory: MemoryService,
        limiter: SlidingWindowLimiter,
        max_tool_calls: int,
        history_messages: int,
    ):
        self.db = db
        self.ai = ai
        self.memory = memory
        self.limiter = limiter
        self.max_tool_calls = max_tool_calls
        self.history_messages = history_messages

    # -- чаты ---------------------------------------------------------------

    def threads(self, admin: User, *, archived: bool = False) -> list[dict]:
        rows = (
            self.db.query(AssistantThread)
            .filter(
                AssistantThread.admin_id == admin.id,
                AssistantThread.archived.is_(archived),
            )
            .order_by(AssistantThread.updated_at.desc())
            .all()
        )
        return [self._thread_out(row) for row in rows]

    def create(self, admin: User, *, course_id: int | None = None) -> dict:
        if course_id is not None and self.db.get(Course, course_id) is None:
            raise NotFoundError("Курс не найден")
        # Обе метки одними часами: `server_default` взял бы время базы,
        # а `updated_at` дальше пишется временем процесса — и свежесозданный
        # чат оказывался бы «изменён раньше, чем создан» на разнице часов
        now = now_utc()
        row = AssistantThread(
            admin_id=admin.id, course_id=course_id, created_at=now, updated_at=now
        )
        self.db.add(row)
        self.db.flush()
        return self._thread_out(row)

    def patch(self, admin: User, thread_id: int, data: dict) -> dict:
        thread = self._own(admin, thread_id)
        if "title" in data:
            title = (data["title"] or "").strip()
            if len(title) > TITLE_MAX:
                raise FieldError("title", f"Не длиннее {TITLE_MAX} символов")
            thread.title = title
        if "course_id" in data:
            course_id = data["course_id"]
            if course_id is not None and self.db.get(Course, course_id) is None:
                raise NotFoundError("Курс не найден")
            thread.course_id = course_id
        if "archived" in data:
            thread.archived = bool(data["archived"])
        thread.updated_at = now_utc()
        return self._thread_out(thread)

    def delete(self, admin: User, thread_id: int) -> None:
        """Удаление настоящее, а не мягкое: чат — черновик рассуждений
        одного человека, и хранить чужие черновики после «удалить» незачем.
        Договорённости живут в памяти, а она общая и отдельная."""
        self.db.delete(self._own(admin, thread_id))

    # -- лента --------------------------------------------------------------

    def messages(self, admin: User, thread_id: int) -> list[dict]:
        thread = self._own(admin, thread_id)
        return [self._message_out(row) for row in self._rows(thread.id)]

    # -- ход ----------------------------------------------------------------

    def check_say(self, admin: User, thread_id: int, text: str) -> str:
        """Проверки, которые обязаны стать обычным HTTP-отказом.

        Стоят до стрима нарочно: как только ответ начал течь, статус уже
        отдан, и отказ пришлось бы сообщать событием внутри `200`. Отказ
        по потолку обращений админ должен видеть кодом `429` с числом
        секунд, а не строкой в теле.
        """
        self._own(admin, thread_id)
        text = text.strip()
        if not text:
            raise FieldError("text", "Введите сообщение")
        if len(text) > MESSAGE_MAX:
            raise FieldError("text", f"Не длиннее {MESSAGE_MAX} символов")
        wait = self.limiter.hit(str(admin.id))
        if wait:
            raise RateLimitedError(
                "Слишком много сообщений ассистенту — подождите немного",
                retry_after_sec=wait,
            )
        return text

    def say_stream(self, admin: User, thread_id: int, text: str) -> Iterator[dict]:
        """Ход событиями: куски ответа, карточки вызовов и лента в конце.

        Событий четыре вида, и на экране у каждого своё место:

        - `delta` — кусок текста по мере генерации;
        - `tool` — карточка вызова **в момент вызова**, а не после всего
          ответа: иначе на длинной задаче экран молчит полминуты;
        - `done` — лента целиком и расход токенов за ход;
        - `error` — провайдер не ответил.

        Ничего не коммитится до конца: провайдер замолчал — **хода не было**,
        и в ленте не остаётся ни оборванного вопроса, ни половины вызовов.
        Текст у админа в поле, он нажмёт ещё раз.

        Проверки входа сюда не входят — они в `check_say`, до начала стрима.
        """
        thread = self._own(admin, thread_id)
        now = now_utc()
        # Меняющееся идёт отдельным сообщением, а не в системном промпте:
        # тот обязан быть побайтно одинаковым у всех чатов, иначе реестр
        # выпадает из кэша провайдера (`prompt.py`, шапка модуля)
        history = [
            AiMessage(
                role="system",
                content=build_state(
                    now=now,
                    memory_index=self.memory.index(thread.course_id),
                    course=self._course_out(thread.course_id),
                    grants=self._grants(thread.id, now),
                ),
            ),
            *self._history(thread.id),
        ]
        self.db.add(AssistantMessage(thread_id=thread.id, role="user", content=text))
        history.append(AiMessage(role="user", content=text))

        ctx = ToolCtx(
            db=self.db,
            admin=admin,
            now=now,
            thread_id=thread.id,
            course_id=thread.course_id,
            grants=registry.load_grants(self.db, thread.id, now),
        )
        system = build_system(tools=registry.all_tools())
        loop = AssistantLoop(self.ai, self.max_tool_calls)
        result: LoopResult | None = None
        try:
            for event in loop.run_stream(system=system, history=history, ctx=ctx):
                if event.result is not None:
                    result = event.result
                elif event.step is not None:
                    yield {
                        "event": "tool",
                        "data": {
                            "tool_name": event.step.name,
                            "tool_args": registry.json_safe(_stored_args(event.step)),
                            "tool_result": registry.json_safe(
                                self._stored_result(event.step, now)
                            ),
                        },
                    }
                elif event.delta:
                    yield {"event": "delta", "data": {"text": event.delta}}
        except (AiUnavailableError, AiStreamBrokenError):
            # Ход не состоялся: откатываем сообщение админа вместе со всем
            # остальным. Статус уже отдан, поэтому отказ уходит событием
            self.db.rollback()
            yield {
                "event": "error",
                "data": {"error": {"code": "ai_failed", "message": AiFailedError().message}},
            }
            return
        assert result is not None

        for step in result.steps:
            self.db.add(
                AssistantMessage(
                    thread_id=thread.id,
                    role="tool",
                    tool_call_id=step.call_id,
                    tool_name=step.name,
                    tool_args=registry.json_safe(_stored_args(step)),
                    tool_result=registry.json_safe(self._stored_result(step, now)),
                    proposal_id=step.proposal_id,
                )
            )
        self.db.add(
            AssistantMessage(
                thread_id=thread.id,
                role="assistant",
                content=result.text,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
            )
        )
        if not thread.title:
            thread.title = self._title(text, result.text)
        thread.updated_at = now
        self.db.flush()
        yield {
            "event": "done",
            "data": {
                "thread": self._thread_out(thread),
                "items": [self._message_out(row) for row in self._rows(thread.id)],
                "stopped_at_cap": result.stopped_at_cap,
                # Расход хода — тот же, что лёг в последнее сообщение ленты.
                # Отдельным полем, чтобы экран показал его, не пересобирая
                # ленту (ASSISTANT_BRIEF, раздел 9)
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
            },
        }

    def _title(self, question: str, answer: str) -> str:
        """Как ассистент подписывает чат после первого ответа.

        Неудача здесь ходу не мешает: заголовок — украшение списка, а не часть
        разговора. Модель промолчала или ответила простынёй — берётся начало
        вопроса админа, как было до этого.
        """
        try:
            reply = self.ai.complete(
                system=TITLE_PROMPT,
                messages=[
                    AiMessage(role="user", content=question[:TITLE_SOURCE_MAX]),
                    AiMessage(role="assistant", content=answer[:TITLE_SOURCE_MAX]),
                ],
            )
        except AiUnavailableError:
            # Провайдер молчит — ход уже состоялся, и терять его из-за подписи
            # нельзя. Текст исключения в лог не пишем: в нём бывает переписка
            log.warning("Заголовок чата не написан — остаётся начало вопроса")
            return question[:TITLE_FALLBACK]
        except Exception:
            # А это уже наша ошибка: ход всё равно сохраняем, но в лог она
            # обязана попасть, иначе заголовки молча перестанут писаться
            log.exception("Заголовок чата не написан")
            return question[:TITLE_FALLBACK]
        title = " ".join(reply.text.split()).strip("«»\"'.")
        return title[:TITLE_MAX] if title else question[:TITLE_FALLBACK]

    @staticmethod
    def _stored_result(step: Step, now: datetime) -> dict:
        """Что ложится в базу вместо ответа инструмента.

        Модель настоящую карточку увидела — она ушла ей в этом ходе. В базе
        от карточки остаётся пометка: назавтра админ увидит на её месте
        строку, а не персональные данные. Так и задумано (раздел 5).
        """
        spec = registry.all_tools().get(step.name)
        if spec is not None and spec.pd and "error" not in step.result:
            subject = step.args.get("user_id") or step.result.get("user_id") or 0
            return card_placeholder(int(subject), now)
        return step.result

    # -- допуски ------------------------------------------------------------

    def grant(self, admin: User, thread_id: int, user_id: int, level: str) -> dict:
        """Допуск к данным одного человека. Клик по строке в чате и есть
        выдача: отдельного подтверждения у неё нет — её делает сам админ
        руками, а не модель."""
        thread = self._own(admin, thread_id)
        if level not in ("read", "write"):
            raise FieldError("level", "Допуск бывает на чтение или на чтение и правку")
        subject = self.db.get(User, user_id)
        if subject is None:
            raise NotFoundError("Человек не найден")
        now = now_utc()
        row = (
            self.db.query(AssistantPdGrant)
            .filter(
                AssistantPdGrant.thread_id == thread.id,
                AssistantPdGrant.subject_user_id == user_id,
            )
            .one_or_none()
        )
        if row is None:
            row = AssistantPdGrant(
                thread_id=thread.id, subject_user_id=user_id, granted_by=admin.id
            )
            self.db.add(row)
        row.level = level
        row.granted_by = admin.id
        row.granted_at = now
        row.expires_at = end_of_day(now)
        row.revoked_at = None
        self.db.flush()
        return self._grant_out(row)

    def revoke(self, admin: User, thread_id: int, user_id: int) -> None:
        thread = self._own(admin, thread_id)
        row = (
            self.db.query(AssistantPdGrant)
            .filter(
                AssistantPdGrant.thread_id == thread.id,
                AssistantPdGrant.subject_user_id == user_id,
                AssistantPdGrant.revoked_at.is_(None),
            )
            .one_or_none()
        )
        if row is None:
            raise NotFoundError("Допуска нет")
        row.revoked_at = now_utc()

    # -- подтверждение опасного действия ------------------------------------

    def execute(self, admin: User, proposal_id: int) -> dict:
        """Кнопка «Выполнить».

        Аргументы берутся из строки предложения, а не из тела запроса:
        иначе подтверждение подтверждает не то, что было показано.
        """
        proposal = self.db.get(AssistantProposal, proposal_id)
        if proposal is None:
            raise NotFoundError("Предложение не найдено")
        thread = self._own(admin, proposal.thread_id)
        now = now_utc()
        ctx = ToolCtx(
            db=self.db,
            admin=admin,
            now=now,
            thread_id=thread.id,
            course_id=thread.course_id,
            grants=registry.load_grants(self.db, thread.id, now),
        )
        try:
            result = registry.run_proposal(proposal, ctx)
        except ToolError as exc:
            # Отказ инструмента здесь читает человек, а не модель: он нажал
            # кнопку и обязан узнать, почему ничего не произошло
            raise _as_app_error(exc) from None
        self.db.flush()
        return {"proposal": self._proposal_out(proposal), "result": result}

    def cancel(self, admin: User, proposal_id: int) -> dict:
        proposal = self.db.get(AssistantProposal, proposal_id)
        if proposal is None:
            raise NotFoundError("Предложение не найдено")
        self._own(admin, proposal.thread_id)
        if proposal.executed_at is None and proposal.cancelled_at is None:
            proposal.cancelled_at = now_utc()
        self.db.flush()
        return {"proposal": self._proposal_out(proposal)}

    # -- сборка -------------------------------------------------------------

    def _own(self, admin: User, thread_id: int) -> AssistantThread:
        """Чужой чат не существует, а не «запрещён»: 404 не рассказывает,
        что у соседа есть чат с таким номером."""
        thread = self.db.get(AssistantThread, thread_id)
        if thread is None or thread.admin_id != admin.id:
            raise NotFoundError("Чат не найден")
        return thread

    def _rows(self, thread_id: int) -> list[AssistantMessage]:
        return (
            self.db.query(AssistantMessage)
            .filter(AssistantMessage.thread_id == thread_id)
            .order_by(AssistantMessage.id)
            .all()
        )

    def _history(self, thread_id: int) -> list[AiMessage]:
        """Хвост истории в том виде, в каком его понимает модель.

        Окно режется не по строкам, а по ходам: обрезанный посередине ход
        оставил бы просьбу вызвать инструмент без результата, и провайдер
        отверг бы такую переписку целиком. Поэтому от головы отбрасываем
        всё до первого сообщения админа.
        """
        rows = self._rows(thread_id)[-self.history_messages :]
        while rows and rows[0].role != "user":
            rows.pop(0)

        out: list[AiMessage] = []
        calls: list[AiToolCall] = []
        results: list[AiMessage] = []

        def flush() -> None:
            if not calls:
                return
            out.append(AiMessage(role="assistant", tool_calls=tuple(calls)))
            out.extend(results)
            calls.clear()
            results.clear()

        for row in rows:
            if row.role == "tool":
                call_id = row.tool_call_id or f"call_{row.id}"
                calls.append(
                    AiToolCall(
                        id=call_id, name=row.tool_name or "", arguments=row.tool_args or {}
                    )
                )
                results.append(
                    AiMessage(
                        role="tool",
                        tool_call_id=call_id,
                        content=json.dumps(
                            row.tool_result or {}, ensure_ascii=False, default=str
                        ),
                    )
                )
                continue
            flush()
            out.append(AiMessage(role=row.role, content=row.content))
        flush()
        return out

    def _grants(self, thread_id: int, now: datetime) -> list[dict]:
        rows = (
            self.db.query(AssistantPdGrant)
            .filter(
                AssistantPdGrant.thread_id == thread_id,
                AssistantPdGrant.revoked_at.is_(None),
                AssistantPdGrant.expires_at > now,
            )
            .order_by(AssistantPdGrant.id)
            .all()
        )
        return [self._grant_out(row) for row in rows]

    def thread_grants(self, admin: User, thread_id: int) -> list[dict]:
        thread = self._own(admin, thread_id)
        return self._grants(thread.id, now_utc())

    @staticmethod
    def _grant_out(row: AssistantPdGrant) -> dict:
        # Имени здесь нет и не будет: чип в шапке чата подписывает браузер,
        # обычной ручкой /admin/teachers — право на неё у админа и так есть
        return {
            "subject_user_id": row.subject_user_id,
            "level": row.level,
            "granted_at": row.granted_at,
            "expires_at": row.expires_at,
        }

    def _thread_out(self, row: AssistantThread) -> dict:
        return {
            "id": row.id,
            "title": row.title,
            "course_id": row.course_id,
            "archived": row.archived,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def _course_out(self, course_id: int | None) -> dict | None:
        if course_id is None:
            return None
        course = self.db.get(Course, course_id)
        if course is None:
            return None
        return {"id": course.id, "title": course.title, "lang": course.lang}

    def _message_out(self, row: AssistantMessage) -> dict:
        proposal = self.db.get(AssistantProposal, row.proposal_id) if row.proposal_id else None
        return {
            "id": row.id,
            "role": row.role,
            "content": row.content,
            "tool_name": row.tool_name,
            "tool_args": row.tool_args,
            "tool_result": row.tool_result,
            "proposal": self._proposal_out(proposal) if proposal else None,
            "tokens_in": row.tokens_in,
            "tokens_out": row.tokens_out,
            "created_at": row.created_at,
        }

    def _proposal_out(self, row: AssistantProposal) -> dict:
        return {
            "id": row.id,
            "tool_name": row.tool_name,
            "what_happens": row.what_happens,
            "status": self._proposal_status(row),
            "expires_at": row.expires_at,
            "executed_at": row.executed_at,
            "result": row.result,
        }

    @staticmethod
    def _proposal_status(row: AssistantProposal) -> str:
        if row.executed_at is not None:
            return "executed"
        if row.cancelled_at is not None:
            return "cancelled"
        if row.expires_at <= now_utc():
            return "expired"
        return "waiting"


def _stored_args(step: Step) -> dict:
    """Аргументы вызова в том виде, в каком они лягут в ленту: значения
    персональных полей заменены пометкой (`ASSISTANT_BRIEF`, раздел 5)."""
    spec = registry.all_tools().get(step.name)
    return registry.mask_args(spec, step.args) if spec is not None else step.args


def end_of_day(now: datetime) -> datetime:
    """Конец казахстанских суток. Допуск живёт в своём чате до конца суток
    или до отзыва, что раньше (раздел 5); границы суток считаем по Алматы,
    а не по UTC."""
    local = now.astimezone(ALMATY)
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.astimezone(UTC)


def _as_app_error(exc: ToolError) -> AssistantActionError:
    """Отказ инструмента в ответ человеку, а не модели."""
    return AssistantActionError(exc.code, exc.message, exc.details or None)
