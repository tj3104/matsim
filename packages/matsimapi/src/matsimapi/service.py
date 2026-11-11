"""計算の操作をまとめた層。MatSimApp の API・Client・MCP サーバーはどれもここを呼ぶ。

どの関数も Web を知らない。第 1 引数は SQLite の接続で、コミットは呼び出し側が行う (db.transaction)。
入力の誤りは ValueError、指定したものがないときは NotFound で知らせる。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path

from ase.io import read as ase_read
from ase.io import write as ase_write
from gpumd_toolkit.structure import StructureHandler
from pydantic import BaseModel, Field

from . import (__version__, ase_engine, calcjob, distribution, jobs, kinds, mdjob, modeling, potentials, results,
               settings, structures, tutorial_kinds, voronoi)


class NotFound(ValueError):
    """指定した構造・ジョブなどが見つからない。"""


def limits() -> dict:
    """入力の上限。"""
    return {"max_atoms": settings.MAX_ATOMS, "warn_atoms": settings.WARN_ATOMS,
            "max_total_steps": settings.MAX_TOTAL_STEPS, "max_stages": settings.MAX_STAGES,
            "max_repeat": settings.MAX_REPEAT}


def set_mp_key(key: str) -> bool:
    """Materials Project の API キーを保存する。空文字で削除。"""
    config = structures.read_config()
    if key:
        config["mp_api_key"] = key
    else:
        config.pop("mp_api_key", None)
    structures.write_config(config)
    return bool(key)


# ---- 構造 -------------------------------------------------------------------------

def list_structures(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT * FROM structures ORDER BY id DESC").fetchall()
    return [structures.row_to_dict(row) for row in rows]


def add_structure_bytes(db: sqlite3.Connection, filename: str, data: bytes, name: str = "") -> int:
    """構造ファイルの中身を検証して登録する。"""
    filename = Path(filename or "").name
    atoms = structures.parse_upload(filename, data)
    return structures.save(db, atoms, name.strip() or Path(filename).stem, "upload", filename)


def add_sample(db: sqlite3.Connection, key: str) -> int:
    """動作確認用のテスト構造 (Cu・MgO) を登録する。"""
    atoms, label = structures.sample(key)
    return structures.save(db, atoms, label, "sample", key)


def _repeat(na: int, nb: int, nc: int) -> tuple[int, int, int]:
    if any(not 1 <= r <= settings.MAX_REPEAT for r in (na, nb, nc)):
        raise ValueError(f"スーパーセルの繰り返しは 1〜{settings.MAX_REPEAT} です")
    return na, nb, nc


def structure_detail(db: sqlite3.Connection, structure_id: int, na: int = 1, nb: int = 1, nc: int = 1) -> dict:
    """構造の情報と 3D 表示用のデータ。na, nb, nc を付けるとスーパーセルで返す。"""
    row = db.execute("SELECT * FROM structures WHERE id = ?", (structure_id,)).fetchone()
    if row is None:
        raise NotFound("構造が見つかりません")
    repeat = _repeat(na, nb, nc)
    atoms = structures.load(structure_id)
    repeated = structures.repeated_info(atoms, repeat)
    data = structures.row_to_dict(row)
    data["repeat"] = {"repeat": repeat, **repeated, "too_many": repeated["n_atoms"] > settings.MAX_ATOMS,
                      "over_guideline": repeated["n_atoms"] > settings.WARN_ATOMS}
    if repeated["n_atoms"] <= settings.MAX_ATOMS:
        data["view"] = structures.view_data(atoms.repeat(repeat))
    return data


def default_repeat(db: sqlite3.Connection, structure_id: int) -> list[int]:
    """繰り返しを指定しないときのスーパーセル。"""
    row = db.execute("SELECT * FROM structures WHERE id = ?", (structure_id,)).fetchone()
    if row is None:
        raise NotFound("構造が見つかりません")
    return structures.row_to_dict(row)["default_repeat"]


def delete_structure(db: sqlite3.Connection, structure_id: int) -> None:
    if not structures.delete(db, structure_id):
        raise NotFound("構造が見つかりません")


def delete_structures(db: sqlite3.Connection, ids: list[int]) -> dict:
    """構造をまとめて削除する。見つからなかったものは missing に返す。"""
    deleted, missing = 0, []
    for structure_id in dict.fromkeys(ids):
        if structures.delete(db, structure_id):
            deleted += 1
        else:
            missing.append(structure_id)
    return {"deleted": deleted, "missing": missing}


def mp_import(db: sqlite3.Connection, material_id: str, conventional: bool = True) -> int:
    """Materials Project の構造を取得して登録する。"""
    atoms = structures.mp_fetch(material_id, conventional)
    if len(atoms) > settings.MAX_ATOMS:
        raise ValueError("原子数が多すぎます")
    name = f"{atoms.get_chemical_formula(mode='metal')} ({material_id})"
    return structures.save(db, atoms, name, "mp", material_id)


# ---- 構造のモデリング -----------------------------------------------------------------

# 大きな構造を同時にいくつも組み立てるとサーバーが重くなるので、同時に 2 つまで
_model_slots = threading.Semaphore(2)


class ModelIn(BaseModel):
    op: str = Field(max_length=40)
    structure_id: int | None = None
    options: dict = Field(default_factory=dict)
    name: str = Field("", max_length=60)


def _structure_row(db: sqlite3.Connection, structure_id: int, label: str = "構造") -> sqlite3.Row:
    row = db.execute("SELECT * FROM structures WHERE id = ?", (structure_id,)).fetchone()
    if row is None:
        raise ValueError(f"{label}が見つかりません")
    return row


def _model_cache_path(body: ModelIn) -> Path:
    key = json.dumps({"op": body.op, "structure_id": body.structure_id, "options": body.options},
                     sort_keys=True, ensure_ascii=False)
    return settings.TMP_DIR / f"model-{hashlib.sha256(key.encode()).hexdigest()[:32]}.xyz"


def _model_build(db: sqlite3.Connection, body: ModelIn):
    """モデリングを実行する。プレビューと同じ入力の結果があれば使い回す。"""
    op = modeling.get_op(body.op)
    base = None
    atoms = None
    if op.needs_structure:
        if body.structure_id is None:
            raise ValueError("元の構造を選んでください")
        base = _structure_row(db, body.structure_id)
        atoms = structures.load(body.structure_id)
    others = {}
    for key in modeling.structure_refs(op):
        ref = body.options.get(key)
        if ref in (None, ""):
            continue
        try:
            ref = int(ref)
        except (TypeError, ValueError):
            raise ValueError("選んだ構造が正しくありません") from None
        _structure_row(db, ref, "選んだ構造")
        others[key] = structures.load(ref)
    cache = _model_cache_path(body)
    meta = cache.with_suffix(".json")
    if cache.is_file() and meta.is_file():
        info = json.loads(meta.read_text())
        return op, base, structures.clean(ase_read(cache, format="extxyz", index=-1)), info["warnings"], info["summary"]
    with _model_slots:
        result, warnings, values = modeling.build(op, atoms, body.options, others)
    result = structures.clean(result)
    summary = modeling.summary_text(op, values)
    ase_write(cache, result, format="extxyz")
    meta.write_text(json.dumps({"warnings": warnings, "summary": summary}, ensure_ascii=False))
    # プレビューの使い回し用なので、新しいものから 20 件だけ残す
    old = sorted(settings.TMP_DIR.glob("model-*.xyz"), key=lambda p: p.stat().st_mtime, reverse=True)[20:]
    for path in old:
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
    return op, base, result, warnings, summary


def _model_default_name(op, base, atoms) -> str:
    formula = atoms.get_chemical_formula(mode="metal")
    return (f"{base['name']} → {op.label}" if base is not None else f"{formula} ({op.label})")[:60]


def model_preview(db: sqlite3.Connection, body: ModelIn) -> dict:
    """モデリングの結果を保存せずに見る。"""
    op, base, atoms, warnings, summary = _model_build(db, body)
    close = modeling.too_close(atoms)
    if close:
        warnings = [*warnings, close]
    return {"formula": atoms.get_chemical_formula(mode="metal"), "n_atoms": len(atoms),
            "info": structures._jsonable(StructureHandler.info(atoms).as_dict()), "warnings": warnings,
            "summary": summary, "default_name": _model_default_name(op, base, atoms),
            "over_guideline": len(atoms) > settings.WARN_ATOMS, "view": structures.view_data(atoms)}


def model_save(db: sqlite3.Connection, body: ModelIn) -> int:
    """モデリングの結果を構造として登録する。"""
    op, base, atoms, _, summary = _model_build(db, body)
    name = body.name.strip() or _model_default_name(op, base, atoms)
    origin = f" (元: {base['name']})" if base is not None else ""
    return structures.save(db, atoms, name, "model", f"{summary}{origin}")


# ---- ポテンシャル -------------------------------------------------------------------

def list_potentials(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT * FROM potentials ORDER BY builtin DESC, id").fetchall()
    return [potentials.row_to_dict(row) for row in rows]


# ---- MD --------------------------------------------------------------------------

def md_preview(db: sqlite3.Connection, body: mdjob.MDJobIn) -> dict:
    """実行前の確認。原子数・所要時間の目安・生成される run.in を返す (ファイルは残さない)。"""
    atoms, potential, _ = mdjob.load_inputs(db, body)
    with tempfile.TemporaryDirectory(dir=settings.TMP_DIR) as tmp:
        calc, notes = mdjob.build(body, atoms, potential, Path(tmp))
        close = modeling.too_close(atoms)   # 重なった原子は MD で壊れるので、投入の前に知らせる
        return mdjob.describe(db, body, calc, [close, *notes] if close else notes)


def md_submit(db: sqlite3.Connection, body: mdjob.MDJobIn, user_id: int | None = None) -> dict:
    """MD ジョブをキューに入れる。"""
    atoms, potential, structure = mdjob.load_inputs(db, body)
    name = body.name.strip() or f"{structure['formula']} MD"
    n_atoms = len(atoms) * body.repeat[0] * body.repeat[1] * body.repeat[2]
    summary = {"structure": structure["name"], "formula": structure["formula"], "potential": potential["name"],
               "time_ps": round(sum(s.steps for s in body.stages) * body.time_step / 1000, 4), "kind_label": "MD",
               "engine": ase_engine.engine_key(potential)}   # gpumd / cpunep / sevennet (速さの目安を同じエンジンから取る)
    job_id = jobs.create(db, name=name, kind="md", params=body.model_dump(), summary=summary, n_atoms=n_atoms,
                         total_steps=sum(s.steps for s in body.stages), user_id=user_id)
    directory = jobs.workdir(job_id)
    try:
        calc, _ = mdjob.build(body.model_copy(update={"name": name}), atoms, potential, directory)
        calc.write_inputs()
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    jobs.enqueue(db, job_id)
    return {"id": job_id}


# ---- MD 以外の計算 -----------------------------------------------------------------

def list_kinds() -> dict:
    """計算の種類と、それぞれの入力欄の定義。"""
    return {"groups": kinds.GROUPS, "kinds": kinds.public_list()}


def _scan(body: calcjob.CalcJobIn) -> list[tuple[float | None, calcjob.CalcJobIn]]:
    """温度スキャンを指定されたら、温度ごとの入力に分ける。指定がなければ入力そのもの 1 つ。"""
    kind = kinds.get(body.kind)
    has_scan = any(spec["key"] == "scan_temperatures" for spec in kind.fields)
    temperatures = kinds.scan_temperatures(str(body.options.get("scan_temperatures") or "")) if has_scan else []
    if not temperatures:
        return [(None, body)]
    return [(t, body.model_copy(update={"options": {**body.options, "temperature": t, "scan_temperatures": ""}}))
            for t in temperatures]


def calc_preview(db: sqlite3.Connection, body: calcjob.CalcJobIn) -> dict:
    """実行前の確認 (ファイルは残さない)。温度スキャンなら 1 点目の内容と、全体の合計を返す。"""
    kind, atoms, used, _ = calcjob.load_inputs(db, body)
    scan = _scan(body)
    with tempfile.TemporaryDirectory(dir=settings.TMP_DIR) as tmp:
        calc, notes, _ = calcjob.build(scan[0][1], kind, atoms, used, Path(tmp))
        close = modeling.too_close(atoms)
        result = calcjob.describe(db, scan[0][1], calc, [close, *notes] if close else notes)
    if len(scan) > 1:
        temps = [t for t, _ in scan]
        result["notes"] = [f"温度スキャン: {len(temps)} 個のジョブを作ります "
                           f"({', '.join(f'{t:g}' for t in temps)} K)。下の内容は 1 つ分で、所要時間は全体の合計です",
                           *result["notes"]]
        result["estimated_seconds"] = round(result["estimated_seconds"] * len(temps), 1)
        result["scan"] = temps
    return result


def calc_submit(db: sqlite3.Connection, body: calcjob.CalcJobIn, user_id: int | None = None) -> dict:
    """MD 以外の計算をキューに入れる。温度スキャンなら温度ごとにジョブを作り、同じシリーズにまとめる。"""
    kind, atoms, used, structure = calcjob.load_inputs(db, body)
    scan = _scan(body)
    base_name = body.name.strip() or f"{structure['formula']} {kind.label}"
    series = None
    if len(scan) > 1:
        active = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running', 'preparing')").fetchone()[0]
        if active + len(scan) > settings.MAX_QUEUED_JOBS:
            raise ValueError(f"実行待ちのジョブは {settings.MAX_QUEUED_JOBS} 件までです "
                             f"(今 {active} 件、温度スキャンで {len(scan)} 件)")
        series = f"scan-{secrets.token_hex(6)}"
    repeat = (1, 1, 1) if kind.no_repeat else body.repeat
    n_atoms = len(atoms) * repeat[0] * repeat[1] * repeat[2]
    ids = []
    for temperature, item in scan:
        name = f"{base_name} {temperature:g}K" if temperature is not None else base_name
        # 総ステップ数は組み立ててみないと分からないので、仮の値で行を作ってから更新する
        job_id = jobs.create(db, name=name[:60], kind=kind.key, params=item.model_dump(), summary={}, n_atoms=n_atoms,
                             total_steps=0, user_id=user_id, series=series)
        directory = jobs.workdir(job_id)
        try:
            calc, _, options = calcjob.build(item, kind, atoms, used, directory)
            calc.write_inputs()
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            for done in ids:
                shutil.rmtree(jobs.workdir(done), ignore_errors=True)
            raise
        summary = {"structure": structure["name"], "formula": structure["formula"],
                   "potential": " / ".join(p["name"] for p in used), "time_ps": round(calc.builder.total_time_ps(), 4),
                   "kind_label": kind.label, "engine": ase_engine.engine_key(used[0])}
        if temperature is not None:
            summary["temperature"] = temperature
        params = {**item.model_dump(), "options": options}
        db.execute("UPDATE jobs SET total_steps = ?, summary = ?, params = ? WHERE id = ?",
                   (calc.builder.total_steps, json.dumps(summary), json.dumps(params), job_id))
        ids.append(job_id)
    for job_id in ids:
        jobs.enqueue(db, job_id)
    return {"id": ids[0], "ids": ids, "series": series}


# ---- 計算結果の解析 --------------------------------------------------------------------

class VoronoiIn(BaseModel):
    job_id: int | None = None
    structure_id: int | None = None
    center: str = Field("*", pattern=r"^(\*|[A-Z][a-z]?)$")
    area_threshold: float = Field(0.01, ge=0, le=0.2)
    edge_fraction: float = Field(0.18, ge=0, le=0.5)
    frames: int = Field(20, ge=0, le=200)        # 時間変化に使うトラジェクトリのフレーム数 (0 で最終構造だけ)
    top: int = Field(10, ge=1, le=30)


class CnaIn(BaseModel):
    job_id: int | None = None
    structure_id: int | None = None
    frames: int = Field(40, ge=0, le=500)        # 時間変化に使うトラジェクトリのフレーム数 (0 で最終構造だけ)


class CoordIn(BaseModel):
    job_id: int | None = None
    structure_id: int | None = None
    cutoff: float | None = Field(None, ge=0.5, le=10)   # 空なら g(r) の第 1 の谷から自動
    frames: int = Field(40, ge=0, le=500)
    per_frame: bool = True   # 自動のとき、時間変化の数える距離をフレームごとに決め直す


class SroIn(BaseModel):
    job_id: int | None = None
    structure_id: int | None = None
    frames: int = Field(40, ge=0, le=500)


class PolyIn(BaseModel):
    job_id: int | None = None
    structure_id: int | None = None
    center: str | None = Field(None, pattern=r"^[A-Z][a-z]?$")     # 空なら自動 (陰イオンでない元素)
    ligand: str | None = Field(None, pattern=r"^(\*|[A-Z][a-z]?)$")  # 空なら自動 (O などの陰イオン、なければすべて)
    cutoff: float | None = Field(None, ge=0.5, le=10)
    frames: int = Field(40, ge=0, le=500)


# 解析の種類 → (入力, 表示名, 結果を別の種類のジョブに使ったときの言葉)
ANALYSES = {
    "voronoi": (VoronoiIn, "ボロノイ解析", "ボロノイ解析"),
    "cna": (CnaIn, "結晶構造の判定", "結晶構造の判定"),
    "coord": (CoordIn, "配位数", "配位数の解析"),
    "sro": (SroIn, "短範囲秩序", "短範囲秩序の解析"),
    "poly": (PolyIn, "配位多面体", "配位多面体の解析"),
}


def submit_analysis(db: sqlite3.Connection, kind: str, body: BaseModel, user_id: int | None = None) -> dict:
    """解析のジョブを作って実行待ちに入れる。同じ条件で済んでいる (実行中の) 解析があれば、それを返す。

    GPU の計算とは別に、ワーカーの中で順に動かす。条件には版も入れる (解析の計算を直したあとに、
    直す前の結果を使い回さないように)。
    """
    label = ANALYSES[kind][1]
    job_id, structure_id = body.job_id, body.structure_id
    params = {**body.model_dump(), "app_version": __version__}
    if (job_id is None) == (structure_id is None):
        raise ValueError("ジョブか構造のどちらか一方を選んでください")
    if job_id is not None:
        row = job_row(db, job_id)
        if row["status"] != "done" or row["kind"] in jobs.ANALYSIS_KINDS:
            raise ValueError("完了した計算のジョブだけ解析できます")
        if not (jobs.workdir(job_id) / "CONTCAR").is_file():
            raise ValueError("このジョブには最終構造がありません")
        source_name, n_atoms = row["name"], row["n_atoms"]
        formula = json.loads(row["summary"]).get("formula", "")
    else:
        row = _structure_row(db, structure_id)
        source_name, n_atoms, formula = row["name"], row["n_atoms"], row["formula"]
        if n_atoms > 200_000:
            raise ValueError("原子が多すぎます (20 万原子まで)")
    for done in db.execute("SELECT id, params FROM jobs WHERE kind = ? AND status IN ('done', 'queued', 'running') "
                           "ORDER BY id DESC", (kind,)).fetchall():
        if json.loads(done["params"]) == params:
            return {"id": done["id"], "reused": True}
    summary = {"kind_label": label, "structure": source_name, "formula": formula, "potential": "—",
               "time_ps": 0, "source": {"job_id": job_id, "structure_id": structure_id}}
    new_id = jobs.create(db, name=f"{source_name} の{label}"[:60], kind=kind, params=params, summary=summary,
                         n_atoms=n_atoms, total_steps=1 + (body.frames if job_id is not None else 0), user_id=user_id)
    jobs.enqueue(db, new_id)
    return {"id": new_id, "reused": False}


def analysis_result(db: sqlite3.Connection, job_id: int, kind: str) -> dict:
    """解析のジョブの結果と、投入したときの条件。"""
    row = job_row(db, job_id)
    if row["kind"] != kind:
        raise NotFound(f"{ANALYSES[kind][2]}のジョブではありません")
    path = jobs.workdir(job_id) / "result.json"
    result = json.loads(path.read_text()) if row["status"] == "done" and path.is_file() else None
    if result is not None and kind == "voronoi":
        result = voronoi.enrich(result)   # 前の版の結果にも、まとまりと説明を足す
    return {"job": jobs.to_dict(row), "params": json.loads(row["params"]), "result": result}


class DistributionIn(BaseModel):
    job_id: int | None = None
    structure_id: int | None = None
    species: list[str] = Field(default_factory=list, max_length=20)
    axis: str = Field("z", pattern=r"^[xyz]$")
    bin_width: float = Field(1.0, gt=0, le=1000)
    frames: int = Field(0, ge=0, le=200)        # トラジェクトリの最後の何枚で平均するか (0 で最終構造だけ)


def atom_distribution(db: sqlite3.Connection, body: DistributionIn) -> dict:
    """選んだ元素の原子を軸に射影し、分割幅ごとの原子数を返す。"""
    if (body.job_id is None) == (body.structure_id is None):
        raise ValueError("ジョブか構造のどちらか一方を選んでください")
    if any(not re.fullmatch(r"[A-Z][a-z]?", s) for s in body.species):
        raise ValueError("元素記号が正しくありません")
    if body.job_id is not None:
        row = job_row(db, body.job_id)
        if row["status"] != "done" or row["kind"] in jobs.ANALYSIS_KINDS:
            raise ValueError("完了した計算のジョブだけ解析できます")
        directory = jobs.workdir(body.job_id)
        if not (directory / "CONTCAR").is_file():
            raise ValueError("このジョブには最終構造がありません")
        final = structures.clean(ase_read(directory / "CONTCAR", format="vasp"))
        frames = [final]
        if body.frames:
            picked = voronoi.trajectory_frames(directory / "dump.xyz", len(final), body.frames, last=True)
            if picked:
                frames = [atoms for _, atoms in picked]
        source = {"kind": "job", "id": body.job_id, "name": row["name"]}
    else:
        row = _structure_row(db, body.structure_id)
        frames = [structures.load(body.structure_id)]
        source = {"kind": "structure", "id": body.structure_id, "name": row["name"]}
    result = distribution.profile(frames, body.species, body.axis, body.bin_width)
    result["source"] = source
    result["all_species"] = sorted(set(frames[0].get_chemical_symbols()))
    result["cell_lengths"] = [round(float(v), 3) for v in frames[0].cell.lengths()]
    return result


# ---- シリーズ (温度スキャンでまとめて投入したジョブ) -----------------------------------------

def series_summary(db: sqlite3.Connection, series: str) -> dict:
    """シリーズのジョブと、それぞれの結果の要点。融点 (固液共存法) なら融点の推定も返す。"""
    if not re.fullmatch(r"scan-[0-9a-f]{6,32}", series):
        raise NotFound("シリーズが見つかりません")
    rows = db.execute("SELECT * FROM jobs WHERE series = ? ORDER BY id", (series,)).fetchall()
    if not rows:
        raise NotFound("シリーズが見つかりません")
    items = []
    for row in rows:
        job = jobs.to_dict(row)
        params = json.loads(row["params"])
        overview = results.results_overview(jobs.workdir(row["id"]), row["n_atoms"]) if row["status"] == "done" else {}
        job["temperature"] = params.get("options", {}).get("temperature")
        job["highlights"] = overview.get("highlights", [])
        items.append(job)
    kind = rows[0]["kind"]
    estimate = None
    if kind == "melting":
        points = []
        for item in items:
            values = {h.get("key"): h["value"] for h in item["highlights"] if h.get("key")}
            if "melting:code" in values:
                points.append({"temperature": float(item["temperature"]), "code": int(values["melting:code"])})
        if points:
            estimate = tutorial_kinds.melting_point(points)
    return {"series": series, "kind": kind, "kind_label": json.loads(rows[0]["summary"]).get("kind_label", kind),
            "jobs": items, "estimate": estimate}


# ---- ジョブ -----------------------------------------------------------------------

def job_row(db: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise NotFound("ジョブが見つかりません")
    return row


def list_jobs(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT * FROM jobs WHERE status != 'preparing' ORDER BY id DESC LIMIT 200").fetchall()
    return [jobs.to_dict(row) for row in rows]


def job_detail(db: sqlite3.Connection, job_id: int) -> dict:
    """ジョブの状態・順番・ログの末尾・ファイルの一覧。"""
    row = job_row(db, job_id)
    data = jobs.to_dict(row, detail=True)
    data["queue_position"] = jobs.queue_position(db, job_id) if row["status"] == "queued" else 0
    data["log"] = jobs.tail(jobs.workdir(job_id) / "gpumd.out")
    data["error_log"] = jobs.tail(jobs.workdir(job_id) / "gpumd.err", 2000)
    data["files"] = results.list_files(jobs.workdir(job_id))
    return data


def delete_jobs(db: sqlite3.Connection, ids: list[int]) -> dict:
    """ジョブをまとめて削除する。実行中・実行待ちのものは消さずに skipped に返す。"""
    deleted, skipped, missing = 0, [], []
    for job_id in dict.fromkeys(ids):
        row = db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            missing.append(job_id)
        elif row["status"] in jobs.ACTIVE or row["status"] == "preparing":
            skipped.append(job_id)
        else:
            jobs.delete(db, job_id)
            deleted += 1
    return {"deleted": deleted, "skipped": skipped, "missing": missing}


def job_thermo(db: sqlite3.Connection, job_id: int) -> dict:
    """熱力学量の時系列とステージごとの要約。実行中でもその時点までを返す。"""
    row = job_row(db, job_id)
    directory = jobs.workdir(job_id)
    return {**results.thermo_series(directory, row["n_atoms"]),
            "summary": results.stage_summary(directory, row["n_atoms"])}


def job_results(db: sqlite3.Connection, job_id: int) -> dict:
    """要点の数値と、グラフにできる表の一覧 (列の名前つき)。"""
    row = job_row(db, job_id)
    return results.results_overview(jobs.workdir(job_id), row["n_atoms"])


def job_dataset(db: sqlite3.Connection, job_id: int, key: str) -> dict:
    """表 1 つ分のデータ。"""
    row = job_row(db, job_id)
    return results.dataset(jobs.workdir(job_id), row["n_atoms"], key)


def job_trajectory(db: sqlite3.Connection, job_id: int) -> dict:
    job_row(db, job_id)
    return results.trajectory_frames(jobs.workdir(job_id))


def job_file(db: sqlite3.Connection, job_id: int, name: str) -> Path:
    """ジョブのファイルのパス。ジョブのファイルの一覧にある名前だけを受け付ける。"""
    job_row(db, job_id)
    return results.resolve_file(jobs.workdir(job_id), name)


def export_job(db: sqlite3.Connection, job_id: int, directory: str, max_file_mb: float = 200) -> dict:
    """ジョブのファイルを directory にコピーする (作業のディレクトリに結果を置く)。大きすぎるファイルは飛ばして skipped に返す。"""
    row = job_row(db, job_id)
    target = Path(directory).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    copied, skipped = [], []
    source = jobs.workdir(job_id)
    for item in results.list_files(source):
        if item["size"] > max_file_mb * 1024 * 1024:
            skipped.append(item)
            continue
        destination = target / item["name"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / item["name"], destination)
        copied.append(item["name"])
    (target / "job.json").write_text(json.dumps({k: row[k] for k in row.keys() if k not in ("log",)}, ensure_ascii=False,
                                                indent=1, default=str))
    return {"directory": str(target), "copied": copied, "skipped": skipped}


def export_structure(db: sqlite3.Connection, structure_id: int, directory: str) -> dict:
    """構造を directory に cif・xyz (拡張 xyz)・POSCAR で書き出す。"""
    from ase.io import write
    row = db.execute("SELECT * FROM structures WHERE id = ?", (structure_id,)).fetchone()
    if row is None:
        raise NotFound("構造が見つかりません")
    atoms = structures.load(structure_id)
    target = Path(directory).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    stem = f"structure{structure_id}_{atoms.get_chemical_formula(mode='metal')}"
    files = []
    for suffix, fmt in ((".cif", "cif"), (".xyz", "extxyz"), (".vasp", "vasp")):
        path = target / (stem + suffix)
        write(path, atoms, format=fmt)
        files.append(path.name)
    return {"directory": str(target), "files": files, "name": row["name"]}


def job_text(db: sqlite3.Connection, job_id: int, name: str) -> dict:
    """ジョブのファイル (run.in・plumed.dat など) の中身をテキストで返す。大きいものは先頭だけ。"""
    job_row(db, job_id)
    return results.read_text(jobs.workdir(job_id), name)


def _phonon_dir(db: sqlite3.Connection, job_id: int) -> Path:
    row = job_row(db, job_id)
    if row["kind"] != "phonon" or row["status"] != "done":
        raise ValueError("完了したフォノン分散のジョブだけ、振動のようすを見られます")
    directory = jobs.workdir(job_id)
    if not (directory / "D.out").is_file():
        raise ValueError("このジョブには動的行列 (D.out) がありません")
    return directory


def phonon_overview(db: sqlite3.Connection, job_id: int) -> dict:
    """フォノンの振動のようすを見るための情報: q 点の数・分枝の数・高対称点・基本単位胞。"""
    from . import phonon_modes
    return phonon_modes.overview(_phonon_dir(db, job_id))


def phonon_mode(db: sqlite3.Connection, job_id: int, q: int, band: int) -> dict:
    """q 点 q・分枝 band (0 始まり、振動数の低い順) の振動数と、原子ごとの変位の振幅。"""
    from . import phonon_modes
    return phonon_modes.mode(_phonon_dir(db, job_id), q, band)


def job_zip(db: sqlite3.Connection, job_id: int) -> Path:
    """結果一式の zip を作る。使い終わったら呼び出し側で消す。"""
    row = job_row(db, job_id)
    if row["status"] in ("queued", "running"):
        raise ValueError("実行が終わってからダウンロードしてください")
    return results.make_zip(jobs.workdir(job_id), job_id)


def save_final_structure(db: sqlite3.Connection, job_id: int, name: str = "") -> int:
    """完了したジョブの最終構造を、次の計算に使える構造として登録する。"""
    row = job_row(db, job_id)
    if row["status"] != "done":
        raise ValueError("完了したジョブだけが対象です")
    from gpumd_toolkit.md import GPUMDResult
    directory = jobs.workdir(job_id)
    result = GPUMDResult(workdir=directory, command=None, n_atoms=row["n_atoms"], run_in=directory / "run.in")
    atoms = structures.clean(result.final_structure())
    return structures.save(db, atoms, name.strip() or f"{row['name']} の最終構造", "job", f"job {job_id}")


