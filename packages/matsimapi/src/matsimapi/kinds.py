"""MD 以外の計算の種類。GPUMD_toolkit の workflows クラスに 1 対 1 で対応する。

種類ごとに「入力欄の定義 (fields)」「toolkit の計算を組み立てる関数 (build)」
「終わったあとに結果を取り出す関数 (collect)」を持つ。画面の入力フォームは fields から
自動で作るので、種類を足すときはここに Kind を 1 つ追加すればよい。
"""
from __future__ import annotations

import math
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
from ase import Atoms
from gpumd_toolkit import GPUMDCalculation
from gpumd_toolkit import workflows as wf
from gpumd_toolkit.inputs import modifiers
from gpumd_toolkit.config import GPUMDEnvironment

from . import settings

AXES = [{"value": a, "label": f"{a} 方向"} for a in "xyz"]


# ---- 入力欄の定義 ---------------------------------------------------------------------

def num(key, label, default, *, min=None, max=None, unit="", hint="", advanced=False, show_if=None, integer=False,
        optional=False) -> dict:
    return {"key": key, "label": label, "type": "int" if integer else "number", "default": default, "min": min,
            "max": max, "unit": unit, "hint": hint, "advanced": advanced, "show_if": show_if, "optional": optional}


def steps(key, label, default, **kwargs) -> dict:
    return num(key, label, default, min=1, max=settings.MAX_TOTAL_STEPS, integer=True, unit="ステップ", **kwargs)


def choice(key, label, default, options, *, hint="", advanced=False, show_if=None) -> dict:
    return {"key": key, "label": label, "type": "select", "default": default, "options": options, "hint": hint,
            "advanced": advanced, "show_if": show_if}


def flag(key, label, default, *, hint="", advanced=False, show_if=None) -> dict:
    return {"key": key, "label": label, "type": "bool", "default": default, "hint": hint, "advanced": advanced,
            "show_if": show_if}


def text(key, label, default="", *, hint="", advanced=False, show_if=None, max_length=20) -> dict:
    return {"key": key, "label": label, "type": "text", "default": default, "hint": hint, "advanced": advanced,
            "show_if": show_if, "max_length": max_length}


def ptext(key, label, default, pattern, *, hint="", advanced=False, show_if=None, max_length=80) -> dict:
    """決まった書式の文字列 (温度の並び・ミラー指数・組成など)。pattern は正規表現 (全体一致)。"""
    return {"key": key, "label": label, "type": "text", "default": default, "pattern": pattern, "hint": hint,
            "advanced": advanced, "show_if": show_if, "max_length": max_length}


def _visible(spec: dict, values: dict) -> bool:
    condition = spec.get("show_if")
    if not condition:
        return True
    return all(values.get(key) in (wanted if isinstance(wanted, list) else [wanted]) for key, wanted in condition.items())


def validate_options(fields: list[dict], raw: dict) -> dict:
    """画面から来た値を fields に照らして検証し、既定値を補った辞書を返す。"""
    values: dict[str, Any] = {}
    for spec in fields:
        key, kind = spec["key"], spec["type"]
        value = raw.get(key, spec["default"])
        if kind in ("number", "int"):
            if value is None or value == "":
                if spec.get("optional"):
                    values[key] = None
                    continue
                raise ValueError(f"{spec['label']} を入力してください")
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{spec['label']} は数値で入力してください") from None
            if not math.isfinite(value):
                raise ValueError(f"{spec['label']} は数値で入力してください")
            if kind == "int":
                if value != int(value):
                    raise ValueError(f"{spec['label']} は整数で入力してください")
                value = int(value)
            if spec["min"] is not None and value < spec["min"]:
                raise ValueError(f"{spec['label']} は {spec['min']} 以上にしてください")
            if spec["max"] is not None and value > spec["max"]:
                raise ValueError(f"{spec['label']} は {spec['max']} 以下にしてください")
        elif kind == "select":
            if value not in [option["value"] for option in spec["options"]]:
                raise ValueError(f"{spec['label']} の選択が正しくありません")
        elif kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{spec['label']} の値が正しくありません")
        elif kind == "text" and spec.get("pattern"):
            value = str(value if value is not None else "").strip()
            if len(value) > spec["max_length"] or not re.fullmatch(spec["pattern"], value):
                raise ValueError(f"{spec['label']} の書き方が正しくありません" + (f" ({spec['hint']})" if spec["hint"] else ""))
        elif kind == "text":
            value = str(value or "").strip()
            if len(value) > spec["max_length"] or not all(ch.isalnum() for ch in value):
                raise ValueError(f"{spec['label']} は英数字 {spec['max_length']} 文字以内で入力してください")
        values[key] = value
    # 表示されていない欄の値は既定値に戻す (隠れた欄の古い値で計算が変わらないように)
    for spec in fields:
        if not _visible(spec, values):
            values[spec["key"]] = spec["default"]
    return values


# ---- 計算の組み立てに渡す情報 ----------------------------------------------------------

@dataclass
class Context:
    atoms: Atoms
    potential: dict                 # {"path", "elements", "name"}
    potentials: list[dict]          # committee を使う種類だけ 2 つ以上
    workdir: Path
    repeat: tuple[int, int, int]
    time_step: float
    thermo_interval: int
    traj_interval: int
    o: dict                         # 検証済みの種類ごとの入力

    def base(self) -> dict:
        """どの workflows クラスにも渡す共通の引数。"""
        return {"repeat": self.repeat, "time_step": self.time_step, "max_atoms": settings.MAX_ATOMS,
                "environment": GPUMDEnvironment(venv=None, use_login_shell=False,
                                                gpumd_command=str(settings.GPUMD_BIN))}

    def make(self, cls, **extra):
        calc = cls(self.atoms, self.potential["path"], self.workdir, **{**self.base(), **extra})
        # 表示用なので座標だけを保存する (速度・力を付けるとファイルが数倍になる)
        calc.set_dump(thermo=self.thermo_interval, traj=self.traj_interval or None, traj_properties=())
        return calc


@dataclass
class Dataset:
    """グラフと表にできる 1 つの結果。frame の列を画面で自由に軸に選べる。"""
    key: str
    title: str
    frame: pd.DataFrame
    x: str
    y: list[str]
    labels: dict[str, str] = field(default_factory=dict)
    table: bool = False   # グラフではなく表で見せる (弾性定数の行列など)
    style: str = "lines"  # "lines" / "bands" (y の列をすべて同じ色で重ねる。分散曲線など) / "colormap" (点を color 列で色分け)
    ticks: list[dict] = field(default_factory=list)   # 横軸の目盛りに名前を付ける [{"x", "label"}] (高対称点など)
    color: str | None = None                           # style="colormap" で色に使う列
    equal_axes: bool = True                            # style="colormap" で縦横の目盛りをそろえる (座標の地図)


@dataclass
class Collected:
    highlights: list[dict] = field(default_factory=list)   # {"label", "value", "unit"} (+ 機械で読む用の "key")
    datasets: list[Dataset] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, label: str, value, unit: str = "", digits: int = 4, key: str | None = None) -> None:
        if value is None:
            return
        if isinstance(value, (float, np.floating)):
            if not math.isfinite(float(value)):
                return
            value = float(f"{float(value):.{digits}g}")
        elif isinstance(value, np.integer):
            value = int(value)
        item = {"label": label, "value": value, "unit": unit}
        if key:
            item["key"] = key   # 他の画面 (アレニウス解析など) が値を探すための名前
        self.highlights.append(item)

    def attempt(self, what: str, action: Callable[[], None]) -> None:
        """結果の 1 項目を取り出す。失敗しても他の項目は残す。"""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                action()
        except Exception as exc:
            self.notes.append(f"{what} を読めませんでした ({type(exc).__name__})")


@dataclass
class Kind:
    key: str
    label: str
    group: str
    note: str
    fields: list[dict]
    build: Callable[[Context], GPUMDCalculation]
    collect: Callable[[GPUMDCalculation, Context, Collected], None]
    committee: bool = False          # 複数のポテンシャルを選ばせるか
    min_species: int = 1
    no_repeat: bool = False          # スーパーセルにしない (フォノン分散は単位胞のまま GPUMD に渡す)

    def public(self) -> dict:
        return {"key": self.key, "label": self.label, "group": self.group, "note": self.note,
                "fields": self.fields, "committee": self.committee, "no_repeat": self.no_repeat,
                "scan": any(spec["key"] == "scan_temperatures" for spec in self.fields)}


def _frame(data) -> pd.DataFrame:
    frame = data if isinstance(data, pd.DataFrame) else pd.DataFrame(data)
    return frame.select_dtypes(include=[np.number]).reset_index(drop=True)


def _first(mapping: dict, *names):
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


# ---- 静的計算 -----------------------------------------------------------------------

def build_static(c: Context):
    calc = c.make(wf.StaticCalculation)
    if c.o["relax"]:
        calc.relax(relax_cell=c.o["relax_cell"], max_steps=c.o["relax_steps"])
    if c.o["elastic"]:
        calc.elastic(strain=c.o["strain"])
    if c.o["cohesive"]:
        if c.o["e1"] >= c.o["e2"]:
            raise ValueError("状態方程式の範囲は 最小 < 最大 にしてください")
        calc.cohesive_curve(c.o["e1"], c.o["e2"])
    calc.single_point()
    return calc


def collect_static(calc, c: Context, out: Collected):
    out.attempt("エネルギー", lambda: out.add("エネルギー", calc.energy_per_atom(), "eV/atom", 7))

    def stress():
        s = np.asarray(calc.stress(), dtype=float).ravel()
        out.add("圧力 (応力の対角平均)", -float(np.mean(s[:3])), "GPa")
    out.attempt("応力", stress)

    def forces():
        out.add("力の最大値", float(np.abs(calc.forces()).max()), "eV/Å")
    out.attempt("力", forces)

    if c.o["elastic"]:
        def elastic():
            m = calc.elastic_moduli()
            for label, name in (("体積弾性率 B", "bulk"), ("剛性率 G", "shear"), ("ヤング率 E", "young")):
                value = getattr(m, name, None)
                if value is None:
                    value = getattr(m, name + "_modulus", None)
                out.add(label, value, "GPa")
            out.add("ポアソン比", getattr(m, "poisson", None) if hasattr(m, "poisson") else getattr(m, "poisson_ratio", None))
            matrix = np.asarray(calc.elastic_matrix(), dtype=float)
            frame = pd.DataFrame(matrix, columns=[f"C_i{j + 1}" for j in range(6)])
            frame.insert(0, "i", np.arange(1, 7))
            out.datasets.append(Dataset("elastic", "弾性定数 C_ij (GPa)", frame, "i", ["C_i1"],
                                        {"i": "i ＼ j", **{f"C_i{j + 1}": str(j + 1) for j in range(6)}}, table=True))
        out.attempt("弾性定数", elastic)
    if c.o["cohesive"]:
        def cohesive():
            frame = _frame(calc.cohesive())
            columns = list(frame.columns)
            y = "energy_per_atom" if "energy_per_atom" in columns else columns[-1]
            out.datasets.append(Dataset("cohesive", "状態方程式 (凝集エネルギー曲線)", frame, columns[0], [y],
                                        {"scale": "セルの伸縮 (倍)", "energy_per_atom": "エネルギー (eV/atom)",
                                         "energy": "エネルギー (eV)", "volume": "体積 (Å³)"}))
            eq = calc.equilibrium_from_cohesive()
            out.add("平衡点の伸縮", eq.get("scale"), "倍", 5)
            out.add("平衡点のエネルギー", eq.get("energy_per_atom"), "eV/atom", 7)
            out.add("平衡点の体積", eq.get("volume"), "Å³", 6)
        out.attempt("状態方程式", cohesive)


STATIC = Kind(
    "static", "静的計算", "基本", "構造最適化・弾性定数・状態方程式・エネルギー (MD はしません)",
    [flag("relax", "構造最適化をする", True, hint="FIRE 法で原子位置を緩和します"),
     flag("relax_cell", "セルも最適化する", False, show_if={"relax": True}),
     num("relax_steps", "最適化の最大ステップ", 1000, min=1, max=100000, integer=True, show_if={"relax": True}, advanced=True),
     flag("elastic", "弾性定数を計算する", True),
     num("strain", "弾性定数のひずみ", 0.01, min=1e-4, max=0.1, show_if={"elastic": True}, advanced=True),
     flag("cohesive", "状態方程式を計算する", True, hint="セルを等方的に伸縮させてエネルギーを調べます"),
     num("e1", "伸縮の最小", 0.9, min=0.5, max=1.0, unit="倍", show_if={"cohesive": True}, advanced=True),
     num("e2", "伸縮の最大", 1.1, min=1.0, max=2.0, unit="倍", show_if={"cohesive": True}, advanced=True)],
    build_static, collect_static)


# ---- 拡散・イオン伝導 -----------------------------------------------------------------

def _species_order(atoms: Atoms) -> list[str]:
    order: list[str] = []
    for symbol in atoms.get_chemical_symbols():
        if symbol not in order:
            order.append(symbol)
    return order


TEMPERATURE_LIST = r"(?:\d+(?:\.\d+)?(?:\s*[,、 ]\s*\d+(?:\.\d+)?)*)?"
MAX_SCAN = 20


def scan_temperatures(text: str) -> list[float]:
    """温度スキャンの欄 ("600, 700, 800") を温度のリストにする。空欄なら空リスト。"""
    values = [float(v) for v in re.split(r"[,、\s]+", text.strip()) if v]
    if not values:
        return []
    if len(values) < 2:
        raise ValueError("温度スキャンには温度を 2 つ以上入れてください")
    if len(values) > MAX_SCAN:
        raise ValueError(f"温度スキャンは {MAX_SCAN} 点までです")
    if len(set(values)) != len(values):
        raise ValueError("温度スキャンに同じ温度が重なっています")
    if any(not 1 <= v <= 20000 for v in values):
        raise ValueError("温度スキャンの温度は 1〜20000 K にしてください")
    return sorted(values)


def build_diffusion(c: Context):
    o = c.o
    calc = c.make(wf.DiffusionCalculation)
    calc.group_by_species()
    ic = None
    if o["ic_element"]:
        order = _species_order(calc.atoms)
        if o["ic_element"] not in order:
            raise ValueError(f"イオン伝導度の元素 {o['ic_element']} は構造に含まれていません ({'・'.join(order)})")
        ic = (order.index(o["ic_element"]), o["ic_charge"])
    pressure = o["pressure"] if o["ensemble"] == "npt" else None
    if o["equil_steps"]:
        calc.equilibrate(temperature=o["temperature"], steps=o["equil_steps"], pressure=pressure)
    calc.production(temperature=o["temperature"], steps=o["steps"], msd=True, rdf=o["rdf"], viscosity=o["viscosity"],
                    ic=ic, per_species=True, sample_interval=o["sample_interval"],
                    Nc=min(o["correlation"], max(1, o["steps"] // o["sample_interval"] // 2)))
    return calc


def collect_diffusion(calc, c: Context, out: Collected):
    order = _species_order(calc.atoms)

    def per_species():
        if len(order) == 1:
            # 元素が 1 種類だと msd.out の列にグループ番号が付かず、元素別の読み取りが使えないので全体から求める
            result = calc.diffusion_coefficient()
            out.add(f"拡散係数 D ({order[0]})", float(result["D_cm2_per_s"]), "cm²/s", key=f"D:{order[0]}")
            return
        table = calc.diffusion_per_species()
        for _, row in table.iterrows():
            out.add(f"拡散係数 D ({row['species']})", float(row["D_cm2_per_s"]), "cm²/s", key=f"D:{row['species']}")
    out.attempt("拡散係数", per_species)

    def msd():
        frame = _frame(calc.msd())
        labels = {"time": "時間 (ps)"}
        totals = []
        # 元素ごとに x, y, z を足した MSD を先頭に並べる (GPUMD の出力は方向別)
        for group, symbol in enumerate(order):
            parts = [f"msd_{axis}_g{group}" for axis in "xyz"]
            if len(order) == 1 and not all(part in frame for part in parts):
                parts = [f"msd_{axis}" for axis in "xyz"]   # 元素が 1 種類のときは列にグループ番号が付かない
            if all(part in frame for part in parts):
                frame[f"msd_{symbol}"] = frame[parts].sum(axis=1)
                labels[f"msd_{symbol}"] = f"MSD {symbol} (Å²)"
                totals.append(f"msd_{symbol}")
        frame = frame[["time", *totals, *[col for col in frame.columns if col not in ("time", *totals)]]]
        out.datasets.append(Dataset("msd", "平均二乗変位 (MSD)", frame, "time", totals[:4] or list(frame.columns[1:2]),
                                    labels))
    out.attempt("MSD", msd)

    if c.o["rdf"]:
        def rdf():
            frame = _frame(calc.rdf())
            columns = list(frame.columns)
            out.datasets.append(Dataset("rdf", "動径分布関数 (RDF)", frame, columns[0], columns[1:2],
                                        {"radius": "距離 (Å)", "total": "g(r) 全体"}))
        out.attempt("RDF", rdf)
    if c.o["ic_element"]:
        def ic():
            result = calc.ionic_conductivity()
            out.add(f"イオン伝導度 ({c.o['ic_element']})", _first(result, "ic", "sigma_mS_per_cm"), "mS/cm",
                    key=f"sigma:{c.o['ic_element']}")
        out.attempt("イオン伝導度", ic)
    if c.o["viscosity"]:
        def viscosity():
            result = calc.viscosity()
            for key, value in result.items():
                if isinstance(value, (int, float)) and "Pa" in key:
                    out.add(f"粘性率 {key}", value, "Pa·s")
        out.attempt("粘性率", viscosity)


DIFFUSION = Kind(
    "diffusion", "拡散・イオン伝導", "物性", "平衡化のあと本計算を行い、MSD から拡散係数を求めます",
    [num("temperature", "温度", 1000, min=1, max=20000, unit="K"),
     ptext("scan_temperatures", "温度スキャン (アレニウスプロット用)", "", TEMPERATURE_LIST, max_length=200,
           hint="例: 600,700,800,900,1000。温度ごとにジョブを作り、上の温度は使いません。空欄なら 1 点だけ"),
     choice("ensemble", "アンサンブル", "nvt", [{"value": "nvt", "label": "NVT (体積一定)"},
                                             {"value": "npt", "label": "NPT (圧力一定で平衡化)"}]),
     num("pressure", "圧力", 0, min=-100, max=1000, unit="GPa", show_if={"ensemble": "npt"}),
     steps("equil_steps", "平衡化", 10000),
     steps("steps", "本計算", 50000),
     text("ic_element", "イオン伝導度を求める元素", "", hint="例: Li。空欄なら計算しません"),
     num("ic_charge", "その元素の電荷", 1, min=-8, max=8, unit="e", show_if=None),
     flag("rdf", "動径分布関数 (RDF) も計算する", True),
     flag("viscosity", "粘性率も計算する", False, advanced=True),
     num("sample_interval", "サンプリング間隔", 5, min=1, max=1000, integer=True, unit="ステップ", advanced=True),
     num("correlation", "相関の長さ", 500, min=10, max=100000, integer=True, unit="サンプル", advanced=True)],
    build_diffusion, collect_diffusion)


# ---- 熱輸送 -------------------------------------------------------------------------

SHC_NC = 250          # スペクトル分解の相関の長さ (チュートリアル 04 と同じ)


def build_thermal(c: Context):
    o = c.o
    if o["method"] == "nemd":
        # 熱流の方向にブロックへ分け、両端を固定・熱源・熱浴にする (チュートリアル 04 の ballistic)
        calc = wf.ThermalTransport.for_nemd(c.atoms, c.potential["path"], c.workdir, axis=o["direction"],
                                            n_blocks=o["n_blocks"], fixed_ends=True, **c.base())
        calc.set_dump(thermo=c.thermo_interval, traj=c.traj_interval or None, traj_properties=())
        calc.nvt(temperature=o["temperature"], steps=o["equil_steps"])
        calc.add_commands(modifiers.fix(calc.layout.fixed[0]))
        calc.nemd(temperature=o["temperature"], steps=o["steps"], delta_T=o["delta_T"])
        if o["spectral"]:
            calc.spectral(direction=o["direction"], Nc=SHC_NC, group=(0, calc.layout.middle[len(calc.layout.middle) // 2]))
        return calc
    calc = c.make(wf.ThermalTransport)
    calc.equilibrate(temperature=o["temperature"], steps=o["equil_steps"])
    if o["method"] == "hnemd":
        calc.hnemd(temperature=o["temperature"], steps=o["steps"], Fe=o["Fe"], direction=o["direction"],
                   output_interval=o["output_interval"])
        if o["spectral"]:
            calc.spectral(direction=o["direction"], Nc=SHC_NC)
    else:
        samples = o["steps"] // o["sampling_interval"]
        if samples < 20:
            raise ValueError("ステップ数が少なすぎます。サンプリング間隔の 20 倍以上にしてください")
        calc.green_kubo(temperature=o["temperature"], steps=o["steps"], sampling_interval=o["sampling_interval"],
                        correlation_steps=min(o["correlation_steps"], samples // 2))
    return calc


def collect_thermal(calc, c: Context, out: Collected):
    hnemd = c.o["method"] == "hnemd"
    if c.o["method"] == "nemd":
        collect_nemd(calc, c, out)
        return

    def kappa():
        result = calc.thermal_conductivity()
        if hnemd:   # 駆動した方向だけが意味を持つ
            axis = c.o["direction"]
            out.add(f"熱伝導率 κ ({axis})", result.get(f"kappa_{axis}"), "W/m/K")
            out.add("標準誤差", result.get(f"kappa_{axis}_stderr"), "W/m/K", 3)
        else:
            out.add("熱伝導率 κ (平均)", result.get("kappa"), "W/m/K")
            for axis in "xyz":
                out.add(f"熱伝導率 κ ({axis})", result.get(f"kappa_{axis}"), "W/m/K")
    out.attempt("熱伝導率", kappa)

    def series():
        from gpumd_toolkit import outputs
        if hnemd:
            frame = _frame(outputs.read_kappa(calc.workdir / "kappa.out"))
            frame.insert(0, "time_ps", np.arange(1, len(frame) + 1) * c.o["output_interval"] * c.time_step * 1e-3)
            axis = c.o["direction"]
            # 累積平均 (その時刻までの平均) で収束の様子を見る
            out.datasets.append(Dataset("kappa", "熱伝導率の累積平均 (HNEMD)", frame, "time_ps", [f"kappa_{axis}_cum"],
                                        {"time_ps": "時間 (ps)", **{f"kappa_{a}_cum": f"κ {a} 累積平均 (W/m/K)" for a in "xyz"},
                                         **{f"kappa_{a}": f"κ {a} 瞬間値 (W/m/K)" for a in "xyz"}}))
        else:
            frame = _frame(outputs.read_hac(calc.workdir / "hac.out"))
            y = [col for col in ("kappa_x", "kappa_y", "kappa_z") if col in frame]
            out.datasets.append(Dataset("hac", "熱伝導率の積分 (Green-Kubo)", frame, "time", y,
                                        {"time": "相関時間 (ps)", **{f"kappa_{a}": f"κ {a} (W/m/K)" for a in "xyz"}}))
    out.attempt("熱伝導率の推移", series)
    if hnemd and c.o["spectral"]:
        def spectral():
            _, spectrum = calc.spectral_result()
            # κ(ω) = J(ω) / (V T Fe) (単位換算 1602.17662: eV·Å/ps/THz/Å³/K·Å → W/m/K/THz)
            volume = float(calc.atoms.get_volume())
            factor = 1602.17662 / (volume * c.o["temperature"] * c.o["Fe"])
            frame = pd.DataFrame({"nu": spectrum["omega"] / (2 * np.pi), "kappa_in": spectrum["Jq_in"] * factor,
                                  "kappa_out": spectrum["Jq_out"] * factor})
            frame["kappa"] = frame["kappa_in"] + frame["kappa_out"]
            total = float(np.trapezoid(frame["kappa"], frame["nu"]))
            out.add("スペクトルの積分 ∫κ(ν)dν", total, "W/m/K")
            out.datasets.append(Dataset("shc", "スペクトル熱伝導率 κ(ν)", frame, "nu", ["kappa", "kappa_in", "kappa_out"],
                                        {"nu": "振動数 (THz)", "kappa": "κ(ν) 合計 (W/m/K/THz)",
                                         "kappa_in": "面内成分 (W/m/K/THz)", "kappa_out": "面外成分 (W/m/K/THz)"}))
        out.attempt("スペクトル分解", spectral)


def collect_nemd(calc, c: Context, out: Collected):
    def kappa():
        result = calc.nemd_result()
        for key, value in result.items():
            if isinstance(value, (int, float)) and "kappa" in key:
                out.add(f"熱伝導率 ({key})", value, "W/m/K")
            elif isinstance(value, (int, float)) and "gradient" in key:
                out.add("温度勾配", value, "K/Å", 4)
            elif isinstance(value, (int, float)) and key in ("heat_flux", "flux", "conductance"):
                out.add(key, value)
        out.notes.append("NEMD の熱伝導率は系の長さ (熱源と熱浴の距離) に依存します。長さを変えて比べてください")
    out.attempt("熱伝導率", kappa)

    def profile():
        frame = _frame(calc.temperature_profile())
        out.datasets.append(Dataset("profile", "温度プロファイル (定常状態)", frame, "position", ["temperature"],
                                    {"position": "位置 (Å)", "temperature": "温度 (K)", "group": "ブロック",
                                     "is_middle": "測定区間"}))
    out.attempt("温度プロファイル", profile)
    if c.o["spectral"]:
        def spectral():
            _, spectrum = calc.spectral_result()
            frame = pd.DataFrame({"nu": spectrum["omega"] / (2 * np.pi), "jq": spectrum["Jq"],
                                  "jq_in": spectrum["Jq_in"], "jq_out": spectrum["Jq_out"]})
            out.datasets.append(Dataset("shc", "スペクトル熱流 J(ν) (中央のブロック)", frame, "nu", ["jq", "jq_in", "jq_out"],
                                        {"nu": "振動数 (THz)", "jq": "J(ν) 合計", "jq_in": "面内成分", "jq_out": "面外成分"}))
        out.attempt("スペクトル分解", spectral)


THERMAL = Kind(
    "thermal", "熱輸送", "物性", "格子熱伝導率を HNEMD・Green-Kubo・NEMD で求めます。HNEMD と NEMD はスペクトル分解もできます",
    [choice("method", "手法", "hnemd", [{"value": "hnemd", "label": "HNEMD (効率がよい)"},
                                      {"value": "gk", "label": "Green-Kubo (平衡 MD)"},
                                      {"value": "nemd", "label": "NEMD (熱源と熱浴で温度勾配をつける)"}]),
     num("temperature", "温度", 300, min=1, max=20000, unit="K"),
     steps("equil_steps", "平衡化", 20000),
     steps("steps", "本計算", 200000),
     choice("direction", "熱流の方向", "x", AXES, show_if={"method": ["hnemd", "nemd"]}),
     num("delta_T", "熱源と熱浴の温度差の半分", 10, min=0.1, max=1000, unit="K", show_if={"method": "nemd"},
         hint="熱源を 温度 + この値、熱浴を 温度 - この値 に保つ"),
     num("n_blocks", "ブロックの数", 10, min=4, max=200, integer=True, show_if={"method": "nemd"}, advanced=True,
         hint="熱流の方向に分ける数 (両端の固定層は別)。方向に長いスーパーセルにしてください"),
     flag("spectral", "スペクトル分解 (振動数ごとの寄与) もする", False, show_if={"method": ["hnemd", "nemd"]}),
     num("Fe", "駆動力 Fe", 1e-5, min=1e-8, max=1e-2, unit="1/Å", show_if={"method": "hnemd"},
         hint="大きすぎると線形応答から外れます", advanced=True),
     num("output_interval", "出力間隔", 1000, min=1, max=1000000, integer=True, unit="ステップ",
         show_if={"method": "hnemd"}, advanced=True),
     num("sampling_interval", "サンプリング間隔", 10, min=1, max=1000, integer=True, unit="ステップ",
         show_if={"method": "gk"}, advanced=True),
     num("correlation_steps", "相関の長さ", 5000, min=10, max=1000000, integer=True, unit="サンプル",
         show_if={"method": "gk"}, advanced=True)],
    build_thermal, collect_thermal)


# ---- 自由エネルギー -------------------------------------------------------------------

def build_free_energy(c: Context):
    o = c.o
    calc = c.make(wf.FreeEnergyCalculation)
    calc.equilibrate(temperature=o["temperature"], steps=o["equil_steps"], pressure=o["pressure"])
    absolute = calc.frenkel_ladd if o["phase"] == "solid" else calc.uhlenbeck_ford
    absolute(temperature=o["temperature"], t_equil=o["t_equil"], t_switch=o["t_switch"], pressure=o["pressure"])
    if o["scan"]:
        if o["T_end"] == o["temperature"]:
            raise ValueError("温度スキャンの終点は、基準の温度と違う値にしてください")
        calc.reversible_scaling(T_min=o["temperature"], T_max=o["T_end"], pressure=o["pressure"],
                                t_equil=o["t_equil"], t_switch=o["scan_switch"],
                                transition_guard="truncate" if o["guard"] else "off")
    return calc


def collect_free_energy(calc, c: Context, out: Collected):
    kind = "ti_spring" if c.o["phase"] == "solid" else "ti_liquid"

    def absolute():
        try:
            g = calc.gibbs_free_energy()
        except Exception:
            g = calc.gibbs_free_energy(kind)
        out.add(f"自由エネルギー G ({c.o['temperature']:g} K)", _first(g, "G"), "eV/atom", 6)
        out.add("ヘルムホルツ自由エネルギー F", _first(g, "F"), "eV/atom", 6)
        out.add("参照系の自由エネルギー", _first(g, "E_Einstein", "F_UF", "E_UF"), "eV/atom", 6)
    out.attempt("自由エネルギー", absolute)

    def hysteresis():
        h = calc.hysteresis()
        out.add("往復のずれ (RMS)", h.get("rms"), "", 3)
        out.add("往復のずれ (最大)", h.get("max_abs"), "", 3)
    out.attempt("往復のずれ", hysteresis)

    if c.o["scan"]:
        def curve():
            frame = _frame(calc.free_energy_curve())
            columns = list(frame.columns)
            x = next((col for col in columns if col.lower() in ("t", "temperature", "t_k")), columns[0])
            y = next((col for col in columns if col.lower().startswith("g")), columns[-1])
            out.datasets.append(Dataset("g_t", "自由エネルギー G(T)", frame, x, [y],
                                        {"temperature": "温度 (K)", "G": "G (eV/atom)"}))
        out.attempt("G(T) 曲線", curve)


FREE_ENERGY = Kind(
    "free_energy", "自由エネルギー", "物性", "熱力学的積分で絶対自由エネルギーを求め、温度スキャンで G(T) を作ります",
    [choice("phase", "相", "solid", [{"value": "solid", "label": "固体 (Frenkel-Ladd)"},
                                    {"value": "liquid", "label": "液体 (Uhlenbeck-Ford)"}]),
     num("temperature", "基準の温度", 300, min=1, max=20000, unit="K"),
     num("pressure", "圧力", 0, min=-100, max=1000, unit="GPa"),
     steps("equil_steps", "平衡化", 10000),
     steps("t_switch", "切り替えの長さ", 20000, hint="短いと往復のずれが大きくなります"),
     steps("t_equil", "切り替え前後の保持", 4000, advanced=True),
     flag("scan", "温度スキャンで G(T) を求める", True, hint="可逆スケーリング。基準の温度から終点まで走査します"),
     num("T_end", "スキャンの終点", 800, min=1, max=20000, unit="K", show_if={"scan": True}),
     steps("scan_switch", "スキャンの長さ", 20000, show_if={"scan": True}),
     flag("guard", "相転移をまたいだ範囲を除く", True, show_if={"scan": True}, advanced=True)],
    build_free_energy, collect_free_energy)


# ---- 引張試験 ------------------------------------------------------------------------

def build_tensile(c: Context):
    o = c.o
    calc = c.make(wf.MechanicalCalculation)
    calc.equilibrate(temperature=o["temperature"], steps=o["equil_steps"], pressure=0.0)
    calc.tensile(temperature=o["temperature"], steps=o["steps"], strain_rate=o["strain_rate"], axis=o["axis"])
    return calc


def collect_tensile(calc, c: Context, out: Collected):
    def properties():
        result = calc.mechanical_properties()
        out.add("ヤング率", result.get("young_modulus_GPa"), "GPa")
        out.add("最大応力", result.get("max_stress_GPa"), "GPa")
        out.add("最大応力のときのひずみ", result.get("strain_at_max"))
        out.add("到達したひずみ", result.get("final_strain"))
    out.attempt("機械的性質", properties)

    def curve():
        frame = _frame(calc.stress_strain())
        columns = list(frame.columns)
        x = next((col for col in columns if "strain" in col.lower()), columns[0])
        y = next((col for col in columns if "stress" in col.lower()), columns[-1])
        out.datasets.append(Dataset("stress_strain", "応力ひずみ曲線", frame, x, [y],
                                    {"strain": "ひずみ", "stress_GPa": "応力 (GPa)", "temperature": "温度 (K)",
                                     "potential_energy": "ポテンシャルエネルギー (eV)"}))
    out.attempt("応力ひずみ曲線", curve)


TENSILE = Kind(
    "tensile", "引張試験", "物性", "平衡化のあと 1 方向に一定のひずみ速度で引っ張り、応力ひずみ曲線を求めます",
    [num("temperature", "温度", 300, min=1, max=20000, unit="K"),
     choice("axis", "引っ張る方向", "x", AXES),
     num("strain_rate", "ひずみ速度", 1e9, min=1e5, max=1e12, unit="1/s",
         hint="最終ひずみ = ひずみ速度 × ステップ数 × 時間刻み"),
     steps("equil_steps", "平衡化", 10000),
     steps("steps", "引張", 100000)],
    build_tensile, collect_tensile)


# ---- Monte Carlo --------------------------------------------------------------------

def build_mc(c: Context):
    o = c.o
    calc = c.make(wf.MonteCarloCalculation)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            calc.check_box_size(strict=True)
        except Exception as exc:
            raise ValueError(f"セルが小さすぎます。スーパーセルを大きくしてください ({exc})") from exc
    if o["mode"] == "mcmd":
        calc.canonical(temperature=o["temperature"], temperature_end=o["temperature_end"], steps=o["steps"],
                       md_steps=o["md_steps"], mc_trials=o["mc_trials"])
    else:
        calc.mcmc(temperature=o["temperature"], temperature_end=o["temperature_end"], trials=o["trials"],
                  trials_per_call=o["trials_per_call"])
    return calc


def collect_mc(calc, c: Context, out: Collected):
    out.attempt("受理率", lambda: out.add("受理率", calc.acceptance_ratio(), "", 3))

    def history():
        frame = _frame(calc.mcmd())
        columns = list(frame.columns)
        y = next((col for col in columns if "accept" in col.lower()), columns[-1])
        out.datasets.append(Dataset("mcmd", "MC の受理率の推移", frame, columns[0], [y],
                                    {"step": "ステップ", "acceptance": "受理率", "mc_trials": "試行回数"}))
    out.attempt("受理率の推移", history)


MC = Kind(
    "mc", "Monte Carlo", "発展", "原子の種類を入れ替える MC。合金や固溶体の規則化を調べます (元素が 2 種類以上必要)",
    [choice("mode", "方法", "mcmd", [{"value": "mcmd", "label": "MC + MD (MD の合間に交換)"},
                                    {"value": "mcmc", "label": "MC のみ (原子は動かさず交換だけ)"}]),
     num("temperature", "温度", 1000, min=1, max=20000, unit="K"),
     num("temperature_end", "終了温度", None, min=1, max=20000, unit="K", optional=True, hint="入れると焼きなまし"),
     steps("steps", "MD のステップ数", 20000, show_if={"mode": "mcmd"}),
     num("md_steps", "MC を挟む間隔", 100, min=1, max=100000, integer=True, unit="ステップ", show_if={"mode": "mcmd"}),
     num("mc_trials", "1 回あたりの交換試行", 100, min=1, max=100000, integer=True, unit="回", show_if={"mode": "mcmd"}),
     num("trials", "交換試行の総数", 100000, min=1, max=100_000_000, integer=True, unit="回", show_if={"mode": "mcmc"}),
     num("trials_per_call", "1 回の呼び出しあたり", 1000, min=1, max=1_000_000, integer=True, unit="回",
         show_if={"mode": "mcmc"}, advanced=True)],
    build_mc, collect_mc, min_species=2)


# ---- PIMD ---------------------------------------------------------------------------

def build_pimd(c: Context):
    o = c.o
    calc = c.make(wf.PathIntegralCalculation)
    if o["method"] == "pimd":
        calc.pimd(num_beads=int(o["num_beads"]), temperature=o["temperature"], steps=o["steps"])
    else:
        calc.qtb(temperature=o["temperature"], steps=o["steps"], f_max=o["f_max"])
    return calc


def collect_pimd(calc, c: Context, out: Collected):
    if c.o["method"] == "qtb":
        out.notes.append("量子熱浴では零点エネルギーの分だけ、運動温度が目標温度より高く出ます。異常ではありません。")


PIMD = Kind(
    "pimd", "PIMD", "発展", "核の量子効果を入れた MD (経路積分 MD または量子熱浴)",
    [choice("method", "方法", "pimd", [{"value": "pimd", "label": "経路積分 MD (PIMD)"},
                                      {"value": "qtb", "label": "量子熱浴 (QTB、軽い)"}]),
     num("temperature", "温度", 300, min=1, max=20000, unit="K"),
     steps("steps", "ステップ数", 20000),
     choice("num_beads", "ビーズ数", "8", [{"value": str(n), "label": str(n)} for n in (4, 8, 16, 32, 64)],
            show_if={"method": "pimd"}, hint="多いほど正確ですが、その分だけ遅くなります"),
     num("f_max", "最大振動数", None, min=1, max=10000, unit="THz", optional=True, show_if={"method": "qtb"},
         advanced=True, hint="空欄なら自動")],
    build_pimd, collect_pimd)


# ---- 衝撃波 -------------------------------------------------------------------------

def build_shock(c: Context):
    o = c.o
    calc = c.make(wf.ShockCalculation)
    calc.equilibrate(temperature=o["temperature"], steps=o["equil_steps"])
    calc.msst(shock_velocity=o["shock_velocity"], steps=o["steps"], direction=o["direction"], qmass=o["qmass"],
              mu=o["mu"])
    return calc


def collect_shock(calc, c: Context, out: Collected):
    def state():
        state = calc.hugoniot_state()
        out.add("衝撃後の温度", state.get("temperature"), "K")
        out.add("衝撃後の圧力", state.get("pressure_GPa"), "GPa")
        out.add("衝撃方向の応力", state.get("Pxx_GPa"), "GPa")
        out.add("衝撃後の体積", state.get("volume_per_atom"), "Å³/atom")
        out.add("衝撃後のエネルギー", state.get("energy_per_atom"), "eV/atom", 6)
    out.attempt("衝撃後の状態", state)


SHOCK = Kind(
    "shock", "衝撃波", "発展", "MSST 法で、定常衝撃波が通過したあとの状態 (Hugoniot) を求めます",
    [num("shock_velocity", "衝撃波の速度", 6.0, min=0.1, max=100, unit="km/s"),
     choice("direction", "衝撃の方向", "x", AXES),
     num("temperature", "初期温度", 300, min=1, max=20000, unit="K"),
     steps("equil_steps", "平衡化", 10000),
     steps("steps", "衝撃圧縮", 50000),
     num("qmass", "セルの仮想質量", 10000, min=1, max=1e9, advanced=True),
     num("mu", "人工粘性", 10, min=0, max=1e6, advanced=True)],
    build_shock, collect_shock)


# ---- 二温度モデル ---------------------------------------------------------------------

def build_ttm(c: Context):
    o = c.o
    calc = c.make(wf.TwoTemperatureCalculation)
    calc.limit_displacement(o["max_displacement"])
    calc.equilibrate(temperature=o["temperature"], steps=o["equil_steps"])
    calc.ttm(steps=o["steps"], Ce=o["Ce"], rho_e=o["rho_e"], kappa_e=o["kappa_e"], gamma_p=o["gamma_p"],
             grid=(o["nx"], o["ny"], o["nz"]), T_e_init=o["T_e_init"], out_interval=o["out_interval"],
             source=o["source"] or None)
    return calc


def collect_ttm(calc, c: Context, out: Collected):
    def history():
        frame = _frame(calc.two_temperature_history())
        columns = list(frame.columns)
        y = [col for col in columns if col.startswith("T_")] or columns[1:]
        out.datasets.append(Dataset("ttm", "電子温度と格子温度", frame, columns[0], y,
                                    {"T_electron": "電子温度 (K)", "T_lattice": "格子温度 (K)", "step": "ステップ"}))
        if len(frame):
            out.add("最後の電子温度", float(frame["T_electron"].iloc[-1]), "K")
            out.add("最後の格子温度", float(frame["T_lattice"].iloc[-1]), "K")
    out.attempt("温度の推移", history)


TTM = Kind(
    "ttm", "二温度モデル", "発展", "電子系を連続体として扱い、電子と格子のエネルギーのやり取りを入れた MD (レーザー照射など)",
    [num("temperature", "格子の初期温度", 300, min=1, max=20000, unit="K"),
     num("T_e_init", "電子の初期温度", 2000, min=1, max=1e6, unit="K"),
     steps("equil_steps", "平衡化", 5000),
     steps("steps", "二温度モデルの計算", 20000),
     num("Ce", "電子比熱 Ce", 1.0, min=1e-8, max=1e6, unit="eV/K (電子 1 個あたり)"),
     num("rho_e", "電子の数密度", 1.0, min=1e-8, max=1e3, unit="1/Å³"),
     num("kappa_e", "電子の熱伝導率", 0.005, min=0, max=1e6, unit="eV/(ps·Å·K)"),
     num("gamma_p", "電子フォノン結合", 0.01, min=0, max=1e6, unit="g/(mol·ps)"),
     num("source", "レーザーの吸収", 0, min=0, max=1e6, unit="eV/(ps·Å³)", hint="0 なら加熱なし"),
     num("nx", "電子グリッド x", 1, min=1, max=200, integer=True, advanced=True),
     num("ny", "電子グリッド y", 1, min=1, max=200, integer=True, advanced=True),
     num("nz", "電子グリッド z", 4, min=1, max=200, integer=True, advanced=True),
     num("out_interval", "電子温度の出力間隔", 200, min=1, max=1000000, integer=True, unit="ステップ", advanced=True),
     num("max_displacement", "1 ステップの最大移動", 0.05, min=0.001, max=1, unit="Å", advanced=True)],
    build_ttm, collect_ttm)


# ---- Active Learning ----------------------------------------------------------------

def build_al(c: Context):
    o = c.o
    if len(c.potentials) < 2:
        raise ValueError("ポテンシャルを 2 つ以上選んでください (予測のばらつきを使うため)")
    committee = [[p["path"]] for p in c.potentials]
    calc = wf.ActiveLearningCalculation(c.atoms, committee, c.workdir, **c.base())
    calc.set_dump(thermo=c.thermo_interval, traj=c.traj_interval or None, traj_properties=())
    calc.explore(T_start=o["temperature"], T_end=o["temperature_end"], steps=o["steps"], threshold=o["threshold"],
                 interval=o["interval"])
    return calc


def collect_al(calc, c: Context, out: Collected):
    def summary():
        summary = calc.uncertainty_summary()
        out.add("不確かさの平均", summary.get("mean"), "eV/Å")
        out.add("不確かさの 95 パーセンタイル", summary.get("p95"), "eV/Å")
        out.add("不確かさの最大", summary.get("max"), "eV/Å")
        out.add("評価した回数", int(summary.get("n_samples", 0)), "回")
        out.add("しきい値を超えた回数", int(summary.get("n_exceeded", 0)), "回")
    out.attempt("不確かさの要約", summary)

    def series():
        frame = _frame(calc.uncertainty())
        columns = list(frame.columns)
        frame.insert(0, "sample", np.arange(1, len(frame) + 1))
        out.datasets.append(Dataset("uncertainty", "予測のばらつき (不確かさ)", frame, "sample", columns[-1:],
                                    {"sample": "評価の回数", "time": "時間 (ps)", "uncertainty": "不確かさ (eV/Å)"}))
    out.attempt("不確かさの推移", series)

    def candidates():
        path = calc.write_candidates()
        out.notes.append(f"しきい値を超えた構造を {Path(path).name} にまとめました (DFT で計算する候補)。")
    out.attempt("候補構造", candidates)


AL = Kind(
    "al", "Active Learning", "発展", "複数の NEP の予測のばらつきが大きい構造を MD 中に集めます (学習データの候補)",
    [num("temperature", "開始温度", 300, min=1, max=20000, unit="K"),
     num("temperature_end", "終了温度", 1000, min=1, max=20000, unit="K"),
     steps("steps", "ステップ数", 20000),
     num("threshold", "不確かさのしきい値", 0.05, min=0, max=100, unit="eV/Å", hint="これを超えた構造を保存します"),
     num("interval", "評価の間隔", 10, min=1, max=100000, integer=True, unit="ステップ")],
    build_al, collect_al, committee=True)


KINDS: dict[str, Kind] = {k.key: k for k in (STATIC, DIFFUSION, THERMAL, FREE_ENERGY, TENSILE, MC, PIMD, SHOCK, TTM, AL)}
MD_PUBLIC = {"key": "md", "label": "MD", "group": "基本", "note": "NVT / NPT / アニール / 任意温度プロファイル",
             "fields": None, "committee": False}
GROUPS = ["基本", "物性", "発展"]

# GPUMD-Tutorials 由来の種類 (tutorial_kinds.py)。kinds の部品を使うので、ここまで定義してから読み込む
from . import tutorial_kinds  # noqa: E402

KINDS.update({k.key: k for k in tutorial_kinds.KINDS})
GROUPS += tutorial_kinds.GROUPS

# PLUMED を使う種類 (plumed_kind.py)。PLUMED 対応の gpumd で動かす
from . import plumed_kind  # noqa: E402

KINDS.update({k.key: k for k in plumed_kind.KINDS})
PLUMED_KINDS = {k.key for k in plumed_kind.KINDS}

# 構造相転移 (温度・圧力スキャン + 結晶構造の判定、transition_kind.py)
from . import transition_kind  # noqa: E402

KINDS.update({k.key: k for k in transition_kind.KINDS})

# NEP のファインチューニング (SevenNet を先生にした蒸留 + 能動学習、finetune.py)
from . import finetune  # noqa: E402

KINDS.update({k.key: k for k in finetune.KINDS})


def public_list() -> list[dict]:
    return [MD_PUBLIC] + [kind.public() for kind in KINDS.values()]


def get(key: str) -> Kind:
    if key not in KINDS:
        raise ValueError("計算の種類が正しくありません")
    return KINDS[key]


def build(kind: Kind, context: Context) -> tuple[GPUMDCalculation, list[str]]:
    """計算を組み立てる。toolkit の警告も一緒に返す。"""
    if len(set(context.atoms.get_chemical_symbols())) < kind.min_species:
        raise ValueError(f"{kind.label} には元素が {kind.min_species} 種類以上の構造が必要です")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            calc = kind.build(context)
        except ValueError:
            raise
        except (TypeError, KeyError, RuntimeError) as exc:
            raise ValueError(f"この条件では計算を組み立てられません: {exc}") from exc
        from .mdjob import cell_problem
        problem = cell_problem(calc, context.atoms, context.repeat)
        if problem:
            raise ValueError(problem)
    if calc.builder.total_steps > settings.MAX_TOTAL_STEPS:
        raise ValueError(f"合計ステップ数が上限を超えています "
                         f"({calc.builder.total_steps:,} > {settings.MAX_TOTAL_STEPS:,})")
    notes = [str(w.message) for w in caught if not issubclass(w.category, DeprecationWarning)]
    return calc, list(dict.fromkeys(notes))
