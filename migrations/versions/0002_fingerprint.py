"""指纹按正文和规则缓存。"""

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).parent / "0002_fingerprint.sql").read_text(encoding="utf-8")
    bind = op.get_bind()
    for statement in sql.split(";\n"):
        if statement.strip():
            bind.exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("指纹缓存不提供回退")
