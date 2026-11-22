"""Reliable recording: who recorded, egress id, chunk counter

Adds three columns to recordings. Purely additive; nothing is dropped.

Revision ID: b9e2c4a6d013
Revises: a7d3f9c1b248
Create Date: 2026-11-22 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b9e2c4a6d013'
down_revision = 'a7d3f9c1b248'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('recordings') as batch:
        batch.add_column(sa.Column('source', sa.String(length=10), nullable=False, server_default='browser'))
        batch.add_column(sa.Column('egress_id', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('chunks_received', sa.Integer(), nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('recordings') as batch:
        batch.drop_column('chunks_received')
        batch.drop_column('egress_id')
        batch.drop_column('source')
