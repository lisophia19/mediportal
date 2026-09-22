"""add doctor gender column

Revision ID: bcb2d41cdc1a
Revises: 7a600a9a551e
Create Date: 2026-09-22 09:54:42.923033

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'bcb2d41cdc1a'
down_revision = '7a600a9a551e'
branch_labels = None
depends_on = None


def upgrade():
    # Autogenerate doesn't detect CHECK constraints -- added by hand here,
    # same as every other status/enum-style column in this schema.
    with op.batch_alter_table('doctors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gender', sa.Text(), nullable=True))
        batch_op.create_check_constraint('ck_doctor_gender', "gender IN ('male', 'female')")


def downgrade():
    with op.batch_alter_table('doctors', schema=None) as batch_op:
        batch_op.drop_constraint('ck_doctor_gender', type_='check')
        batch_op.drop_column('gender')
