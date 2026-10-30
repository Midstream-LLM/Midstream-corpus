"""暂存正文哈希及相同清洗正文的复用。"""

from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).parent / "0004_staging_hash.sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    op.execute("UPDATE staging_text sc SET content = st.content FROM staging_text st"
               " WHERE sc.content IS NULL AND sc.batch_id = st.batch_id"
               " AND sc.source_key = st.source_key AND st.step = 'decoded'")
    op.alter_column("staging_text", "content", nullable=False)
    op.drop_column("staging_text", "char_count")
    op.drop_column("staging_text", "text_hash")
