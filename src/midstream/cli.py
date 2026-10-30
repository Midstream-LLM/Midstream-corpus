"""命令行入口。"""

import argparse
import dataclasses
import json
import sys
from pathlib import Path

import psycopg

from . import config, export, ingest, query, reprocess
from .adapters import ADAPTERS
from .rules import RULES


def _print(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _ingest(conn, args) -> int:
    adapter = ADAPTERS[args.source]
    paths = [Path(p) for p in args.path] or adapter.default_paths()
    try:
        report = ingest.ingest(conn, adapter, paths)
    except ingest.AlreadyImported as exc:
        print(f"跳过：{exc}", file=sys.stderr)
        return 0
    _print(dataclasses.asdict(report))
    return 0


def _search(conn, args) -> int:
    filters = query.Filters(
        sources=args.source,
        title_prefix=args.title,
        author=args.author,
        genre=args.genre,
        category=args.category,
        year_from=args.year_from,
        year_to=args.year_to,
        include_unknown_years=args.include_unknown_years,
    )
    _print(query.search(conn, filters, limit=args.limit, after_doc=args.after))
    return 0


def _reprocess(conn, args) -> int:
    _print(reprocess.reprocess(conn, RULES[args.rule], dry_run=not args.apply))
    return 0


def _show(conn, args) -> int:
    document = query.get_text(conn, args.doc_id, args.version)
    if document is None:
        print("文档或版本不存在", file=sys.stderr)
        return 1
    _print(document)
    return 0


def _export(conn, args) -> int:
    data = json.loads(Path(args.recipe).read_text(encoding="utf-8"))
    recipe = export.Recipe.load(data)
    release_id = export.create_release(conn, recipe)
    files = export.write_parquet(conn, release_id, config.EXPORT_ROOT / recipe.name)
    _print({"release_id": release_id, "files": [str(f) for f in files]})
    return 0


def _verify(conn, args) -> int:
    ok = export.verify_release(conn, args.release_id)
    print("通过" if ok else "未通过")
    return 0 if ok else 1


def _record_training(conn, args) -> int:
    export.record_training(conn, args.release, args.run)
    return 0


def _stats(conn, _args) -> int:
    _print(query.stats(conn))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="midstream")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("ingest", help="入库一个来源")
    p.add_argument("source", choices=sorted(ADAPTERS))
    p.add_argument("--path", action="append", default=[])
    p.set_defaults(handler=_ingest)

    p = commands.add_parser("reprocess", help="按规则生成新版本，默认只预览")
    p.add_argument("rule", choices=sorted(RULES))
    p.add_argument("--apply", action="store_true")
    p.set_defaults(handler=_reprocess)

    p = commands.add_parser("search", help="按元数据查询当前版本")
    p.add_argument("--source", action="append", default=[])
    p.add_argument("--title")
    p.add_argument("--author")
    p.add_argument("--genre")
    p.add_argument("--category")
    p.add_argument("--year-from", type=int)
    p.add_argument("--year-to", type=int)
    p.add_argument("--include-unknown-years", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--after", type=int, default=0)
    p.set_defaults(handler=_search)

    p = commands.add_parser("show", help="读取一篇文档")
    p.add_argument("doc_id", type=int)
    p.add_argument("--version", type=int)
    p.set_defaults(handler=_show)

    p = commands.add_parser("export", help="按配方生成数据集")
    p.add_argument("recipe")
    p.set_defaults(handler=_export)

    p = commands.add_parser("verify", help="校验数据集成员与正文")
    p.add_argument("release_id", type=int)
    p.set_defaults(handler=_verify)

    p = commands.add_parser("record-training", help="登记数据集的训练使用")
    p.add_argument("release")
    p.add_argument("run")
    p.set_defaults(handler=_record_training)

    p = commands.add_parser("stats", help="查看库内统计")
    p.set_defaults(handler=_stats)

    args = parser.parse_args()
    with psycopg.connect(config.dsn()) as conn:
        return args.handler(conn, args)


if __name__ == "__main__":
    sys.exit(main())
