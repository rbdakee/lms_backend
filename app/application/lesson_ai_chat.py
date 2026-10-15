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

**Один чат — одна сессия — один комплект** (решение владельца 15.10.2026).
После «Готовить» чат не закрывается, а переходит в режим правки: те же реплики,
но разбора заказа нет, а у модели другой набор инструментов — оглавление и
правка блоков материалов комплекта (`app/lesson_ai/edit_tools.py`). Правки идут
через `LessonMaterialsService` — ту же, что кнопка ИИ на блоке и ручная правка.
Каждая правка и каждая смена статуса комплекта — событие в ленте (роль `event`).

Чат виден только своему учителю: чужой — 404, а не 403.
"""

import json
import logging
from collections.abc import Iterator

from sqlalchemy.orm import Session as DbSession

from app.adapters.ai.openai_chat import AiUnavailableError
from app.adapters.curriculum.parser import subject_title_kz
from app.adapters.db.lesson_ai_repos import (
    LessonChatRepo,
    LessonUsageRepo,
    ObjectiveRow,
    record_kit_event,
)
from app.adapters.db.models import LessonAiChat, LessonAiChatMessage, User, now_utc
from app.application.ai_loop import AiStreamBrokenError, LoopResult, Step, ToolLoop
from app.application.lesson_kits import LessonKitsService
from app.application.lesson_materials import LessonMaterialsService
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
from app.lesson_ai.edit_tools import EditCtx, EditTools, event_text
from app.lesson_ai.order_tools import FIND_LIMIT_MAX, OrderCtx, OrderTools, objective_item
from app.lesson_ai.prompts import edit_chat as edit_prompt
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
# Первая реплика в истории чатов — строкой-подписью, а не текстом целиком
FIRST_MESSAGE_MAX = 120


class LessonChatService:
    def __init__(
        self,
        db: DbSession,
        ai: AiPort,
        *,
        kits: LessonKitsService,
        limiter: SlidingWindowLimiter,
        materials: LessonMaterialsService,
        max_tool_calls: int,
        max_edit_tool_calls: int,
        history_messages: int,
        model: str,
    ):
        self.db = db
        self.ai = ai
        # Имя модели из настройки, а не из ответа провайдера: тот отвечает
        # датированным снимком (`gpt-4.1-2025-04-14`), которого нет в прайсе
        self.model = model
        self.repo = LessonChatRepo(db)
        self.kits = kits
        # Правка материалов из чата — тем же сценарием, что кнопка ИИ на блоке
        self.materials = materials
        self.limiter = limiter
        self.max_tool_calls = max_tool_calls
        self.max_edit_tool_calls = max_edit_tool_calls
        self.history_messages = history_messages

    # -- чат ----------------------------------------------------------------

    def create(self, user: User) -> dict:
        return self._out(self.repo.create_chat(user.id, now_utc()))

    def chat(self, user: User, chat_id: int) -> dict:
        return self._out(self._own(user, chat_id))

    def chats(self, user: User, *, offset: int, limit: int) -> dict:
        """История разговоров учителя — только свои, свежие по активности сверху.

        Страница собирается тремя запросами при любом её размере (чаты, первые
        реплики, цели), а не `_out` на каждый чат: тот тянет ленту целиком.
        """
        chats, total = self.repo.user_chats(user.id, offset=offset, limit=limit)
        firsts = self.repo.first_user_messages([chat.id for chat in chats])
        objectives = self.repo.objectives_by_ids(
            list({chat.objective_id for chat in chats if chat.objective_id})
        )
        kit_statuses = self.repo.kit_statuses([chat.kit_id for chat in chats if chat.kit_id])
        items = []
        for chat in chats:
            row = objectives.get(chat.objective_id) if chat.objective_id else None
            # То же правило, что в `_order_out`: цель не в редакции языка класса
            # в заказ не засчитывается — и в истории предмета и класса у неё нет
            if row is not None and chat.lang and row[1].lang != chat.lang:
                row = None
            first = firsts.get(chat.id)
            items.append(
                {
                    "id": chat.id,
                    "status": chat.status,
                    "mode": _mode(chat),
                    "kit_id": chat.kit_id,
                    "kit_status": kit_statuses.get(chat.kit_id),
                    # Подпись строки в боковой панели: тема урока, пока её нет —
                    # первая реплика; у пустого чата подписи нет, её даёт экран
                    "title": chat.topic or (first[:FIRST_MESSAGE_MAX] if first else None),
                    "topic": chat.topic,
                    "subject_code": row[2].code if row is not None else None,
                    "grade": row[0].grade if row is not None else None,
                    "first_message": first[:FIRST_MESSAGE_MAX] if first else None,
                    "created_at": chat.created_at,
                    "updated_at": chat.updated_at,
                }
            )
        return {"items": items, "total": total}

    # -- ход ----------------------------------------------------------------

    def check_say(self, user: User, chat_id: int, text: str) -> str:
        """Проверки, которые обязаны стать обычным HTTP-отказом, — до стрима:
        как только ответ потёк, статус уже отдан."""
        self._own(user, chat_id)
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
        - `material_changed` — только в режиме правки: инструмент изменил блок
          материала, в данных объект события ленты (`event`);
        - `done` — чат целиком: лента и заказ, каким его записал сервер;
        - `error` — провайдер не ответил.

        Сначала разбор реплики — он записывает заказ, — потом разговор: модель
        отвечает, уже зная, что сервер записал, и не спрашивает второй раз то,
        что учитель только что сказал. Ничего не коммитится до конца: провайдер
        замолчал посреди разговора — **хода не было**, откатывается и реплика,
        и то, что по ней записал разбор.
        """
        chat = self._own(user, chat_id)
        editing = _mode(chat) == "edit"
        history = self._history(chat.id)
        self.repo.add_message(chat.id, role="user", content=text)
        if editing:
            # Заказ подтверждён и записан в комплект: разбирать в реплике нечего
            extract_in = extract_out = 0
            state = edit_prompt.state_message(self._kit_summary(chat))
            loop = ToolLoop(
                self.ai,
                self.max_edit_tool_calls,
                tools=EditTools(),
                cap_message=edit_prompt.CAP_MESSAGE,
            )
            system = edit_prompt.SYSTEM
            ctx: OrderCtx | EditCtx = EditCtx(
                svc=self.materials, kits=self.kits.kits, user=user, kit_id=chat.kit_id
            )
        else:
            extract_in, extract_out = self._extract(chat, text)
            state = prompt.state_message(self._order_out(chat), said=self._said(chat))
            loop = ToolLoop(
                self.ai, self.max_tool_calls, tools=OrderTools(), cap_message=prompt.CAP_MESSAGE
            )
            system = prompt.SYSTEM
            ctx = OrderCtx(repo=self.repo, user_id=user.id, chat_id=chat.id)
        messages = [
            *history,
            # Служебное сообщение — перед репликой, а не в системном промпте:
            # тот одинаков у всех чатов, и с описаниями инструментов его
            # провайдер берёт из кэша
            AiMessage(role="system", content=state),
            AiMessage(role="user", content=text),
        ]
        result: LoopResult | None = None
        try:
            for event in loop.run_stream(system=system, history=messages, ctx=ctx):
                if event.result is not None:
                    result = event.result
                elif event.step is not None:
                    # Карточка вызова и событие правки пишутся сразу, а не после
                    # хода: правка блока может закоммитить транзакцию посреди
                    # хода (перегенерация закрывает читающую транзакцию до вызова
                    # модели), и событие обязано уйти в базу вместе с правкой
                    self._persist_step(chat.id, event.step)
                    yield {
                        "event": "tool",
                        "data": {
                            "tool_name": event.step.name,
                            "tool_args": event.step.args,
                            "tool_result": event.step.result,
                        },
                    }
                    changed = _step_event(event.step.result)
                    if changed is not None:
                        # Материал справа можно подсветить, не дожидаясь конца хода
                        yield {"event": "material_changed", "data": changed}
                elif event.delta:
                    yield {"event": "delta", "data": {"text": event.delta}}
        except (AiUnavailableError, AiStreamBrokenError):
            self.db.rollback()
            # Хода не было, но разбор реплики до обрыва уже ответил — за него
            # заплачено, и в учёт он идёт отдельной строкой после отката
            self._spent(user.id, chat_id, extract_in, extract_out)
            yield {
                "event": "error",
                "data": {"error": {"code": "ai_failed", "message": AiFailedError().message}},
            }
            return
        assert result is not None

        self.repo.add_message(
            chat.id,
            role="assistant",
            content=result.text,
            # Расход хода целиком — с разбором заказа: он тоже стоит денег
            tokens_in=result.tokens_in + extract_in,
            tokens_out=result.tokens_out + extract_out,
        )
        self._spent(
            user.id, chat.id, result.tokens_in + extract_in, result.tokens_out + extract_out
        )
        chat.updated_at = now_utc()
        self.db.flush()
        yield {
            "event": "done",
            "data": {"chat": self._out(chat), "stopped_at_cap": result.stopped_at_cap},
        }

    def _persist_step(self, chat_id: int, step: Step) -> None:
        self.repo.add_message(
            chat_id,
            role="tool",
            tool_call_id=step.call_id,
            tool_name=step.name,
            tool_args=step.args,
            tool_result=step.result,
        )
        changed = _step_event(step.result)
        if changed is not None:
            # Событие — сразу за карточкой вызова, который его породил
            self.repo.add_message(
                chat_id, role="event", content=event_text(changed), event=changed
            )

    def _spent(self, user_id: int, chat_id: int, tokens_in: int, tokens_out: int) -> None:
        LessonUsageRepo(self.db).add(
            user_id,
            "chat",
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            chat_id=chat_id,
        )

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
            self._apply(chat, data, shown={item["id"]: item for item in shown})
        return reply.tokens_in, reply.tokens_out

    def _apply(self, chat: LessonAiChat, data: dict, *, shown: dict[int, dict]) -> None:
        """Что разбор принёс — в заказ, каждое поле через проверку.

        Неподходящее молча отбрасывается, а не превращается в ошибку: это
        прочтение модели, а не ввод учителя, и учитель его не видел. Цель
        берётся только из показанных ему — подтвердить можно то, что видел.
        Порядок — цель, потом язык: «да, она, класс казахский» подтверждает
        русскую строку и тут же меняет её на казахскую того же кода.
        """
        objective_id = data.get("objective_id")
        if isinstance(objective_id, int) and objective_id in shown:
            # Код цели общий для многих предметов: если учитель назвал предмет,
            # а подтверждённая строка из другого, цель в заказ не идёт
            named = data.get("subject_code")
            if not (
                isinstance(named, str) and named and named != shown[objective_id]["subject_code"]
            ):
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
        # Чат остаётся рабочим местом комплекта: в ленте — «комплект готовится»
        # (или сразу «готов», если весь собран из банка); дальше событие
        # о готовности пишет воркер, закрывший последнее задание
        record_kit_event(self.db, self.kits.kits.kit(kit["id"]))
        self.db.flush()
        return {"chat": self._out(chat), "kit": kit}

    # -- справочник для экрана «выбрать самому» ------------------------------

    def objectives(
        self,
        *,
        q: str,
        grade: int | None,
        lang: str | None,
        subject_code: str | None = None,
    ) -> dict:
        """Цели без модели: поиск по тем же основам, что у инструмента, или
        список класса целиком — учитель открывает его и выбирает сам (раздел 1)."""
        q = q.strip()
        # Незнакомый код — отказ, как у инструмента чата: пустой список
        # экран прочёл бы как «в программе такого нет»
        if subject_code is not None and not self.repo.subject_known(subject_code):
            raise FieldError("subject_code", "Такого предмета в программе нет")
        if q:
            stems = search_stems(q)
            code = objective_code_in(q)
            rows = (
                self.repo.find_objectives(
                    stems,
                    code=code,
                    grade=grade,
                    lang=lang,
                    limit=FIND_LIMIT_MAX,
                    subject_code=subject_code,
                )
                if stems or code
                else []
            )
        elif grade is not None and lang is not None:
            rows = self.repo.grade_objectives(grade, lang, subject_code)
        else:
            raise FieldError("q", "Введите слова из цели или выберите класс и язык")
        return {"items": [objective_item(row) for row in rows]}

    def subjects(self) -> dict:
        """Предметы для чипов «выбрать самому»: только с опубликованными
        программами. Пустой список — программы ещё не опубликованы."""
        return {
            "items": [
                {
                    "code": code,
                    "title_ru": title,
                    "title_kz": subject_title_kz(code, title),
                    "grade_from": grade_from,
                    "grade_to": grade_to,
                    "langs": langs,
                }
                for code, title, grade_from, grade_to, langs in self.repo.published_subjects()
            ]
        }

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

    def feed(self, user: User, chat_id: int, *, after_id: int) -> dict:
        """Лента после указанной строки — для опроса, пока комплект собирается:
        воркер дописывает события, когда экран ничего не отправляет. Вместе
        с лентой — комплект: статусы материалов нужны экрану в тот же момент."""
        chat = self._own(user, chat_id)
        return {
            "status": chat.status,
            "mode": _mode(chat),
            "kit": self._kit_summary(chat),
            "items": [_message_out(row) for row in self.repo.messages(chat.id, after_id=after_id)],
        }

    def _kit_summary(self, chat: LessonAiChat) -> dict | None:
        """Комплект чата без содержимого: статусы и параметры урока. Содержимое
        экран берёт у ручек материалов, а модель — у инструментов."""
        kit = self.kits.kits.kit(chat.kit_id) if chat.kit_id else None
        if kit is None:
            return None
        return {
            "id": kit.id,
            "status": kit.status,
            "topic": kit.topic,
            "grade": kit.grade,
            "lang": kit.lang,
            "engagement_level": kit.engagement_level,
            "difficulty_level": kit.difficulty_level,
            "materials": [
                {"id": m.id, "kind": m.kind, "status": m.status}
                for m in self.kits.kits.materials(kit.id)
            ],
        }

    def _out(self, chat: LessonAiChat) -> dict:
        return {
            "id": chat.id,
            "status": chat.status,
            "mode": _mode(chat),
            "kit_id": chat.kit_id,
            "kit": self._kit_summary(chat),
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
        # События ленты — для экрана; модель их не читает
        rows = [row for row in self.repo.messages(chat_id) if row.role != "event"]
        rows = rows[-self.history_messages :]
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


def _mode(chat: LessonAiChat) -> str:
    """`order` — заказ собирается; `edit` — комплект заказан, чат правит материалы."""
    return "edit" if chat.status == "confirmed" else "order"


def _step_event(result: dict) -> dict | None:
    """Событие правки, если инструмент что-то изменил: его несёт успешный результат."""
    event = result.get("event") if isinstance(result, dict) else None
    return event if isinstance(event, dict) else None


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
        "event": row.event,
        "created_at": row.created_at,
    }
