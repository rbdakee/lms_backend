"""Память ассистента: общая на всех админов, в базе, а не файлами.

Устройство ровно как у `MEMORY.md` в Claude Code (`ASSISTANT_BRIEF`, раздел 7):
индекс — строки «имя — описание» — уходит в системный промпт каждый ход, тело
записи грузится, только когда ассистент сам его попросил.

Почему в базе: админов несколько, память общая, а бэкенд живёт на площадке,
где локальная ФС не гарантирована и не бэкапится вместе с базой. Файлы дали бы
точное сходство с Claude Code и потеряли бы данные при первом же переезде.

**Персданные в память не пишутся никогда** — ни с допуском, ни без. Это
проверка на сервере, а не инструкция модели: модель уговаривается, проверка
нет.
"""

import re

from sqlalchemy import case
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.models import AssistantMemory, User
from app.adapters.db.repos import now_utc
from app.assistant.registry import ToolError

# Границы не из экономии: индекс, который не прочесть за один взгляд,
# перестаёт работать как индекс (раздел 7).
NAME_MAX = 64
DESCRIPTION_MAX = 200
BODY_MAX = 4000
TOTAL_MAX = 100

# Слаг: имя записи пишется в промпт и произносится моделью вслух.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Запрет персданных. Три шаблона, и каждый ловит своё:
#   +7      — казахстанский номер в любом написании;
#   12 цифр — ИИН (`CERTIFICATES_BRIEF`, раздел 1);
#   длинная цепочка цифр с разделителями — телефон, записанный как угодно.
PLUS_SEVEN = re.compile(r"\+\s*7")
TWELVE_DIGITS = re.compile(r"\d{12}")
PHONE_SHAPED = re.compile(r"(?:\d[\s\-()]*){10,}")

PD_REFUSED = (
    "В память нельзя писать персональные данные: в тексте есть номер телефона"
    " или ИИН. Перепиши запись без них — человека называй как «учитель #418»."
)


def check_no_pd(*texts: str) -> None:
    """Отказ, если в тексте видна цифра, которой в общей памяти не место.

    Сам текст в отказ не попадает — иначе персданные переехали бы из памяти
    в ленту чата и в лог, то есть ровно туда, куда их не пускает `CLAUDE.md`.
    """
    for text in texts:
        if PLUS_SEVEN.search(text) or TWELVE_DIGITS.search(text) or PHONE_SHAPED.search(text):
            raise ToolError("pd_in_memory", PD_REFUSED)


class MemoryService:
    def __init__(self, db: DbSession):
        self.db = db

    # -- индекс -------------------------------------------------------------

    def index(self, course_id: int | None = None) -> list[dict]:
        """Что уходит в промпт каждый ход: общие записи плюс записи
        закреплённого курса. Память курса подмешивается автоматически —
        именно ради этого курс у чата и закрепляется."""
        rows = self._scoped(course_id).order_by(AssistantMemory.name).all()
        return [
            {
                "name": row.name,
                "description": row.description,
                "scope": row.scope,
                "course_id": row.course_id,
            }
            for row in rows
        ]

    def screen(self) -> list[dict]:
        """Всё для экрана памяти в админке: и общее, и по курсам, с датами
        и автором. Тела записей здесь нет — их читают по одной."""
        # Общие записи сверху, записи курсов под ними: экран читают глазами,
        # а алфавит поставил бы «course» впереди «global» без всякого смысла
        rows = (
            self.db.query(AssistantMemory)
            .order_by(
                case((AssistantMemory.scope == "global", 0), else_=1),
                AssistantMemory.course_id,
                AssistantMemory.name,
            )
            .all()
        )
        return [
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "scope": row.scope,
                "course_id": row.course_id,
                "author_id": row.author_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]

    # -- четыре действия ассистента -----------------------------------------

    def list_names(self, course_id: int | None = None) -> list[dict]:
        return self.index(course_id)

    def read(self, name: str, course_id: int | None = None) -> dict:
        # Имя приводится к слагу здесь же, где на записи и на удалении:
        # «Pricing» от модели — та же запись, что `pricing`, и отказ на ней
        # выглядел бы как «записи нет», хотя она есть
        row = self._find(name.strip().lower(), course_id)
        if row is None:
            raise ToolError("memory_not_found", f"Записи «{name}» в памяти нет")
        return {
            "name": row.name,
            "description": row.description,
            "scope": row.scope,
            "course_id": row.course_id,
            "body": row.body,
            "updated_at": row.updated_at,
        }

    def write(
        self,
        admin: User,
        *,
        name: str,
        description: str,
        body: str,
        course_id: int | None = None,
    ) -> dict:
        name = name.strip().lower()
        description = description.strip()
        body = body.strip()
        if not NAME_RE.match(name) or len(name) > NAME_MAX:
            raise ToolError(
                "bad_memory_name",
                f"Имя записи — слаг латиницей до {NAME_MAX} знаков: «course-pricing»",
            )
        if not description:
            raise ToolError("bad_memory", "У записи должно быть описание — оно идёт в индекс")
        if len(description) > DESCRIPTION_MAX:
            raise ToolError("bad_memory", f"Описание — не длиннее {DESCRIPTION_MAX} знаков")
        if len(body) > BODY_MAX:
            raise ToolError("bad_memory", f"Тело записи — не длиннее {BODY_MAX} знаков")
        check_no_pd(name, description, body)

        row = self._find(name, course_id)
        if row is None:
            if self.db.query(AssistantMemory).count() >= TOTAL_MAX:
                raise ToolError(
                    "memory_full",
                    f"В памяти уже {TOTAL_MAX} записей — удали ненужную и повтори",
                )
            row = AssistantMemory(
                scope="course" if course_id else "global",
                course_id=course_id,
                name=name,
                author_id=admin.id,
            )
            self.db.add(row)
        row.description = description
        row.body = body
        row.updated_at = now_utc()
        self.db.flush()
        return {
            "name": row.name,
            "scope": row.scope,
            "course_id": row.course_id,
            "description": row.description,
            "saved": True,
        }

    def delete(self, name: str, course_id: int | None = None) -> dict:
        row = self._find(name.strip().lower(), course_id)
        if row is None:
            raise ToolError("memory_not_found", f"Записи «{name}» в памяти нет")
        self.db.delete(row)
        self.db.flush()
        return {"name": name, "deleted": True}

    # -- поиск --------------------------------------------------------------

    def _scoped(self, course_id: int | None):
        query = self.db.query(AssistantMemory)
        if course_id is None:
            return query.filter(AssistantMemory.scope == "global")
        return query.filter(
            (AssistantMemory.scope == "global") | (AssistantMemory.course_id == course_id)
        )

    def _find(self, name: str, course_id: int | None) -> AssistantMemory | None:
        """Запись курса и общая с одним именем — разные записи: у курса своя
        цена и свои договорённости, и общая не должна их затирать."""
        query = self.db.query(AssistantMemory).filter(AssistantMemory.name == name)
        if course_id is None:
            query = query.filter(AssistantMemory.scope == "global")
        else:
            query = query.filter(AssistantMemory.course_id == course_id)
        return query.one_or_none()
