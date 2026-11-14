"""存储基础：文本编解码与数据库连接。"""

from contextlib import contextmanager

import psycopg


@contextmanager
def reader(conn):
    """按 conn.info.dsn 打开只读的第二条连接，用于在执行连接写入时并行读取已提交数据。"""
    with psycopg.connect(conn.info.dsn) as connection:
        connection.read_only = True
        yield connection
