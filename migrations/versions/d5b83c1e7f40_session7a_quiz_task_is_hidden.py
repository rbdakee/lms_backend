# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""session7a quiz and task is_hidden

Скрытие теста и задания. Правило «элемент с чужими данными не удаляется,
а скрывается» (BACKEND_NOTES, раздел 10) одно на все три вида элемента,
но работало только для урока: колонки у теста и задания не было, и тест
с попытками прятать было нечем — оставалось только удалить его вместе
с чужими баллами.

server_default false, а не просто default: колонка добавляется в курсы,
где уже учатся, и существующие тесты с заданиями обязаны остаться видимыми.

Revision ID: d5b83c1e7f40
Revises: c4f9d2b7a318
Create Date: 2026-08-19 16:40:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'd5b83c1e7f40'
down_revision = 'c4f9d2b7a318'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('quiz', sa.Column('is_hidden', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('task', sa.Column('is_hidden', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('quiz', 'is_hidden')
    op.drop_column('task', 'is_hidden')
