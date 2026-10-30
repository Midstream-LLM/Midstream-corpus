"""第一版 schema。"""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).parent / "0001_initial.sql").read_text(encoding="utf-8")
    bind = op.get_bind()
    for statement in sql.split(";\n"):
        if statement.strip():
            bind.exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("初始 schema 不提供回退")
