"""Slide presentations shown during a session

slide_decks: one row per presentation a host uploaded to a session. The slide
pictures live in file storage (see slides.py), not in the database. Purely
additive; nothing is dropped.

Revision ID: a7d3f9c1b248
Revises: f6c9d2e4b857
Create Date: 2026-11-21 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a7d3f9c1b248'
down_revision = 'f6c9d2e4b857'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'slide_decks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False, server_default='processing'),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.Column('page_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_slide_decks_meeting_id', 'slide_decks', ['meeting_id'], unique=False)


def downgrade():
    op.drop_index('ix_slide_decks_meeting_id', table_name='slide_decks')
    op.drop_table('slide_decks')
