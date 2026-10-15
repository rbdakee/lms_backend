# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""lesson_ai chat events

«Жоспар» как рабочее место (решение владельца 15.10.2026): чат живёт и после
генерации комплекта, и в его ленте появляются события — «комплект готовится»,
«готово», «изменён слайд 3». Событие — строка ленты с ролью `event` и объектом
`event` с подробностями; прежние строки не меняются.

Revision ID: b3d91e6a4c58
Revises: 8c3f1a9d5e27
Create Date: 2026-10-15 18:10:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'b3d91e6a4c58'
down_revision = '8c3f1a9d5e27'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'lesson_ai_chat_message',
        sa.Column('event', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_constraint(
        op.f('ck_lesson_ai_chat_message_role'), 'lesson_ai_chat_message', type_='check'
    )
    op.create_check_constraint(
        op.f('ck_lesson_ai_chat_message_role'),
        'lesson_ai_chat_message',
        "role IN ('user', 'assistant', 'tool', 'event')",
    )
