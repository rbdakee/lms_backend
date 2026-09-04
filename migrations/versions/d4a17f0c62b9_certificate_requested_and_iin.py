# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""certificate requested and iin

Сертификат перестаёт выписываться сам: учитель просит, документ выдаёт админ
руками (CERTIFICATES_BRIEF). Отдельной таблицы заявок нет — заявка это та же
строка `certificate` до выдачи, и три её состояния различаются колонками:
заявка (ни номера, ни даты выдачи), выданный (есть оба), отозванный (стоит
`revoked_at`). Поэтому `number` и `issued_at` становятся необязательными,
а `requested_at` появляется у всех строк сразу.

Существующим сертификатам `requested_at` ставится равным `issued_at`: дня
заявки у них не было, а выданными они остаются. Это ближайшая правда, и
очередь, отсортированная по дате запроса, на них не разъезжается.

`registration_number` — номер академии, который админ вводит руками. У всех
документов до 04.09.2026 он пустой: их админ проставит на новой странице,
когда дойдут руки.

`issued_by` и `revoked_by` — actor_id у действия админа, как у
`enrollment.granted_by` и `review.reply_by` (backend/CLAUDE.md).

`user.iin` — 12 цифр строкой, ведущий ноль значащий. Всем, кто
зарегистрировался раньше, ставится метка «не заполнен»: настоящий ИИН из
одних нулей не бывает. Уникальность на неё не распространяется — иначе
второй же старый аккаунт перестал бы сохраняться.

Вниз не откатывается: у заявок нет ни номера, ни даты выдачи, и вернуть этим
колонкам NOT NULL нечем — откат упал бы на первой же заявке, успев потерять
регистрационные номера. Чиним новой ревизией.

Revision ID: d4a17f0c62b9
Revises: e6a41d0c7b58
Create Date: 2026-09-04 11:20:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'd4a17f0c62b9'
down_revision = 'e6a41d0c7b58'
branch_labels = None
depends_on = None

# Метка «не заполнен» написана строкой нарочно: миграция описывает состояние
# базы на свою дату и не должна меняться вместе с константой в домене.
IIN_PLACEHOLDER = '000000000000'


def upgrade() -> None:
    op.add_column('user', sa.Column('iin', sa.Text(), server_default=IIN_PLACEHOLDER, nullable=False))
    # Частичный: под заглушкой сидят все, кто зарегистрировался раньше
    op.create_index('uq_user_iin', 'user', ['iin'], unique=True,
                    postgresql_where=sa.text(f"iin <> '{IIN_PLACEHOLDER}'"))

    op.add_column('certificate', sa.Column('registration_number', sa.Text(), server_default='', nullable=False))
    op.add_column('certificate', sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True))
    # Дня заявки у выданных раньше не было — ближайшая правда это день выдачи
    op.execute('UPDATE certificate SET requested_at = issued_at')
    op.alter_column('certificate', 'requested_at', nullable=False)
    op.add_column('certificate', sa.Column('issued_by', sa.BigInteger(), nullable=True))
    op.add_column('certificate', sa.Column('revoked_by', sa.BigInteger(), nullable=True))
    op.create_foreign_key(op.f('fk_certificate_issued_by_user'), 'certificate', 'user', ['issued_by'], ['id'])
    op.create_foreign_key(op.f('fk_certificate_revoked_by_user'), 'certificate', 'user', ['revoked_by'], ['id'])

    # У заявки номера и даты выдачи нет; server_default у issued_at снимается,
    # иначе заявка получала бы дату выдачи прямо на вставке
    op.alter_column('certificate', 'number', existing_type=sa.Text(), nullable=True)
    op.alter_column('certificate', 'issued_at', existing_type=sa.DateTime(timezone=True), nullable=True, server_default=None)
    # Номер и дата выдачи появляются одним движением — выдачей
    op.create_check_constraint('issued', 'certificate', '(number IS NULL) = (issued_at IS NULL)')


def downgrade() -> None:
    # Заявка — строка без номера и без даты выдачи, и вернуть этим колонкам
    # NOT NULL нечем: откат упал бы на первой же заявке, успев потерять
    # регистрационные номера и ИИН. Чиним новой ревизией (backend/CLAUDE.md).
    raise RuntimeError('Ревизия d4a17f0c62b9 необратима: вниз не откатываемся, чиним новой')
