"""コマンド ``matsimapp``: 画面のサーバーを起動する。

    matsimapp              この PC からのみ (http://localhost:8020)
    matsimapp --lan        同じネットワークのスマホからも
    matsimapp --tailscale  Tailscale (tailscale serve) 経由。Cookie を HTTPS 専用にする
"""
from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="matsimapp", description="MatSimApp の画面のサーバーを起動する")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--lan", action="store_true", help="同じネットワークからの接続を受ける")
    where.add_argument("--tailscale", action="store_true", help="HTTPS の中継の後ろで動かす")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MATSIM_PORT", 8020)))
    parser.add_argument("--data-dir", help="データ置き場 (既定は MATSIM_DATA_DIR)")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["MATSIM_DATA_DIR"] = args.data_dir
    if args.tailscale:
        os.environ["MATSIM_COOKIE_SECURE"] = "1"
    import uvicorn
    uvicorn.run("matsimapp.main:app", host="0.0.0.0" if args.lan else "127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
