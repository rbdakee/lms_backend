# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""lesson_ai access usage bank

Доступ, учёт расхода и банк готовых материалов «Жоспара» (TEACHER_AI_BRIEF,
разделы 7 и 8; сессия L7).

1. `lesson_ai_access` — доступ к продукту, отдельный от доступа к курсам: строка
   на учителя, кто выдал, срок по дату включительно (пусто — бессрочно),
   отметка оплаты, снятие.
2. `lesson_ai_usage` — расход по учителю: строка на каждое оплаченное обращение
   к модели — генерация материала, перегенерация блока, ход чата.
3. `lesson_material_bank` — обезличенные материалы по ключу цель + вид + язык +
   оба уровня (+ вариант плана у видов, которые строятся по плану).
4. `lesson_material.bank_id` — какой вариант банка достался учителю.

Только новые таблицы и новая пустая колонка: существующие строки не трогаются.

Revision ID: 5a4722bb70e4
Revises: 3a549b02f794
Create Date: 2026-09-23 01:32:46.271595

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '5a4722bb70e4'
down_revision = '3a549b02f794'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('lesson_ai_access',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('granted_by', sa.BigInteger(), nullable=False),
    sa.Column('granted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_on', sa.Date(), nullable=True),
    sa.Column('paid_note', sa.Text(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by', sa.BigInteger(), nullable=True),
    sa.ForeignKeyConstraint(['granted_by'], ['user.id'], name=op.f('fk_lesson_ai_access_granted_by_user')),
    sa.ForeignKeyConstraint(['revoked_by'], ['user.id'], name=op.f('fk_lesson_ai_access_revoked_by_user')),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_lesson_ai_access_user_id_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_ai_access')),
    sa.UniqueConstraint('user_id', name=op.f('uq_lesson_ai_access_user_id'))
    )
    op.create_table('lesson_material_bank',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('objective_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('lang', sa.Text(), nullable=False),
    sa.Column('engagement_level', sa.Text(), nullable=False),
    sa.Column('difficulty_level', sa.Text(), nullable=False),
    sa.Column('plan_bank_id', sa.BigInteger(), nullable=True),
    sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('block_order', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('mechanics', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("difficulty_level IN ('basic', 'standard', 'advanced')", name=op.f('ck_lesson_material_bank_difficulty_level')),
    sa.CheckConstraint("engagement_level IN ('calm', 'active', 'full_game')", name=op.f('ck_lesson_material_bank_engagement_level')),
    sa.CheckConstraint("kind IN ('lesson_plan', 'presentation', 'tasks', 'homework', 'assessment', 'game')", name=op.f('ck_lesson_material_bank_kind')),
    sa.CheckConstraint("lang IN ('ru', 'kz')", name=op.f('ck_lesson_material_bank_lang')),
    sa.ForeignKeyConstraint(['objective_id'], ['learning_objective.id'], name=op.f('fk_lesson_material_bank_objective_id_learning_objective')),
    sa.ForeignKeyConstraint(['plan_bank_id'], ['lesson_material_bank.id'], name=op.f('fk_lesson_material_bank_plan_bank_id_lesson_material_bank')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_material_bank'))
    )
    op.create_index('ix_lesson_material_bank_key', 'lesson_material_bank', ['objective_id', 'kind', 'lang', 'engagement_level', 'difficulty_level', 'id'], unique=False)
    op.create_table('lesson_ai_usage',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('kit_id', sa.BigInteger(), nullable=True),
    sa.Column('chat_id', sa.BigInteger(), nullable=True),
    sa.Column('kind', sa.Text(), nullable=True),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('tokens_in', sa.Integer(), server_default='0', nullable=False),
    sa.Column('tokens_out', sa.Integer(), server_default='0', nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('material', 'block', 'chat')", name=op.f('ck_lesson_ai_usage_source')),
    sa.ForeignKeyConstraint(['chat_id'], ['lesson_ai_chat.id'], name=op.f('fk_lesson_ai_usage_chat_id_lesson_ai_chat'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['kit_id'], ['lesson_kit.id'], name=op.f('fk_lesson_ai_usage_kit_id_lesson_kit'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_lesson_ai_usage_user_id_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_ai_usage'))
    )
    op.create_index('ix_lesson_ai_usage_created_at', 'lesson_ai_usage', ['created_at'], unique=False)
    op.create_index('ix_lesson_ai_usage_user_id_created_at', 'lesson_ai_usage', ['user_id', 'created_at'], unique=False)
    op.add_column('lesson_material', sa.Column('bank_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_lesson_material_bank_id'), 'lesson_material', ['bank_id'], unique=False)
    op.create_foreign_key(op.f('fk_lesson_material_bank_id_lesson_material_bank'), 'lesson_material', 'lesson_material_bank', ['bank_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint(op.f('fk_lesson_material_bank_id_lesson_material_bank'), 'lesson_material', type_='foreignkey')
    op.drop_index(op.f('ix_lesson_material_bank_id'), table_name='lesson_material')
    op.drop_column('lesson_material', 'bank_id')
    op.drop_index('ix_lesson_ai_usage_user_id_created_at', table_name='lesson_ai_usage')
    op.drop_index('ix_lesson_ai_usage_created_at', table_name='lesson_ai_usage')
    op.drop_table('lesson_ai_usage')
    op.drop_index('ix_lesson_material_bank_key', table_name='lesson_material_bank')
    op.drop_table('lesson_material_bank')
    op.drop_table('lesson_ai_access')
