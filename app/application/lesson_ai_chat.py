"""Чат сбора заказа — вход в «Жоспар» (TEACHER_AI_BRIEF, раздел 1).

Учитель пишет своими словами, какой у него урок; помощник добывает из разговора
предмет, класс, язык обучения, цель обучения из справочника и тему, спрашивает
два уровня и показывает список материалов галочками.

Три вещи, на которых держится чат, и все три — в коде, а не в промпте:

1. **Чат не генерирует материалы.** У модели разговора два инструмента,
   и оба только читают опубликованный справочник (`app/lesson_ai/order_tools.py`).
   Комплект заводит `confirm` — по заказу, записанному здесь, а не по тому,
   что модель наговорила в диалоге.
2. **Заказ записывает сервер.** Ход начинается с разбора последней реплики
   учителя в строгий JSON (`prompts/order_chat.py`), и каждое поле проверяется
   здесь: цель — только строка опубликованного справочника и только из
   показанных учителю, уровни и виды — только из списков брифа.
3. **Пока не собраны все пятеро, генерация не запускается** (`order_missing`),
   а оба уровня приходят с подтверждением обязательными полями: экран не может
   отправить заказ, не показав вопроса.

Реестр админских действий, маскирование, память и промпт ассистента сюда
не импортируются (раздел 6): общий у двух продуктов только водопровод —
`ai_loop`, стрим, `SlidingWindowLimiter` и учёт токенов в ленте.

Чат виден только своему учителю: чужой — 404, а не 403.
"""

import json
import logging
from collections.abc import Iterator

from sqlalchemy.orm import Session as DbSession

from app.adapters.ai.openai_chat import AiUnavailableError
from app.adapters.db.lesson_ai_repos import LessonChatRepo, ObjectiveRow
from app.adapters.db.models import LessonAiChat, LessonAiChatMessage, User, now_utc
from app.application.ai_loop import AiStreamBrokenError, LoopResult, ToolLoop
from app.application.lesson_kits import LessonKitsService
from app.application.ports import AiMessage, AiPort, AiToolCall
from app.application.ratelimit import SlidingWindowLimiter
from app.domain.errors import (
    AiFailedError,
    ChatConfirmedError,
    FieldError,
    NotFoundError,
    ObjectiveLangMissingError,
    OrderIncompleteError,
    RateLimitedError,
)
from app.domain.lesson_ai import (
    DEFAULT_DIFFICULTY,
    DEFAULT_ENGAGEMENT,
    DEFAULT_KINDS,
    DIFFICULTY_LEVELS,
    ENGAGEMENT_LEVELS,
    MATERIAL_KINDS,
    objective_code_in,
    order_missing,
    search_stems,
)
from app.lesson_ai.order_tools import FIND_LIMIT_MAX, OrderCtx, OrderTools, objective_item
from app.lesson_ai.prompts import order_chat as prompt

log = logging.getLogger(__name__)

MESSAGE_MAX = 2000
# Те же потолки, что у заказа комплекта напрямую (`LessonKitIn`): заказ из чата
# уходит в ту же ручку сценария и не должен отбиться там, где чат его принял
TOPIC_MAX = 200
NOTES_MAX = 1000
CLASS_SIZE_RANGE = (1, 60)
# Разбору заказа хватает конца разговора: он ищет, что сказано в последней
# реплике, а прежнее уже записано в заказ
EXTRACT_DIALOG = 6
# Сколько найденных целей разбор видит списком «показано учителю»: подтверждают
# недавно показанное, а не то, что мелькнуло в начале длинного чата
SHOWN_MAX = 20


class LessonChatService:
    def __init__(
        self,
        db: DbSession,
        ai: AiPort,
        *,
        kits: LessonKitsService,
        limiter: SlidingWindowLimiter,
        max_tool_calls: int,
        history_messages: int,
    ):
        self.db = db
        self.ai = ai
        self.repo = LessonChatRepo(db)
        self.kits = kits
        self.limiter = limiter
        self.max_tool_calls = max_tool_calls
        self.history_messages = history_messages

    # -- чат ----------------------------------------------------------------

    def create(self, user: User) -> dict:
        return self._out(self.repo.create_chat(user.id, now_utc()))

    def chat(self, user: User, chat_id: int) -> dict:
        return self._out(self._own(user, chat_id))

    # -- ход ----------------------------------------------------------------

    def check_say(self, user: User, chat_id: int, text: str) -> str:
        """Проверки, которые обязаны стать обычным HTTP-отказом, — до стрима:
        как только ответ потёк, статус уже отдан."""
        chat = self._own(user, chat_id)
        if chat.status == "confirmed":
            raise ChatConfirmedError(chat.kit_id)
        text = text.strip()
        if not text:
            raise FieldError("text", "Введите сообщение")
        if len(text) > MESSAGE_MAX:
            raise FieldError("text", f"Не длиннее {MESSAGE_MAX} символов")
        wait = self.limiter.hit(str(user.id))
        if wait:
            raise RateLimitedError(
                "Слишком много сообщений помощнику — подождите немного",
                retry_after_sec=wait,
            )
        return text

    def say_stream(self, user: User, chat_id: int, text: str) -> Iterator[dict]:
        """Ход событиями — той же формы, что у ассистента админки:

        - `delta` — кусок ответа по мере генерации;
        - `tool` — найденные цели в момент поиска: экран рисует их карточками;
        - `done` — чат целиком: лента и заказ, каким его записал сервер;
        - `error` — провайдер не ответил.

        Сначала разбор реплики — он записывает заказ, — потом разговор: модель
        отвечает, уже зная, что сервер записал, и не спрашивает второй раз то,
        что учитель только что сказал. Ничего не коммитится до конца: провайдер
        замолчал посреди разговора — **хода не было**, откатывается и реплика,
        и то, что по ней записал разбор.
        """
        chat = self._own(user, chat_id)
        history = self._history(chat.id)
        self.repo.add_message(chat.id, role="user", content=text)
        extract_in, extract_out = self._extract(chat, text)

        order = self._order_out(chat)
        state = prompt.state_message(order, said=self._said(chat))
        messages = [
            *history,
            # Служебное сообщение — перед репликой, а не в системном промпте:
            # тот одинаков у всех чатов, и с описаниями инструментов его
            # провайдер берёт из кэша
            AiMessage(role="system", content=state),
            AiMessage(role="user", content=text),
        ]
        loop = ToolLoop(
            self.ai, self.max_tool_calls, tools=OrderTools(), cap_message=prompt.CAP_MESSAGE
        )
        ctx = OrderCtx(repo=self.repo, user_id=user.id, chat_id=chat.id)
        result: LoopResult | None = None
        try:
            for event in loop.run_stream(system=prompt.SYSTEM, history=messages, ctx=ctx):
                if event.result is not None:
                    result = event.result
                elif event.step is not None:
                    yield {
                        "event": "tool",
                        "data": {
                            "tool_name": event.step.name,
                            "tool_args": event.step.args,
                            "tool_result": event.step.result,
                        },
                    }
                elif event.delta:
                    yield {"event": "delta", "data": {"text": event.delta}}
        except (AiUnavailableError, AiStreamBrokenError):
            self.db.rollback()
            yield {
                "event": "error",
                "data": {"error": {"code": "ai_failed", "message": AiFailedError().message}},
            }
            return
        assert result is not None

        for step in result.steps:
            self.repo.add_message(
                chat.id,
                role="tool",
                tool_call_id=step.call_id,
                tool_name=step.name,
                tool_args=step.args,
                tool_result=step.result,
            )
        self.repo.add_message(
            chat.id,
            role="assistant",
            content=result.text,
            # Расход хода целиком — с разбором заказа: он тоже стоит денег
            tokens_in=result.tokens_in + extract_in,
            tokens_out=result.tokens_out + extract_out,
        )
        chat.updated_at = now_utc()
        self.db.flush()
        yield {
            "event": "done",
            "data": {"chat": self._out(chat), "stopped_at_cap": result.stopped_at_cap},
        }

    def _extract(self, chat: LessonAiChat, text: str) -> tuple[int, int]:
        """Разбор реплики учителя в поля заказа — и запись того, что прошло
        проверку. Возвращает расход.

        Неудача разбора ходу не мешает: заказ просто не изменился, а модель
        разговора увидит это в служебном сообщении и переспросит. Ронять ход
        из-за разбора значило бы, что учитель не получает ответа вовсе.
        """
        shown = self._shown(chat.id)
        dialog = [
            (row.role, row.content)
            for row in self.repo.messages(chat.id)
            if row.role in ("user", "assistant") and row.content
        ][-EXTRACT_DIALOG:]
        if not dialog or dialog[-1] != ("user", text):
            # Реплика ещё не долетела до базы (flush не было) — дописываем сами
            dialog.append(("user", text))
        content = prompt.extract_message(
            order=self._order_out(chat), said=self._said(chat), shown=shown, dialog=dialog
        )
        try:
            reply = self.ai.complete_json(
                system=prompt.EXTRACT_SYSTEM,
                messages=[AiMessage(role="user", content=content)],
                name="lesson_order",
                schema=prompt.EXTRACT_SCHEMA,
            )
        except AiUnavailableError:
            # Текст исключения в лог не идёт: в нём бывает переписка учителя
            log.warning("Разбор заказа не ответил — заказ чата %s не изменился", chat.id)
            return 0, 0
        try:
            data = json.loads(reply.text)
        except json.JSONDecodeError:
            log.warning("Разбор заказа вернул не JSON — заказ чата %s не изменился", chat.id)
            return reply.tokens_in, reply.tokens_out
        if isinstance(data, dict):
            self._apply(chat, data, shown_ids={item["id"] for item in shown})
        return reply.tokens_in, reply.tokens_out

    def _apply(self, chat: LessonAiChat, data: dict, *, shown_ids: set[int]) -> None:
        """Что разбор принёс — в заказ, каждое поле через проверку.

        Неподходящее молча отбрасывается, а не превращается в ошибку: это
        прочтение модели, а не ввод учителя, и учитель его не видел. Цель
        берётся только из показанных ему — подтвердить можно то, что видел.
        Порядок — цель, потом язык: «да, она, класс казахский» подтверждает
        русскую строку и тут же меняет её на казахскую того же кода.
        """
        objective_id = data.get("objective_id")
        if isinstance(objective_id, int) and objective_id in shown_ids:
            self._set_objective(chat, objective_id, strict=False)
        lang = data.get("lang")
        if lang in ("ru", "kz"):
            self._set_lang(chat, lang, strict=False)
        topic = data.get("topic")
        if isinstance(topic, str) and topic.strip() and len(topic.strip()) <= TOPIC_MAX:
            chat.topic = topic.strip()
        if data.get("engagement_level") in ENGAGEMENT_LEVELS:
            chat.engagement_level = data["engagement_level"]
        if data.get("difficulty_level") in DIFFICULTY_LEVELS:
            chat.difficulty_level = data["difficulty_level"]
        kinds = data.get("kinds")
        if isinstance(kinds, list):
            clean = _kinds(kinds)
            if clean:
                chat.kinds = clean
        class_size = data.get("class_size")
        if isinstance(class_size, int) and not isinstance(class_size, bool):
            if CLASS_SIZE_RANGE[0] <= class_size <= CLASS_SIZE_RANGE[1]:
                chat.class_size = class_size
        notes = data.get("notes")
        if isinstance(notes, str) and notes.strip():
            chat.notes = _joined_notes(chat.notes, notes.strip())

    # -- заказ: кнопки и подтверждение ---------------------------------------

    def patch_order(self, user: User, chat_id: int, data: dict) -> dict:
        """Кнопки под сообщениями: «Да, она» у карточки цели, «Русский» или
        «Қазақша», правка темы. Это ввод учителя, поэтому отказ здесь —
        внятная ошибка, а не молчаливый пропуск, как у разбора."""
        chat = self._own(user, chat_id)
        if chat.status == "confirmed":
            raise ChatConfirmedError(chat.kit_id)
        if "objective_id" in data:
            if data["objective_id"] is None:
                chat.objective_id = None
            else:
                self._set_objective(chat, data["objective_id"], strict=True)
        if "lang" in data:
            if data["lang"] is None:
                chat.lang = None
            else:
                self._set_lang(chat, data["lang"], strict=True)
        if "topic" in data:
            topic = (data["topic"] or "").strip()
            if len(topic) > TOPIC_MAX:
                raise FieldError("topic", f"Не длиннее {TOPIC_MAX} символов")
            chat.topic = topic or None
        chat.updated_at = now_utc()
        self.db.flush()
        return self._out(chat)

    def confirm(
        self,
        user: User,
        chat_id: int,
        *,
        kinds: list[str],
        engagement_level: str,
        difficulty_level: str,
        class_size: int | None = None,
        notes: str | None = None,
        fields_set: frozenset[str] = frozenset(),
    ) -> dict:
        """Кнопка «Готовить»: заказ из чата становится комплектом.

        Цель, язык и тему берём из записанного в чате, а не из запроса: учитель
        их подтвердил в разговоре. Галочки и оба уровня — из запроса: это
        карточка заказа, которую он только что видел. `class_size` и `notes`
        не прислали — берутся из разговора; прислали `null` — стёрты.

        Комплект заводит тот же сценарий, что и прямой заказ: проверки
        опубликованности программы и ключей без заданий у них общие.
        """
        chat = self.repo.lock_chat(chat_id)
        if chat is None or chat.user_id != user.id:
            raise NotFoundError("Чат не найден")
        if chat.status == "confirmed":
            raise ChatConfirmedError(chat.kit_id)
        order = self._order_out(chat)
        if order["missing"]:
            raise OrderIncompleteError(order["missing"])
        if "class_size" not in fields_set:
            class_size = chat.class_size
        if "notes" not in fields_set:
            notes = chat.notes
        kit = self.kits.create(
            user,
            objective_id=order["objective"]["id"],
            topic=chat.topic,
            kinds=kinds,
            class_size=class_size,
            notes=notes or "",
            engagement_level=engagement_level,
            difficulty_level=difficulty_level,
        )
        chat.status = "confirmed"
        chat.kit_id = kit["id"]
        chat.kinds = _kinds(kinds)
        chat.engagement_level = engagement_level
        chat.difficulty_level = difficulty_level
        chat.class_size = class_size
        chat.notes = notes
        chat.updated_at = now_utc()
        self.db.flush()
        return {"chat": self._out(chat), "kit": kit}

    # -- справочник для экрана «выбрать самому» ------------------------------

    def objectives(self, *, q: str, grade: int | None, lang: str | None) -> dict:
        """Цели без модели: поиск по тем же основам, что у инструмента, или
        список класса целиком — учитель открывает его и выбирает сам (раздел 1)."""
        q = q.strip()
        if q:
            stems = search_stems(q)
            code = objective_code_in(q)
            rows = (
                self.repo.find_objectives(
                    stems, code=code, grade=grade, lang=lang, limit=FIND_LIMIT_MAX
                )
                if stems or code
                else []
            )
        elif grade is not None and lang is not None:
            rows = self.repo.grade_objectives(grade, lang)
        else:
            raise FieldError("q", "Введите слова из цели или выберите класс и язык")
        return {"items": [objective_item(row) for row in rows]}

    # -- сборка -------------------------------------------------------------

    def _set_objective(self, chat: LessonAiChat, objective_id: int, *, strict: bool) -> None:
        """Цель — строка опубликованного справочника, в редакции языка класса.

        Язык уже назван, а цель выбрана в другой редакции — берётся та же
        цель по коду из редакции языка класса: переводить формулировку нельзя.
        """
        row = self.repo.objective(objective_id)
        if row is None:
            if strict:
                raise NotFoundError("Цель обучения не найдена")
            return
        if chat.lang and row[1].lang != chat.lang:
            counterpart = self.repo.counterpart(objective_id, chat.lang)
            if counterpart is None:
                if strict:
                    raise ObjectiveLangMissingError(row[0].code, chat.lang)
                return
            row = counterpart
        chat.objective_id = row[0].id

    def _set_lang(self, chat: LessonAiChat, lang: str, *, strict: bool) -> None:
        """Язык обучения и цель в его редакции — меняются вместе.

        Цели нет в редакции нового языка: кнопка учителя получает отказ,
        а разбор реплики выбирает язык — учитель его назвал только что,
        и цель ему покажут заново уже из нужной редакции.
        """
        if chat.objective_id is not None:
            # Язык цели — без фильтра публикации: программу могли снять на правку,
            # а заказ всё равно обязан уйти в редакцию языка класса
            source = self.repo.objective_lang(chat.objective_id)
            if source is not None and source[1] != lang:
                counterpart = self.repo.counterpart(chat.objective_id, lang)
                if counterpart is None:
                    if strict:
                        raise ObjectiveLangMissingError(source[0], lang)
                    chat.objective_id = None
                else:
                    chat.objective_id = counterpart[0].id
        chat.lang = lang

    def _own(self, user: User, chat_id: int) -> LessonAiChat:
        chat = self.repo.chat(chat_id)
        if chat is None or chat.user_id != user.id:
            raise NotFoundError("Чат не найден")
        return chat

    @staticmethod
    def _said(chat: LessonAiChat) -> dict:
        return {
            "engagement_level": chat.engagement_level,
            "difficulty_level": chat.difficulty_level,
        }

    def _objective_row(self, chat: LessonAiChat) -> ObjectiveRow | None:
        return self.repo.objective(chat.objective_id) if chat.objective_id else None

    def _order_out(self, chat: LessonAiChat) -> dict:
        """Карточка заказа. Цель снятой с публикации программы в заказе
        не числится: генерация по ней закрыта, и учителю покажут недостачу,
        а не отказ после «Готовить»."""
        row = self._objective_row(chat)
        # Цель не в редакции языка класса в заказ не засчитывается: комплект
        # берёт язык из цели, и такой заказ ушёл бы не на том языке
        if row is not None and chat.lang and row[1].lang != chat.lang:
            row = None
        objective = objective_item(row) if row is not None else None
        missing = order_missing(
            subject=objective["subject_code"] if objective else None,
            grade=objective["grade"] if objective else None,
            lang=chat.lang,
            objective=objective is not None,
            topic=chat.topic,
        )
        return {
            "subject_code": objective["subject_code"] if objective else None,
            "grade": objective["grade"] if objective else None,
            "lang": chat.lang,
            "objective": objective,
            "topic": chat.topic,
            # Середина, пока учитель не сказал иначе: вопрос не блокирующий,
            # но задан он будет всегда — оба поля обязательны в подтверждении
            "engagement_level": chat.engagement_level or DEFAULT_ENGAGEMENT,
            "difficulty_level": chat.difficulty_level or DEFAULT_DIFFICULTY,
            "kinds": list(chat.kinds) if chat.kinds else list(DEFAULT_KINDS),
            "class_size": chat.class_size,
            "notes": chat.notes or "",
            "missing": missing,
            "ready": not missing,
        }

    def _out(self, chat: LessonAiChat) -> dict:
        return {
            "id": chat.id,
            "status": chat.status,
            "kit_id": chat.kit_id,
            "order": self._order_out(chat),
            "items": [_message_out(row) for row in self.repo.messages(chat.id)],
            "created_at": chat.created_at,
            "updated_at": chat.updated_at,
        }

    def _shown(self, chat_id: int) -> list[dict]:
        """Цели, которые учитель видел карточками, — последние, без повторов."""
        seen: dict[int, dict] = {}
        for row in self.repo.messages(chat_id):
            if row.role != "tool" or not isinstance(row.tool_result, dict):
                continue
            for item in row.tool_result.get("items") or []:
                seen.pop(item["id"], None)
                seen[item["id"]] = item
        return list(seen.values())[-SHOWN_MAX:]

    def _history(self, chat_id: int) -> list[AiMessage]:
        """Хвост ленты в том виде, в каком его понимает модель.

        Окно режется по ходам, а не по строкам: обрезанный посередине ход
        оставил бы просьбу вызвать инструмент без результата, и провайдер
        отверг бы переписку целиком. Как у ассистента — это второе такое
        место, и выносить общее пока рано (CLAUDE.md, DRY по третьему).
        """
        rows = self.repo.messages(chat_id)[-self.history_messages :]
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
                    AiToolCall(id=call_id, name=row.tool_name or "", arguments=row.tool_args or {})
                )
                results.append(
                    AiMessage(
                        role="tool",
                        tool_call_id=call_id,
                        content=json.dumps(row.tool_result or {}, ensure_ascii=False),
                    )
                )
                continue
            flush()
            out.append(AiMessage(role=row.role, content=row.content))
        flush()
        return out


def _kinds(kinds: list) -> list[str]:
    """Виды без повторов и чужих слов, в порядке брифа (раздел 4.2)."""
    return [kind for kind in MATERIAL_KINDS if kind in kinds]


def _joined_notes(before: str | None, new: str) -> str:
    """Пожелания копятся, а не перетираются: «24 ребёнка» и «путают разряды»
    учитель говорит разными репликами."""
    if not before:
        return new[:NOTES_MAX]
    if new in before:
        return before
    return f"{before}; {new}"[:NOTES_MAX]


def _message_out(row: LessonAiChatMessage) -> dict:
    return {
        "id": row.id,
        "role": row.role,
        "content": row.content,
        "tool_name": row.tool_name,
        "tool_args": row.tool_args,
        "tool_result": row.tool_result,
        "created_at": row.created_at,
    }
