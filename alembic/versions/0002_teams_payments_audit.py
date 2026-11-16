"""teams payments audit

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16 10:58:29.548250
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('teams',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_teams')),
    sa.UniqueConstraint('name', name=op.f('uq_teams_name'))
    )
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('entity', sa.String(length=40), nullable=False),
    sa.Column('entity_id', sa.Integer(), nullable=True),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_audit_log_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log'))
    )
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_log_created_at'), ['created_at'], unique=False)

    op.create_table('payments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('deal_id', sa.Integer(), nullable=False),
    sa.Column('amount_minor', sa.Integer(), nullable=False),
    sa.Column('paid_on', sa.Date(), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.Column('credited_to_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('confirmed_by_id', sa.Integer(), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['confirmed_by_id'], ['users.id'], name=op.f('fk_payments_confirmed_by_id_users')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_payments_created_by_id_users')),
    sa.ForeignKeyConstraint(['credited_to_id'], ['users.id'], name=op.f('fk_payments_credited_to_id_users')),
    sa.ForeignKeyConstraint(['deal_id'], ['deals.id'], name=op.f('fk_payments_deal_id_deals'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payments'))
    )
    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payments_credited_to_id'), ['credited_to_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payments_deal_id'), ['deal_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payments_paid_on'), ['paid_on'], unique=False)
        batch_op.create_index(batch_op.f('ix_payments_status'), ['status'], unique=False)

    with op.batch_alter_table('import_batches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deal_owner_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_import_batches_deal_owner_id_users'), 'users', ['deal_owner_id'], ['id'])

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('team_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_team_id'), ['team_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_users_team_id_teams'), 'teams', ['team_id'], ['id'], ondelete='SET NULL')

    # Прежняя роль «руководитель» становится владельцем: полные права на всю базу.
    op.execute("UPDATE users SET role = 'owner' WHERE role = 'head'")


def downgrade() -> None:
    op.execute("UPDATE users SET role = 'head' WHERE role = 'owner'")
    op.execute("UPDATE users SET role = 'manager' WHERE role = 'lead'")
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_users_team_id_teams'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_users_team_id'))
        batch_op.drop_column('team_id')

    with op.batch_alter_table('import_batches', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_import_batches_deal_owner_id_users'), type_='foreignkey')
        batch_op.drop_column('deal_owner_id')

    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_payments_status'))
        batch_op.drop_index(batch_op.f('ix_payments_paid_on'))
        batch_op.drop_index(batch_op.f('ix_payments_deal_id'))
        batch_op.drop_index(batch_op.f('ix_payments_credited_to_id'))

    op.drop_table('payments')
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_log_created_at'))

    op.drop_table('audit_log')
    op.drop_table('teams')
