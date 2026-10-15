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
    LessonMaterialRepo,
    LessonUsageRepo,
    ObjectiveRow,
    record_kit_event,
)
from app.adapters.db.models import LessonAiChat, LessonAiChatMessage, User, now_utc
from app.application.ai_loop import AiStreamBrokenError, LoopResult, Step, ToolLoop
from app.application.lesson_kits import LessonKitsService, objectives_out
from app.application.lesson_materials import LessonMaterialsService
from app.application.ports import AiMessage, AiPort, AiToolCall
from app.application.ratelimit import SlidingWindowLimiter
from app.domain.errors import (
    AiFailedError,
    ChatConfirmedError,
    FieldError,
    NotFoundError,
    ObjectiveLangMissingError,
    ObjectivesMixedError,
    OrderIncompleteError,
    RateLimitedError,
)
from app.domain.lesson_ai import (
    DEFAULT_DIFFICULTY,
    DEFAULT_ENGAGEMENT,
    DEFAULT_KINDS,
    DIFFICULTY_LEVELS,
    ENGAGEMENT_LEVELS,
    MANY_OBJECTIVES,
    MATERIAL_KINDS,
    distinct_ids,
    objective_code_in,
    objectives_compatible,
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
# Цель в названии чата усекается: название — строка боковой панели
TITLE_OBJECTIVE_MAX = 70


def _group_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 60:
        raise FieldError("name", "От 1 до 60 символов")
    return name


def _group_out(group, chats_count: int) -> dict:
    return {
        "id": group.id,
        "name": group.name,
        "position": group.position,
        "chats_count": chats_count,
        "created_at": group.created_at,
    }


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

    def open_for_kit(self, user: User, kit_id: int) -> dict:
        """Чат готового комплекта: существующий (архивный возвращается из архива)
        или новый — сразу в режиме правки, с целями, темой и уровнями комплекта.
        Нужен комплектам, заказанным напрямую, и тем, чей чат не нашли."""
        kit = self.kits.kits.kit(kit_id)
        if kit is None or kit.user_id != user.id:
            raise NotFoundError("Комплект не найден")
        existing = self.repo.chat_by_kit(kit.id)
        if existing is not None:
            if existing.archived_at is not None:
                existing.archived_at = None
                existing.position = None
                self.db.flush()
            return self._out(existing)
        chat = self.repo.create_chat(user.id, now_utc())
        cards = self.kits.kits.kit_objectives([kit.id])[kit.id]
        self.repo.set_chat_objectives(chat.id, [obj.id for obj, _p, _s in cards])
        chat.status = "confirmed"
        chat.kit_id = kit.id
        chat.lang = kit.lang
        chat.topic = kit.topic
        chat.kinds = [m.kind for m in self.kits.kits.materials(kit.id)]
        chat.engagement_level = kit.engagement_level
        chat.difficulty_level = kit.difficulty_level
        chat.class_size = kit.class_size
        chat.notes = kit.notes or None
        self.repo.add_message(
            chat.id,
            role="event",
            content="Урок открыт для правки",
            event={"type": "kit_opened", "kit_id": kit.id},
        )
        self.db.flush()
        return self._out(chat)

    def chat(self, user: User, chat_id: int) -> dict:
        return self._out(self._own(user, chat_id))

    def chats(self, user: User, *, offset: int, limit: int, archived: bool = False) -> dict:
        """История разговоров учителя — только свои, свежие по активности сверху.

        Страница собирается тремя запросами при любом её размере (чаты, первые
        реплики, цели), а не `_out` на каждый чат: тот тянет ленту целиком.
        """
        chats, total = self.repo.user_chats(user.id, offset=offset, limit=limit, archived=archived)
        return {"items": self._list_items(chats), "total": total}

    def _list_items(self, chats: list[LessonAiChat]) -> list[dict]:
        firsts = self.repo.first_user_messages([chat.id for chat in chats])
        chosen = self.repo.chat_objective_ids([chat.id for chat in chats])
        objectives = self.repo.objectives_by_ids(
            list({oid for ids in chosen.values() for oid in ids})
        )
        kit_statuses = self.repo.kit_statuses([chat.kit_id for chat in chats if chat.kit_id])
        items = []
        for chat in chats:
            rows = self._visible(chat, [objectives.get(oid) for oid in chosen[chat.id]])
            row = rows[0] if rows else None
            first = firsts.get(chat.id)
            items.append(
                {
                    "id": chat.id,
                    "status": chat.status,
                    "mode": _mode(chat),
                    "kit_id": chat.kit_id,
                    "kit_status": kit_statuses.get(chat.kit_id),
                    # Название чата — название урока, а не первая реплика:
                    # меняется само по мере того, как урок уточняется
                    "title": _title(chat.topic, row),
                    "topic": chat.topic,
                    "subject_code": row[2].code if row is not None else None,
                    "grade": row[0].grade if row is not None else None,
                    "first_message": first[:FIRST_MESSAGE_MAX] if first else None,
                    "pinned": chat.pinned_at is not None,
                    "group_id": chat.group_id,
                    "position": chat.position,
                    "archived": chat.archived_at is not None,
                    "created_at": chat.created_at,
                    "updated_at": chat.updated_at,
                }
            )
        return items

    # -- сайдбар: закрепление, группы, порядок, архив -----------------------

    def update_chat(self, user: User, chat_id: int, fields: dict) -> dict:
        """`pinned`, `group_id`, `archived` — только то, что прислано. Архив снимает
        закрепление; возврат из архива оставляет прежнюю группу, если она жива
        (удалённая группа уже обнулилась внешним ключом). `updated_at` не двигаем:
        иначе перенос менял бы порядок «по свежести»."""
        chat = self._own(user, chat_id)
        if "group_id" in fields:
            gid = fields["group_id"]
            if gid is not None:
                self._own_group(user, gid)
            if gid != chat.group_id:
                chat.group_id = gid
                chat.position = self.repo.top_position(
                    user.id, section="group" if gid else "ungrouped", group_id=gid
                )
        if fields.get("archived") is True and chat.archived_at is None:
            chat.archived_at = now_utc()
            chat.pinned_at = None
            chat.position = None
        elif fields.get("archived") is False and chat.archived_at is not None:
            chat.archived_at = None
            chat.position = None
        if chat.archived_at is None and "pinned" in fields:
            if fields["pinned"] and chat.pinned_at is None:
                chat.position = self.repo.top_position(user.id, section="pinned")
                chat.pinned_at = now_utc()
            elif not fields["pinned"] and chat.pinned_at is not None:
                chat.pinned_at = None
                chat.position = self.repo.top_position(
                    user.id,
                    section="group" if chat.group_id else "ungrouped",
                    group_id=chat.group_id,
                )
        self.db.flush()
        return self._list_items([chat])[0]

    def reorder_chats(
        self, user: User, section: str, group_id: int | None, chat_ids: list[int]
    ) -> None:
        """Новый порядок секции целиком. Заодно и перенос: чаты из списка
        оказываются в этой секции (закреплённые / группа / без группы), так что
        перетаскивание в другую секцию — один запрос. Архивные не двигаются."""
        if section == "group":
            if group_id is None:
                raise FieldError("group_id", "Укажите группу")
            self._own_group(user, group_id)
        elif group_id is not None:
            raise FieldError("group_id", "Группа нужна только для секции group")
        if len(set(chat_ids)) != len(chat_ids):
            raise FieldError("chat_ids", "Чаты в списке не должны повторяться")
        found = self.repo.own_chats(user.id, chat_ids)
        if len(found) != len(chat_ids) or any(c.archived_at is not None for c in found.values()):
            raise NotFoundError("Чат не найден")
        moment = now_utc()
        for position, chat_id in enumerate(chat_ids, start=1):
            chat = found[chat_id]
            chat.position = position
            if section == "pinned":
                chat.pinned_at = chat.pinned_at or moment
            else:
                chat.pinned_at = None
                chat.group_id = group_id
        self.db.flush()

    def groups(self, user: User) -> dict:
        counts = self.repo.group_counts(user.id)
        return {"items": [_group_out(g, counts.get(g.id, 0)) for g in self.repo.groups(user.id)]}

    def create_group(self, user: User, name: str) -> dict:
        return _group_out(self.repo.create_group(user.id, _group_name(name)), 0)

    def rename_group(self, user: User, group_id: int, name: str | None) -> dict:
        group = self._own_group(user, group_id)
        if name is not None:
            group.name = _group_name(name)
            self.db.flush()
        return _group_out(group, self.repo.group_counts(user.id).get(group.id, 0))

    def delete_group(self, user: User, group_id: int) -> None:
        self.repo.delete_group(self._own_group(user, group_id))

    def reorder_groups(self, user: User, group_ids: list[int]) -> None:
        mine = {g.id: g for g in self.repo.groups(user.id)}
        if len(set(group_ids)) != len(group_ids) or not set(group_ids) <= mine.keys():
            raise NotFoundError("Группа не найдена")
        for position, group_id in enumerate(group_ids, start=1):
            mine[group_id].position = position
        self.db.flush()

    def _own_group(self, user: User, group_id: int):
        group = self.repo.group(group_id)
        if group is None or group.user_id != user.id:
            raise NotFoundError("Группа не найдена")
        return group

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
        if chat.archived_at is not None:
            # Написали в архивный чат — значит он снова рабочий: возвращаем сами,
            # учителю не нужен отказ «сначала верните из архива»
            chat.archived_at = None
            chat.position = None
        if editing:
            # Заказ подтверждён и записан в комплект: разбирать в реплике нечего
            extract_in = extract_out = 0
            state = edit_prompt.state_message(self._edit_state(chat))
            loop = ToolLoop(
                self.ai,
                self.max_edit_tool_calls,
                tools=EditTools(),
                cap_message=edit_prompt.CAP_MESSAGE,
            )
            system = edit_prompt.SYSTEM
            ctx: OrderCtx | EditCtx = EditCtx(
                svc=self.materials,
                kits=self.kits.kits,
                user=user,
                kit_id=chat.kit_id,
                kits_svc=self.kits,
                search=OrderCtx(repo=self.repo, user_id=user.id, chat_id=chat.id, lang=chat.lang),
            )
        else:
            extract_in, extract_out = self._extract(chat, text)
            state = prompt.state_message(self._order_out(chat), said=self._said(chat))
            loop = ToolLoop(
                self.ai, self.max_tool_calls, tools=OrderTools(), cap_message=prompt.CAP_MESSAGE
            )
            system = prompt.SYSTEM
            ctx = OrderCtx(repo=self.repo, user_id=user.id, chat_id=chat.id, lang=chat.lang)
        messages = [
            *history,
            # Служебное сообщение — перед репликой, а не в системном промпте:
            # тот одинаков у всех чатов, и с описаниями инструментов его
            # провайдер берёт из кэша
            AiMessage(role="system", content=state),
            AiMessage(role="user", content=text),
        ]
        result: LoopResult | None = None
        turn_events: list[dict] = []
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
                        turn_events.append(changed)
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
            suggestions=edit_prompt.suggestions(
                turn_events,
                [step.name for step in result.steps],
                stopped_at_cap=result.stopped_at_cap,
                text=f"{text} {result.text}",
            )
            if editing
            else None,
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
        ids = data.get("objective_ids")
        if isinstance(ids, list):
            ids = [i for i in ids if isinstance(i, int) and not isinstance(i, bool)]
            # Цели ещё не записанные берутся только из показанных учителю: подтвердить
            # можно то, что видел. Код цели общий для многих предметов: если учитель
            # назвал предмет, а строка из другого, цель в урок не идёт
            current = set(self.repo.chat_objective_ids([chat.id])[chat.id])
            named = data.get("subject_code")
            ids = [
                i
                for i in ids
                if i in current
                or (
                    i in shown
                    and not (isinstance(named, str) and named and named != shown[i]["subject_code"])
                )
            ]
            if ids:
                self._set_objectives(chat, ids, strict=False)
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
        if data.get("many_confirmed") is True and len(self._rows(chat)) > MANY_OBJECTIVES:
            chat.many_objectives_confirmed = True
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
        if "objective_ids" in data:
            if not data["objective_ids"]:
                self.repo.set_chat_objectives(chat.id, [])
                chat.many_objectives_confirmed = False
            else:
                self._set_objectives(chat, data["objective_ids"], strict=True)
        if data.get("many_objectives_confirmed") is True:
            chat.many_objectives_confirmed = True
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
            objective_ids=[o["id"] for o in order["objectives"]],
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

    def _set_objectives(
        self, chat: LessonAiChat, objective_ids: list[int], *, strict: bool
    ) -> None:
        """Цели урока — строки опубликованного справочника, в редакции языка класса,
        из одной программы и одного класса.

        Язык уже назван, а цель выбрана в другой редакции — берётся та же
        цель по коду из редакции языка класса: переводить формулировку нельзя.
        Кнопка учителя (`strict`) получает отказ на смешанный набор; разбор реплики
        оставляет цели первой программы и класса — остальное учитель не видел.
        """
        rows: list[ObjectiveRow] = []
        for objective_id in distinct_ids(objective_ids):
            row = self.repo.objective(objective_id)
            if row is None:
                if strict:
                    raise NotFoundError("Цель обучения не найдена")
                continue
            if chat.lang and row[1].lang != chat.lang:
                counterpart = self.repo.counterpart(objective_id, chat.lang)
                if counterpart is None:
                    if strict:
                        raise ObjectiveLangMissingError(row[0].code, chat.lang)
                    continue
                row = counterpart
            rows.append(row)
        if not rows:
            return
        if not objectives_compatible((r[1].id, r[0].grade) for r in rows):
            if strict:
                raise ObjectivesMixedError()
            head = (rows[0][1].id, rows[0][0].grade)
            rows = [r for r in rows if (r[1].id, r[0].grade) == head]
        self._store_objectives(chat, [r[0].id for r in rows])

    def _store_objectives(self, chat: LessonAiChat, objective_ids: list[int]) -> None:
        """Набор целей изменился — прежнее «да, столько» к нему уже не относится."""
        if self.repo.chat_objective_ids([chat.id])[chat.id] != objective_ids:
            chat.many_objectives_confirmed = False
        self.repo.set_chat_objectives(chat.id, objective_ids)

    def _set_lang(self, chat: LessonAiChat, lang: str, *, strict: bool) -> None:
        """Язык обучения и цели в его редакции — меняются вместе.

        Какой-то цели нет в редакции нового языка: кнопка учителя получает отказ,
        а разбор реплики выбирает язык — учитель его назвал только что,
        и цели ему покажут заново уже из нужной редакции.
        """
        current = self.repo.chat_objective_ids([chat.id])[chat.id]
        swapped: list[int] = []
        for objective_id in current:
            # Язык цели — без фильтра публикации: программу могли снять на правку,
            # а урок всё равно обязан уйти в редакцию языка класса
            source = self.repo.objective_lang(objective_id)
            if source is None or source[1] == lang:
                swapped.append(objective_id)
                continue
            counterpart = self.repo.counterpart(objective_id, lang)
            if counterpart is None:
                if strict:
                    raise ObjectiveLangMissingError(source[0], lang)
                swapped = []
                break
            swapped.append(counterpart[0].id)
        if swapped != current:
            self._store_objectives(chat, swapped)
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

    def _rows(self, chat: LessonAiChat) -> list[ObjectiveRow]:
        """Цели урока, видимые учителю, по его порядку."""
        ids = self.repo.chat_objective_ids([chat.id])[chat.id]
        found = self.repo.objectives_by_ids(ids)
        return self._visible(chat, [found.get(i) for i in ids])

    @staticmethod
    def _visible(chat: LessonAiChat, rows: list[ObjectiveRow | None]) -> list[ObjectiveRow]:
        """Цель снятой с публикации программы в уроке не числится: генерация по ней
        закрыта, и учителю покажут недостачу, а не отказ после «Готовить». Так же —
        цель не в редакции языка класса: комплект берёт язык из цели, и урок
        ушёл бы не на том языке."""
        return [
            row
            for row in rows
            if row is not None and not (chat.lang and row[1].lang != chat.lang)
        ]

    def _order_out(self, chat: LessonAiChat) -> dict:
        """Карточка материалов к уроку, каким его записал сервер."""
        objectives = [objective_item(row) for row in self._rows(chat)]
        missing = order_missing(
            subject=objectives[0]["subject_code"] if objectives else None,
            grade=objectives[0]["grade"] if objectives else None,
            lang=chat.lang,
            objective=bool(objectives),
            topic=chat.topic,
        )
        return {
            "subject_code": objectives[0]["subject_code"] if objectives else None,
            "grade": objectives[0]["grade"] if objectives else None,
            "lang": chat.lang,
            "objectives": objectives,
            "many_objectives_confirmed": chat.many_objectives_confirmed,
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
        cards = self.kits.kits.kit_objectives([kit.id])[kit.id]
        return {
            "id": kit.id,
            # Цели нужны экрану: шторка «Изменить цели» открывается с текущим набором,
            # а смену набора (из чата или кнопкой) он замечает по этому списку
            "subject_code": cards[0][2].code,
            "objectives": objectives_out(cards),
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

    def _edit_state(self, chat: LessonAiChat) -> dict | None:
        """Комплект для служебного сообщения режима правки: сводка плюс предмет,
        цели по порядку и блоки каждого материала — модель должна знать,
        что выбрано и что в материалах, а не переспрашивать учителя."""
        summary = self._kit_summary(chat)
        if summary is None:
            return None
        materials = LessonMaterialRepo(self.db)
        rewriter = self.materials.rewriter
        for item in summary["materials"]:
            item["blocks"] = [
                {
                    "name": block.kind,
                    "content": block.content,
                    "server": rewriter.is_server_block(item["kind"], block.kind),
                }
                for block in (materials.blocks(item["id"]) if item["status"] == "ready" else [])
            ]
        return {
            **summary,
            "subject_title": self.kits.kits.kit_objectives([chat.kit_id])[chat.kit_id][0][2].title,
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


def _title(topic: str | None, row: ObjectiveRow | None) -> str | None:
    """Название чата в боковой панели — название урока. По порядку:
    тема; «Предмет, класс кл.: цель» по первой цели; предмет и класс;
    ничего — `None`, и экран пишет «Новый урок». Первая реплика в название
    не идёт: чат, начатый с «что ты умеешь?», так бы и назывался."""
    if topic:
        return topic
    if row is None:
        return None
    objective, program, subject = row[0], row[1], row[2]
    kz = program.lang == "kz"
    name = subject_title_kz(subject.code, subject.title) if kz else subject.title
    head = f"{name}, {objective.grade}-сынып" if kz else f"{name}, {objective.grade} кл."
    text = " ".join(objective.text.split())
    if not text:
        return head
    if len(text) > TITLE_OBJECTIVE_MAX:
        text = text[: TITLE_OBJECTIVE_MAX - 1].rstrip() + "…"
    return f"{head}: {text}"


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
        "suggestions": row.suggestions,
        "created_at": row.created_at,
    }
