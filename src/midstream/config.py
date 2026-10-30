"""运行配置，全部可用环境变量覆盖。"""

import os
from pathlib import Path

HDD_ROOT = "/media/sidi762/b41354bc-9442-475d-9c51-ad8d110f2139"
JISHUI_RAW = Path(os.environ.get("MIDSTREAM_JISHUI_RAW", f"{HDD_ROOT}/Jishui/dataset/raw"))
EXPORT_ROOT = Path(os.environ.get("MIDSTREAM_EXPORT_ROOT", f"{HDD_ROOT}/midstream/exports"))
WORKERS = int(os.environ.get("MIDSTREAM_WORKERS", "8"))
CHUNK_DOCS = int(os.environ.get("MIDSTREAM_CHUNK_DOCS", "2000"))


def dsn() -> str:
    return os.environ.get("MIDSTREAM_DSN", "dbname=midstream")


def alembic_url() -> str:
    """Alembic 用的 SQLAlchemy URL，默认走本机 socket。"""
    return os.environ.get("MIDSTREAM_ALEMBIC_URL", "postgresql+psycopg:///midstream")
