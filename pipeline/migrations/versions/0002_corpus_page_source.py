"""read.corpus_page.source: which reader a page's text came from (text_layer | ocr).

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table read.corpus_page add column if not exists source text")


def downgrade() -> None:
    op.execute("alter table read.corpus_page drop column if exists source")
