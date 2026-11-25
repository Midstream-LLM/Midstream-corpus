"""运行配置，全部可用环境变量覆盖。"""

import os
from pathlib import Path

from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import URL

HOME = Path(os.environ.get("MIDSTREAM_HOME", "~/.local/share/midstream")).expanduser().resolve()
JISHUI_RAW = Path(os.environ["MIDSTREAM_JISHUI_RAW"]) if "MIDSTREAM_JISHUI_RAW" in os.environ else None
EXPORT_ROOT = Path(os.environ.get("MIDSTREAM_EXPORT_ROOT", HOME / "exports"))
# 段落查重等大临时文件的位置；默认与导出目录同级的 tmp（服务器上在 HDD），不放 /tmp 内存盘。
TMP_ROOT = Path(os.environ.get("MIDSTREAM_TMP", EXPORT_ROOT.parent / "tmp"))
# 扫描件 OCR 的逐页结果缓存；按引擎、文件哈希和页码保存，重跑不重复识别。
OCR_ROOT = Path(os.environ.get("MIDSTREAM_OCR_ROOT", EXPORT_ROOT.parent / "ocr"))
OCR_CONFIG = Path(os.environ.get("MIDSTREAM_OCR_CONFIG", HOME / "ocr.toml"))
WORKERS = int(os.environ.get("MIDSTREAM_WORKERS", "8"))
CHUNK_DOCS = int(os.environ.get("MIDSTREAM_CHUNK_DOCS", "2000"))


def dsn() -> str:
    if "MIDSTREAM_DSN" in os.environ:
        return os.environ["MIDSTREAM_DSN"]
    return make_conninfo(dbname="midstream", host=str(HOME / "run"))


def alembic_url() -> str:
    """Alembic 用的 SQLAlchemy URL，默认走私有实例的 socket。"""
    if "MIDSTREAM_ALEMBIC_URL" in os.environ:
        return os.environ["MIDSTREAM_ALEMBIC_URL"]
    return URL.create(
        "postgresql+psycopg", database="midstream", query={"host": str(HOME / "run")}
    ).render_as_string(hide_password=False)
