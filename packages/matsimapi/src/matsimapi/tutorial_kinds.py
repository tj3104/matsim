"""GPUMD-Tutorials (~/repos/GPUMD-Tutorials/examples) の内容を、材料を選んで使えるようにした計算の種類。

既存の種類 (静的計算・拡散・熱輸送・自由エネルギー・引張…) と重なるチュートリアルは入れていない。
ここにあるのは次のとおり (括弧内は元のチュートリアルの番号):

    フォノン分散 (06)・振動状態密度 (02)・熱膨張 (07)・融点 (08, 19)・有限温度の弾性定数 (30)・
    疲労 (21)・衝突 (20)・成膜 (16, 27, 33)・表面の緩和と再構成 (23)・サイトエネルギー分布 (32)

熱輸送の NEMD とスペクトル分解 (04) は kinds.THERMAL に足してある。
書き方は kinds.py と同じ: 入力欄 (fields)・組み立て (build)・結果の取り出し (collect)。
"""
from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd
from ase import Atoms
from gpumd_toolkit import workflows as wf
from gpumd_toolkit.groups import GroupingScheme, by_region
from gpumd_toolkit.inputs import computes, modifiers
from gpumd_toolkit.inputs.ensembles import HeatBath

from . import settings
from .kinds import (AXES, TEMPERATURE_LIST, Collected, Context, Dataset, Kind, choice, flag, num, ptext,
                    steps, text)

KB = 8.617333262e-5          # [eV/K]
EV_A3_TO_GPA = 160.21766208
AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


def _thermo_frame(c: Context) -> pd.DataFrame:
    """thermo.out (セル 9 成分つき) と、各行がどのステージかの列。"""
    from gpumd_toolkit.analysis import RunSpec
    from gpumd_toolkit.outputs import read_thermo
    frame = read_thermo(c.workdir / "thermo.out")
    # 成膜 (deposit) は GPUMD が run.in を書き換えて元を run.in.original に残すので、元のほうを読む
    run_in = c.workdir / "run.in.original"
    stages = RunSpec.from_file(run_in if run_in.is_file() else c.workdir / "run.in").stages
    stage = np.full(len(frame), max(len(stages) - 1, 0), dtype=int)
    row = 0
    for i, item in enumerate(stages):
        stage[row:row + item.n_thermo_rows] = i
        row += item.n_thermo_rows
    frame["stage"] = stage
    return frame


def _lengths(frame: pd.DataFrame) -> np.ndarray:
    cell = frame[["ax", "ay", "az", "bx", "by", "bz", "cx", "cy", "cz"]].to_numpy().reshape(-1, 3, 3)
    return np.linalg.norm(cell, axis=2)


def _final(c: Context) -> Atoms:
    """計算後の構造 (後処理で書く CONTCAR)。"""
    from ase.io import read
    return read(c.workdir / "CONTCAR", format="vasp")


def _surface_height(atoms: Atoms, axis: int = 2) -> tuple[float, float]:
    pos = atoms.positions[:, axis]
    return float(pos.min()), float(pos.max())


def _need_vacuum(atoms: Atoms, gap: float, axis: int = 2) -> float:
    """原子のない厚み (真空) が gap 以上あるか。足りなければ説明つきで止める。"""
    lo, hi = _surface_height(atoms, axis)
    length = float(np.linalg.norm(atoms.cell[axis]))
    vacuum = length - (hi - lo)
    if vacuum < gap:
        raise ValueError(f"{'xyz'[axis]} 方向の真空層が {vacuum:.1f} Å しかありません ({gap:g} Å 以上必要)。"
                         "構造のモデリングの「真空層を加える (表面)」で表面スラブにしてください")
    return vacuum


# ---- フォノン分散 (06) -----------------------------------------------------------------

class PhononCalculation(wf.StaticCalculation):
    """compute_phonon 用。GPUMD は run.in の 1 行目にしか replicate を探さないので、書き出したあとで先頭に移す。"""

    def write_inputs(self):
        paths = super().write_inputs()
        run_in = self.workdir / "run.in"
        lines = run_in.read_text().splitlines()
        replicate = [line for line in lines if line.split()[:1] == ["replicate"]]
        rest = [line for line in lines if line.split()[:1] != ["replicate"]]
        run_in.write_text("\n".join(replicate + rest) + "\n")
        return paths


def _band_path(atoms: Atoms, text_path: str) -> tuple[list[str], dict]:
    """高対称点の並び (例: GXWKGL) と各点の座標。空欄なら ASE が選ぶ標準の経路 (最初のつながった区間)。

    点の名前は数字の付くもの (単斜晶の M1・H1 など) があるので、文字列ではなく名前のリストで返す。
    """
    path = atoms.cell.bandpath(npoints=0)
    points = path.special_points
    chosen = text_path.strip() or path.path.split(",")[0]
    labels = re.findall(r"[A-Z][0-9]?", chosen.replace("Γ", "G"))
    missing = [label for label in labels if label not in points]
    if missing:
        raise ValueError(f"この格子にない高対称点です: {', '.join(missing)} (使えるのは {' '.join(sorted(points))})")
    if len(labels) < 2:
        raise ValueError("高対称点は 2 つ以上並べてください")
    return labels, {label: points[label] for label in labels}


def build_phonon(c: Context):
    o = c.o
    calc = PhononCalculation(c.atoms, c.potential["path"], c.workdir, **{**c.base(), "repeat": (1, 1, 1)})
    calc.set_dump(thermo=None, traj=None)
    labels, points = _band_path(calc.atoms, o["path"])
    (c.workdir / "kpoints.in").parent.mkdir(parents=True, exist_ok=True)
    (c.workdir / "kpoints.in").write_text(
        "".join(f"{points[label][0]:.6f} {points[label][1]:.6f} {points[label][2]:.6f} {label}\n" for label in labels))
    from gpumd_toolkit.structure import StructureHandler
    thickness = StructureHandler.cell_thickness(calc.atoms)
    size = o["min_size"]
    if size is None:
        # GPUMD の推奨: 厚みがポテンシャルの (動径) カットオフの 4 倍以上
        cutoffs = calc.nep_cutoffs()
        size = 4 * (cutoffs[0] if cutoffs else 6.0)
    reps = [max(1, math.ceil(size / t)) for t in thickness]
    if len(calc.atoms) * reps[0] * reps[1] * reps[2] > 20000:
        raise ValueError("力定数を取るスーパーセルが大きすぎます。基本単位胞を使うか、スーパーセルの大きさを小さくしてください")
    if o["relax"]:
        calc.relax(relax_cell=False, max_steps=1000)
    calc.phonon(displacement=o["displacement"], replicate=reps)
    return calc


def _read_omega2(path) -> tuple[pd.DataFrame, list[tuple[float, str]]]:
    lines = path.read_text().splitlines()
    ticks = []
    if lines and lines[0].startswith("#"):
        tokens = lines[0].lstrip("#").split()
        values = [t for t in tokens if re.fullmatch(r"-?\d+(\.\d+)?", t)]
        names = tokens[len(values):]
        ticks = [(float(v), "Γ" if n == "G" else n) for v, n in zip(values, names)]
    data = np.loadtxt([line for line in lines if line.strip() and not line.startswith("#")], ndmin=2)
    frame = pd.DataFrame({"distance": data[:, 0]})
    for i in range(data.shape[1] - 1):
        omega2 = data[:, i + 1]
        frame[f"nu_{i + 1}"] = np.sign(omega2) * np.sqrt(np.abs(omega2)) / (2 * np.pi)   # [THz]、虚振動は負
    return frame, ticks


def collect_phonon(calc, c: Context, out: Collected):
    def dispersion():
        frame, ticks = _read_omega2(c.workdir / "omega2.out")
        branches = [col for col in frame.columns if col.startswith("nu_")]
        nu = frame[branches].to_numpy()
        out.add("最大振動数", float(nu.max()), "THz")
        out.add("最小振動数 (負は虚振動)", float(nu.min()), "THz")
        # Γ 点のすぐそばの音響分枝の小さな負の値は数値誤差で出やすいので、Γ 点から離れたところだけで判定する
        gammas = [x for x, label in ticks if label == "Γ"]
        span = float(frame["distance"].max() - frame["distance"].min()) or 1.0
        away = np.ones(len(frame), dtype=bool)
        for x in gammas:
            away &= np.abs(frame["distance"].to_numpy() - x) > 0.03 * span
        imaginary = float(nu[away].min()) if away.any() else 0.0
        if imaginary < -0.1:
            out.notes.append(f"Γ 点以外に虚振動 ({imaginary:.2f} THz) があります。構造が力学的に不安定か、"
                             "最適化が足りないか、スーパーセルが小さい可能性があります")
        elif nu.min() < -0.1:
            out.notes.append("Γ 点付近の小さな負の振動数は、有限変位法の数値誤差によるものです")
        labels = {"distance": "高対称線に沿った距離", **{b: f"分枝 {b[3:]} (THz)" for b in branches}}
        out.datasets.append(Dataset("dispersion", "フォノン分散 (THz)", frame, "distance", branches, labels,
                                    style="bands", ticks=[{"x": x, "label": t} for x, t in ticks]))
    out.attempt("フォノン分散", dispersion)


PHONON = Kind(
    "phonon", "フォノン分散", "物性",
    "有限変位法で力定数を求め、高対称線に沿ったフォノン分散を描きます (チュートリアル 06)。"
    "基本単位胞で使ってください (スーパーセルの指定は使わず、力定数用の大きさは下で決めます)",
    [ptext("path", "高対称点の並び", "", r"[A-ZΓ0-9]{0,30}", max_length=30,
           hint="例: GXWKGL (G = Γ)。空欄なら格子に合わせた標準の経路"),
     num("min_size", "力定数を取るスーパーセルの厚み", None, min=5, max=100, unit="Å", optional=True,
         hint="空欄なら GPUMD の推奨 (ポテンシャルのカットオフの 4 倍)。小さいと虚振動が出やすい"),
     num("displacement", "変位の大きさ", 0.01, min=0.001, max=0.1, unit="Å", advanced=True),
     flag("relax", "先に原子位置を最適化する", True)],
    build_phonon, collect_phonon, no_repeat=True)


# ---- 振動状態密度 (02) -------------------------------------------------------------------

def build_vdos(c: Context):
    o = c.o
    calc = c.make(wf.ThermalTransport)
    calc.equilibrate(temperature=o["temperature"], steps=o["equil_steps"])
    calc.nve(steps=o["steps"])
    calc.phonon_dos(sample_interval=o["sample_interval"], Nc=o["correlation"], omega_max=o["omega_max"])
    return calc


def collect_vdos(calc, c: Context, out: Collected):
    def dos():
        from gpumd_toolkit.outputs import read_dos
        frame = read_dos(c.workdir / "dos.out")
        frame["dos"] = frame[["dos_x", "dos_y", "dos_z"]].sum(axis=1)
        frame = frame[["nu", "dos", "dos_x", "dos_y", "dos_z", "omega"]]
        out.datasets.append(Dataset("dos", "振動状態密度 (VDOS)", frame, "nu", ["dos"],
                                    {"nu": "振動数 (THz)", "omega": "角振動数 (rad/ps)", "dos": "DOS 合計 (1/THz)",
                                     **{f"dos_{a}": f"DOS {a} (1/THz)" for a in "xyz"}}))
        peak = frame.loc[frame["dos"].idxmax(), "nu"]
        out.add("DOS の最大のピーク", float(peak), "THz")
    out.attempt("振動状態密度", dos)

    def vac():
        from gpumd_toolkit.outputs import read_mvac
        frame = read_mvac(c.workdir / "mvac.out")
        out.datasets.append(Dataset("vac", "速度自己相関 (規格化)", frame, "time", ["vac_x", "vac_y", "vac_z"],
                                    {"time": "相関時間 (ps)", **{f"vac_{a}": f"VAC {a}" for a in "xyz"}}))
    out.attempt("速度自己相関", vac)


VDOS = Kind(
    "vdos", "振動状態密度 (DOS)", "物性",
    "平衡化のあと NVE で速度自己相関をとり、振動 (フォノン) 状態密度を求めます (チュートリアル 02)",
    [num("temperature", "温度", 300, min=1, max=20000, unit="K"),
     steps("equil_steps", "平衡化", 20000),
     steps("steps", "本計算 (NVE)", 50000),
     num("omega_max", "最大の角振動数", 400, min=10, max=2000, unit="rad/ps", hint="振動数 (THz) の 2π 倍"),
     num("sample_interval", "サンプリング間隔", 5, min=1, max=100, integer=True, unit="ステップ", advanced=True),
     num("correlation", "相関の長さ", 200, min=10, max=10000, integer=True, unit="サンプル", advanced=True)],
    build_vdos, collect_vdos)


# ---- 熱膨張 (07) -------------------------------------------------------------------------

def _temperature_list(text_value: str, label: str) -> list[float]:
    values = [float(v) for v in re.split(r"[,、\s]+", text_value.strip()) if v]
    if len(values) < 2:
        raise ValueError(f"{label}は 2 つ以上並べてください")
    if len(values) > 30:
        raise ValueError(f"{label}は 30 点までです")
    if any(not 1 <= v <= 20000 for v in values):
        raise ValueError(f"{label}は 1〜20000 K にしてください")
    return values


def build_expansion(c: Context):
    o = c.o
    temps = _temperature_list(o["temperatures"], "温度")
    calc = c.make(wf.MechanicalCalculation)
    for t in temps:
        calc.npt(temperature=t, steps=o["steps"], pressure=o["pressure"], barostat="npt_scr",
                 cell_mode="ortho" if o["aniso"] else None)
    return calc


def _stage_means(c: Context, keep: float = 0.7) -> pd.DataFrame:
    """ステージごとの後半 keep の平均 (温度・格子定数・体積・PE)。"""
    frame = _thermo_frame(c)
    lengths = _lengths(frame)
    frame["a"], frame["b"], frame["c"] = lengths[:, 0], lengths[:, 1], lengths[:, 2]
    rows = []
    for stage, group in frame.groupby("stage"):
        tail = group.iloc[int(len(group) * (1 - keep)):]
        rows.append({"stage": int(stage), "temperature": tail["temperature"].mean(), "a": tail["a"].mean(),
                     "b": tail["b"].mean(), "c": tail["c"].mean(), "volume": tail["volume"].mean(),
                     "volume_per_atom": tail["volume"].mean() / calc_atoms(c),
                     "potential_energy_per_atom": tail["potential_energy"].mean() / calc_atoms(c)})
    return pd.DataFrame(rows)


def calc_atoms(c: Context) -> int:
    return len(c.atoms) * c.repeat[0] * c.repeat[1] * c.repeat[2]


def collect_expansion(calc, c: Context, out: Collected):
    def expansion():
        table = _stage_means(c)
        t = table["temperature"].to_numpy()
        for col, name in (("a", "a"), ("b", "b"), ("c", "c")):
            slope = np.polyfit(t, table[col], 1)[0]
            out.add(f"線膨張係数 α_{name}", slope / table[col].iloc[0], "1/K", 4)
        slope_v = np.polyfit(t, table["volume"], 1)[0]
        out.add("体膨張係数 β", slope_v / table["volume"].iloc[0], "1/K", 4)
        out.add("温度範囲", f"{t.min():.0f}〜{t.max():.0f} K")
        out.notes.append("膨張係数は、各温度の平均値を直線で当てはめた傾き ÷ 最低温度での値です")
        for col in ("a", "b", "c", "volume"):
            table[f"{col}_rel"] = table[col] / table[col].iloc[0]
        out.datasets.append(Dataset("expansion", "格子定数と温度", table, "temperature", ["a_rel", "b_rel", "c_rel"],
                                    {"temperature": "温度 (K)", "a": "a (Å)", "b": "b (Å)", "c": "c (Å)",
                                     "volume": "体積 (Å³)", "volume_per_atom": "体積 (Å³/atom)",
                                     "potential_energy_per_atom": "ポテンシャルエネルギー (eV/atom)", "stage": "ステージ",
                                     "a_rel": "a / a(最低温度)", "b_rel": "b / b(最低温度)", "c_rel": "c / c(最低温度)",
                                     "volume_rel": "V / V(最低温度)"}))
    out.attempt("熱膨張", expansion)


EXPANSION = Kind(
    "expansion", "熱膨張", "物性",
    "温度を段階的に変えた NPT で格子定数を測り、線膨張係数・体膨張係数を求めます (チュートリアル 07)",
    [ptext("temperatures", "温度の並び", "100, 200, 300, 400, 500, 600", TEMPERATURE_LIST, max_length=200,
           hint="低い順に。各温度で NPT を流し、後半 70% の平均を使います"),
     steps("steps", "各温度のステップ数", 20000),
     num("pressure", "圧力", 0, min=-100, max=1000, unit="GPa"),
     flag("aniso", "a・b・c を別々に伸び縮みさせる", True, hint="外すと等方的 (形を保って) に変える")],
    build_expansion, collect_expansion)


# ---- 融点: 固液共存法 (08, 19) ----------------------------------------------------------

class LocalBath(HeatBath):
    """局所熱浴 heat_lan / heat_nhc。toolkit の HeatBath と違い、温度差 0 (同じ温度で 2 つのグループを保つ) も許す。"""

    def __post_init__(self) -> None:
        if self.method not in ("nhc", "bdp", "lan"):
            raise ValueError("method は nhc / bdp / lan です")
        if int(self.source) == int(self.sink):
            raise ValueError("source と sink には別のグループを指定してください")
        if self.delta_T < 0:
            raise ValueError("温度差は 0 以上にしてください")


def build_melting(c: Context):
    o = c.o
    calc = c.make(wf.MechanicalCalculation)
    axis = AXIS_INDEX[o["axis"]]
    labels = by_region(calc.atoms, [0.5], axis=axis, fractional=True)   # 0: 下半分 (固体のまま) / 1: 上半分 (融かす)
    if len(set(labels)) < 2:
        raise ValueError("構造を半分に分けられません。スーパーセルを大きくしてください")
    scheme = GroupingScheme()
    scheme.add(labels, "halves")
    calc.grouping_scheme = scheme
    calc.groupings = scheme.to_groupings()
    t = o["temperature"]
    calc.npt(temperature=t, steps=o["equil_steps"], pressure=o["pressure"], barostat="npt_ber")
    # 上半分を T + ΔT、下半分を T に保って上半分だけ融かす (体積一定)。
    # 局所熱浴は「中心 ± 半分」で指定するので、中心を T + ΔT/2 にする (下側が負の温度にならないように)
    half = o["melt_offset"] / 2
    calc.add_ensemble(LocalBath(temperature=t + half, delta_T=half, source=1, sink=0, method="lan"),
                      steps=o["melt_steps"])
    # 共存させたまま NPT。融解が進めばエネルギーが上がり、凝固が進めば下がる
    calc.npt(temperature=t, steps=o["steps"], pressure=o["pressure"], barostat="npt_ber",
             cell_mode="ortho")
    return calc


def melting_verdict(frame: pd.DataFrame, n_atoms: int) -> dict:
    """共存させた本計算 (最後のステージ) のエネルギーの変化から、融解・凝固・共存を判定する。"""
    last = frame[frame["stage"] == frame["stage"].max()]
    pe = last["potential_energy"].to_numpy() / n_atoms
    n = len(pe)
    if n < 10:
        raise ValueError("本計算の熱力学量が少なすぎます")
    head, tail = pe[: n // 5].mean(), pe[-(n // 5):].mean()
    change = tail - head
    noise = pe[n // 5: n // 2].std() if n >= 10 else pe.std()
    if change > max(3 * noise, 2e-3):
        verdict, code = "融解が進んだ (融点より高い)", 1
    elif change < -max(3 * noise, 2e-3):
        verdict, code = "凝固が進んだ (融点より低い)", -1
    else:
        verdict, code = "共存のまま (融点に近い)", 0
    return {"verdict": verdict, "code": code, "pe_change": float(change), "noise": float(noise),
            "temperature": float(last["temperature"].iloc[n // 5:].mean())}


def collect_melting(calc, c: Context, out: Collected):
    def verdict():
        frame = _thermo_frame(c)
        result = melting_verdict(frame, calc_atoms(c))
        out.add("判定", result["verdict"], key="melting:verdict")
        out.add("判定コード (1 融解 / 0 共存 / -1 凝固)", result["code"], key="melting:code")
        out.add("ポテンシャルエネルギーの変化", result["pe_change"], "eV/atom", 3, key="melting:pe_change")
        out.add("共存中の平均温度", result["temperature"], "K", 5, key="melting:temperature")
        out.notes.append("温度スキャンで流すと、凝固した最も高い温度と融解した最も低い温度のあいだに融点があります "
                         "(シリーズの画面で推定値を出します)")
        last = frame[frame["stage"] == frame["stage"].max()].copy()
        last["time_ps"] = np.arange(len(last)) * c.thermo_interval * c.time_step * 1e-3
        last["pe_per_atom"] = last["potential_energy"] / calc_atoms(c)
        out.datasets.append(Dataset("coexistence", "共存させた本計算のエネルギー", last[["time_ps", "pe_per_atom",
                                    "temperature", "volume"]], "time_ps", ["pe_per_atom"],
                                    {"time_ps": "時間 (ps)", "pe_per_atom": "ポテンシャルエネルギー (eV/atom)",
                                     "temperature": "温度 (K)", "volume": "体積 (Å³)"}))
    out.attempt("融解・凝固の判定", verdict)


MELTING = Kind(
    "melting", "融点 (固液共存法)", "物性",
    "結晶の半分だけを融かして固体と液体を共存させ、その温度で融解と凝固のどちらが進むかを調べます。"
    "温度スキャンで融点を挟み込みます (チュートリアル 08, 19)",
    [num("temperature", "温度", 1500, min=1, max=20000, unit="K"),
     ptext("scan_temperatures", "温度スキャン", "", TEMPERATURE_LIST, max_length=200,
           hint="例: 1200,1300,1400,1500,1600。温度ごとにジョブを作ります。空欄なら上の温度 1 点"),
     choice("axis", "固体と液体を並べる方向", "z", AXES, hint="この方向に長いスーパーセルにしてください"),
     num("pressure", "圧力", 0, min=-100, max=1000, unit="GPa"),
     steps("equil_steps", "固体の平衡化", 10000),
     num("melt_offset", "融かすときの温度差", 1000, min=10, max=10000, unit="K",
         hint="上半分を 温度 + この値、下半分を 温度 のままにして上半分だけ融かす"),
     steps("melt_steps", "融かす時間", 10000),
     steps("steps", "共存させる本計算", 100000)],
    build_melting, collect_melting)


def melting_point(points: list[dict]) -> dict:
    """シリーズの判定 [{temperature, code}] から融点を挟み込む。"""
    pts = sorted(points, key=lambda p: p["temperature"])
    frozen = [p["temperature"] for p in pts if p["code"] == -1]
    melted = [p["temperature"] for p in pts if p["code"] == 1]
    coexist = [p["temperature"] for p in pts if p["code"] == 0]
    if coexist:
        return {"estimate": float(np.mean(coexist)), "low": min(coexist), "high": max(coexist),
                "note": "共存のままの温度の平均です"}
    if frozen and melted and max(frozen) < min(melted):
        low, high = max(frozen), min(melted)
        return {"estimate": (low + high) / 2, "low": low, "high": high,
                "note": f"凝固した {low:g} K と融解した {high:g} K のあいだです。間を細かく流すと絞り込めます"}
    if frozen and not melted:
        return {"estimate": None, "note": "どの温度でも凝固しました。もっと高い温度を流してください"}
    if melted and not frozen:
        return {"estimate": None, "note": "どの温度でも融解しました。もっと低い温度を流してください"}
    return {"estimate": None, "note": "判定が温度の順に並んでいません。統計が足りない可能性があります (本計算を長く)"}


# ---- 有限温度の弾性定数: ひずみゆらぎ法 (30) ------------------------------------------------

def build_elastic_t(c: Context):
    o = c.o
    calc = c.make(wf.MechanicalCalculation)
    calc.set_dump(thermo=o["sample_interval"], traj=c.traj_interval or None, traj_properties=())
    calc.npt(temperature=o["temperature"], steps=o["equil_steps"], pressure=0.0, barostat="npt_scr")
    calc.npt(temperature=o["temperature"], steps=o["steps"], pressure=(0.0,) * 6, barostat="npt_scr", cell_mode="tri",
             elastic_modulus=(o["modulus"],) * 6)
    return calc


def strain_fluctuation(cells: np.ndarray, temperature: float) -> tuple[np.ndarray, int]:
    """セル行列の時系列 (N, 3, 3; 行が a, b, c) から、ひずみゆらぎ法で弾性定数 C (GPa, 6×6) を求める。

    C = kB T / <V> · S⁻¹、S はラグランジュひずみ (Voigt 表記) の共分散。
    """
    h = np.transpose(cells, (0, 2, 1))          # 列がセルベクトル
    h0 = h.mean(axis=0)
    inv0 = np.linalg.inv(h0)
    volume = float(np.abs(np.linalg.det(h0)))
    f = h @ inv0                                 # 変形勾配
    e = 0.5 * (np.transpose(f, (0, 2, 1)) @ f - np.eye(3))
    voigt = np.stack([e[:, 0, 0], e[:, 1, 1], e[:, 2, 2], 2 * e[:, 1, 2], 2 * e[:, 0, 2], 2 * e[:, 0, 1]], axis=1)
    s = np.cov(voigt, rowvar=False)
    c = KB * temperature / volume * np.linalg.inv(s) * EV_A3_TO_GPA
    return c, len(voigt)


def collect_elastic_t(calc, c: Context, out: Collected):
    def elastic():
        frame = _thermo_frame(c)
        last = frame[frame["stage"] == frame["stage"].max()]
        last = last.iloc[len(last) // 10:]
        cells = last[["ax", "ay", "az", "bx", "by", "bz", "cx", "cy", "cz"]].to_numpy().reshape(-1, 3, 3)
        temperature = float(last["temperature"].mean())
        cmat, n = strain_fluctuation(cells, temperature)
        cmat = 0.5 * (cmat + cmat.T)
        table = pd.DataFrame(cmat, columns=[f"C_i{j + 1}" for j in range(6)])
        table.insert(0, "i", np.arange(1, 7))
        out.datasets.append(Dataset("elastic_t", f"弾性定数 C_ij (GPa, {temperature:.0f} K)", table, "i", ["C_i1"],
                                    {"i": "i ＼ j", **{f"C_i{j + 1}": str(j + 1) for j in range(6)}}, table=True))
        bulk = (cmat[0, 0] + cmat[1, 1] + cmat[2, 2] + 2 * (cmat[0, 1] + cmat[1, 2] + cmat[0, 2])) / 9
        shear = (cmat[0, 0] + cmat[1, 1] + cmat[2, 2] - (cmat[0, 1] + cmat[1, 2] + cmat[0, 2])
                 + 3 * (cmat[3, 3] + cmat[4, 4] + cmat[5, 5])) / 15
        for (i, j) in ((0, 0), (1, 1), (2, 2), (0, 1), (0, 2), (1, 2), (3, 3), (4, 4), (5, 5)):
            out.add(f"C{i + 1}{j + 1}", float(cmat[i, j]), "GPa")
        out.add("体積弾性率 B (Voigt)", float(bulk), "GPa")
        out.add("剛性率 G (Voigt)", float(shear), "GPa")
        out.add("平均温度", temperature, "K", 5)
        out.add("使ったサンプル数", n)
        if n < 5000:
            out.notes.append(f"サンプルが {n} 個と少ないので、値の誤差が大きい可能性があります "
                             "(本計算を長くするか、サンプリング間隔を短く)")
    out.attempt("弾性定数", elastic)


ELASTIC_T = Kind(
    "elastic_t", "弾性定数 (有限温度・ひずみゆらぎ)", "物性",
    "セルの 6 成分を自由にした NPT でセルのゆらぎを測り、有限温度の弾性定数 C_ij を求めます (チュートリアル 30)",
    [num("temperature", "温度", 300, min=1, max=20000, unit="K"),
     steps("equil_steps", "平衡化", 20000),
     steps("steps", "本計算", 500000, hint="ゆらぎの統計が要るので長めに (目安 50 万ステップ以上)"),
     num("sample_interval", "セルを記録する間隔", 20, min=1, max=10000, integer=True, unit="ステップ"),
     num("modulus", "弾性率の目安 (バロスタット用)", 100, min=1, max=2000, unit="GPa", advanced=True,
         hint="桁が合っていればよい")],
    build_elastic_t, collect_elastic_t)


# ---- 疲労: 繰り返し負荷 (21) -------------------------------------------------------------

def build_fatigue(c: Context):
    o = c.o
    calc = c.make(wf.MechanicalCalculation)
    calc.equilibrate(temperature=o["temperature"], steps=o["equil_steps"], pressure=0.0)
    # 1 サイクル = 引張 (0→+ε) → 戻し (+ε→0) → 圧縮 (0→-ε) → 戻し (-ε→0)
    quarter = int(round(o["amplitude"] / 100 / (o["strain_rate"] * c.time_step * 1e-15)))
    if quarter < 10:
        raise ValueError("1/4 サイクルのステップ数が少なすぎます。ひずみ速度を下げるか振幅を大きくしてください")
    if 4 * quarter * o["cycles"] > settings.MAX_TOTAL_STEPS:
        raise ValueError(f"合計ステップ数が上限を超えます ({4 * quarter * o['cycles']:,})")
    signs = [1, -1, -1, 1] if not o["tension_only"] else [1, -1]
    for _ in range(o["cycles"]):
        for sign in signs:
            calc.tensile(temperature=o["temperature"], steps=quarter, strain_rate=sign * o["strain_rate"],
                         axis=o["axis"])
    return calc


def collect_fatigue(calc, c: Context, out: Collected):
    def loops():
        frame = _thermo_frame(c)
        axis = AXIS_INDEX[c.o["axis"]]
        lengths = _lengths(frame)[:, axis]
        start = frame["stage"] >= 1
        frame, lengths = frame[start].copy(), lengths[start.to_numpy()]
        frame["strain"] = lengths / lengths[0] - 1
        frame["stress"] = -frame[["Pxx", "Pyy", "Pzz"][axis]]          # 引張を正にする
        per = 4 if not c.o["tension_only"] else 2
        frame["cycle"] = (frame["stage"] - 1) // per + 1
        peaks = frame.groupby("cycle")["stress"].max()
        out.add("1 サイクル目の最大応力", float(peaks.iloc[0]), "GPa")
        out.add("最後のサイクルの最大応力", float(peaks.iloc[-1]), "GPa")
        out.add("最大応力の比 (最後 / 最初)", float(peaks.iloc[-1] / peaks.iloc[0]) if peaks.iloc[0] else None, "", 4)
        out.add("最終のひずみ (残留)", float(frame["strain"].iloc[-1]), "", 4)
        frame["time_ps"] = np.arange(len(frame)) * c.thermo_interval * c.time_step * 1e-3
        out.datasets.append(Dataset("fatigue", "応力ひずみのループ", frame[["strain", "stress", "time_ps", "cycle",
                                    "temperature", "potential_energy"]], "strain", ["stress"],
                                    {"strain": "ひずみ", "stress": "応力 (GPa)", "time_ps": "時間 (ps)",
                                     "cycle": "サイクル", "temperature": "温度 (K)",
                                     "potential_energy": "ポテンシャルエネルギー (eV)"}))
        table = pd.DataFrame({"cycle": peaks.index, "max_stress": peaks.values,
                              "min_stress": frame.groupby("cycle")["stress"].min().values})
        out.datasets.append(Dataset("fatigue_peaks", "サイクルごとの最大・最小応力", table, "cycle",
                                    ["max_stress", "min_stress"],
                                    {"cycle": "サイクル", "max_stress": "最大応力 (GPa)", "min_stress": "最小応力 (GPa)"}))
    out.attempt("応力ひずみのループ", loops)


FATIGUE = Kind(
    "fatigue", "疲労 (繰り返し負荷)", "物性",
    "1 方向に引張と圧縮をくり返し、応力ひずみのループとサイクルごとの最大応力の変化を調べます (チュートリアル 21)",
    [num("temperature", "温度", 300, min=1, max=20000, unit="K"),
     choice("axis", "負荷の方向", "x", AXES),
     num("amplitude", "ひずみの振幅", 2, min=0.05, max=30, unit="%"),
     num("strain_rate", "ひずみ速度", 1e9, min=1e6, max=1e11, unit="1/s"),
     num("cycles", "サイクル数", 3, min=1, max=50, integer=True),
     flag("tension_only", "引張側だけで繰り返す (0 → +振幅 → 0)", False),
     steps("equil_steps", "平衡化", 10000)],
    build_fatigue, collect_fatigue)


# ---- 衝突 (20) ---------------------------------------------------------------------------

def build_impact(c: Context):
    o = c.o
    calc = c.make(wf.MechanicalCalculation)
    axis = AXIS_INDEX[o["axis"]]
    _need_vacuum(calc.atoms, 2.0, axis)
    pos = calc.atoms.positions[:, axis]
    lo, hi = pos.min(), pos.max()
    if o["projectile"] + o["fixed"] >= hi - lo:
        raise ValueError("打ち込む部分と固定層の厚みの合計が、構造の厚みを超えています")
    # 0: 下端の固定層 / 1: 標的 / 2: 上端の打ち込む部分 (剛体として一定速度で動かす)
    labels = np.ones(len(pos), dtype=int)
    labels[pos < lo + o["fixed"]] = 0
    labels[pos > hi - o["projectile"]] = 2
    target = (hi - o["projectile"]) - (lo + o["fixed"])
    travel = o["velocity"] * 0.01 * o["steps"] * c.time_step
    if travel > 0.8 * target:
        raise ValueError(f"打ち込む部分が {travel:.1f} Å 進み、標的 ({target:.1f} Å) を押しつぶしてしまいます。"
                         "打ち込みのステップ数か速度を下げてください (標的の厚みの 8 割まで)")
    scheme = GroupingScheme()
    scheme.add(labels, "impact")
    calc.grouping_scheme = scheme
    calc.groupings = scheme.to_groupings()
    calc.nvt(temperature=o["temperature"], steps=o["equil_steps"], thermostat="nvt_ber")
    calc.add_commands(modifiers.fix(0))
    velocity = [0.0, 0.0, 0.0]
    velocity[axis] = -o["velocity"] * 0.01        # km/s → Å/fs、標的に向かって (負の向き)
    # 熱浴を事実上切った (T_coup を極端に大きくした) NVT で move を使う (チュートリアルと同じ)
    calc.nvt(temperature=o["temperature"], steps=o["steps"], thermostat="nvt_ber", T_coup=1e20)
    calc.add_commands(modifiers.fix(0), modifiers.move(2, velocity),
                      computes.compute(0, 10, max(1, c.thermo_interval // 10), ("temperature",)))
    return calc


def collect_impact(calc, c: Context, out: Collected):
    def temps():
        # compute.out は グループごとの温度 (3 列) のあとに熱浴のエネルギー 2 列が続く
        data = np.loadtxt(c.workdir / "compute.out", ndmin=2)
        frame = pd.DataFrame({f"temperature_g{g}": data[:, g] for g in range(3)})
        frame.insert(0, "time_ps", np.arange(1, len(frame) + 1) * c.thermo_interval * c.time_step * 1e-3)
        out.add("標的の最高温度", float(frame["temperature_g1"].max()), "K", 5)
        out.add("標的の最終温度", float(frame["temperature_g1"].iloc[-1]), "K", 5)
        out.datasets.append(Dataset("impact_t", "部分ごとの温度", frame, "time_ps", ["temperature_g1"],
                                    {"time_ps": "衝突開始からの時間 (ps)", "temperature_g0": "固定層 (K)",
                                     "temperature_g1": "標的 (K)", "temperature_g2": "打ち込む部分 (K)"}))
    out.attempt("温度の推移", temps)
    distance = c.o["velocity"] * 0.01 * c.o["steps"] * c.time_step
    out.notes.append(f"打ち込む部分は本計算のあいだに {distance:.1f} Å 進みます")


IMPACT = Kind(
    "impact", "衝突 (剛体の打ち込み)", "プロセス・表面",
    "構造の上端を剛体として一定速度で標的に打ち込み、下端は固定します。標的の温度上昇や損傷を調べます (チュートリアル 20)",
    [choice("axis", "打ち込む方向 (この軸の負の向き)", "z", AXES, hint="この方向に真空層のある構造を使ってください"),
     num("velocity", "速度", 1, min=0.01, max=100, unit="km/s"),
     num("projectile", "打ち込む部分の厚み (上端から)", 8, min=1, max=1000, unit="Å"),
     num("fixed", "固定層の厚み (下端から)", 4, min=0, max=1000, unit="Å"),
     num("temperature", "温度", 300, min=1, max=20000, unit="K"),
     steps("equil_steps", "平衡化", 10000),
     steps("steps", "打ち込み", 20000)],
    build_impact, collect_impact)


# ---- 成膜 (16, 27, 33) ---------------------------------------------------------------------

def build_deposition(c: Context):
    o = c.o
    calc = c.make(wf.MechanicalCalculation)
    axis = AXIS_INDEX[o["axis"]]
    atoms = calc.atoms
    vacuum = _need_vacuum(atoms, o["gap"] + 10, axis)
    lo, hi = _surface_height(atoms, axis)
    length = float(np.linalg.norm(atoms.cell[axis]))
    h_min = hi + o["gap"]
    h_max = min(h_min + 5.0, length - 1.0)
    if h_max <= h_min:
        raise ValueError(f"原子を入れる高さの余裕がありません (真空層 {vacuum:.1f} Å)。真空層を厚くしてください")
    pos = atoms.positions[:, axis]
    # 0: 下端の固定層 / 1: その上の温度を保つ層 / 2: 表面 (成膜される側)
    labels = np.full(len(pos), 2, dtype=int)
    labels[pos < lo + o["fixed"] + o["bath"]] = 1
    labels[pos < lo + o["fixed"]] = 0
    if len(set(labels)) < 3:
        raise ValueError("固定層・温度を保つ層・表面に分けられません。厚みを見直してください")
    scheme = GroupingScheme()
    scheme.add(labels, "deposition")
    calc.grouping_scheme = scheme
    calc.groupings = scheme.to_groupings()
    total = o["interval"] * o["rounds"]
    calc.add_ensemble(LocalBath(temperature=o["temperature"], delta_T=0, source=1, sink=2, method="nhc"), steps=total)
    species = [(sym, int(n), -o["speed"] * 0.01) for sym, n in _species_counts(o["species"])]
    calc.add_commands(modifiers.fix(0),
                      modifiers.deposit(o["interval"], "xyz"[axis], h_min, h_max, atoms=species))
    return calc


def _species_counts(text_value: str) -> list[tuple[str, int]]:
    items = []
    for part in text_value.split(","):
        sym, count = [s.strip() for s in part.split(":")]
        items.append((sym, int(float(count))))
    if not items or any(n <= 0 for _, n in items):
        raise ValueError("1 回に入れる原子の数は 1 以上にしてください")
    return items


def collect_deposition(calc, c: Context, out: Collected):
    def result():
        final = _final(c)
        added = len(final) - calc_atoms(c)
        out.add("入った原子の数", added)
        axis = AXIS_INDEX[c.o["axis"]]
        pos = final.positions[:, axis]
        lo = calc.atoms.positions[:, axis].min()
        bins = np.arange(lo - 2, pos.max() + 2, 1.0)
        rows = {"height": (bins[:-1] + bins[1:]) / 2 - lo}
        symbols = np.array(final.get_chemical_symbols())
        for sym in sorted(set(symbols)):
            rows[sym] = np.histogram(pos[symbols == sym], bins=bins)[0]
        frame = pd.DataFrame(rows)
        out.datasets.append(Dataset("profile", "高さ方向の原子数分布 (最終構造)", frame, "height",
                                    [s for s in frame.columns if s != "height"][:4],
                                    {"height": "下端からの高さ (Å)", **{s: f"{s} の原子数 (1 Å ごと)" for s in frame.columns
                                                                     if s != "height"}}))
        surface_top = calc.atoms.positions[:, axis].max()
        film = pos[calc_atoms(c):]                     # 入った原子は元の原子の後ろに足される
        if len(film):
            out.add("膜の厚みの目安 (元の表面から最上部の原子まで)", float(film.max() - surface_top), "Å", 4)
    out.attempt("成膜の結果", result)
    out.notes.append("最終構造は「最終構造を登録」で構造の一覧に追加できます")


DEPOSITION = Kind(
    "deposition", "成膜 (堆積)", "プロセス・表面",
    "表面スラブの上から原子を一定間隔で打ち込んで堆積させます。下端は固定し、その上の層で温度を保ちます "
    "(チュートリアル 16, 27, 33)",
    [ptext("species", "入れる原子 (元素:1 回の個数)", "C:2", r"[A-Z][a-z]?\s*:\s*\d+(?:\s*,\s*[A-Z][a-z]?\s*:\s*\d+)*",
           hint="例: C:2 / Ga:1,O:1"),
     num("interval", "入れる間隔", 2000, min=10, max=1000000, integer=True, unit="ステップ"),
     num("rounds", "入れる回数", 20, min=1, max=10000, integer=True),
     num("speed", "入射の速さ", 0.5, min=0.001, max=100, unit="km/s"),
     choice("axis", "成膜の方向 (表面の法線)", "z", AXES),
     num("gap", "表面から入れる位置までの高さ", 8, min=2, max=100, unit="Å"),
     num("temperature", "基板の温度", 300, min=1, max=20000, unit="K"),
     num("fixed", "固定層の厚み", 3, min=0.5, max=100, unit="Å", advanced=True),
     num("bath", "温度を保つ層の厚み", 5, min=0.5, max=100, unit="Å", advanced=True)],
    build_deposition, collect_deposition)


# ---- 表面の緩和・再構成 (23) ---------------------------------------------------------------

def build_surface(c: Context):
    o = c.o
    calc = c.make(wf.MechanicalCalculation)
    axis = AXIS_INDEX[o["axis"]]
    _need_vacuum(calc.atoms, 5.0, axis)
    pos = calc.atoms.positions[:, axis]
    labels = (pos >= pos.min() + o["fixed"]).astype(int)            # 0: 固定する底 / 1: 動く部分
    if len(set(labels)) < 2:
        raise ValueError("固定層の厚みを見直してください")
    scheme = GroupingScheme()
    scheme.add(labels, "surface")
    calc.grouping_scheme = scheme
    calc.groupings = scheme.to_groupings()
    calc.nvt(temperature=o["temperature"], steps=o["steps"])
    calc.add_commands(modifiers.fix(0))
    return calc


def _coordination(atoms: Atoms, cutoff: float) -> np.ndarray:
    from ase.neighborlist import neighbor_list
    i = neighbor_list("i", atoms, cutoff)
    return np.bincount(i, minlength=len(atoms))


def collect_surface(calc, c: Context, out: Collected):
    def coordination():
        from atomsk_kit import nearest_distance
        before = calc.atoms
        after = _final(c)
        cutoff = 1.2 * nearest_distance(before)
        axis = AXIS_INDEX[c.o["axis"]]
        top = before.positions[:, axis] > before.positions[:, axis].max() - c.o["layer"]
        cn0, cn1 = _coordination(before, cutoff), _coordination(after, cutoff)
        out.add("表面層の平均配位数 (はじめ)", float(cn0[top].mean()), "", 4)
        out.add("表面層の平均配位数 (おわり)", float(cn1[top].mean()), "", 4)
        out.add("配位数が変わった表面原子の割合", float(np.mean(cn0[top] != cn1[top]) * 100), "%", 3)
        out.notes.append(f"配位数は {cutoff:.2f} Å 以内の原子の数 (最近接距離の 1.2 倍)")
        values = np.arange(0, max(cn0.max(), cn1.max()) + 1)
        table = pd.DataFrame({"cn": values, "before": [np.sum(cn0[top] == v) for v in values],
                              "after": [np.sum(cn1[top] == v) for v in values]})
        out.datasets.append(Dataset("cn", "表面層の配位数の分布", table, "cn", ["before", "after"],
                                    {"cn": "配位数", "before": "はじめ (原子数)", "after": "おわり (原子数)"}))
    out.attempt("配位数", coordination)


SURFACE = Kind(
    "surface", "表面の緩和・再構成", "プロセス・表面",
    "表面スラブの底を固定して長めの NVT を流し、表面の原子の並び替わり (再構成) を配位数の変化で調べます "
    "(チュートリアル 23)",
    [choice("axis", "表面の法線", "z", AXES),
     num("temperature", "温度", 800, min=1, max=20000, unit="K"),
     steps("steps", "ステップ数", 500000, hint="再構成には数 ns かかることがあります"),
     num("fixed", "底の固定層の厚み", 4, min=0.5, max=100, unit="Å"),
     num("layer", "表面層とみなす厚み", 3, min=0.5, max=50, unit="Å", advanced=True)],
    build_surface, collect_surface)


# ---- サイトエネルギー分布 DOAS / AEDP (32) --------------------------------------------------

def build_doas(c: Context):
    o = c.o
    calc = c.make(wf.MechanicalCalculation)
    # 原子ごとのポテンシャルエネルギーつきのトラジェクトリ (平衡化のあいだの分は解析で捨てる)
    calc.set_dump(thermo=c.thermo_interval, traj=o["dump_interval"], traj_file="energy.xyz",
                  traj_properties=("potential",))
    calc.nvt(temperature=o["temperature"], steps=o["equil_steps"])
    calc.nvt(temperature=o["temperature"], steps=o["steps"])
    return calc


def _energy_frames(path, skip: int, max_frames: int = 500) -> list[Atoms]:
    from ase.io import read
    frames = read(path, index=":", format="extxyz")[skip:]
    stride = max(1, len(frames) // max_frames)
    return frames[::stride]


def collect_doas(calc, c: Context, out: Collected):
    o = c.o

    def doas():
        frames = _energy_frames(c.workdir / "energy.xyz", skip=o["equil_steps"] // o["dump_interval"])
        if not frames:
            raise ValueError("本計算のフレームがありません")
        key = next((k for k in ("energy_atom", "potential", "potential_energy", "energy") if k in frames[0].arrays), None)
        if key is None:
            raise ValueError("原子ごとのエネルギーがトラジェクトリにありません")
        symbols = np.array(frames[0].get_chemical_symbols())
        energy = np.stack([f.arrays[key].ravel() for f in frames])               # (フレーム, 原子)
        positions = np.stack([f.get_scaled_positions(wrap=True) for f in frames])
        targets = [o["element"]] if o["element"] in set(symbols) else sorted(set(symbols))
        if o["element"] and o["element"] not in set(symbols):
            out.notes.append(f"{o['element']} が構造にないので、全元素の分布を出しました")
        lo = min(float(energy[:, symbols == s].min()) for s in targets)
        hi = max(float(energy[:, symbols == s].max()) for s in targets)
        bins = np.linspace(lo, hi, 101)
        hist = {"energy": (bins[:-1] + bins[1:]) / 2}
        for sym in targets:
            values = energy[:, symbols == sym].ravel()
            hist[sym] = np.histogram(values, bins=bins, density=True)[0]
            out.add(f"サイトエネルギーの平均 ({sym})", float(values.mean()), "eV", 6)
            out.add(f"サイトエネルギーの標準偏差 ({sym})", float(values.std()), "eV", 4)
        out.datasets.append(Dataset("doas", "原子ごとのエネルギーの分布 (DOAS)", pd.DataFrame(hist), "energy",
                                    targets[:4], {"energy": "サイトエネルギー (eV)",
                                                  **{s: f"{s} の確率密度 (1/eV)" for s in targets}}))
        # AEDP: 選んだ面に投影し、マス目ごとの平均エネルギー
        sym = targets[0]
        mask = symbols == sym
        a1, a2 = [AXIS_INDEX[a] for a in o["plane"]]
        grid = o["grid"]
        u = positions[:, mask, a1].ravel()
        v = positions[:, mask, a2].ravel()
        e = energy[:, mask].ravel()
        iu = np.minimum((u * grid).astype(int), grid - 1)
        iv = np.minimum((v * grid).astype(int), grid - 1)
        total = np.zeros((grid, grid))
        count = np.zeros((grid, grid))
        np.add.at(total, (iu, iv), e)
        np.add.at(count, (iu, iv), 1)
        lengths = calc.atoms.cell.lengths()
        uu, vv = np.meshgrid((np.arange(grid) + 0.5) / grid * lengths[a1], (np.arange(grid) + 0.5) / grid * lengths[a2],
                             indexing="ij")
        filled = count > 0
        aedp = pd.DataFrame({"u": uu[filled], "v": vv[filled], "energy": total[filled] / count[filled],
                             "visits": count[filled]})
        out.datasets.append(Dataset("aedp", f"{sym} のエネルギー分布マップ (AEDP, {o['plane']} 面)", aedp, "u", ["v"],
                                    {"u": f"{o['plane'][0]} (Å)", "v": f"{o['plane'][1]} (Å)",
                                     "energy": "平均サイトエネルギー (eV)", "visits": "滞在回数"},
                                    style="colormap", color="energy"))
        out.add("使ったフレーム数", len(frames))
    out.attempt("サイトエネルギー分布", doas)


DOAS = Kind(
    "doas", "サイトエネルギー分布 (DOAS/AEDP)", "解析",
    "MD 中の原子ごとのポテンシャルエネルギーを集め、エネルギーの分布 (DOAS) と、場所ごとの平均エネルギーの地図 (AEDP) を作ります。"
    "可動イオンのサイトの違いを見るのに使います (チュートリアル 32)",
    [text("element", "注目する元素", "Li", hint="例: Li。空欄や構造にない元素ならすべての元素"),
     num("temperature", "温度", 600, min=1, max=20000, unit="K"),
     steps("equil_steps", "平衡化", 20000),
     steps("steps", "本計算", 100000),
     num("dump_interval", "エネルギーを記録する間隔", 1000, min=10, max=1000000, integer=True, unit="ステップ"),
     choice("plane", "地図にする面", "xy", [{"value": p, "label": f"{p} 面"} for p in ("xy", "xz", "yz")]),
     num("grid", "地図のマス目の数 (1 辺)", 60, min=10, max=200, integer=True, advanced=True)],
    build_doas, collect_doas)


KINDS = [PHONON, VDOS, EXPANSION, MELTING, ELASTIC_T, FATIGUE, IMPACT, DEPOSITION, SURFACE, DOAS]
GROUPS = ["プロセス・表面", "解析"]
