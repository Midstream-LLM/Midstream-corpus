"""python -m midstream.api：只运行 API，不启动 worker。"""

import argparse
from pathlib import Path

from . import STATIC_DIR, serve


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--static-dir', type=Path, default=STATIC_DIR)
    args = parser.parse_args()
    serve(args.host, args.port, args.static_dir)


main()
