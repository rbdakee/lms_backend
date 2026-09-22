# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""lesson_ai generation

Каркас генерации «Жоспара», помощника учителя (TEACHER_AI_BRIEF, разделы 4, 7, 8;
сессия L3). Пять таблиц одной ревизией: порознь ни одна не работает.

1. `lesson_kit` — комплект к уроку: чей, по какой цели обучения, тема, класс,
   язык обучения и сводный статус материалов. Внешний ключ на цель — без каскада:
   повторный импорт программы не выдёргивает цель из-под собранного документа.
2. `lesson_material` — материал комплекта одного из семи видов, документ
   целиком в `content`. Виды в `CHECK` все семь сразу: генерируется пока
   один, но состав утверждён владельцем, и расширять его миграцией в L5 незачем.
3. `lesson_material_block` — поле верхнего уровня материала, единица правки.
4. `lesson_material_version` — история блока, начиная с первой генерации.
5. `lesson_ai_job` — очередь генерации своей таблицей, без Celery и Redis
   (раздел 7): задание на материал, статус, попытки, аренда, код ошибки.
   Два частичных индекса: что брать следующим и «одно живое задание
   на материал».

Колонки `platform` нет ни в одной таблице: продукт один на обе площадки (раздел 8).
Уровней `engagement_level` и `difficulty_level` тоже нет — они приходят с L5.

Со старыми строками не происходит ничего: таблицы новые, существующие колонки
не меняются. Данными миграция не сеет.

Revision ID: 52101890dfd7
Revises: c1e7a9d4b620
Create Date: 2026-09-22 21:14:50.594972

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '52101890dfd7'
down_revision = 'c1e7a9d4b620'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('lesson_kit',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('objective_id', sa.BigInteger(), nullable=False),
    sa.Column('topic', sa.Text(), nullable=False),
    sa.Column('grade', sa.Integer(), nullable=False),
    sa.Column('lang', sa.Text(), nullable=False),
    sa.Column('class_size', sa.Integer(), nullable=True),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('status', sa.Text(), server_default='generating', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("lang IN ('ru', 'kz')", name=op.f('ck_lesson_kit_lang')),
    sa.CheckConstraint("status IN ('generating', 'ready', 'partial', 'failed')", name=op.f('ck_lesson_kit_status')),
    sa.CheckConstraint('class_size BETWEEN 1 AND 60', name=op.f('ck_lesson_kit_class_size')),
    sa.ForeignKeyConstraint(['objective_id'], ['learning_objective.id'], name=op.f('fk_lesson_kit_objective_id_learning_objective')),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_lesson_kit_user_id_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_kit'))
    )
    op.create_index(op.f('ix_lesson_kit_objective_id'), 'lesson_kit', ['objective_id'], unique=False)
    op.create_index('ix_lesson_kit_user_id_id', 'lesson_kit', ['user_id', 'id'], unique=False)
    op.create_table('lesson_ai_job',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('kit_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='queued', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('run_after', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tokens_in', sa.Integer(), server_default='0', nullable=False),
    sa.Column('tokens_out', sa.Integer(), server_default='0', nullable=False),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('lesson_plan', 'presentation', 'tasks', 'homework', 'assessment', 'game', 'answer_key')", name=op.f('ck_lesson_ai_job_kind')),
    sa.CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name=op.f('ck_lesson_ai_job_status')),
    sa.ForeignKeyConstraint(['kit_id'], ['lesson_kit.id'], name=op.f('fk_lesson_ai_job_kit_id_lesson_kit'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_ai_job'))
    )
    op.create_index('ix_lesson_ai_job_kit_id', 'lesson_ai_job', ['kit_id'], unique=False)
    op.create_index('ix_lesson_ai_job_pending', 'lesson_ai_job', ['run_after', 'id'], unique=False, postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.create_index('uq_lesson_ai_job_active', 'lesson_ai_job', ['kit_id', 'kind'], unique=True, postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.create_table('lesson_material',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('kit_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='queued', nullable=False),
    sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('file_path', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('lesson_plan', 'presentation', 'tasks', 'homework', 'assessment', 'game', 'answer_key')", name=op.f('ck_lesson_material_kind')),
    sa.CheckConstraint("status IN ('queued', 'generating', 'ready', 'failed')", name=op.f('ck_lesson_material_status')),
    sa.ForeignKeyConstraint(['kit_id'], ['lesson_kit.id'], name=op.f('fk_lesson_material_kit_id_lesson_kit'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_material')),
    sa.UniqueConstraint('kit_id', 'kind', name=op.f('uq_lesson_material_kit_id_kind'))
    )
    op.create_table('lesson_material_block',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('material_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('order_index', sa.Integer(), nullable=False),
    sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['material_id'], ['lesson_material.id'], name=op.f('fk_lesson_material_block_material_id_lesson_material'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_material_block')),
    sa.UniqueConstraint('material_id', 'kind', name=op.f('uq_lesson_material_block_material_id_kind'))
    )
    op.create_table('lesson_material_version',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('block_id', sa.BigInteger(), nullable=False),
    sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('instruction', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('generated', 'regenerated', 'edited', 'restored')", name=op.f('ck_lesson_material_version_source')),
    sa.ForeignKeyConstraint(['block_id'], ['lesson_material_block.id'], name=op.f('fk_lesson_material_version_block_id_lesson_material_block'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_lesson_material_version_user_id_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_material_version'))
    )
    op.create_index('ix_lesson_material_version_block_id_id', 'lesson_material_version', ['block_id', 'id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_lesson_material_version_block_id_id', table_name='lesson_material_version')
    op.drop_table('lesson_material_version')
    op.drop_table('lesson_material_block')
    op.drop_table('lesson_material')
    op.drop_index('uq_lesson_ai_job_active', table_name='lesson_ai_job', postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.drop_index('ix_lesson_ai_job_pending', table_name='lesson_ai_job', postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.drop_index('ix_lesson_ai_job_kit_id', table_name='lesson_ai_job')
    op.drop_table('lesson_ai_job')
    op.drop_index('ix_lesson_kit_user_id_id', table_name='lesson_kit')
    op.drop_index(op.f('ix_lesson_kit_objective_id'), table_name='lesson_kit')
    op.drop_table('lesson_kit')
