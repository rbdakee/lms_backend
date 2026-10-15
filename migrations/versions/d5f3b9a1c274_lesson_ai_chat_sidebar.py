"""lesson ai chat sidebar

Сайдбар чатов «Жоспара» как рабочий инструмент (решение владельца 15.10.2026):
личные группы чатов, закрепление, порядок внутри секции и архив. Только
добавление: у чата четыре новые колонки, группы — новая таблица.

Revision ID: d5f3b9a1c274
Revises: c4e8a2f17b93
Create Date: 2026-10-15 22:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "d5f3b9a1c274"
down_revision = "c4e8a2f17b93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lesson_ai_chat_group",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 60", name="ck_lesson_ai_chat_group_name"
        ),
    )
    op.create_index(
        "ix_lesson_ai_chat_group_user_id_position", "lesson_ai_chat_group", ["user_id", "position"]
    )
    op.add_column(
        "lesson_ai_chat", sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "lesson_ai_chat",
        sa.Column(
            "group_id",
            sa.BigInteger(),
            sa.ForeignKey("lesson_ai_chat_group.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("lesson_ai_chat", sa.Column("position", sa.Integer(), nullable=True))
    op.add_column(
        "lesson_ai_chat", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_lesson_ai_chat_user_id_archived_at", "lesson_ai_chat", ["user_id", "archived_at"]
    )
    op.create_index("ix_lesson_ai_chat_group_id", "lesson_ai_chat", ["group_id"])
