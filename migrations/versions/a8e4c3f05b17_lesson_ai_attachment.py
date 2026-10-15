"""lesson ai attachment

Вложения в чат «Жоспара» (решение владельца 16.10.2026): фото, PDF, .docx
и .pptx, приложенные к чату скрепкой или перетаскиванием. Новая таблица;
кроме неё — расход разбора вложения в учёте: `lesson_ai_usage.source`
принимает новое значение `attachment`, и его CHECK переписывается.

Только добавление: ни одной колонки не меняем, вниз не откатываемся
(backend/CLAUDE.md, «Миграции»).

Revision ID: a8e4c3f05b17
Revises: d5f3b9a1c274
Create Date: 2026-10-16 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a8e4c3f05b17"
down_revision = "d5f3b9a1c274"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lesson_ai_attachment",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("lesson_ai_chat.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("lesson_ai_chat_message.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default="processing", nullable=False
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('image', 'pdf', 'docx', 'pptx')", name="ck_lesson_ai_attachment_kind"
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'ready', 'failed')",
            name="ck_lesson_ai_attachment_status",
        ),
    )
    op.create_index(
        "ix_lesson_ai_attachment_chat_id_id", "lesson_ai_attachment", ["chat_id", "id"]
    )
    op.create_index("ix_lesson_ai_attachment_message_id", "lesson_ai_attachment", ["message_id"])
    # Разбор вложения — новый источник расхода: список CHECK расширяется.
    # Constraint пересоздаётся: Postgres CHECK не расширяет на месте.
    op.drop_constraint(
        op.f("ck_lesson_ai_usage_source"), "lesson_ai_usage", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_lesson_ai_usage_source"),
        "lesson_ai_usage",
        "source IN ('material', 'block', 'chat', 'attachment')",
    )
