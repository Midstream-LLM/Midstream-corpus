"""固定 OpenCC 字符表第一候选；不处理词组、标点、空白或段落。"""
import hashlib
from functools import cache
from pathlib import Path


@cache
def table():
    data = (Path(__file__).with_name('data') / 'TSCharacters.txt').read_bytes()
    mapping = {}
    for line in data.decode('utf-8').splitlines():
        if line and not line.startswith('#'):
            source, target, *_ = line.split()
            mapping[ord(source)] = target
    return mapping, hashlib.sha256(data).hexdigest(), all(len(value) == 1 for value in mapping.values())


def simplify(text):
    return text.translate(table()[0])


def info():
    _, fingerprint, equal_length = table()
    return {'table_sha256': fingerprint, 'length_preserving': equal_length}
