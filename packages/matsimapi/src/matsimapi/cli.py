"""コマンド ``matsimapi``。

    matsimapi info                 計算環境とデータ置き場
    matsimapi kinds                計算の種類
    matsimapi structures           構造の一覧
    matsimapi potentials           ポテンシャルの一覧
    matsimapi jobs                 ジョブの一覧
    matsimapi job 12               ジョブの状態
    matsimapi worker               ジョブを動かす係を常駐させる
    matsimapi mcp                  MCP サーバーを起動する (標準入出力)
    matsimapi doctor               paths.env のパスと仮想環境で動くか (GPUMD がなければ CPU の NEP) を確かめる

どのコマンドも --data-dir でデータ置き場を指定できる (環境変数 MATSIM_DATA_DIR と同じ)。
"""
from __future__ import annotations

import argparse
import json
import sys


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=1, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="matsimapi", description="GPUMD・SevenNet の計算を動かすエンジン")
    parser.add_argument("--data-dir", help="データ置き場 (既定は MATSIM_DATA_DIR)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("info", "計算環境とデータ置き場"), ("kinds", "計算の種類"), ("structures", "構造の一覧"),
                       ("potentials", "ポテンシャルの一覧"), ("jobs", "ジョブの一覧")):
        commands.add_parser(name, help=text)
    commands.add_parser("job", help="ジョブの状態").add_argument("id", type=int)
    worker = commands.add_parser("worker", help="ジョブを動かす係を常駐させる")
    worker.add_argument("--idle-exit", type=float, default=0, help="ジョブがない状態がこの秒数続いたら終わる (0 で終わらない)")
    commands.add_parser("mcp", help="MCP サーバーを起動する (標準入出力)")
    commands.add_parser("doctor", help="paths.env のパスと仮想環境で動くかを確かめる")
    args = parser.parse_args(argv)

    from . import settings
    if args.data_dir:
        settings.set_data_dir(args.data_dir)
    if args.command == "worker":
        from . import runtime
        runtime.run_worker(args.idle_exit)
        return 0
    if args.command == "mcp":
        from . import mcp_server
        mcp_server.main()
        return 0
    if args.command == "doctor":
        from . import system
        return system.doctor()

    from .client import Client
    client = Client(start_worker=False)
    if args.command == "info":
        _print({**client.system(), "limits": client.limits()})
    elif args.command == "kinds":
        _print([{"key": k["key"], "label": k["label"], "group": k["group"]} for k in client.kinds()["kinds"]])
    elif args.command == "structures":
        _print([{k: s[k] for k in ("id", "name", "formula", "n_atoms", "source")} for s in client.structures()])
    elif args.command == "potentials":
        _print([{k: p[k] for k in ("id", "name", "engine", "available", "builtin")} for p in client.potentials()])
    elif args.command == "jobs":
        _print([{k: j[k] for k in ("id", "name", "kind", "status", "progress", "message")} for j in client.jobs()])
    elif args.command == "job":
        data = client.job(args.id).data
        _print({k: v for k, v in data.items() if k not in ("files", "log", "error_log")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
