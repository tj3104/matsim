"""ジョブのキュー実行。GPU は 1 枚なので、同時に動かすのは 1 つだけ。

gpumd はサーバーとは別のセッションで起動するので、ブラウザを閉じても、サーバーを
再起動しても計算は続く。再起動後は pid から実行中のジョブを見つけ直す。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db as database
from . import settings

FINISHED_MARK = "Finished running GPUMD."
FINISHED_ASE = "Finished running ASE engine"   # SevenNet (engines/ase_runner.py) の終わりの印
FINISHED_FT = "Finished running fine-tuning"   # ファインチューニング (engines/ft_runner.py) の終わりの印
RUN_RE = re.compile(r"^Run (\d+) steps\.")
STEP_RE = re.compile(r"^\s+(\d+) steps completed\.")
SPEED_RE = re.compile(r"^Speed of this run = ([0-9.eE+]+) atom\*step/second")
ACTIVE = ("queued", "running")
# GPU を使わず、サーバーの中で動かす解析のジョブ (analysis_jobs.py の作業スレッドが受け持つ)
ANALYSIS_KINDS = ("voronoi", "cna", "coord", "sro", "poly")
_GPU_ONLY = "kind NOT IN ('" + "', '".join(ANALYSIS_KINDS) + "')"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def workdir(job_id: int) -> Path:
    return settings.JOB_DIR / str(int(job_id))


def parse_log(path: Path) -> dict:
    """gpumd.out から、進んだステップ数・速度・正常終了したかを読む。"""
    done = current = current_total = 0
    speeds: list[float] = []
    finished = False
    try:
        with open(path, "r", errors="replace") as handle:
            for line in handle:
                if match := RUN_RE.match(line):
                    current_total, current = int(match.group(1)), 0
                elif match := STEP_RE.match(line):
                    current = int(match.group(1))
                elif match := SPEED_RE.match(line):
                    speeds.append(float(match.group(1)))
                    done += current_total
                    current = current_total = 0
                elif FINISHED_MARK in line or FINISHED_ASE in line or FINISHED_FT in line:
                    finished = True
    except OSError:
        pass
    return {"steps_done": done + current, "finished": finished,
            "speed": sum(speeds) / len(speeds) if speeds else None}


def _count_lines(path: Path) -> int:
    count = 0
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(1 << 20):
                count += chunk.count(b"\n")
    except OSError:
        pass
    return count


def steps_from_thermo(directory: Path) -> int:
    """thermo.out の行数から、進んだステップ数を求める。

    gpumd.out の進捗表示は 10% ごとで、しかもファイルへの出力がバッファされて遅れるので、
    実行中の進捗には dump_thermo の間隔ごとに 1 行ずつ増える thermo.out を使う。
    ステージの並びは run.in から読むので、計算の種類によらず同じ方法で求められる。
    """
    from gpumd_toolkit.analysis import RunSpec
    try:
        stages = RunSpec.from_file(directory / "run.in").stages
    except (OSError, ValueError):
        return 0
    rows = _count_lines(directory / "thermo.out")
    done = 0
    for stage in stages:
        stage_rows = stage.n_thermo_rows
        if rows >= stage_rows:
            done += stage.steps
            rows -= stage_rows
        else:
            done += rows * (stage.thermo_interval or 1)
            break
    return done


def tail(path: Path, max_bytes: int = 6000) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return text if size <= max_bytes else text.split("\n", 1)[-1]


def _is_gpumd_alive(pid: int | None) -> bool:
    """pid がまだ gpumd として動いているか (pid の使い回しを取り違えない)。"""
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0]
    except OSError:
        return False
    if state == "Z":
        return False
    args = cmdline.split(b"\0")
    # gpumd か、SevenNet の計算 (python engines/ase_runner.py)、ファインチューニング (python engines/ft_runner.py)
    return os.path.basename(args[0]) == b"gpumd" or any(a.endswith((b"ase_runner.py", b"ft_runner.py")) for a in args[1:3])


class Scheduler:
    """キューを見張って、順番に gpumd を起動・後始末するスレッド。"""

    def __init__(self) -> None:
        self._procs: dict[int, subprocess.Popen] = {}
        self._kill_sent: dict[int, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="job-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn = database.connect()
                try:
                    self.tick(conn)
                finally:
                    conn.close()
            except Exception as exc:  # 見張りが止まるとキューが進まなくなるので落とさない
                print(f"[scheduler] {type(exc).__name__}: {exc}", flush=True)
            self._stop.wait(1.0)

    def tick(self, conn: sqlite3.Connection) -> None:
        # 解析のジョブ (GPU を使わない) は別の作業スレッドが受け持つので、ここでは見ない
        running = conn.execute(f"SELECT * FROM jobs WHERE status = 'running' AND {_GPU_ONLY} ORDER BY id").fetchall()
        for job in running:
            self._watch(conn, job)
        if conn.execute(f"SELECT 1 FROM jobs WHERE status = 'running' AND {_GPU_ONLY}").fetchone():
            return
        queued = conn.execute(f"SELECT * FROM jobs WHERE status = 'queued' AND {_GPU_ONLY} ORDER BY id LIMIT 1").fetchone()
        if queued is not None:
            self._launch(conn, queued)

    def _launch(self, conn: sqlite3.Connection, job: sqlite3.Row) -> None:
        directory = workdir(job["id"])
        try:
            with open(directory / "gpumd.out", "w") as out, open(directory / "gpumd.err", "w") as err:
                command, env = [str(settings.GPUMD_BIN)], None
                from .kinds import PLUMED_KINDS   # 起動時の循環 import を避けてここで読む
                if (directory / "ase_job.json").is_file():   # SevenNet: ASE で計算する (SevenNet の入った Python)
                    command = [str(settings.ML_PYTHON), str(settings.ASE_RUNNER), str(directory)]
                elif (directory / "ft_job.json").is_file():   # ファインチューニング: gpumd・SevenNet・nep を順に動かす
                    command = [str(settings.ML_PYTHON), str(settings.FT_RUNNER), str(directory)]
                elif job["kind"] in PLUMED_KINDS:   # PLUMED 対応でビルドした gpumd で動かす
                    command = [str(settings.GPUMD_PLUMED_BIN)]
                    env = {**os.environ, "PLUMED_NUM_THREADS": str(settings.PLUMED_THREADS)}
                proc = subprocess.Popen(command, cwd=directory, stdout=out, stderr=err, env=env,
                                        stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            conn.execute("UPDATE jobs SET status = 'failed', message = ?, finished_at = ? WHERE id = ?",
                         (f"gpumd を起動できませんでした: {exc}", now(), job["id"]))
            return
        self._procs[job["id"]] = proc
        conn.execute("UPDATE jobs SET status = 'running', pid = ?, started_at = ? WHERE id = ? AND status = 'queued'",
                     (proc.pid, now(), job["id"]))

    def _watch(self, conn: sqlite3.Connection, job: sqlite3.Row) -> None:
        proc = self._procs.get(job["id"])
        if proc is not None:
            returncode = proc.poll()
            alive = returncode is None
        else:  # サーバー再起動前に起動したジョブ。終了コードは分からない
            returncode = None
            alive = _is_gpumd_alive(job["pid"])
        if alive:
            if job["cancel_requested"]:
                # まず SIGTERM、10 秒たっても止まらなければ SIGKILL
                first = self._kill_sent.setdefault(job["id"], time.monotonic())
                self._kill(job["pid"], signal.SIGKILL if time.monotonic() - first > 10 else signal.SIGTERM)
            return
        self._procs.pop(job["id"], None)
        self._kill_sent.pop(job["id"], None)
        self._finalize(conn, job, returncode)

    @staticmethod
    def _kill(pid: int | None, sig: int) -> None:
        if not _is_gpumd_alive(pid):
            return
        try:
            os.killpg(pid, sig)  # 別セッションで起動しているので pid がそのままプロセスグループ
        except (ProcessLookupError, PermissionError):
            pass

    def _finalize(self, conn: sqlite3.Connection, job: sqlite3.Row, returncode: int | None) -> None:
        directory = workdir(job["id"])
        log = parse_log(directory / "gpumd.out")
        message = ""
        if job["cancel_requested"]:
            status = "cancelled"
            message = "中止しました"
        elif log["finished"] and returncode in (0, None):
            status = "done"
        else:
            status = "failed"
            error = tail(directory / "gpumd.err", 600).strip() or tail(directory / "gpumd.out", 600).strip()
            message = error.splitlines()[-1][:300] if error else "GPUMD が異常終了しました"
        if status == "done":
            # 一覧に「完了」と出た時点で csv やグラフ画像もそろっているよう、状態を変える前に後処理する
            try:
                from . import results
                results.postprocess(directory, job["n_atoms"], job["kind"], json.loads(job["params"]))
            except Exception as exc:  # 後処理の失敗で計算結果を失敗扱いにしない
                message = f"計算は完了しましたが、後処理に失敗しました ({type(exc).__name__})"
        conn.execute("UPDATE jobs SET status = ?, message = ?, speed = ?, finished_at = ?, pid = NULL WHERE id = ?",
                     (status, message, log["speed"], now(), job["id"]))

scheduler = Scheduler()


# ---- API から呼ぶ操作 ----------------------------------------------------------------

def create(conn: sqlite3.Connection, *, name: str, kind: str, params: dict, summary: dict,
           n_atoms: int, total_steps: int, user_id: int | None = None, series: str | None = None) -> int:
    """ジョブの行を作る。入力ファイルを書き終えてから enqueue() でキューに入れる。"""
    active = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running', 'preparing')").fetchone()[0]
    if active >= settings.MAX_QUEUED_JOBS:
        raise ValueError(f"実行待ちのジョブは {settings.MAX_QUEUED_JOBS} 件までです")
    return conn.execute(
        "INSERT INTO jobs (name, kind, status, params, summary, n_atoms, total_steps, user_id, series) "
        "VALUES (?, ?, 'preparing', ?, ?, ?, ?, ?, ?)",
        (name, kind, json.dumps(params), json.dumps(summary), n_atoms, total_steps, user_id, series)).lastrowid


def enqueue(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("UPDATE jobs SET status = 'queued' WHERE id = ? AND status = 'preparing'", (job_id,))


def cancel(conn: sqlite3.Connection, job_id: int) -> str:
    job = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError("ジョブが見つかりません")
    if job["status"] == "queued":
        conn.execute("UPDATE jobs SET status = 'cancelled', message = '実行前に取り消しました', finished_at = ? "
                     "WHERE id = ? AND status = 'queued'", (now(), job_id))
    elif job["status"] == "running":
        # 実際に止めるのは見張りスレッド。止まったことを確認してから状態を変える
        conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
    else:
        raise ValueError("このジョブは実行中でも実行待ちでもありません")
    return job["status"]


def delete(conn: sqlite3.Connection, job_id: int) -> None:
    job = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError("ジョブが見つかりません")
    if job["status"] in ACTIVE:
        raise ValueError("実行中・実行待ちのジョブは、先に中止してください")
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    directory = workdir(job_id)
    if directory.parent == settings.JOB_DIR and directory.is_dir():
        shutil.rmtree(directory)


def to_dict(job: sqlite3.Row, *, detail: bool = False) -> dict:
    data = {key: job[key] for key in ("id", "name", "kind", "status", "n_atoms", "total_steps", "message",
                                      "created_at", "started_at", "finished_at", "series")}
    data["summary"] = json.loads(job["summary"])
    data["cancel_requested"] = bool(job["cancel_requested"])
    directory = workdir(job["id"])
    steps_done = 0
    data["unit"] = "フレーム" if job["kind"] in ANALYSIS_KINDS else "ステップ"
    if job["kind"] == "finetune":   # MD のステップ数と学習の世代数の合計で進み具合を表す
        data["unit"] = "ステップ・世代"
        try:
            data["phase"] = json.loads((directory / "progress.json").read_text()).get("phase")
        except (OSError, ValueError):
            data["phase"] = None
    if job["status"] == "done":
        steps_done = job["total_steps"]
    elif job["kind"] in ANALYSIS_KINDS or job["kind"] == "finetune":
        try:   # 解析・ファインチューニングの進み具合は progress.json に書かれる
            steps_done = min(int(json.loads((directory / "progress.json").read_text())["done"]), job["total_steps"])
        except (OSError, ValueError, KeyError):
            steps_done = 0
    elif job["status"] in ("running", "failed", "cancelled"):
        steps_done = max(steps_from_thermo(directory), parse_log(directory / "gpumd.out")["steps_done"])
        steps_done = min(steps_done, job["total_steps"])
    data["steps_done"] = steps_done
    data["progress"] = steps_done / job["total_steps"] if job["total_steps"] else 0.0
    data["elapsed_seconds"] = _elapsed(job)
    data["remaining_seconds"] = None
    if job["status"] == "running" and steps_done > 0 and data["elapsed_seconds"]:
        data["remaining_seconds"] = round(data["elapsed_seconds"] * (job["total_steps"] - steps_done) / steps_done)
    if detail:
        data["params"] = json.loads(job["params"])
        data["speed"] = job["speed"]
    return data


def _elapsed(job: sqlite3.Row) -> float | None:
    if not job["started_at"]:
        return None
    start = datetime.fromisoformat(job["started_at"])
    end = datetime.fromisoformat(job["finished_at"]) if job["finished_at"] else datetime.now(timezone.utc)
    return max(0.0, (end - start).total_seconds())


def queue_position(conn: sqlite3.Connection, job_id: int) -> int:
    """このジョブより前に並んでいる (実行中を含む) ジョブの数。GPU の計算と解析は別々に並ぶので、同じ側だけ数える。"""
    kind = conn.execute("SELECT kind FROM jobs WHERE id = ?", (job_id,)).fetchone()
    lane = "NOT IN" if kind is None or kind[0] not in ANALYSIS_KINDS else "IN"
    kinds = "('" + "', '".join(ANALYSIS_KINDS) + "')"
    return conn.execute(f"SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running') AND kind {lane} {kinds} "
                        "AND id < ?", (job_id,)).fetchone()[0]
