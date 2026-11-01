"""正文 token 缓存与固定发布统计。"""
from pathlib import Path
from alembic import op
revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix('.sql').read_text())

def downgrade():
    op.drop_table('release_token_stats')
    op.drop_table('text_token_count')
