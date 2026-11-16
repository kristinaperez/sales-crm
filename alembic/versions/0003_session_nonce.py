"""session nonce

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16 16:48:46.477594
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('session_nonce', sa.String(length=40), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('session_nonce')

