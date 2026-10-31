"""数据集发布的总字符数和各划分字符数，回填已有发布。"""

from pathlib import Path

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).parent / "0005_release_char_count.sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    op.drop_constraint("release_char_count_sum", "dataset_release", type_="check")
    for column in ("char_count", "train_char_count", "val_char_count", "test_char_count"):
        op.drop_column("dataset_release", column)
