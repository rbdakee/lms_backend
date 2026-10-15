# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""lesson multi objectives

Урок идёт по нескольким целям обучения (решение владельца 15.10.2026): у комплекта,
заказа чата и варианта банка вместо одной `objective_id` — упорядоченный список
в таблице связи. Существующая цель переезжает на позицию 1, старые колонки
удаляются: на них ничего больше не держится (индексы уходят вместе с колонкой).

Ключ банка — `objective_key`, id целей по возрастанию через запятую. Порядок
в ключ не входит: банк выдаёт на совпадение набора, а порядок целей в документе
при выдаче ставится по комплекту учителя.

Revision ID: c4e8a2f17b93
Revises: b3d91e6a4c58
Create Date: 2026-10-15 20:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'c4e8a2f17b93'
down_revision = 'b3d91e6a4c58'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- комплект ---------------------------------------------------------
    op.create_table(
        'lesson_kit_objective',
        sa.Column('kit_id', sa.BigInteger(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('objective_id', sa.BigInteger(), nullable=False),
        sa.CheckConstraint('position >= 1', name=op.f('ck_lesson_kit_objective_position')),
        sa.ForeignKeyConstraint(
            ['kit_id'], ['lesson_kit.id'],
            name=op.f('fk_lesson_kit_objective_kit_id_lesson_kit'), ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['objective_id'], ['learning_objective.id'],
            name=op.f('fk_lesson_kit_objective_objective_id_learning_objective'),
        ),
        sa.PrimaryKeyConstraint('kit_id', 'position', name=op.f('pk_lesson_kit_objective')),
        sa.UniqueConstraint(
            'kit_id', 'objective_id', name=op.f('uq_lesson_kit_objective_kit_id_objective_id')
        ),
    )
    op.create_index(
        op.f('ix_lesson_kit_objective_objective_id'), 'lesson_kit_objective', ['objective_id']
    )
    op.execute(
        "INSERT INTO lesson_kit_objective (kit_id, position, objective_id)"
        " SELECT id, 1, objective_id FROM lesson_kit"
    )
    op.drop_column('lesson_kit', 'objective_id')

    # -- заказ чата -------------------------------------------------------
    op.create_table(
        'lesson_ai_chat_objective',
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('objective_id', sa.BigInteger(), nullable=False),
        sa.CheckConstraint('position >= 1', name=op.f('ck_lesson_ai_chat_objective_position')),
        sa.ForeignKeyConstraint(
            ['chat_id'], ['lesson_ai_chat.id'],
            name=op.f('fk_lesson_ai_chat_objective_chat_id_lesson_ai_chat'), ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['objective_id'], ['learning_objective.id'],
            name=op.f('fk_lesson_ai_chat_objective_objective_id_learning_objective'),
        ),
        sa.PrimaryKeyConstraint('chat_id', 'position', name=op.f('pk_lesson_ai_chat_objective')),
        sa.UniqueConstraint(
            'chat_id', 'objective_id',
            name=op.f('uq_lesson_ai_chat_objective_chat_id_objective_id'),
        ),
    )
    op.execute(
        "INSERT INTO lesson_ai_chat_objective (chat_id, position, objective_id)"
        " SELECT id, 1, objective_id FROM lesson_ai_chat WHERE objective_id IS NOT NULL"
    )
    op.drop_column('lesson_ai_chat', 'objective_id')
    op.add_column(
        'lesson_ai_chat',
        sa.Column(
            'many_objectives_confirmed', sa.Boolean(), server_default=sa.text('false'),
            nullable=False,
        ),
    )

    # -- банк -------------------------------------------------------------
    op.create_table(
        'lesson_material_bank_objective',
        sa.Column('bank_id', sa.BigInteger(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('objective_id', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ['bank_id'], ['lesson_material_bank.id'],
            name=op.f('fk_lesson_material_bank_objective_bank_id_lesson_material_bank'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['objective_id'], ['learning_objective.id'],
            name=op.f('fk_lesson_material_bank_objective_objective_id_learning_objective'),
        ),
        sa.PrimaryKeyConstraint(
            'bank_id', 'position', name=op.f('pk_lesson_material_bank_objective')
        ),
    )
    op.create_index(
        op.f('ix_lesson_material_bank_objective_objective_id'),
        'lesson_material_bank_objective',
        ['objective_id'],
    )
    op.add_column('lesson_material_bank', sa.Column('objective_key', sa.Text(), nullable=True))
    op.execute("UPDATE lesson_material_bank SET objective_key = objective_id::text")
    op.alter_column('lesson_material_bank', 'objective_key', nullable=False)
    op.execute(
        "INSERT INTO lesson_material_bank_objective (bank_id, position, objective_id)"
        " SELECT id, 1, objective_id FROM lesson_material_bank"
    )
    op.drop_index('ix_lesson_material_bank_key', table_name='lesson_material_bank')
    op.drop_column('lesson_material_bank', 'objective_id')
    op.create_index(
        'ix_lesson_material_bank_key',
        'lesson_material_bank',
        ['objective_key', 'kind', 'lang', 'engagement_level', 'difficulty_level', 'id'],
    )
