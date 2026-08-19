"""Категории курсов: справочник каталога и его редактор в настройках.

Отдельным сценарием, потому что категория — не курс: у неё свой экран
в настройках площадки, и правит её тот, кто настраивает платформу, а не тот,
кто пишет курс. До сессии 7б категории лежали константой в
`domain/dictionaries.py` и менялись правкой кода (бриф, 5.25).

Здесь же собирается публичный кусок `/dictionaries`: форма ответа с переездом
в таблицу не изменилась — изменился источник, и двум читателям одного
справочника незачем ходить в базу по-разному.
"""

from app.adapters.db.models import Category
from app.adapters.db.repos import CategoryRepo
from app.domain.errors import (
    CategoryExistsError,
    CategoryInUseError,
    FieldError,
    NotFoundError,
)
from app.domain.plural import plural


class CategoriesService:
    def __init__(self, categories: CategoryRepo):
        self.categories = categories

    # -- GET /dictionaries (категории) -------------------------------------

    def public_list(self) -> list[dict]:
        """Категории для фильтра каталога: id и название, без счётчиков —
        каталог открытый, и знать, сколько у категории черновиков, читателю
        незачем."""
        return [
            {"id": category.id, "title": category.title}
            for category in self.categories.all()
        ]

    # -- GET /admin/categories ---------------------------------------------

    def admin_list(self) -> dict:
        """Без пагинации: категорий единицы, и экран её не рисует."""
        rows = self.categories.all()
        counts = self.categories.courses_count([category.id for category in rows])
        return {"items": [self._item_out(category, counts) for category in rows]}

    # -- POST /admin/categories --------------------------------------------

    def create(self, title: str) -> dict:
        category = self.categories.create(self._checked_title(title))
        # Курсов на новой категории нет по определению — счётчик нулевой
        return self._item_out(category, {})

    # -- PATCH /admin/categories/{id} --------------------------------------

    def rename(self, category_id: int, title: str) -> dict:
        """Единственное, что у категории правится: `order_index` задаётся
        при создании, а `courses_count` считается, а не хранится."""
        category = self._found(category_id)
        category.title = self._checked_title(title, category_id=category.id)
        return self._item_out(category, self.categories.courses_count([category.id]))

    # -- DELETE /admin/categories/{id} -------------------------------------

    def delete(self, category_id: int) -> None:
        """Категорию, на которой висит хоть один курс, удалить нельзя:
        `category_id` у курса обязателен, и осиротевший курс исчез бы
        из каталога."""
        category = self._found(category_id)
        count = self.categories.courses_count([category.id]).get(category.id, 0)
        if count:
            raise CategoryInUseError(
                f"В категории {count} {plural(count, 'курс', 'курса', 'курсов')}"
                " — сначала переведите их в другую",
                count,
            )
        self.categories.delete(category)

    # -- сборка ответа ------------------------------------------------------

    def _found(self, category_id: int) -> Category:
        category = self.categories.by_id(category_id)
        if category is None:
            raise NotFoundError("Категория не найдена")
        return category

    def _checked_title(self, raw: str, *, category_id: int | None = None) -> str:
        """Названия уникальны — их же читают в фильтре каталога, и две
        одинаковые строки там неразличимы. При переименовании собственное
        название конфликтом не считается."""
        title = raw.strip()
        if not title:
            raise FieldError("title", "Без названия категорию не сохранить")
        existing = self.categories.by_title(title)
        if existing is not None and existing.id != category_id:
            raise CategoryExistsError(existing.id)
        return title

    @staticmethod
    def _item_out(category: Category, counts: dict[int, int]) -> dict:
        return {
            "id": category.id,
            "title": category.title,
            "order_index": category.order_index,
            # Все версии, включая черновики: по этому числу решают,
            # можно ли категорию удалить
            "courses_count": counts.get(category.id, 0),
        }
