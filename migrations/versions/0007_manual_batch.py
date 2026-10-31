"""人工审核留痕和批次放弃状态。"""

from pathlib import Path

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        (Path(__file__).parent / "0007_manual_batch.sql").read_text(encoding="utf-8")
    )


def downgrade() -> None:
    op.drop_table("management_event")
    op.execute("UPDATE source_batch SET status = 'needs_review' WHERE status = 'abandoned'")
    op.drop_constraint("source_batch_status_check", "source_batch", type_="check")
    op.create_check_constraint(
        "source_batch_status_check", "source_batch",
        "status IN ('received', 'staged', 'validated', 'published', 'failed', 'needs_review')",
    )
