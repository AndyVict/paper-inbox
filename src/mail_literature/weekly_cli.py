"""每周流水线：默认缓存与预览，联网及发送分别需要 --fetch / --send。"""
import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from . import delivery_cli, enrich_cli, jcr_match_cli, sync_cli, translate_cli
from .delivery import receipt_confirmed
from .state import StateError, StateStore
from .sync_cli import _run_lock
from .wxpusher import PushError, load_config, recipient_key

STAGE_NAMES = ("sync", "abstracts", "translation", "jcr", "delivery")
DEFAULT_STAGES = "sync,abstracts,delivery"


def selected_stages(value):
    names = value.split(",")
    if not names or len(names) != len(set(names)) or any(name not in STAGE_NAMES for name in names):
        raise argparse.ArgumentTypeError("stages must be unique comma-separated stage names")
    return tuple(name for name in STAGE_NAMES if name in names)


def push_ready(root, env):
    config = load_config(env)
    with _run_lock(root / "data/state.sqlite3"), StateStore(root / "data/state.sqlite3", readonly=True) as store:
        if any(not receipt_confirmed(store, recipient_key(uid)) for uid in config.uids):
            raise PushError("wxpusher_receipt_not_confirmed")


def run_stages(root, args, log):
    network = ["--fetch"] if args.fetch else []
    env = ["--env-file", str(args.env_file)]
    stages = [
        ("sync", sync_cli.main, network + env + ["--max-mails", str(args.max_mails)]),
        ("abstracts", enrich_cli.main, network + ["--max-articles", str(args.max_articles)]),
        ("translation", translate_cli.main, network + env + ["--max-articles", str(args.max_articles)]),
        ("jcr", jcr_match_cli.main, network + ["--max-articles", str(args.max_articles),
                                              "--snapshot", getattr(args, "snapshot", jcr_match_cli.DEFAULT_SNAPSHOT)]),
        ("delivery", delivery_cli.main, (["--send", "--require-receipt"] if args.send else []) + env +
         ["--max-messages", str(args.max_messages), "--daily-limit", str(args.daily_limit),
          "--max-abstract", str(args.max_abstract), "--language", getattr(args, "language", "zh")]),
    ]
    partial = False
    selected = getattr(args, "stages", STAGE_NAMES)
    for name, entry, parameters in stages:
        if name not in selected:
            log.write(f"\n跳过阶段：{name}\n")
            continue
        log.write(f"\n阶段：{name}\n")
        log.flush()
        with redirect_stdout(log), redirect_stderr(log):
            code = entry(parameters, project_root=root)
        log.write(f"阶段退出码：{name}={code}\n")
        log.flush()
        if code not in {0, 3}:
            return code
        partial |= code == 3
    return 3 if partial else 0


def main(argv=None, *, project_root=None):
    root = (project_root or Path.cwd()).resolve()
    parser = argparse.ArgumentParser(description="每周文献流水线；--fetch 联网，--send 推送且要求已有人工送达确认")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--check-push-ready", action="store_true", help="离线检查当前接收者送达确认，不发送")
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument("--stages", type=selected_stages, default=selected_stages(DEFAULT_STAGES),
                        help="可选 sync,abstracts,translation,jcr,delivery；默认 sync,abstracts,delivery；按固定顺序执行")
    parser.add_argument("--snapshot", default=jcr_match_cli.DEFAULT_SNAPSHOT, help="期刊快照；默认使用随包期刊库，可指定自己的本地快照")
    parser.add_argument("--language", choices=("en", "zh"), default="en", help="默认英文；中文模式需先翻译")
    parser.add_argument("--max-mails", type=int, default=100)
    parser.add_argument("--max-articles", type=int, default=100)
    parser.add_argument("--max-messages", type=int, default=100)
    parser.add_argument("--daily-limit", type=int, default=200)
    parser.add_argument("--max-abstract", type=int, default=1000)
    args = parser.parse_args(argv)
    args.env_file = args.env_file.resolve()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if (any(not 1 <= value <= 1000 for value in (args.max_mails, args.max_articles, args.max_messages, args.daily_limit)) or
            not 100 <= args.max_abstract <= 10000 or (args.send and "delivery" not in args.stages) or
            args.env_file.parent != root or
            not (args.env_file.name == ".env" or args.env_file.name.startswith("config.local."))):
        print("参数无效：invalid_weekly_parameters", file=sys.stderr)
        return 2
    try:
        if args.send or args.check_push_ready:
            push_ready(root, args.env_file)
        if args.check_push_ready:
            print("当前接收者已有人工 App 接收确认；未联网、未发送。")
            return 0
        logs = root / "logs"
        logs.mkdir(exist_ok=True)
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%dT%H%M%S%f")
        filename = "weekly-" + stamp + ".log"
        with _run_lock(root / "data/.weekly"), (logs / filename).open("x", encoding="utf-8") as log:
            log.write(f"Asia/Shanghai；fetch={args.fetch}；send={args.send}\n")
            code = run_stages(root, args, log)
        print(f"每周流水线完成；退出码 {code}；日志 logs/{filename}。")
        return code
    except (StateError, PushError) as exc:
        print(f"每周流水线失败：{exc}", file=sys.stderr)
        return 4
    except Exception:
        # 不将任何可能含配置/邮件/服务端数据的原始异常带入计划任务日志。
        print("每周流水线失败：weekly_execution_failed", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
