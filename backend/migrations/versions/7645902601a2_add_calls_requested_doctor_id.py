"""add calls requested_doctor_id

Revision ID: 7645902601a2
Revises: bcb2d41cdc1a
Create Date: 2026-09-23 20:51:12.497094

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7645902601a2'
down_revision = 'bcb2d41cdc1a'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('calls', schema=None) as batch_op:
        batch_op.add_column(sa.Column('requested_doctor_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_calls_requested_doctor_id', 'doctors', ['requested_doctor_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('calls', schema=None) as batch_op:
        batch_op.drop_constraint('fk_calls_requested_doctor_id', type_='foreignkey')
        batch_op.drop_column('requested_doctor_id')
