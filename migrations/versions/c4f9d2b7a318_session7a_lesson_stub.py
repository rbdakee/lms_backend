# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""session7a lesson stub

Заготовка урока. Окно «Добавить в программу» заводит строку до того, как
у неё появилось содержимое: название, вид и требуемое время, а ссылка
и текст приходят уже в редакторе (CONTRACT, сессия 7а). Прежняя проверка
такую строку не пропускала — у kind=video требовался video_url,
у kind=text — body.

Проверка не снимается, а сужается до «пусто целиком»: урок без содержимого
допустим, урок с содержимым не своего вида — нет. Видеоурок с заметкой,
но без ссылки, в базу по-прежнему не ложится, и правило раздела 2
BACKEND_NOTES остаётся за базой, а не только за сценарием.

Revision ID: c4f9d2b7a318
Revises: a7c31f0b95e4
Create Date: 2026-08-19 14:20:00.000000

"""
from alembic import op

revision = 'c4f9d2b7a318'
down_revision = 'a7c31f0b95e4'
branch_labels = None
depends_on = None

FILLED = "(kind = 'video' AND video_url IS NOT NULL) OR (kind = 'text' AND body IS NOT NULL)"
# Заготовка: содержимого нет вовсе — ни ссылки, ни текста.
STUB = "(video_url IS NULL AND body IS NULL)"


def upgrade() -> None:
    op.drop_constraint(op.f('ck_lesson_kind_content'), 'lesson', type_='check')
    op.create_check_constraint(op.f('ck_lesson_kind_content'), 'lesson', f"{FILLED} OR {STUB}")


def downgrade() -> None:
    op.drop_constraint(op.f('ck_lesson_kind_content'), 'lesson', type_='check')
    op.create_check_constraint(op.f('ck_lesson_kind_content'), 'lesson', FILLED)
