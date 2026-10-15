# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""mcp tables

MCP: работа админа из внешнего ИИ (MCP_BRIEF, решение владельца 15.10.2026).
Четыре новые таблицы: клиенты OAuth, коды авторизации, подключения (OAuth и
личные токены в одной таблице), журнал вызовов.

Таблицы чата ассистента (`assistant_*`) из моделей убраны, но в базе остаются
вместе с историей: autogenerate предлагал их дропнуть, и это вычеркнуто нарочно —
необратимое удаление отдельным решением владельца. Живых данных миграция
не трогает.

Revision ID: 8c3f1a9d5e27
Revises: 5a4722bb70e4
Create Date: 2026-10-15 15:29:21.586070

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '8c3f1a9d5e27'
down_revision = '5a4722bb70e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('mcp_client',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('redirect_uris', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mcp_client')),
    sa.UniqueConstraint('client_id', name=op.f('uq_mcp_client_client_id'))
    )
    op.create_table('mcp_auth_code',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('code_hash', sa.Text(), nullable=False),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('admin_id', sa.BigInteger(), nullable=False),
    sa.Column('redirect_uri', sa.Text(), nullable=False),
    sa.Column('code_challenge', sa.Text(), nullable=False),
    sa.Column('pd_allowed', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['admin_id'], ['user.id'], name=op.f('fk_mcp_auth_code_admin_id_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mcp_auth_code')),
    sa.UniqueConstraint('code_hash', name=op.f('uq_mcp_auth_code_code_hash'))
    )
    op.create_table('mcp_connection',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('admin_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('client_id', sa.Text(), nullable=True),
    sa.Column('pd_allowed', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('access_hash', sa.Text(), nullable=False),
    sa.Column('access_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('refresh_hash', sa.Text(), nullable=True),
    sa.Column('refresh_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('oauth', 'token')", name=op.f('ck_mcp_connection_kind')),
    sa.ForeignKeyConstraint(['admin_id'], ['user.id'], name=op.f('fk_mcp_connection_admin_id_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mcp_connection')),
    sa.UniqueConstraint('access_hash', name=op.f('uq_mcp_connection_access_hash')),
    sa.UniqueConstraint('refresh_hash', name=op.f('uq_mcp_connection_refresh_hash'))
    )
    op.create_index(op.f('ix_mcp_connection_admin_id'), 'mcp_connection', ['admin_id'], unique=False)
    op.create_table('mcp_call_log',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('connection_id', sa.BigInteger(), nullable=False),
    sa.Column('tool', sa.Text(), nullable=False),
    sa.Column('args', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('error_code', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['connection_id'], ['mcp_connection.id'], name=op.f('fk_mcp_call_log_connection_id_mcp_connection')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mcp_call_log'))
    )
    op.create_index(op.f('ix_mcp_call_log_connection_id'), 'mcp_call_log', ['connection_id'], unique=False)
