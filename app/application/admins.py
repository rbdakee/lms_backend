"""Администраторы площадки: список и добавление нового.

До этого экрана админ заводился одним способом — переменными окружения
на старте (`application/bootstrap.py`), и второго можно было получить только
руками в базе. Админов в продукте несколько (CLAUDE.md), поэтому список
и кнопка «Добавить» живут в настройках, рядом с остальным, что настраивает
владелец площадки.

Добавляют по номеру телефона, и только по нему: ФИО человек пишет себе сам
в профиле, а вписанное за него разошлось бы с тем, что он там укажет.
Незнакомый номер заводится пользователем сразу — тем же `UserRepo.create`,
что и вход по SMS: иначе первый вход создал бы второго человека с тем же
номером, а `phone` в базе уникален.

Снятия прав здесь нет — так решил владелец 21.08.2026: админов заводят редко
и всерьёз, а кнопка, которой можно снять права себе или последнему админу,
стоит дороже похода в базу в тот редкий раз, когда это правда нужно.
"""

from app.adapters.db.models import User
from app.adapters.db.repos import UserRepo
from app.domain.errors import FieldError
from app.domain.phone import normalize_phone


class AdminsService:
    def __init__(self, users: UserRepo):
        self.users = users

    # -- GET /admin/admins -------------------------------------------------

    def admin_list(self, current: User) -> dict:
        """Без пагинации: админов единицы — как категорий в тех же настройках."""
        return {"items": [self._item_out(user, current) for user in self.users.list_admins()]}

    # -- POST /admin/admins ------------------------------------------------

    def add(self, current: User, raw_phone: str) -> dict:
        """Номер становится админом. Идемпотентности здесь нет намеренно:
        повторное добавление — это опечатка в номере или непонимание, кого
        уже добавили, и молчаливое «ок» скрыло бы и то, и другое."""
        phone = normalize_phone(raw_phone)
        if phone is None:
            raise FieldError("phone", "Проверьте номер: нужен казахстанский, 10 цифр после +7")
        user = self.users.by_phone(phone)
        if user is None:
            user = self.users.create(phone)
        elif user.is_admin:
            raise FieldError("phone", "Этот номер уже администратор")
        elif user.is_blocked:
            # Права админа заблокированному не выдаём: войти он всё равно
            # не сможет, а в списке стоял бы полноправным
            raise FieldError("phone", "Этот номер заблокирован — сначала снимите блокировку")
        user.is_admin = True
        return self._item_out(user, current)

    # -- сборка ответа ------------------------------------------------------

    @staticmethod
    def _item_out(user: User, current: User) -> dict:
        return {
            "id": user.id,
            "last_name": user.last_name,
            "first_name": user.first_name,
            "middle_name": user.middle_name,
            "phone": user.phone,
            # «Это вы» — чтобы в списке из четырёх номеров было видно себя
            "is_current": user.id == current.id,
            "created_at": user.created_at,
        }
