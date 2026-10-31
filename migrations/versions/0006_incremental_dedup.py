"""复用查重输入快照和已验证候选，保留未变化的文档簇。"""

from pathlib import Path

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).parent / "0006_incremental_dedup.sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    op.drop_table("dedup_snapshot")
    op.drop_table("dedup_pair")
    op.drop_column("dup_cluster", "anchor_version_id")
