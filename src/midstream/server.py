"""构建和管理 MIDSTREAM_HOME 下的私有 PostgreSQL 实例。"""

import os
import subprocess
from pathlib import Path

from alembic import command
from alembic.config import Config

from . import config

ROOT = Path(__file__).resolve().parents[2]
BIN = config.HOME / "pg/bin"
DATA = config.HOME / "pgdata"
RUN = config.HOME / "run"
LOG = config.HOME / "log"


def tuned_settings(total_ram_bytes: int, cpus: int) -> dict:
    mib = 1024 ** 2
    gib = 1024 ** 3
    return {
        "shared_buffers": f"{min(total_ram_bytes // 5, 8 * gib) // mib}MB",
        "effective_cache_size": f"{total_ram_bytes // 2 // mib}MB",
        "maintenance_work_mem": f"{min(total_ram_bytes // 10, 2 * gib) // mib}MB",
        "work_mem": "64MB",
        "max_wal_size": "16GB",
        "checkpoint_completion_target": 0.9,
        "wal_compression": "lz4",
        "default_toast_compression": "lz4",
        "io_method": "worker",
        "max_parallel_workers_per_gather": 4,
        "max_parallel_maintenance_workers": 4,
        "max_parallel_workers": cpus,
        "max_worker_processes": cpus,
        "listen_addresses": "",
        "unix_socket_directories": str(RUN),
    }


def init() -> None:
    if not (BIN / "postgres").exists():
        subprocess.run(["bash", ROOT / "scripts/build-postgres.sh", config.HOME / "pg"], check=True)
    fresh = not (DATA / "PG_VERSION").exists()
    if fresh:
        subprocess.run([
            BIN / "initdb", "-D", DATA,
            "--locale-provider=builtin", "--builtin-locale=C.UTF-8", "--locale=C.UTF-8",
            "-E", "UTF8", "--auth=trust",
        ], check=True)
        settings = tuned_settings(
            os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"), os.cpu_count()
        )
        conf_dir = DATA / "conf.d"
        conf_dir.mkdir()
        lines = []
        for key, value in settings.items():
            if isinstance(value, str):
                value = "'" + value.replace("'", "''") + "'"
            lines.append(f"{key} = {value}\n")
        (conf_dir / "midstream.conf").write_text("".join(lines), encoding="utf-8")
        with (DATA / "postgresql.conf").open("a", encoding="utf-8") as conf:
            conf.write("\ninclude_dir = 'conf.d'\n")
    ensure_running()
    if fresh:
        subprocess.run([
            BIN / "createdb", "-h", RUN, "-T", "template0", "-E", "UTF8", "midstream",
        ], check=True)
    alembic = Config(str(ROOT / "alembic.ini"))
    alembic.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(alembic, "head")


def status() -> bool:
    return subprocess.run(
        [BIN / "pg_ctl", "-D", DATA, "status"], stdout=subprocess.DEVNULL,
    ).returncode == 0


def ensure_running() -> None:
    RUN.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUN.chmod(0o700)
    LOG.mkdir(parents=True, exist_ok=True)
    if not status():
        subprocess.run([
            BIN / "pg_ctl", "-D", DATA, "-l", LOG / "postgresql.log", "start", "-w",
        ], check=True)


def stop() -> None:
    subprocess.run([BIN / "pg_ctl", "-D", DATA, "stop", "-w"], check=True)
