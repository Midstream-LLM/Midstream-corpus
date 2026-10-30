"""段落级重复快照。"""

from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).parent / "0003_dup_span.sql").read_text(encoding="utf-8")
    bind = op.get_bind()
    for statement in sql.split(";\n"):
        if statement.strip():
            bind.exec_driver_sql(statement)


def downgrade() -> None:
    op.drop_table("dup_span")
