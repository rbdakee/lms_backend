# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""quiz_attempt active and submission pending unique indexes

Гонки сессии 5 закрываются базой, а не проверками в коде: две активные
попытки одного теста и две работы «на проверке» по одному заданию
не должны существовать даже на миг между check и insert.

Revision ID: e3c40b7d9a12
Revises: b1aee2d4a474
Create Date: 2026-08-18 19:20:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'e3c40b7d9a12'
down_revision = 'b1aee2d4a474'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'uq_quiz_attempt_active',
        'quiz_attempt',
        ['user_id', 'quiz_id'],
        unique=True,
        postgresql_where=sa.text('finished_at IS NULL'),
    )
    op.create_index(
        'uq_submission_pending',
        'submission',
        ['user_id', 'task_id'],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index('uq_submission_pending', table_name='submission')
    op.drop_index('uq_quiz_attempt_active', table_name='quiz_attempt')
