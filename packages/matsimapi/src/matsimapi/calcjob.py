"""MD 以外の計算 (kinds.py の種類) の入力検証・組み立て・結果の取り出し。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ase import Atoms
from gpumd_toolkit.structure import StructureHandler
from pydantic import BaseModel, Field

from . import kinds, mdjob, potentials, results, settings, structures

Scalar = float | int | str | bool | None


class CalcJobIn(BaseModel):
    kind: str = Field(max_length=30)
    name: str = Field("", max_length=60)
    structure_id: int
    potential_id: int
    extra_potential_ids: list[int] = Field(default_factory=list, max_length=7)   # committee の 2 つ目以降
    repeat: tuple[int, int, int] = (1, 1, 1)
    time_step: float = Field(1.0, ge=0.01, le=10.0)   # fs
    thermo_interval: int = Field(100, ge=1, le=1_000_000)
    traj_interval: int = Field(1000, ge=0, le=10_000_000)   # 0 でトラジェクトリを保存しない
    options: dict[str, Scalar] = Field(default_factory=dict)


def _check_common(params: CalcJobIn, atoms: Atoms, used: list[dict]) -> int:
    if any(not 1 <= r <= settings.MAX_REPEAT for r in params.repeat):
        raise ValueError(f"スーパーセルの繰り返しは 1〜{settings.MAX_REPEAT} です")
    n_atoms = len(atoms) * params.repeat[0] * params.repeat[1] * params.repeat[2]
    if n_atoms > settings.MAX_ATOMS:
        raise ValueError(f"原子数が上限を超えています ({n_atoms:,} > {settings.MAX_ATOMS:,})")
    for potential in used:
        missing = potentials.missing_elements(atoms.get_chemical_symbols(), potential["elements"])
        if missing:
            raise ValueError(f"ポテンシャル「{potential['name']}」は {', '.join(missing)} に対応していません")
    return n_atoms


def load_inputs(db: sqlite3.Connection, params: CalcJobIn) -> tuple[kinds.Kind, Atoms, list[dict], sqlite3.Row]:
    kind = kinds.get(params.kind)
    row = db.execute("SELECT * FROM structures WHERE id = ?", (params.structure_id,)).fetchone()
    if row is None:
        raise ValueError("構造が見つかりません")
    ids = [params.potential_id] + (list(dict.fromkeys(params.extra_potential_ids)) if kind.committee else [])
    return kind, structures.load(params.structure_id), [potentials.get(db, i) for i in ids], row


def build(params: CalcJobIn, kind: kinds.Kind, atoms: Atoms, used: list[dict], workdir: Path):
    """入力を検証して toolkit の計算を組み立てる。戻り値は (計算, 警告, 検証済みの種類別入力)。"""
    if kind.no_repeat:
        params = params.model_copy(update={"repeat": (1, 1, 1)})
    _check_common(params, atoms, used)
    options = kinds.validate_options(kind.fields, params.options)
    context = kinds.Context(atoms=atoms, potential=used[0], potentials=used, workdir=workdir, repeat=params.repeat,
                            time_step=params.time_step, thermo_interval=params.thermo_interval,
                            traj_interval=params.traj_interval, o=options)
    if settings.cpu_nep() and (kind.key == "finetune" or kind.key in kinds.PLUMED_KINDS):
        raise ValueError("この計算には GPUMD が要ります (GPUMD が見つからないので、NEP は CPU で MD・静的計算・フォノン分散だけ"
                         "計算できます。GPUMD を入れて paths.env の MATSIM_GPUMD_BIN に書いてください)")
    if kind.key == "finetune":   # SevenNet を先生にした NEP の学び直し (finetune.py)
        from . import finetune
        return finetune.build(context), [], options
    from . import ase_engine
    if any(ase_engine.uses_ase(p) for p in used):
        if len(used) > 1:
            raise ValueError("複数のポテンシャルを使う計算は、GPUMD の NEP でだけできます"
                             if ase_engine.is_cpu_nep(used[0]) else "SevenNet は 1 つだけ選べます (複数のポテンシャルを使う計算は NEP で)")
        return ase_engine.build_calc(kind.key, context), [ase_engine.engine_note(used[0])], options
    calc, notes = kinds.build(kind, context)
    if params.traj_interval:
        frames = calc.builder.total_steps // params.traj_interval
        if frames * calc.n_atoms > settings.MAX_TRAJ_ATOM_FRAMES:
            raise ValueError("トラジェクトリが大きくなりすぎます。保存間隔を長くしてください "
                             f"({frames:,} フレーム × {calc.n_atoms:,} 原子)")
    return calc, notes, options


def describe(db: sqlite3.Connection, params: CalcJobIn, calc, notes: list[str]) -> dict:
    """実行前の確認画面に出す内容。"""
    total_steps = calc.builder.total_steps
    speed, measured = mdjob.estimated_speed(db, params.potential_id)
    info = calc.structure_info.as_dict()
    # 経路積分はビーズの数だけ、committee はモデルの数だけ力の計算が増える
    factor = 1
    if params.kind == "pimd" and params.options.get("method", "pimd") == "pimd":
        factor = int(params.options.get("num_beads") or 8)
    elif params.kind == "al":
        factor = 1 + len(set(params.extra_potential_ids))
    elif params.kind in kinds.PLUMED_KINDS:   # CV の計算 (CPU) の分だけ遅くなる
        from .plumed_kind import cost_factor
        factor = cost_factor(params.options)
    seconds = (calc.estimate_seconds(speed) if hasattr(calc, "estimate_seconds")   # ファインチューニング (MD + 学習)
               else calc.n_atoms * max(total_steps, 1) * factor / speed)
    return {
        "n_atoms": calc.n_atoms, "formula": info["formula"], "cell": [round(info[k], 3) for k in ("a", "b", "c")],
        "total_steps": total_steps, "total_time_ps": round(calc.builder.total_time_ps(), 4),
        "estimated_seconds": round(seconds, 1),
        "estimate_measured": measured, "run_in": calc.preview(), "notes": mdjob.size_note(calc.n_atoms) + notes,
    }


def _potentials_from_run_in(directory: Path) -> list[dict]:
    """run.in の potential 行から、計算に使ったポテンシャルを読む。"""
    used = []
    for line in (directory / "run.in").read_text().splitlines():
        tokens = line.split()
        if tokens[:1] == ["potential"] and len(tokens) > 1:
            path = Path(tokens[1])
            used.append({"path": str(path), "name": path.name, "elements": potentials.parse_elements(path)})
    if not used:
        raise ValueError("run.in に potential の行がありません")
    return used


def collect(directory: Path, kind_key: str, params: dict) -> None:
    """終わった計算から結果 (要点の数値と表) を取り出して analysis/ に保存する。

    toolkit の結果取り出しメソッドは計算オブジェクトの設定を参照するので、
    実行時と同じ条件で計算を組み立て直してから呼ぶ (入力ファイルは書き直さない)。
    """
    if (directory / "ase_job.json").is_file():   # SevenNet (ASE で計算) のジョブ
        from . import ase_engine
        ase_engine.collect(directory, kind_key, params)
        return
    kind = kinds.get(kind_key)
    job = CalcJobIn(**params)
    used = _potentials_from_run_in(directory)
    # 成膜 (deposit) は GPUMD が model.xyz を書き換えて元を model.xyz.original に残すので、元のほうで組み立て直す
    model = directory / "model.xyz.original"
    atoms = structures.clean(StructureHandler.read_model(model if model.is_file() else directory / "model.xyz"))
    context = kinds.Context(atoms=atoms, potential=used[0], potentials=used, workdir=directory, repeat=(1, 1, 1),
                            time_step=job.time_step, thermo_interval=job.thermo_interval,
                            traj_interval=job.traj_interval, o=kinds.validate_options(kind.fields, job.options))
    calc, _ = kinds.build(kind, context)
    out = kinds.Collected()
    kind.collect(calc, context, out)
    results.write_collected(directory, out.highlights, out.notes, out.datasets)
