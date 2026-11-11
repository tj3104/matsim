"""ジョブを動かす係 (ワーカー) の起動と、複数のプロセスでの受け渡し。

GPU は 1 枚なので、ジョブのキューを見張る係は 1 つのプロセスだけが務める。ワーカーは常に独立した
``matsimapi worker`` のプロセスで、データ置き場の worker.lock を取れた 1 つだけがジョブを起動する。
画面のサーバー・Python・MCP はキュー (SQLite) に入れるだけで、ワーカーにはならない。
プロセスが終わると OS がロックを外すので、待っていた別のワーカーが引き継ぐ。計算は別のセッションで
動いているので、引き継ぎの間も止まらない。

画面のサーバーは WorkerKeeper でワーカーがいることを確かめ、いなければ起動する。起動したワーカーは
別のセッションで動くので、画面のサーバーを止めても残る。画面がないときは Python や MCP が起動する。
"""
from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import analysis_jobs, jobs, potentials, settings
from . import db as database

STANDBY_SECONDS = 5.0     # ほかのプロセスがワーカーのとき、引き継げるか確かめる間隔
KEEP_SECONDS = 15.0       # 画面のサーバーがワーカーの有無を確かめる間隔 (起動にかかる時間より長く)
TMP_KEEP_SECONDS = 3600   # 作業用のファイルを消す目安 (ほかのプロセスが使っている途中のものを消さない)


def prepare() -> None:
    """データ置き場を用意する。何度呼んでもよい。"""
    database.init_db()
    conn = database.connect()
    try:
        potentials.sync_builtin(conn)
    finally:
        conn.close()


def _lock_path() -> Path:
    return settings.DATA_DIR / "worker.lock"


def _try_lock() -> int | None:
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def is_running() -> bool:
    """このデータ置き場のワーカーが動いているか。"""
    fd = _try_lock()
    if fd is None:
        return True
    os.close(fd)
    return False


def _recover() -> None:
    """ワーカーになったときの片付け。前のワーカーが途中で止まった分を整理する。"""
    conn = database.connect()
    try:
        # 入力を書いている途中でサーバーが止まったジョブは実行できないので失敗にする
        conn.execute("UPDATE jobs SET status = 'failed', message = '投入の途中でサーバーが停止しました' "
                     "WHERE status = 'preparing'")
        analysis_jobs.requeue_interrupted(conn)   # 途中で止まった解析はやり直す
    finally:
        conn.close()
    limit = time.time() - TMP_KEEP_SECONDS
    for leftover in settings.TMP_DIR.iterdir():
        try:
            if leftover.stat().st_mtime > limit:
                continue
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                leftover.unlink(missing_ok=True)
        except OSError:
            pass


class Runtime:
    """このプロセスをワーカーにする。ほかがワーカーなら待機し、空いたら引き継ぐ。``matsimapi worker`` だけが使う。"""

    def __init__(self) -> None:
        self._fd: int | None = None
        self._stop = threading.Event()

    @property
    def active(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        fd = _try_lock()
        if fd is None:
            return False
        self._fd = fd
        _recover()
        jobs.scheduler.start()
        analysis_jobs.worker.start()
        return True

    def start(self) -> None:
        prepare()
        if not self.acquire():
            threading.Thread(target=self._standby, name="worker-standby", daemon=True).start()

    def _standby(self) -> None:
        while not self._stop.wait(STANDBY_SECONDS):
            if self.acquire():
                return

    def stop(self) -> None:
        self._stop.set()
        jobs.scheduler.stop()   # 実行中の gpumd は止めない。次のワーカーが見つけ直す
        analysis_jobs.worker.stop()
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _active_jobs() -> int:
    conn = database.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')").fetchone()[0]
    finally:
        conn.close()


def run_worker(idle_exit: float = 0) -> None:
    """ワーカーとして動き続ける (``matsimapi worker``)。

    idle_exit > 0 なら、動かすジョブがない状態がその秒数だけ続いたら終わる。このときは、
    ほかにワーカーがいれば何もせずに終わる。
    """
    runtime = Runtime()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if idle_exit > 0:
        prepare()
        for _ in range(10):   # is_running() の確認と重なっただけのことがあるので少し粘る
            if runtime.acquire():
                break
            time.sleep(0.2)
        else:
            return
    else:
        runtime.start()
    idle_since = time.monotonic()
    while not stop.wait(2.0):
        if idle_exit <= 0:
            continue
        if _active_jobs():
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since > idle_exit:
            break
    runtime.stop()


def ensure_worker(idle_exit: float = 300) -> bool:
    """ワーカーがいなければ、裏で 1 つ起動する。起動したら True。

    Python のスクリプトや MCP サーバーが終わってもキューが進むよう、別のプロセスにする。
    ジョブがなくなって idle_exit 秒たつと自分で終わる。
    """
    if is_running():
        return False
    with open(settings.DATA_DIR / "worker.log", "a") as log:
        subprocess.Popen([sys.executable, "-m", "matsimapi", "worker", "--idle-exit", str(idle_exit)],
                         env={**os.environ, "MATSIM_DATA_DIR": str(settings.DATA_DIR)}, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)
    return True


class WorkerKeeper:
    """ワーカーが常にいるように見張る。ワーカーは別のプロセスなので、見張りが止まっても動き続ける。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while True:
            try:
                ensure_worker(idle_exit=0)
            except OSError as exc:  # 見張りが止まるとワーカーを起動し直せなくなるので落とさない
                print(f"[worker-keeper] {type(exc).__name__}: {exc}", flush=True)
            if self._stop.wait(KEEP_SECONDS):
                return

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="worker-keeper", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
