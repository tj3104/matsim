"""ASE で計算するジョブ (SevenNet と、GPUMD がないときの NEP) の組み立てと、結果の取り出し。

SevenNet のポテンシャル (パスが "sevennet:<モデル>[:<modal>]") を選んだとき、または GPUMD がなくて NEP を CPU で計算するとき
(settings.cpu_nep()、calorine の CPUNEP) に、MD・静的計算 (構造緩和・弾性定数・状態方程式)・フォノン分散を、GPUMD ではなく
settings.ML_PYTHON の engines/ase_runner.py を別のプロセスとして動かして計算する。

ここで組み立てる AseJob は、アプリがジョブに使う GPUMDCalculation の部分 (builder.total_steps・total_time_ps()・
n_atoms・structure_info・preview()・write_inputs()・check_cell()) だけを同じ形で持つ。
書き出すのは model.xyz・ase_job.json (計算の指示)・run.in (記録。GPUMD では実行しない。結果のグラフのステージ分けに使う)。
計算の出力は GPUMD と同じ形式なので (ase_runner.py)、結果のグラフ・最終構造・解析はそのまま使える。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from ase import Atoms
from gpumd_toolkit.structure import StructureHandler

from . import settings

SUPPORTED = {"md": "MD", "static": "静的計算", "phonon": "フォノン分散"}


def is_sevennet(potential: dict) -> bool:
    return str(potential.get("path", "")).startswith("sevennet:")


def is_cpu_nep(potential: dict) -> bool:
    """GPUMD がない (または MATSIM_MD_ENGINE=cpu) ので、NEP を CPU (calorine) で計算するか。"""
    return not is_sevennet(potential) and settings.cpu_nep()


def uses_ase(potential: dict) -> bool:
    return is_sevennet(potential) or is_cpu_nep(potential)


def engine_key(potential: dict) -> str:
    """ジョブの記録 (速さの目安を同じエンジンの実測から取る) に使う名前: sevennet / cpunep / gpumd"""
    return "sevennet" if is_sevennet(potential) else "cpunep" if is_cpu_nep(potential) else "gpumd"


def engine_label(potential: dict) -> str:
    if is_sevennet(potential):
        model, modal = model_of(potential)
        return f"SevenNet ({model}{' / ' + modal if modal else ''})"
    return f"NEP ({Path(str(potential.get('path', ''))).name}、CPU・calorine)"


def engine_note(potential: dict) -> str:
    if is_sevennet(potential):
        return "SevenNet (GNN 系の汎用力場) で、ASE で計算します。GPUMD の NEP より数十倍遅いので、数百原子・数万ステップ程度に"
    return ("GPUMD が見つからないので、NEP を CPU (calorine の CPUNEP) で、ASE で計算します。GPUMD より桁違いに遅いので、"
            "数百原子・数千〜数万ステップ程度に")


def model_of(potential: dict) -> tuple[str, str | None]:
    """"sevennet:7net-mf-ompa:mpa" → ("7net-mf-ompa", "mpa")。"""
    parts = str(potential["path"]).split(":")
    return parts[1], (parts[2] if len(parts) > 2 else None)


def check_supported(kind_key: str, potential: dict | None = None) -> None:
    if kind_key in SUPPORTED:
        return
    if potential is not None and is_cpu_nep(potential):
        raise ValueError("GPUMD が見つからないので、NEP は CPU (calorine) で計算します。CPU で使えるのは "
                         + "・".join(SUPPORTED.values()) + " です。この計算には GPUMD が要ります "
                         "(GPUMD を入れて paths.env の MATSIM_GPUMD_BIN に書いてください)")
    raise ValueError("SevenNet (ASE で計算) で使えるのは " + "・".join(SUPPORTED.values())
                     + " です。この計算には GPUMD のポテンシャル (NEP など) を選んでください")


@dataclass
class _Builder:
    time_step: float
    stages: list = field(default_factory=list)   # [{"ensemble", "steps", "temperature", "temperature_end", "pressure"}]
    extra_steps: int = 0                          # MD 以外 (静的計算・フォノン) の進み具合の数

    @property
    def total_steps(self) -> int:
        return sum(s["steps"] for s in self.stages) + self.extra_steps

    def total_time_ps(self) -> float:
        return sum(s["steps"] for s in self.stages) * self.time_step / 1000


class AseJob:
    """SevenNet の計算の指示 (ase_job.json) と、記録の run.in を書き出す。"""

    def __init__(self, atoms: Atoms, potential: dict, workdir: Path, *, task: str, time_step: float = 1.0,
                 thermo_interval: int = 100, traj_interval: int = 0, repeat=(1, 1, 1)):
        self.atoms = atoms.repeat(tuple(repeat)) if tuple(repeat) != (1, 1, 1) else atoms.copy()
        self.potential = potential
        self.workdir = Path(workdir)
        self.task = task
        self.calculator = "sevennet" if is_sevennet(potential) else "cpunep"
        self.model, self.modal = model_of(potential) if self.calculator == "sevennet" else (None, None)
        self.thermo_interval = thermo_interval
        self.traj_interval = traj_interval
        self.builder = _Builder(time_step=time_step)
        self.minimize = False
        self.options: dict = {}
        self.replicate = None
        self.structure_info = StructureHandler.info(self.atoms)
        self.n_atoms = len(self.atoms)

    # GPUMDCalculation と同じ呼び方のステージ
    def nve(self, *, steps: int):
        self.builder.stages.append({"ensemble": "nve", "steps": steps})

    def nvt(self, *, temperature: float, steps: int, temperature_end: float | None = None, **_):
        self.builder.stages.append({"ensemble": "nvt", "steps": steps, "temperature": temperature,
                                    "temperature_end": temperature_end})

    def npt(self, *, temperature: float, steps: int, pressure: float = 0.0, temperature_end: float | None = None, **_):
        self.builder.stages.append({"ensemble": "npt", "steps": steps, "temperature": temperature,
                                    "temperature_end": temperature_end, "pressure": pressure})

    def check_cell(self, strict: bool = False) -> dict:
        return {"ok": True, "message": ""}   # GPUMD の NEP のセルの制約は、ASE では関係ない

    def job(self) -> dict:
        data = {"task": self.task, "calculator": self.calculator, "model": self.model, "modal": self.modal,
                "nep": None if self.calculator == "sevennet" else str(self.potential["path"]), "time_step": self.builder.time_step,
                "thermo_interval": self.thermo_interval, "traj_interval": self.traj_interval,
                "minimize": self.minimize, "stages": self.builder.stages, "options": self.options}
        if self.replicate:
            data["replicate"] = list(self.replicate)
        return data

    def run_in_record(self) -> str:
        """結果のグラフ (ステージ分け・目標温度) のための記録。GPUMD では実行しない。"""
        lines = [f"# この run.in は GPUMD では実行していません。ASE + {engine_label(self.potential)} で計算したステージを、",
                 "# 結果の表示のために GPUMD の書き方で記録したものです (実際の計算の指示は ase_job.json)。",
                 f"potential {self.potential['path']}",
                 f"time_step {self.builder.time_step:g}"]
        for s in self.builder.stages:
            t1 = s.get("temperature") or 0
            t2 = s.get("temperature_end") or t1
            name = {"nve": "nve", "nvt": "nvt_bdp", "npt": "npt_ber"}[s["ensemble"]]
            lines.append(f"ensemble {name} {t1:g} {t2:g} 100" if s["ensemble"] != "nve" else "ensemble nve")
            lines.append(f"dump_thermo {self.thermo_interval}")
            if self.traj_interval:
                lines.append(f"dump_xyz {self.traj_interval} dump.xyz")
            lines.append(f"run {s['steps']}")
        return "\n".join(lines) + "\n"

    def preview(self) -> str:
        return (self.run_in_record() + "\n# ---- ase_job.json (ASE で計算する指示) ----\n"
                + json.dumps(self.job(), ensure_ascii=False, indent=1) + "\n")

    def write_inputs(self) -> dict:
        self.workdir.mkdir(parents=True, exist_ok=True)
        model = StructureHandler.write_model(self.atoms, self.workdir / "model.xyz")
        (self.workdir / "ase_job.json").write_text(json.dumps(self.job(), ensure_ascii=False, indent=1))
        (self.workdir / "run.in").write_text(self.run_in_record())
        return {"model": model, "run_in": self.workdir / "run.in"}


# ---- 組み立て ---------------------------------------------------------------------------

def build_md(params, atoms: Atoms, potential: dict, workdir: Path) -> AseJob:
    job = AseJob(atoms, potential, workdir, task="md", time_step=params.time_step,
                 thermo_interval=params.thermo_interval, traj_interval=params.traj_interval, repeat=params.repeat)
    job.minimize = params.minimize
    for stage in params.stages:
        if stage.ensemble == "nve":
            job.nve(steps=stage.steps)
            job.builder.stages[-1]["temperature"] = stage.temperature
        elif stage.ensemble == "nvt":
            job.nvt(temperature=stage.temperature, temperature_end=stage.temperature_end, steps=stage.steps)
        else:
            job.npt(temperature=stage.temperature, temperature_end=stage.temperature_end, pressure=stage.pressure,
                    steps=stage.steps)
    return job


def build_calc(kind_key: str, c) -> AseJob:
    """静的計算・フォノン分散 (kinds.Context の入力から)。"""
    check_supported(kind_key, c.potential)
    o = c.o
    if kind_key == "static":
        job = AseJob(c.atoms, c.potential, c.workdir, task="static", repeat=c.repeat)
        job.options = {k: o[k] for k in ("relax", "relax_cell", "relax_steps", "elastic", "strain", "cohesive", "e1", "e2")}
        if o["cohesive"] and o["e1"] >= o["e2"]:
            raise ValueError("状態方程式の範囲は 最小 < 最大 にしてください")
        job.builder.extra_steps = 1 + (1 if o["relax"] else 0) + (12 if o["elastic"] else 0) + (21 if o["cohesive"] else 0)
        return job
    if kind_key == "phonon":
        import math
        from .tutorial_kinds import _band_path
        job = AseJob(c.atoms, c.potential, c.workdir, task="phonon")
        labels, points = _band_path(job.atoms, o["path"])
        c.workdir.mkdir(parents=True, exist_ok=True)
        (c.workdir / "kpoints.in").write_text(
            "".join(f"{points[label][0]:.6f} {points[label][1]:.6f} {points[label][2]:.6f} {label}\n" for label in labels))
        thickness = StructureHandler.cell_thickness(job.atoms)
        size = o["min_size"] or 12.0      # SevenNet のカットオフ (5〜6 Å) の 2 倍程度
        reps = [max(1, math.ceil(size / t)) for t in thickness]
        if len(job.atoms) * reps[0] * reps[1] * reps[2] > 3000:
            raise ValueError("力定数を取るスーパーセルが大きすぎます (3000 原子まで)。基本単位胞を使うか、大きさを小さくしてください")
        job.replicate = reps
        job.options = {"displacement": o["displacement"], "relax": o["relax"]}
        job.builder.extra_steps = 6 * len(job.atoms)   # 変位の数の目安 (対称性で減る)
        return job
    raise AssertionError(kind_key)


# ---- 結果の取り出し ----------------------------------------------------------------------

def collect(directory: Path, kind_key: str, params: dict) -> None:
    """ase_job.json のあるジョブの、要点・表を analysis/ に保存する (静的計算・フォノン分散)。"""
    from . import kinds, results
    from .kinds import Collected, Dataset
    out = Collected()
    job = json.loads((directory / "ase_job.json").read_text())
    if job.get("calculator") == "cpunep":
        out.notes.append(f"NEP ({Path(job['nep']).name}) を CPU (calorine の CPUNEP) で、ASE で計算しました")
    else:
        out.notes.append(f"SevenNet ({job['model']}{' / ' + job['modal'] if job.get('modal') else ''}) で、ASE で計算しました")
    if kind_key == "phonon":
        from .tutorial_kinds import collect_phonon
        context = kinds.Context(atoms=None, potential={}, potentials=[], workdir=directory, repeat=(1, 1, 1),
                                time_step=1.0, thermo_interval=1, traj_interval=0, o={})
        collect_phonon(None, context, out)
    elif kind_key == "static":
        r = json.loads((directory / "ase_result.json").read_text())
        out.add("エネルギー", r["energy_per_atom"], "eV/atom", 7)
        out.add("圧力 (応力の対角平均)", float(np.mean(r["stress"][:3])), "GPa")
        out.add("力の最大値", r["max_force"], "eV/Å")
        if "relax_steps" in r:
            out.add("構造最適化のステップ数", r["relax_steps"])
        if "elastic" in r:
            m = r["moduli"]
            for label, key in (("体積弾性率 B", "bulk"), ("剛性率 G", "shear"), ("ヤング率 E", "young")):
                out.add(label, m[key], "GPa")
            out.add("ポアソン比", m["poisson"])
            frame = pd.DataFrame(np.array(r["elastic"]), columns=[f"C_i{j + 1}" for j in range(6)])
            frame.insert(0, "i", np.arange(1, 7))
            out.datasets.append(Dataset("elastic", "弾性定数 C_ij (GPa)", frame, "i", ["C_i1"],
                                        {"i": "i ＼ j", **{f"C_i{j + 1}": str(j + 1) for j in range(6)}}, table=True))
            out.notes.append("弾性定数は、原子を動かさない (clamped-ion) ±ひずみの応力の差から求めています "
                             "(基本単位胞に 2 原子以上ある結晶では、内部緩和のぶん大きめに出ることがあります)")
        if "cohesive" in r:
            frame = pd.DataFrame(r["cohesive"])
            out.datasets.append(Dataset("cohesive", "状態方程式 (凝集エネルギー曲線)", frame, "scale", ["energy_per_atom"],
                                        {"scale": "セルの伸縮 (倍)", "energy_per_atom": "エネルギー (eV/atom)",
                                         "energy": "エネルギー (eV)", "volume": "体積 (Å³)"}))
            fit = np.polyfit(frame["scale"], frame["energy_per_atom"], 3)
            roots = [x.real for x in np.roots(np.polyder(fit)) if abs(x.imag) < 1e-9 and frame["scale"].min() <= x.real <= frame["scale"].max()]
            if roots:
                best = min(roots, key=lambda x: np.polyval(fit, x))
                out.add("平衡点の伸縮", float(best), "倍", 5)
                out.add("平衡点のエネルギー", float(np.polyval(fit, best)), "eV/atom", 7)
    results.write_collected(directory, out.highlights, out.notes, out.datasets)


CPU_NEP_SPEED = 2.0e4   # CPU の NEP (calorine) の速さの目安 [原子×ステップ/秒] (Ryzen 7 9700X、NEP89、108 原子の Cu で実測 1.9e4)


def default_speed(potential_path: str) -> float:
    """実測がないときの速さの目安 [原子×ステップ/秒] (RTX 5070 Ti、cuEquivariance あり、256 原子の Cu で実測)。"""
    if not str(potential_path).startswith("sevennet:"):
        return CPU_NEP_SPEED
    model = potential_path.split(":")[1] if ":" in potential_path else ""
    return {"7net-0": 14000.0, "7net-omat": 9500.0, "7net-mf-ompa": 7500.0, "7net-nano-4.5": 24000.0,
            "7net-nano-5.0": 22000.0, "7net-nano-5.5": 18000.0, "7net-nano-6.0": 17000.0}.get(model, 7000.0)
