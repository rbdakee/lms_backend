# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""assistant tables

Ассистент в админке (ASSISTANT_BRIEF, решение владельца 09.09.2026).
Пять таблиц одной ревизией: порознь они не нужны, а раздавать миграцию
субагентам — значит ломать порядок ревизий соседу.

1. `assistant_thread` — чат админа. Чужие чаты не видны, заголовок пишет
   сам ассистент, курс закрепляется у чата (раздел 8).
2. `assistant_message` — лента: текст, карточка вызова инструмента, токены.
   Персданные сюда не ложатся даже с допуском (раздел 5).
3. `assistant_memory` — общая память, устройство как у MEMORY.md (раздел 7).
4. `assistant_proposal` — предложение опасного действия: аргументы берёт
   `execute`, а не тело запроса (раздел 6).
5. `assistant_pd_grant` — допуск к данным одного человека в одном чате
   (раздел 5).

Таблицы новые, живых данных не трогают: ни одной существующей строки
не переписывается и ни одной колонки не удаляется.

Revision ID: f7b2e91a4c60
Revises: d4a17f0c62b9
Create Date: 2026-09-09 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'f7b2e91a4c60'
down_revision = 'd4a17f0c62b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'assistant_thread',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('admin_id', sa.BigInteger(), nullable=False),
        sa.Column('title', sa.Text(), server_default='', nullable=False),
        sa.Column('course_id', sa.BigInteger(), nullable=True),
        sa.Column('archived', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['admin_id'], ['user.id'], name=op.f('fk_assistant_thread_admin_id_user')),
        # SET NULL, а не запрет: удаление курса иначе роняло бы 500 всякий раз,
        # когда этот курс закреплён в чьём-то чате.
        sa.ForeignKeyConstraint(['course_id'], ['course.id'], name=op.f('fk_assistant_thread_course_id_course'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_assistant_thread')),
    )
    op.create_index(op.f('ix_assistant_thread_admin_id'), 'assistant_thread', ['admin_id'])

    # Предложение заводится раньше ленты: на него ссылается карточка вызова.
    op.create_table(
        'assistant_proposal',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('thread_id', sa.BigInteger(), nullable=False),
        sa.Column('tool_name', sa.Text(), nullable=False),
        sa.Column('tool_args', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('what_happens', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('executed_by', sa.BigInteger(), nullable=True),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['executed_by'], ['user.id'], name=op.f('fk_assistant_proposal_executed_by_user')),
        sa.ForeignKeyConstraint(
            ['thread_id'], ['assistant_thread.id'],
            name=op.f('fk_assistant_proposal_thread_id_assistant_thread'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_assistant_proposal')),
    )
    op.create_index(op.f('ix_assistant_proposal_thread_id'), 'assistant_proposal', ['thread_id'])

    op.create_table(
        'assistant_message',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('thread_id', sa.BigInteger(), nullable=False),
        sa.Column('role', sa.Text(), nullable=False),
        sa.Column('content', sa.Text(), server_default='', nullable=False),
        sa.Column('tool_name', sa.Text(), nullable=True),
        sa.Column('tool_args', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('tool_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('tool_call_id', sa.Text(), nullable=True),
        sa.Column('proposal_id', sa.BigInteger(), nullable=True),
        sa.Column('tokens_in', sa.Integer(), server_default='0', nullable=False),
        sa.Column('tokens_out', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant', 'tool')", name=op.f('ck_assistant_message_role')),
        sa.ForeignKeyConstraint(
            ['proposal_id'], ['assistant_proposal.id'],
            name=op.f('fk_assistant_message_proposal_id_assistant_proposal'), ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['thread_id'], ['assistant_thread.id'],
            name=op.f('fk_assistant_message_thread_id_assistant_thread'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_assistant_message')),
    )
    # Лента читается всегда одним запросом: свой чат, по порядку. Отдельного
    # индекса по одному thread_id нет — этот покрывает и его.
    op.create_index('ix_assistant_message_thread_id_id', 'assistant_message', ['thread_id', 'id'])

    op.create_table(
        'assistant_memory',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('scope', sa.Text(), nullable=False),
        sa.Column('course_id', sa.BigInteger(), nullable=True),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('body', sa.Text(), server_default='', nullable=False),
        sa.Column('author_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("scope IN ('global', 'course')", name=op.f('ck_assistant_memory_scope')),
        sa.CheckConstraint(
            "(scope = 'global' AND course_id IS NULL)"
            " OR (scope = 'course' AND course_id IS NOT NULL)",
            name=op.f('ck_assistant_memory_scope_course'),
        ),
        sa.ForeignKeyConstraint(['author_id'], ['user.id'], name=op.f('fk_assistant_memory_author_id_user')),
        # CASCADE: память про удалённый курс не значит ничего.
        sa.ForeignKeyConstraint(['course_id'], ['course.id'], name=op.f('fk_assistant_memory_course_id_course'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_assistant_memory')),
    )
    op.create_index(op.f('ix_assistant_memory_course_id'), 'assistant_memory', ['course_id'])
    # Уникальность двумя частичными индексами: у общей записи course_id пустой,
    # а Postgres считает NULL-ы различными — общий UNIQUE не запретил бы
    # двух записей с одним именем.
    op.create_index(
        'uq_assistant_memory_global_name', 'assistant_memory', ['name'],
        unique=True, postgresql_where=sa.text("scope = 'global'"),
    )
    op.create_index(
        'uq_assistant_memory_course_name', 'assistant_memory', ['course_id', 'name'],
        unique=True, postgresql_where=sa.text("scope = 'course'"),
    )

    op.create_table(
        'assistant_pd_grant',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('thread_id', sa.BigInteger(), nullable=False),
        sa.Column('subject_user_id', sa.BigInteger(), nullable=False),
        sa.Column('level', sa.Text(), nullable=False),
        sa.Column('granted_by', sa.BigInteger(), nullable=False),
        sa.Column('granted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("level IN ('read', 'write')", name=op.f('ck_assistant_pd_grant_level')),
        sa.ForeignKeyConstraint(['granted_by'], ['user.id'], name=op.f('fk_assistant_pd_grant_granted_by_user')),
        sa.ForeignKeyConstraint(['subject_user_id'], ['user.id'], name=op.f('fk_assistant_pd_grant_subject_user_id_user')),
        sa.ForeignKeyConstraint(
            ['thread_id'], ['assistant_thread.id'],
            name=op.f('fk_assistant_pd_grant_thread_id_assistant_thread'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_assistant_pd_grant')),
        sa.UniqueConstraint('thread_id', 'subject_user_id', name=op.f('uq_assistant_pd_grant_thread_id_subject_user_id')),
    )
    op.create_index(op.f('ix_assistant_pd_grant_thread_id'), 'assistant_pd_grant', ['thread_id'])
    op.create_index(op.f('ix_assistant_pd_grant_subject_user_id'), 'assistant_pd_grant', ['subject_user_id'])
