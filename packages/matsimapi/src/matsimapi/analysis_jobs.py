"""解析のジョブ (GPU を使わない、サーバーの中で動かす重めの解析)。ボロノイ多面体の解析・結晶構造の判定 (CNA)・配位数・
ウォーレン・カウリーの秩序度・配位多面体。

GPU の計算 (jobs.Scheduler) とは別の作業スレッドで、1 件ずつ順に動かす。GPU の計算の順番待ちには割り込まない。
進み具合 (解析したフレーム数) は workdir/progress.json に書き、jobs.to_dict がそれを読んで進捗として返す。
結果は workdir/result.json。中止はフレームの切れ目で受け付ける。サーバーが止まって中断したものは、
次の起動時に実行待ちに戻してやり直す。
"""
from __future__ import annotations

import json
import threading

from ase.io import read as read_any

from . import db as database
from . import cna, coordination, jobs, polyhedra, settings, sro, structures, voronoi


class Cancelled(Exception):
    pass


def _source_atoms(conn, params: dict, budget: int = 400_000):
    """解析する最終構造と、時間変化に使うフレーム (原子数 × フレーム数 が budget 以内)。"""
    if params.get("job_id") is not None:
        directory = jobs.workdir(params["job_id"])
        if not (directory / "CONTCAR").is_file():
            raise ValueError("元のジョブの最終構造がありません (削除された可能性があります)")
        final = structures.clean(read_any(directory / "CONTCAR", format="vasp"))
        wanted = voronoi.budget_frames(len(final), int(params.get("frames", 0)), budget)
        return final, voronoi.trajectory_frames(directory / "dump.xyz", len(final), wanted)
    return structures.load(params["structure_id"]), []


def run_voronoi(conn, job) -> None:
    params = json.loads(job["params"])
    directory = jobs.workdir(job["id"])
    directory.mkdir(parents=True, exist_ok=True)
    final, frames = _source_atoms(conn, params)
    total = len(frames) + 1
    conn.execute("UPDATE jobs SET total_steps = ?, n_atoms = ? WHERE id = ?", (total, len(final), job["id"]))

    def progress(done: int) -> None:
        (directory / "progress.json").write_text(json.dumps({"done": done, "total": total}))
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        if row is None or row["cancel_requested"]:
            raise Cancelled()

    progress(0)
    result = voronoi.analyze(final, frames, center=params.get("center", "*"),
                             area_threshold=params.get("area_threshold", 0.01),
                             edge_fraction=params.get("edge_fraction", 0.18), top=params.get("top", 10),
                             progress=progress)
    view = structures.view_data(final)
    stride = max(1, -(-len(final) // settings.MAX_VIEW_ATOMS))   # view_data と同じ間引き
    result["view"] = view
    result["view_labels"] = result.pop("labels")[::stride]
    result["species"] = sorted(set(final.get_chemical_symbols()))
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    progress(total)


def run_cna(conn, job) -> None:
    params = json.loads(job["params"])
    directory = jobs.workdir(job["id"])
    directory.mkdir(parents=True, exist_ok=True)
    final, frames = _source_atoms(conn, params, budget=4_000_000)   # CNA はボロノイより 10 倍ほど速い
    total = len(frames) + 1
    conn.execute("UPDATE jobs SET total_steps = ?, n_atoms = ? WHERE id = ?", (total, len(final), job["id"]))

    def progress(done: int) -> None:
        (directory / "progress.json").write_text(json.dumps({"done": done, "total": total}))
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        if row is None or row["cancel_requested"]:
            raise Cancelled()

    progress(0)
    result = cna.analyze(final, frames, progress=progress)
    stride = max(1, -(-len(final) // settings.MAX_VIEW_ATOMS))   # view_data と同じ間引き
    result["view"] = structures.view_data(final)
    result["view_labels"] = result.pop("labels")[::stride]
    result["species"] = sorted(set(final.get_chemical_symbols()))
    result["colors"] = cna.COLORS
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    progress(total)


def run_coord(conn, job) -> None:
    params = json.loads(job["params"])
    directory = jobs.workdir(job["id"])
    directory.mkdir(parents=True, exist_ok=True)
    final, frames = _source_atoms(conn, params, budget=4_000_000)
    total = len(frames) + 1
    conn.execute("UPDATE jobs SET total_steps = ?, n_atoms = ? WHERE id = ?", (total, len(final), job["id"]))

    def progress(done: int) -> None:
        (directory / "progress.json").write_text(json.dumps({"done": done, "total": total}))
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        if row is None or row["cancel_requested"]:
            raise Cancelled()

    progress(0)
    stride = max(1, -(-len(final) // settings.MAX_VIEW_ATOMS))   # view_data と同じ間引き
    result = coordination.analyze(final, frames, cutoff=params.get("cutoff"), per_frame=params.get("per_frame", True),
                                  progress=progress, view_stride=stride)
    result["view"] = structures.view_data(final)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    progress(total)


def _simple_runner(analyze):
    """最終構造とフレームを読み、analyze(final, frames, progress=..., view_stride=...) の結果を書くだけの解析。"""
    def run(conn, job) -> None:
        params = json.loads(job["params"])
        directory = jobs.workdir(job["id"])
        directory.mkdir(parents=True, exist_ok=True)
        final, frames = _source_atoms(conn, params, budget=4_000_000)
        total = len(frames) + 1
        conn.execute("UPDATE jobs SET total_steps = ?, n_atoms = ? WHERE id = ?", (total, len(final), job["id"]))

        def progress(done: int) -> None:
            (directory / "progress.json").write_text(json.dumps({"done": done, "total": total}))
            row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job["id"],)).fetchone()
            if row is None or row["cancel_requested"]:
                raise Cancelled()

        progress(0)
        stride = max(1, -(-len(final) // settings.MAX_VIEW_ATOMS))   # view_data と同じ間引き
        result = analyze(final, frames, params, progress, stride)
        result["view"] = structures.view_data(final)
        result["view_stride"] = stride   # 3D 表示の i 番目の原子 = 構造の i × stride 番目の原子
        result["species"] = result.get("species") or sorted(set(final.get_chemical_symbols()))
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
        progress(total)
    return run


run_sro = _simple_runner(lambda final, frames, params, progress, stride: sro.analyze(
    final, frames, progress=progress, view_stride=stride))
run_poly = _simple_runner(lambda final, frames, params, progress, stride: polyhedra.analyze(
    final, frames, center=params.get("center"), ligand=params.get("ligand"), cutoff=params.get("cutoff"),
    progress=progress))

RUNNERS = {"voronoi": run_voronoi, "cna": run_cna, "coord": run_coord, "sro": run_sro, "poly": run_poly}


class Worker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="analysis-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        kinds = "('" + "', '".join(jobs.ANALYSIS_KINDS) + "')"
        while not self._stop.is_set():
            try:
                conn = database.connect()
                try:
                    job = conn.execute(f"SELECT * FROM jobs WHERE status = 'queued' AND kind IN {kinds} "
                                       "ORDER BY id LIMIT 1").fetchone()
                    if job is not None:
                        self._run(conn, job)
                finally:
                    conn.close()
            except Exception as exc:   # 作業スレッドが止まると解析が進まなくなるので落とさない
                print(f"[analysis] {type(exc).__name__}: {exc}", flush=True)
            self._stop.wait(1.0)

    def _run(self, conn, job) -> None:
        started = conn.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                               (jobs.now(), job["id"])).rowcount
        if not started:
            return
        try:
            RUNNERS[job["kind"]](conn, job)
            status, message = "done", ""
        except Cancelled:
            status, message = "cancelled", "中止しました"
        except ValueError as exc:
            status, message = "failed", str(exc)[:300]
        except Exception as exc:
            status, message = "failed", f"解析に失敗しました ({type(exc).__name__}: {str(exc)[:200]})"
        conn.execute("UPDATE jobs SET status = ?, message = ?, finished_at = ? WHERE id = ?",
                     (status, message, jobs.now(), job["id"]))


worker = Worker()


def requeue_interrupted(conn) -> None:
    """サーバーが止まって途中で終わった解析のジョブを、実行待ちに戻す (起動時に呼ぶ)。"""
    kinds = "('" + "', '".join(jobs.ANALYSIS_KINDS) + "')"
    conn.execute(f"UPDATE jobs SET status = 'queued', started_at = NULL WHERE status = 'running' AND kind IN {kinds}")
