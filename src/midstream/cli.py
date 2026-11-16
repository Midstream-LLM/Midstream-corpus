"""命令行入口。"""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import psycopg

from .ingest import batches
from . import config, jobs, operations
from .corpus import management, query
from .ops import server
from .ingest.adapters import ADAPTERS
from .curation.rules import RULES
from .llm.settings import SettingsError
from .datasets.recipe import ExportError, Recipe
from .datasets.release import record_training, release_stats
from .delivery.export import DEFAULT_FIELDS, FieldName, Options, verify_release


def _print(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _finish(job) -> int:
    """就地执行的结果：成功时输出操作结果，附带后续任务。"""
    if job["status"] != "succeeded":
        _print({"status": job["status"], "job_id": job["job_id"], "message": job["error"]})
        return 1
    result = job["result"]
    if job["follow_ups"]:
        result = {**(result or {}), "follow_ups": [
            {key: child[key] for key in ("job_id", "kind", "status", "result", "error")}
            for child in job["follow_ups"]]}
    _print(result)
    return 0


def _operate(conn, args, kind, params, reason=None) -> int:
    reason = args.reason or reason or args.command
    if args.queue:
        _print(jobs.submit(conn, kind, params, args.actor, reason))
        return 0
    return _finish(jobs.run_inline(conn, kind, params, args.actor, reason))


def _run(conn, args) -> int:
    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as exc:
        raise management.ManagementError("invalid_input", f"params 不是有效 JSON：{exc}") from exc
    reason = args.reason or f"run {args.operation}"
    if args.queue:
        _print(jobs.submit(conn, args.operation, params, args.actor, reason))
    else:
        job = jobs.run_inline(conn, args.operation, params, args.actor, reason)
        _print(job)
        return 0 if job["status"] == "succeeded" else 1
    return 0


def _ingest(conn, args) -> int:
    paths = [str(Path(p).expanduser().resolve()) for p in args.path]
    return _operate(conn, args, "ingest", {"source": args.source, "paths": paths})


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
        quality=args.quality or ["ok", "flagged"],
    )
    _print(query.search(conn, filters, limit=args.limit, after_doc=args.after))
    return 0


def _reprocess(conn, args) -> int:
    return _operate(conn, args, "reprocess", {"rule": args.rule, "apply": args.apply})


def _dedup(conn, args) -> int:
    return _operate(conn, args, "dedup_spans" if args.spans else "dedup", {"full": args.full})


def _show(conn, args) -> int:
    document = query.get_text(conn, args.doc_id, args.version)
    if document is None:
        print("文档或版本不存在", file=sys.stderr)
        return 1
    _print(document)
    return 0


def _export(conn, args) -> int:
    data = json.loads(Path(args.recipe).read_text(encoding="utf-8"))
    reason = args.reason or args.command
    release_id = jobs.run_inline(conn, "dataset_create", {"recipe": data}, args.actor, reason)["result"]["release_id"]
    result = jobs.run_inline(conn, "dataset_export", {"release_id": release_id}, args.actor, reason)["result"]
    _print({**release_stats(conn, release_id), **result})
    return 0


def _export_preview(conn, args) -> int:
    from .delivery import token_stats, tokenizing
    recipe = Recipe.load(json.loads(Path(args.recipe).read_text(encoding='utf-8')))
    tokenizer = tokenizing.load(args.tokenizer) if args.tokenizer else None
    conn.execute('SET TRANSACTION READ ONLY')
    _print(token_stats.plan_with_estimate(conn, recipe, tokenizer).report)
    return 0


def _export_release(conn, args) -> int:
    options = Options(format=args.format, fields=args.fields, unit=args.unit,
                                shard_mb=args.shard_mb, max_chars=args.max_chars,
                                overlap_chars=args.overlap_chars, compression=args.compression,
                                template={'format': args.template.replace('\\n', '\n'), 'ratio': args.template_ratio,
                                          'seed': args.template_seed} if args.template else None)
    return _operate(conn, args, "dataset_export",
                    {"release_id": args.release_id, "options": options.model_dump(mode="json")})


def _token_job(conn, args) -> int:
    params = {'release_id': args.release_id, 'tokenizer': str(Path(args.tokenizer).expanduser().resolve())}
    if args.command == 'tokenize':
        params['shard_tokens'] = args.shard_tokens
    return _operate(conn, args, args.command.replace('-', '_'), params)


def _backup(conn,args):
    from .ops import backup
    if args.backup_action=='list':
        _print(backup.list_backups(conn))
    elif args.backup_action=='restore':
        _print(backup.restore(conn,args.backup_id,args.database))
    else:
        params={} if args.backup_action=='run' else {'backup_id':args.backup_id}
        return _operate(conn,args,'backup' if args.backup_action=='run' else 'backup_verify',params,
                        'backup '+args.backup_action)
    return 0


def _release_stats(conn, args) -> int:
    _print(release_stats(conn, args.release_id))
    return 0


def _verify(conn, args) -> int:
    ok = verify_release(conn, args.release_id, hash_files=args.hash)
    print("通过" if ok else "未通过")
    return 0 if ok else 1


def _record_training(conn, args) -> int:
    record_training(conn, args.release, args.run)
    return 0


def _stats(conn, _args) -> int:
    _print(query.stats(conn))
    return 0


def _review_document(conn, args) -> int:
    try:
        metadata = json.loads(args.metadata)
    except json.JSONDecodeError as exc:
        raise management.ManagementError("invalid_input", f"metadata 不是有效 JSON：{exc}") from exc
    _print(management.review_document(
        conn, args.doc_id, args.expected_version_id, metadata,
        actor=args.actor, reason=args.reason,
    ))
    return 0


def _review_work(conn, args) -> int:
    _print(management.review_work(
        conn, args.doc_id, args.work_id, args.relation, args.decision,
        actor=args.actor, reason=args.reason, expected_status=args.expected_status,
    ))
    return 0


def _work_links(conn, args) -> int:
    _print(management.work_links(conn, args.doc_id, status=args.status))
    return 0


def _batch_list(conn, args) -> int:
    _print(batches.list_batches(
        conn, limit=args.limit, include_published=args.include_published,
    ))
    return 0


def _batch_show(conn, args) -> int:
    _print(batches.inspect(conn, args.batch_id))
    return 0


def _batch_resume(conn, args) -> int:
    return _operate(conn, args, "batch_resume", {"batch_id": args.batch_id})


def _batch_abandon(conn, args) -> int:
    _print(batches.abandon(conn, args.batch_id, actor=args.actor, reason=args.reason))
    return 0


def _events(conn, args) -> int:
    _print(management.events(
        conn, doc_id=args.doc_id, batch_id=args.batch_id, limit=args.limit,
    ))
    return 0


def _job_options(parser) -> None:
    parser.add_argument("--actor", default=getpass.getuser())
    parser.add_argument("--reason", help="默认使用命令名")
    parser.add_argument("--queue", action="store_true", help="只提交到任务队列，不等待")


def main() -> int:
    parser = argparse.ArgumentParser(prog="midstream")
    commands = parser.add_subparsers(dest="command", required=True)
    p=commands.add_parser('backup',help='OSS 备份、目录校验与恢复到全新库')
    actions=p.add_subparsers(dest='backup_action',required=True)
    for name in ('run','list','verify','restore'):
        p=actions.add_parser(name)
        if name in ('verify','restore'):
            p.add_argument('backup_id',type=int)
        if name=='restore':
            p.add_argument('--database',required=True,help='只能使用不存在的新库名')
        if name in ('run','verify'):
            _job_options(p)
        p.set_defaults(handler=_backup)

    commands.add_parser("init", help="构建、初始化私有 PostgreSQL 并迁移到最新版")
    commands.add_parser("operations", help="列出可执行的长操作及参数 JSON Schema")
    p = commands.add_parser("serve", help="启动内置 PostgreSQL、worker 和 Web/API 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--static-dir", type=Path, help="前端构建目录；默认 web/dist")
    p.add_argument("--no-worker", action="store_true", help="不启动 worker，只运行 API")
    p = commands.add_parser("run", help="按名称执行一个已注册操作；默认就地执行")
    p.add_argument("operation")
    p.add_argument("--params", default="{}", help="参数 JSON 对象")
    _job_options(p)
    p.set_defaults(handler=_run)
    p = commands.add_parser("worker", help="串行执行持久任务；需手动启动")
    p.set_defaults(handler=lambda conn, args: jobs.worker(conn))

    p = commands.add_parser("jobs", help="查看、取消或重试持久任务")
    actions = p.add_subparsers(dest="job_action", required=True)
    p = actions.add_parser("list")
    p.add_argument("--limit", type=int, choices=range(1, 101), default=30, metavar="1..100")
    p.add_argument("--before", type=int)
    p.set_defaults(handler=lambda conn, args: _print(jobs.list_jobs(
        conn, limit=args.limit, before=args.before)) or 0)
    p = actions.add_parser("show")
    p.add_argument("job_id", type=int)
    p.set_defaults(handler=lambda conn, args: _print(jobs.show(conn, args.job_id)) or 0)
    for action in ("cancel", "retry"):
        p = actions.add_parser(action)
        p.add_argument("job_id", type=int)
        p.add_argument("--actor", required=True)
        p.add_argument("--reason", required=True)
        p.set_defaults(handler=lambda conn, args: _print(getattr(jobs, args.job_action)(
            conn, args.job_id, args.actor, args.reason)) or 0)
    p = commands.add_parser("server", help="管理私有 PostgreSQL 实例")
    p.add_argument("action", choices=("start", "stop", "status"))

    p = commands.add_parser("ingest", help="入库一个来源")
    p.add_argument("source", choices=sorted(ADAPTERS))
    p.add_argument("--path", action="append", default=[])
    _job_options(p)
    p.set_defaults(handler=_ingest)

    p = commands.add_parser("reprocess", help="按规则生成新版本，默认只预览")
    p.add_argument("rule", choices=sorted(RULES))
    p.add_argument("--apply", action="store_true")
    _job_options(p)
    p.set_defaults(handler=_reprocess)

    p = commands.add_parser("dedup", help="当前版本的文档级或段落级查重")
    p.add_argument("--spans", action="store_true", help="只运行段落级查重")
    p.add_argument("--full", action="store_true", help="忽略输入快照及候选分数缓存，完整重算")
    _job_options(p)
    p.set_defaults(handler=_dedup)

    p = commands.add_parser("search", help="按元数据查询当前版本")
    p.add_argument("--source", action="append", default=[])
    p.add_argument("--title")
    p.add_argument("--author")
    p.add_argument("--genre")
    p.add_argument("--category")
    p.add_argument("--year-from", type=int)
    p.add_argument("--year-to", type=int)
    p.add_argument("--include-unknown-years", action="store_true")
    p.add_argument("--quality", action="append", choices=("ok", "flagged", "quarantined"))
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--after", type=int, default=0)
    p.set_defaults(handler=_search)

    p = commands.add_parser("show", help="读取一篇文档")
    p.add_argument("doc_id", type=int)
    p.add_argument("--version", type=int)
    p.set_defaults(handler=_show)

    p = commands.add_parser("export", help="按配方生成数据集并导出 Parquet")
    p.add_argument("recipe")
    p.add_argument("--actor", default=getpass.getuser())
    p.add_argument("--reason")
    p.set_defaults(handler=_export)

    p = commands.add_parser('export-preview', help='只读试算选用、划分和分布')
    p.add_argument('recipe')
    p.add_argument('--tokenizer', help='本地 tokenizer.json 或所在目录；仅估算缓存之外的 token 数')
    p.set_defaults(handler=_export_preview)

    for name in ('token-stats', 'tokenize'):
        p = commands.add_parser(name, help='统计数据集 token 数' if name == 'token-stats' else '统计 token 并写出 token 分片')
        p.add_argument('release_id', type=int)
        p.add_argument('--tokenizer', required=True)
        _job_options(p)
        if name == 'tokenize':
            p.add_argument('--shard-tokens', type=int, default=200_000_000)
        p.set_defaults(handler=_token_job)

    p = commands.add_parser('export-release', help='按固定发布导出 Parquet、JSONL 或 PKL')
    p.add_argument('release_id', type=int)
    p.add_argument('--format', choices=('parquet', 'jsonl', 'pkl'), default='parquet')
    p.add_argument('--fields', nargs='+', default=DEFAULT_FIELDS,
                   choices=FieldName.__args__)
    p.add_argument('--unit', choices=('document', 'sample'), default='document')
    p.add_argument('--shard-mb', type=float, default=256)
    p.add_argument('--max-chars', type=int, default=8192)
    p.add_argument('--overlap-chars', type=int, default=0)
    p.add_argument('--compression', choices=('zst',))
    p.add_argument('--template', help='正文模板，如 "[《{title}》][{authors}]\\n{text}"；\\n 表示换行')
    p.add_argument('--template-ratio', type=float, default=1.0, help='套用模板的文档比例')
    p.add_argument('--template-seed', type=int, default=0, help='按比例选取文档的种子')
    _job_options(p)
    p.set_defaults(handler=_export_release)

    p = commands.add_parser("release-stats", help="查看数据集发布的总字符数及各划分字符数")
    p.add_argument("release_id", type=int)
    p.set_defaults(handler=_release_stats)

    p = commands.add_parser("verify", help="校验数据集成员和导出文件；可选文件哈希")
    p.add_argument("release_id", type=int)
    p.add_argument('--hash', action='store_true', help='额外重算产物文件 SHA-256')
    p.set_defaults(handler=_verify)

    p = commands.add_parser("record-training", help="登记数据集的训练使用")
    p.add_argument("release")
    p.add_argument("run")
    p.set_defaults(handler=_record_training)

    p = commands.add_parser("stats", help="查看库内统计")
    p.set_defaults(handler=_stats)

    p = commands.add_parser("review-document", help="审核质量或修正元数据，生成新版本")
    p.add_argument("doc_id", type=int)
    p.add_argument("expected_version_id", type=int)
    p.add_argument("--metadata", required=True, help="字段覆盖 JSON 对象")
    p.add_argument("--actor", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(handler=_review_document)

    p = commands.add_parser("review-work", help="确认或拒绝已有作品关联")
    p.add_argument("doc_id", type=int)
    p.add_argument("work_id", type=int)
    p.add_argument("relation", choices=("whole", "part", "excerpt", "contains"))
    p.add_argument("decision", choices=("confirmed", "rejected"))
    p.add_argument("--expected-status", default="candidate")
    p.add_argument("--actor", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(handler=_review_work)

    p = commands.add_parser("work-links", help="查看一篇文档已有的作品关联")
    p.add_argument("doc_id", type=int)
    p.add_argument("--status", choices=("candidate", "confirmed", "rejected"))
    p.set_defaults(handler=_work_links)

    p = commands.add_parser("batch-list", help="列出批次，默认只显示未发布批次")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--include-published", action="store_true")
    p.set_defaults(handler=_batch_list)

    p = commands.add_parser("batch-show", help="查看批次状态与记录数量")
    p.add_argument("batch_id", type=int)
    p.set_defaults(handler=_batch_show)

    for name, handler, help_text in (
        ("batch-resume", _batch_resume, "核对原文件后恢复未发布批次"),
        ("batch-abandon", _batch_abandon, "放弃未发布批次并删除暂存分区"),
    ):
        p = commands.add_parser(name, help=help_text)
        p.add_argument("batch_id", type=int)
        p.add_argument("--actor", required=True)
        p.add_argument("--reason", required=True)
        if name == "batch-resume":
            p.add_argument("--queue", action="store_true", help="只提交到任务队列，不等待")
        p.set_defaults(handler=handler)

    p = commands.add_parser("events", help="查看人工审核及批次操作记录")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--doc-id", type=int)
    group.add_argument("--batch-id", type=int)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(handler=_events)

    from .llm.cli import add_commands, run as run_llm
    add_commands(commands)
    args = parser.parse_args()
    if args.command == "llm":
        return run_llm(args)
    if args.command == "init":
        server.init()
        return 0
    if args.command == "serve":
        from . import api
        api.serve(args.host, args.port, args.static_dir or api.STATIC_DIR, worker=not args.no_worker)
        return 0
    if args.command == "operations":
        _print(operations.describe())
        return 0
    if args.command == "server":
        if args.action == "status":
            running = server.status()
            print("running" if running else "stopped")
            return 0 if running else 1
        if args.action == "start":
            server.ensure_running()
        else:
            server.stop()
        return 0
    if args.command == "ingest" and not args.path and config.JISHUI_RAW is None:
        parser.error("ingest 需要 --path 或 MIDSTREAM_JISHUI_RAW")
    if "MIDSTREAM_DSN" not in os.environ:
        server.ensure_running()
    with psycopg.connect(config.dsn()) as conn:
        try:
            return args.handler(conn, args)
        except management.ManagementError as exc:
            _print({"status": "error", "code": exc.code, "message": str(exc)})
            return 2
        except (ExportError, ValueError, SettingsError) as exc:
            _print({'status': 'error', 'message': str(exc)})
            return 2


if __name__ == "__main__":
    sys.exit(main())
