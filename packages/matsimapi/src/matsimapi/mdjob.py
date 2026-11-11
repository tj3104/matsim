"""MD ジョブの入力検証と、GPUMD_toolkit による run.in / model.xyz の生成。"""
from __future__ import annotations

import sqlite3
import warnings
from pathlib import Path
from typing import Literal

from ase import Atoms
from gpumd_toolkit import GPUMDCalculation
from gpumd_toolkit.config import GPUMDEnvironment
from gpumd_toolkit.structure import StructureHandler
from pydantic import BaseModel, Field

from . import potentials, settings, structures

Repeat = tuple[int, int, int]


class StageIn(BaseModel):
    """MD の 1 ステージ。temperature_end を入れるとその間に線形に昇温・降温する。"""
    ensemble: Literal["nve", "nvt", "npt"]
    steps: int = Field(ge=1, le=settings.MAX_TOTAL_STEPS)
    temperature: float | None = Field(None, gt=0, le=20000)
    temperature_end: float | None = Field(None, gt=0, le=20000)
    pressure: float = Field(0.0, ge=-100, le=1000)   # GPa


class MDJobIn(BaseModel):
    name: str = Field("", max_length=60)
    structure_id: int
    potential_id: int
    repeat: Repeat = (1, 1, 1)
    time_step: float = Field(1.0, ge=0.01, le=10.0)   # fs
    minimize: bool = False
    stages: list[StageIn] = Field(min_length=1, max_length=settings.MAX_STAGES)
    thermo_interval: int = Field(100, ge=1, le=1_000_000)
    traj_interval: int = Field(1000, ge=0, le=10_000_000)   # 0 でトラジェクトリを保存しない


def _environment() -> GPUMDEnvironment:
    # アプリ側で gpumd を直接起動するので、toolkit の環境は入力生成にしか使わない
    return GPUMDEnvironment(venv=None, use_login_shell=False, gpumd_command=str(settings.GPUMD_BIN))


def _validate(params: MDJobIn, atoms: Atoms, potential: dict) -> None:
    if any(not 1 <= r <= settings.MAX_REPEAT for r in params.repeat):
        raise ValueError(f"スーパーセルの繰り返しは 1〜{settings.MAX_REPEAT} です")
    n_atoms = len(atoms) * params.repeat[0] * params.repeat[1] * params.repeat[2]
    if n_atoms > settings.MAX_ATOMS:
        raise ValueError(f"原子数が上限を超えています ({n_atoms:,} > {settings.MAX_ATOMS:,})")
    missing = potentials.missing_elements(atoms.get_chemical_symbols(), potential["elements"])
    if missing:
        raise ValueError(f"このポテンシャルは {', '.join(missing)} に対応していません")
    total = sum(stage.steps for stage in params.stages)
    if total > settings.MAX_TOTAL_STEPS:
        raise ValueError(f"合計ステップ数が上限を超えています ({total:,} > {settings.MAX_TOTAL_STEPS:,})")
    for i, stage in enumerate(params.stages, 1):
        if stage.ensemble != "nve" and stage.temperature is None:
            raise ValueError(f"ステージ {i}: 温度を入力してください")
    if params.stages[0].ensemble == "nve" and params.stages[0].temperature is None:
        raise ValueError("最初のステージが NVE のときは、初期温度として温度を入力してください")
    if params.traj_interval:
        frames = sum(stage.steps // params.traj_interval for stage in params.stages)
        if frames * n_atoms > settings.MAX_TRAJ_ATOM_FRAMES:
            raise ValueError("トラジェクトリが大きくなりすぎます。保存間隔を長くしてください "
                             f"({frames:,} フレーム × {n_atoms:,} 原子)")


def build(params: MDJobIn, atoms: Atoms, potential: dict, workdir: Path) -> tuple[GPUMDCalculation, list[str]]:
    """入力から GPUMDCalculation を組み立てる。toolkit の警告も一緒に返す。
    SevenNet のポテンシャルか、GPUMD がなくて NEP を CPU で計算するときは、ASE で計算するジョブ (ase_engine.AseJob) を組み立てる。"""
    _validate(params, atoms, potential)
    from . import ase_engine
    if ase_engine.uses_ase(potential):
        return ase_engine.build_md(params, atoms, potential, workdir), [ase_engine.engine_note(potential)]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = params.stages[0]
        calc = GPUMDCalculation(
            atoms, potential["path"], workdir,
            name=params.name or None,
            time_step=params.time_step,
            initial_temperature=first.temperature if first.ensemble == "nve" else None,
            repeat=params.repeat,
            max_atoms=settings.MAX_ATOMS,
            environment=_environment(),
        )
        # 表示用なので座標だけを保存する (速度・力を付けるとファイルが数倍になる)
        calc.set_dump(thermo=params.thermo_interval, traj=params.traj_interval or None, traj_properties=())
        if params.minimize:
            calc.minimize()
        for stage in params.stages:
            if stage.ensemble == "nve":
                calc.nve(steps=stage.steps)
            elif stage.ensemble == "nvt":
                calc.nvt(temperature=stage.temperature, temperature_end=stage.temperature_end, steps=stage.steps)
            else:
                calc.npt(temperature=stage.temperature, temperature_end=stage.temperature_end,
                         pressure=stage.pressure, steps=stage.steps)
        problem = cell_problem(calc, atoms, params.repeat)
        if problem:
            raise ValueError(problem)
    notes = [str(w.message) for w in caught if not issubclass(w.category, DeprecationWarning)]
    return calc, list(dict.fromkeys(notes))


def cell_problem(calc: GPUMDCalculation, unit: Atoms, repeat) -> str | None:
    """GPUMD が受け付けないセル形状なら、どう直せばよいかを添えた説明を返す。問題なければ None。

    NEP では、ある方向の厚みが 2.5×(カットオフ+1) 以下なのに、別の方向が 10×カットオフ を
    超えていると GPUMD が起動直後に止まる (細長いセルで起きる)。
    """
    check = calc.check_cell(strict=False)
    if check.get("ok", True):
        return None
    thin_limit, thick_limit = check.get("thin_limit"), check.get("thick_limit")
    if not thin_limit or not thick_limit:
        return check.get("message") or "このセル形状は GPUMD で計算できません"
    thickness = StructureHandler.cell_thickness(unit)
    names = {"x": "a", "y": "b", "z": "c"}
    index = {"x": 0, "y": 1, "z": 2}
    thin = [axis for axis, _ in check.get("thin_directions", [])]
    thick = [axis for axis, _ in check.get("thick_directions", [])]
    widen = "、".join(f"{names[a]} を {int(thin_limit // thickness[index[a]]) + 1} 以上" for a in thin)
    shorten = "、".join(f"{names[a]} を {int(thick_limit // thickness[index[a]])} 以下" for a in thick)
    now = " × ".join(str(r) for r in repeat)
    return (f"このスーパーセル ({now}) は細長すぎて GPUMD が計算できません。"
            f"薄い方向が {thin_limit:g} Å 以下のとき、ほかの方向は {thick_limit:g} Å までしか伸ばせません。"
            f"スーパーセルの {widen} にするか、{shorten} にしてください。")


def size_note(n_atoms: int) -> list[str]:
    """原子数が目安を超えているときの注意書き (実行は止めない)。"""
    if n_atoms <= settings.WARN_ATOMS:
        return []
    return [f"原子数が {n_atoms:,} で、目安の {settings.WARN_ATOMS:,} を超えています。"
            "計算時間と GPU メモリに注意してください。"]


def estimated_speed(db: sqlite3.Connection, potential_id: int) -> tuple[float, bool]:
    """所要時間の目安に使う速度 [原子×ステップ/秒] と、実測に基づくかどうか。実測は同じエンジン (GPUMD・CPU の NEP・SevenNet)
    で動かしたジョブだけから取る (エンジンの記録のない前のジョブは、ポテンシャルの種類から GPUMD か SevenNet とみなす)。"""
    from . import ase_engine
    row = db.execute("SELECT path FROM potentials WHERE id = ?", (potential_id,)).fetchone()
    potential = {"path": row["path"]} if row is not None else {"path": ""}
    engine = ase_engine.engine_key(potential)
    rows = db.execute(
        "SELECT speed FROM jobs WHERE status = 'done' AND speed IS NOT NULL "
        "AND json_extract(params, '$.potential_id') = ? AND COALESCE(json_extract(summary, '$.engine'), ?) = ? "
        "ORDER BY id DESC LIMIT 5", (potential_id, potentials.engine_of(potential["path"]), engine)).fetchall()
    if rows:
        return sum(r["speed"] for r in rows) / len(rows), True
    if engine != "gpumd":
        return ase_engine.default_speed(potential["path"]), False
    return settings.DEFAULT_SPEED, False


def load_inputs(db: sqlite3.Connection, params: MDJobIn) -> tuple[Atoms, dict, sqlite3.Row]:
    row = db.execute("SELECT * FROM structures WHERE id = ?", (params.structure_id,)).fetchone()
    if row is None:
        raise ValueError("構造が見つかりません")
    return structures.load(params.structure_id), potentials.get(db, params.potential_id), row


def describe(db: sqlite3.Connection, params: MDJobIn, calc: GPUMDCalculation, notes: list[str]) -> dict:
    """実行前の確認画面に出す内容。"""
    total_steps = calc.builder.total_steps
    speed, measured = estimated_speed(db, params.potential_id)
    info = calc.structure_info.as_dict()
    return {
        "n_atoms": calc.n_atoms,
        "formula": info["formula"],
        "cell": [round(info[k], 3) for k in ("a", "b", "c")],
        "total_steps": total_steps,
        "total_time_ps": round(calc.builder.total_time_ps(), 4),
        "estimated_seconds": round(calc.n_atoms * total_steps / speed, 1),
        "estimate_measured": measured,
        "run_in": calc.preview(),
        "notes": size_note(calc.n_atoms) + notes,
    }
