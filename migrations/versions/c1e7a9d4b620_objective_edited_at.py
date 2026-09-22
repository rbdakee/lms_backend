# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""learning_objective edited_at

Отметка «текст правили руками» у цели обучения. Нужна экрану сверки
(`CONTRACT.md`, раздел «Справочник учебной программы»): приказ пришёл неполным —
математические знаки (≠, ∩, ∪, ∅, ≥) вставлены в него картинками, и на их месте
стоит `[img]`. Знак проставляет человек, и его работа обязана быть отличима
от текста, приехавшего разбором.

Метка не украшение. Повторный импорт сносит программу целиком, вместе с целями,
и по числу строк с непустым `edited_at` сервер отбивает импорт, пока владелец
не подтвердит потерю явно.

Старым строкам достаётся `NULL` — и это верно по смыслу: до этой ревизии править
цели было нечем, значит текст у всех как в приказе.

Revision ID: c1e7a9d4b620
Revises: b9d3e04f71a2
Create Date: 2026-09-19 17:10:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'c1e7a9d4b620'
down_revision = 'b9d3e04f71a2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'learning_objective',
        sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('learning_objective', 'edited_at')
