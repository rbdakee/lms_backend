"""lesson regenerate and suggestions

Пересборка комплекта под новые цели: `lesson_kit.personal` — комплект личный,
банк не используется. Быстрые ответы под сообщением помощника —
`lesson_ai_chat_message.suggestions`.

Revision ID: d5f1b7c2a904
Revises: c4e8a2f17b93
Create Date: 2026-10-16 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'd5f1b7c2a904'
down_revision = 'c4e8a2f17b93'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'lesson_kit',
        sa.Column('personal', sa.Boolean(), server_default='false', nullable=False),
    )
    op.add_column(
        'lesson_ai_chat_message',
        sa.Column('suggestions', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
