# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""lesson_ai chat

Чат сбора заказа «Жоспара» (TEACHER_AI_BRIEF, разделы 1 и 8; сессия L6).

1. `lesson_ai_chat` — заказ колонками: цель ссылкой на строку справочника, язык
   обучения, тема, что учитель сказал про уровни, список материалов и класс.
   Предмета и класса нет — их знает цель. После подтверждения — `kit_id`
   заведённого комплекта и `status = 'confirmed'`.
2. `lesson_ai_chat_message` — лента той же формы, что у ассистента: карточка
   вызова (`role = 'tool'`) хранит найденные цели, и экран рисует их строками
   справочника.

Только новые таблицы: существующие строки не трогаются.

Revision ID: 3a549b02f794
Revises: 1833bbf87793
Create Date: 2026-09-23 00:52:35.677822

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '3a549b02f794'
down_revision = '1833bbf87793'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('lesson_ai_chat',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.Text(), server_default='collecting', nullable=False),
    sa.Column('objective_id', sa.BigInteger(), nullable=True),
    sa.Column('lang', sa.Text(), nullable=True),
    sa.Column('topic', sa.Text(), nullable=True),
    sa.Column('engagement_level', sa.Text(), nullable=True),
    sa.Column('difficulty_level', sa.Text(), nullable=True),
    sa.Column('kinds', postgresql.ARRAY(sa.Text()), nullable=True),
    sa.Column('class_size', sa.Integer(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('kit_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("difficulty_level IN ('basic', 'standard', 'advanced')", name=op.f('ck_lesson_ai_chat_difficulty_level')),
    sa.CheckConstraint("engagement_level IN ('calm', 'active', 'full_game')", name=op.f('ck_lesson_ai_chat_engagement_level')),
    sa.CheckConstraint("lang IN ('ru', 'kz')", name=op.f('ck_lesson_ai_chat_lang')),
    sa.CheckConstraint("status IN ('collecting', 'confirmed')", name=op.f('ck_lesson_ai_chat_status')),
    sa.CheckConstraint('class_size BETWEEN 1 AND 60', name=op.f('ck_lesson_ai_chat_class_size')),
    sa.ForeignKeyConstraint(['kit_id'], ['lesson_kit.id'], name=op.f('fk_lesson_ai_chat_kit_id_lesson_kit'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['objective_id'], ['learning_objective.id'], name=op.f('fk_lesson_ai_chat_objective_id_learning_objective')),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_lesson_ai_chat_user_id_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_ai_chat')),
    sa.UniqueConstraint('kit_id', name=op.f('uq_lesson_ai_chat_kit_id'))
    )
    op.create_index('ix_lesson_ai_chat_user_id_id', 'lesson_ai_chat', ['user_id', 'id'], unique=False)
    op.create_table('lesson_ai_chat_message',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('role', sa.Text(), nullable=False),
    sa.Column('content', sa.Text(), server_default='', nullable=False),
    sa.Column('tool_name', sa.Text(), nullable=True),
    sa.Column('tool_args', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('tool_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('tool_call_id', sa.Text(), nullable=True),
    sa.Column('tokens_in', sa.Integer(), server_default='0', nullable=False),
    sa.Column('tokens_out', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('user', 'assistant', 'tool')", name=op.f('ck_lesson_ai_chat_message_role')),
    sa.ForeignKeyConstraint(['chat_id'], ['lesson_ai_chat.id'], name=op.f('fk_lesson_ai_chat_message_chat_id_lesson_ai_chat'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_ai_chat_message'))
    )
    op.create_index('ix_lesson_ai_chat_message_chat_id_id', 'lesson_ai_chat_message', ['chat_id', 'id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_lesson_ai_chat_message_chat_id_id', table_name='lesson_ai_chat_message')
    op.drop_table('lesson_ai_chat_message')
    op.drop_index('ix_lesson_ai_chat_user_id_id', table_name='lesson_ai_chat')
    op.drop_table('lesson_ai_chat')
