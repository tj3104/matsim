"""PLUMED を使う計算の種類: 自由エネルギー面 (well-tempered メタダイナミクス) と、CV の記録。

PLUMED 対応でビルドした別の gpumd (settings.GPUMD_PLUMED_BIN、~/repos/plumed/README.md) で動かす。
相転移・融解・結晶化などを、集団変数 (CV) の自由エネルギー面として調べる。CV は材料によらず使えるものに絞った:

    配位数 (平均)        COORDINATIONNUMBER … MEAN       融解・結晶化・アモルファス化・配位の変わる相転移
    Q6 / Q4 (平均)       Q6 / Q4 … MEAN                  結晶らしさ (固相 / 液相)。Q4 は fcc・hcp・bcc の区別にも
    結晶構造との類似度    ENVIRONMENTSIMILARITY … MEAN    fcc / bcc / hcp / 単純立方 / ダイヤモンドにどれだけ近いか
                                                        (構造相転移。Piaggi & Parrinello の CV)
    セルの比 c/a・b/a    CELL から (最初を 1、NPT のみ)    ベイン変形 (fcc ↔ bcc)・正方晶化などセルの形の変わる転移
    体積 (1 原子)        VOLUME / 原子数 (NPT のみ)        密度の変わる相転移
    2 原子の距離         DISTANCE                          拡散のホッピング・結合の切断

構造相転移 (セルの形が変わる転移) では、セルを三斜晶まで自由に変えられる NPT にする (cell_mode、既定は CV に合わせて自動)。

CV は 1 つか 2 つ。自由エネルギー面は、PLUMED の HILLS (well-tempered では高さが γ/(γ-1) 倍で書かれる) の
ガウス関数を足し合わせて作る (plumed sum_hills と同じ計算。打ち切り 6.25σ² とずらしも合わせてある)。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from ase import Atoms
from ase.neighborlist import neighbor_list
from gpumd_toolkit import workflows as wf

from . import settings
from .kinds import Collected, Context, Dataset, Kind, choice, num, steps, text

KB = 8.617333262e-5           # [eV/K]
DP2CUTOFF = 6.25              # PLUMED のガウス関数の打ち切り (d²/2 < 6.25)

CVS = [{"value": "cn", "label": "配位数 (平均)"},
       {"value": "q6", "label": "Q6 (結晶らしさ)"},
       {"value": "q4", "label": "Q4 (fcc・hcp・bcc の区別)"},
       {"value": "similarity", "label": "結晶構造との類似度"},
       {"value": "ca", "label": "セルの比 c/a"},
       {"value": "ba", "label": "セルの比 b/a"},
       {"value": "volume", "label": "体積 (1 原子あたり)"},
       {"value": "distance", "label": "2 原子の距離"}]
CV_NAMES = {item["value"]: item["label"] for item in CVS}
TARGETS = [{"value": "BCC", "label": "bcc"}, {"value": "FCC", "label": "fcc"}, {"value": "HCP", "label": "hcp"},
           {"value": "SC", "label": "単純立方"}, {"value": "DIAMOND", "label": "ダイヤモンド"}]
TARGET_NAMES = {item["value"]: item["label"] for item in TARGETS}
# ガウス関数の幅の既定値 (CV の単位で)
SIGMA = {"cn": 0.1, "q6": 0.01, "q4": 0.01, "similarity": 0.02, "ca": 0.01, "ba": 0.01, "volume": 0.05, "distance": 0.1}
# PLUMED の計算の重さ (力の計算だけのときに対する倍率の目安。256 原子 Cu で実測)
COST = {"cn": 3.0, "q6": 8.0, "q4": 8.0, "similarity": 6.5, "ca": 1.2, "ba": 1.2, "volume": 1.2, "distance": 1.1}
STRUCTURAL = {"q4", "similarity", "ca", "ba"}   # 構造相転移向け (セルの形を自由にする)


class PlumedCalculation(wf.MechanicalCalculation):
    """run.in と一緒に plumed.dat を書き出す。"""

    plumed_text = ""

    def write_inputs(self):
        paths = super().write_inputs()
        (self.workdir / "plumed.dat").write_text(self.plumed_text)
        return paths

    def preview(self) -> str:
        return super().preview() + "\n# ---- plumed.dat ----\n" + self.plumed_text


def atom_ranges(indices) -> str:
    """0 始まりの番号を PLUMED の書き方 (1 始まり、連続は 1-10) にする。"""
    parts = []
    values = sorted(int(i) + 1 for i in indices)
    start = prev = values[0]
    for v in values[1:] + [None]:
        if v is not None and v == prev + 1:
            prev = v
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if v is not None:
            start = prev = v
    return ",".join(parts)


def nearest_distance(atoms: Atoms) -> float:
    """最近接原子間距離 (各原子の最も近い原子までの距離の中央値)。"""
    i, d = neighbor_list("id", atoms, 6.0)
    if not len(d):
        raise ValueError("6 Å 以内にとなりの原子がありません")
    nearest = np.full(len(atoms), np.inf)
    np.minimum.at(nearest, i, d)
    return float(np.median(nearest[np.isfinite(nearest)]))


def _group(atoms: Atoms, symbol: str, label: str) -> tuple[str, str]:
    """元素 (空欄ならすべて) の GROUP 行と、画面に出す名前。"""
    symbols = np.array(atoms.get_chemical_symbols())
    if not symbol:
        return f"{label}: GROUP ATOMS={atom_ranges(range(len(atoms)))}", "全原子"
    idx = np.flatnonzero(symbols == symbol)
    if not len(idx):
        raise ValueError(f"構造に {symbol} がありません ({'・'.join(dict.fromkeys(symbols))})")
    return f"{label}: GROUP ATOMS={atom_ranges(idx)}", symbol


def lattice_constants(target: str, d: float) -> str:
    """最近接距離 d [Å] の理想的な結晶の格子定数 (PLUMED の LATTICE_CONSTANTS の書き方)。"""
    if target == "FCC":
        return f"{math.sqrt(2) * d:.4f}"
    if target == "BCC":
        return f"{2 * d / math.sqrt(3):.4f}"
    if target == "HCP":
        return f"{d:.4f},{math.sqrt(8 / 3) * d:.4f}"
    if target == "DIAMOND":
        return f"{4 * d / math.sqrt(3):.4f}"
    return f"{d:.4f}"   # SC


def _cell_ratio(name: str, atoms: Atoms, which: str) -> list[str]:
    """セルの辺の長さの比 (c/a か b/a) を、最初の構造の値で割って 1 から始まるようにする。"""
    lengths = atoms.cell.lengths()
    other = {"c": 2, "b": 1}[which]
    start = lengths[other] / lengths[0]
    return [f"{name}_cell: CELL",
            f"{name}: CUSTOM ARG={name}_cell.ax,{name}_cell.ay,{name}_cell.az,{name}_cell.{which}x,{name}_cell.{which}y,"
            f"{name}_cell.{which}z VAR=ax,ay,az,ox,oy,oz FUNC=sqrt(ox*ox+oy*oy+oz*oz)/sqrt(ax*ax+ay*ay+az*az)/{start:.8f} "
            "PERIODIC=NO"]


def cv_lines(kind: str, name: str, atoms: Atoms, o: dict, switch: str, target: str = "BCC",
             d: float | None = None) -> tuple[list[str], str, str]:
    """CV 1 つ分の plumed.dat の行と、バイアスをかける値の名前・画面に出す名前。"""
    n = len(atoms)
    if kind in ("ca", "ba"):
        if o["ensemble"] != "npt":
            raise ValueError("セルの比を CV にするときは NPT にしてください")
        return _cell_ratio(name, atoms, kind[0]), name, f"セルの {kind[0]}/a (最初を 1)"
    if kind == "similarity":
        group, label = _group(atoms, o["species_a"], f"{name}_a")
        return [group, f"{name}: ENVIRONMENTSIMILARITY SPECIES={name}_a SIGMA={0.1 * d:.4f} CRYSTAL_STRUCTURE={target} "
                       f"LATTICE_CONSTANTS={lattice_constants(target, d)} MEAN"], \
            f"{name}.mean", f"{TARGET_NAMES[target]} との類似度 ({label})"
    if kind in ("cn", "q6", "q4"):
        lines = []
        group_a, label_a = _group(atoms, o["species_a"], f"{name}_a")
        group_b, label_b = _group(atoms, o["species_b"], f"{name}_b")
        same = o["species_a"] == o["species_b"]
        lines.append(group_a)
        species = f"SPECIES={name}_a" if same else f"SPECIESA={name}_a SPECIESB={name}_b"
        if not same:
            lines.append(group_b)
        action = {"cn": "COORDINATIONNUMBER", "q6": "Q6", "q4": "Q4"}[kind]
        lines.append(f"{name}: {action} {species} SWITCH={{{switch}}} MEAN")
        pair = label_a if same else f"{label_a}–{label_b}"
        title = f"配位数の平均 ({pair})" if kind == "cn" else f"{kind.upper()} の平均 ({pair})"
        return lines, f"{name}.mean", title
    if kind == "volume":
        if o["ensemble"] != "npt":
            raise ValueError("体積を CV にするときは NPT にしてください")
        return [f"{name}_v: VOLUME", f"{name}: CUSTOM ARG={name}_v FUNC=x/{n} PERIODIC=NO"], name, "体積 (Å³/原子)"
    if kind == "distance":
        i, j = int(o["atom_i"]), int(o["atom_j"])
        if i == j or not (1 <= i <= n and 1 <= j <= n):
            raise ValueError(f"距離を測る原子の番号は 1〜{n} の別々の番号にしてください (スーパーセルにしたあとの番号)")
        return [f"{name}: DISTANCE ATOMS={i},{j}"], name, f"原子 {i}–{j} の距離 (Å)"
    raise ValueError("CV の種類が正しくありません")


def plumed_input(atoms: Atoms, o: dict) -> tuple[str, list[dict]]:
    """plumed.dat の中身と、CV の一覧 [{"kind", "arg", "column", "title", "sigma"}]。"""
    kinds_ = [o["cv1"]] + ([o["cv2"]] if o["cv2"] != "none" else [])
    targets = [o["target1"], o["target2"]][:len(kinds_)]
    picked = [(k, t if k == "similarity" else None) for k, t in zip(kinds_, targets)]
    if len(set(picked)) != len(picked):
        raise ValueError("CV 1 と CV 2 には別の種類を選んでください (類似度どうしなら別の結晶構造)")
    d = None
    if any(k in ("cn", "q6", "q4", "similarity") for k in kinds_):
        d = nearest_distance(atoms)
    if any(k in ("cn", "q6", "q4") for k in kinds_):
        rc = o["cutoff"] or round(1.2 * d, 3)
        switch = f"CUBIC D_0={max(rc - 0.4, 0.1):.3f} D_MAX={rc:.3f}"
    else:
        rc, switch = None, ""
    lines = ["# MatSimApp が書き出した PLUMED の入力", "UNITS LENGTH=A TIME=fs ENERGY=eV"]
    cvs = []
    for k, (kind, target, sigma) in enumerate(zip(kinds_, targets, [o["sigma1"], o["sigma2"]]), start=1):
        body, arg, title = cv_lines(kind, f"cv{k}", atoms, o, switch, target, o["similarity_distance"] or d)
        lines += body
        cvs.append({"kind": kind, "arg": arg, "column": arg.replace(".", "_"), "title": title,
                    "sigma": sigma or SIGMA[kind]})
    args = ",".join(cv["arg"] for cv in cvs)
    printed = args
    if o["method"] == "metad":
        sigmas = ",".join(f"{cv['sigma']:g}" for cv in cvs)
        lines.append(f"metad: METAD ARG={args} SIGMA={sigmas} "
                     f"HEIGHT={o['height']:g} PACE={o['pace']} BIASFACTOR={o['biasfactor']:g} "
                     f"TEMP={o['temperature']:g} FILE=HILLS")
        printed += ",metad.bias"
    lines.append(f"PRINT ARG={printed} STRIDE={o['print_interval']} FILE=COLVAR")
    lines.append(f"FLUSH STRIDE={o['print_interval']}")
    if rc is not None:
        lines.insert(2, f"# 配位数・Q6・Q4 の数える距離: {rc - 0.4:.2f} Å まで 1、{rc:.2f} Å で 0 (なめらかにつなぐ)")
    if "similarity" in kinds_:
        lines.insert(2, f"# 類似度の比べる結晶: 最近接距離 {o['similarity_distance'] or d:.3f} Å の理想的な結晶")
    return "\n".join(lines) + "\n", cvs


def build_plumed(c: Context):
    if not (settings.GPUMD_PLUMED_BIN.is_file()):
        raise ValueError(f"PLUMED 対応の gpumd がありません ({settings.GPUMD_PLUMED_BIN})。"
                         "~/repos/plumed/README.md の手順でビルドしてください")
    o = c.o
    calc = c.make(PlumedCalculation)
    text_, _ = plumed_input(calc.atoms, o)
    calc.plumed_text = text_
    mode = cell_mode(o)
    for n in (o["equil_steps"], o["steps"]):   # 平衡化 (バイアスなし) → 本計算 (PLUMED)
        if o["ensemble"] == "npt":
            calc.npt(temperature=o["temperature"], steps=n, pressure=o["pressure"], barostat="npt_ber",
                     cell_mode=mode)
        else:
            calc.nvt(temperature=o["temperature"], steps=n, thermostat="nvt_bdp")
    calc.plumed("plumed.dat", interval=1)   # バイアスをかけるので毎ステップ呼ぶ
    if o["method"] == "metad" and o["steps"] < o["pace"] * 10:
        raise ValueError("本計算が短すぎます (ガウス関数を置く間隔の 10 倍以上にしてください)")
    return calc


def cell_mode(o: dict) -> str:
    """NPT のセルの変え方。自動なら、構造相転移向けの CV では三斜晶 (形も自由)、それ以外は等方。"""
    if o["cell_mode"] != "auto":
        return o["cell_mode"]
    chosen = {o["cv1"], o["cv2"]}
    return "tri" if chosen & STRUCTURAL else "iso"


def cost_factor(options: dict) -> float:
    """所要時間の目安に掛ける倍率。"""
    kinds_ = [options.get("cv1", "cn")] + ([options["cv2"]] if options.get("cv2", "none") != "none" else [])
    return max(COST.get(k, 1.0) for k in kinds_) + 0.2 * (len(kinds_) - 1)


# ---- 結果 -----------------------------------------------------------------------------

def read_plumed_table(path) -> pd.DataFrame:
    """COLVAR / HILLS (先頭の #! FIELDS 行が列名) を読む。"""
    names = None
    with open(path) as handle:
        for line in handle:
            if line.startswith("#! FIELDS"):
                names = line.split()[2:]
                break
    if names is None:
        raise ValueError(f"{path} に列名がありません")
    data = np.loadtxt(path, comments="#", ndmin=2)
    if data.size == 0:
        raise ValueError(f"{path} が空です")
    return pd.DataFrame(data[:, :len(names)], columns=[n.replace(".", "_") for n in names])


def fes_from_hills(hills: pd.DataFrame, columns: list[str], grids: list[np.ndarray], until: float | None = None) -> np.ndarray:
    """HILLS のガウス関数を足して自由エネルギー面 (最小値 0) を返す。until [fs] までの分だけ使うこともできる。"""
    if until is not None:
        hills = hills[hills["time"] <= until]
    centers = hills[columns].to_numpy()
    sigmas = hills[[f"sigma_{c}" for c in columns]].to_numpy()
    heights = hills["height"].to_numpy()
    mesh = np.stack([g.ravel() for g in np.meshgrid(*grids, indexing="ij")], axis=1)
    bias = np.zeros(len(mesh))
    shift = math.exp(-DP2CUTOFF)
    for start in range(0, len(centers), 500):   # メモリを抑えるため 500 個ずつ
        c, s, h = centers[start:start + 500], sigmas[start:start + 500], heights[start:start + 500]
        d2 = (((mesh[:, None, :] - c[None]) / s[None]) ** 2).sum(-1) / 2
        kernel = np.where(d2 < DP2CUTOFF, (np.exp(-d2) - shift) / (1 - shift), 0.0)
        bias += kernel @ h
    fes = -bias.reshape([len(g) for g in grids])
    return fes - fes.min()


def minima_1d(x: np.ndarray, f: np.ndarray, depth: float) -> list[int]:
    """1 次元の自由エネルギー曲線の谷 (まわりより depth 以上低い極小) の番号。"""
    found = []
    for i in range(1, len(f) - 1):
        if f[i] <= f[i - 1] and f[i] <= f[i + 1]:
            left, right = f[:i].max(), f[i + 1:].max()
            if min(left, right) - f[i] >= depth:
                found.append(i)
    return found


def collect_plumed(calc, c: Context, out: Collected):
    o = c.o
    _, cvs = plumed_input(calc.atoms, o)
    columns = [cv["column"] for cv in cvs]
    kt = KB * o["temperature"]
    labels = {cv["column"]: cv["title"] for cv in cvs}

    def colvar():
        frame = read_plumed_table(c.workdir / "COLVAR")
        frame.insert(0, "time_ps", frame["time"] / 1000)
        keep = ["time_ps", *columns] + (["metad_bias"] if "metad_bias" in frame else [])
        out.datasets.append(Dataset("colvar", "CV の時間変化", frame[keep], "time_ps", columns,
                                    {"time_ps": "時間 (ps)", **labels, "metad_bias": "バイアス (eV)"}))
        for cv in cvs:
            values = frame[cv["column"]].to_numpy()
            out.add(f"{cv['title']} の範囲", f"{values.min():.4g} 〜 {values.max():.4g}")
        if o["method"] == "watch":   # バイアスなし: 分布から F = -kT ln P
            tail = frame.iloc[len(frame) // 5:]
            if len(columns) == 1:
                hist, edges = np.histogram(tail[columns[0]], bins=60)
                centers = (edges[:-1] + edges[1:]) / 2
                ok = hist > 0
                f = -kt * np.log(hist[ok] / hist.max())
                out.datasets.append(Dataset("fes", "自由エネルギー (分布から)", pd.DataFrame(
                    {"s": centers[ok], "F": f}), "s", ["F"], {"s": cvs[0]["title"], "F": "自由エネルギー (eV)"}))
            else:
                hist, ex, ey = np.histogram2d(tail[columns[0]], tail[columns[1]], bins=40)
                xx, yy = np.meshgrid((ex[:-1] + ex[1:]) / 2, (ey[:-1] + ey[1:]) / 2, indexing="ij")
                ok = hist > 0
                out.datasets.append(Dataset("fes2d", "自由エネルギー面 (分布から)", pd.DataFrame(
                    {"s1": xx[ok], "s2": yy[ok], "F": -kt * np.log(hist[ok] / hist.max())}), "s1", ["s2"],
                    {"s1": cvs[0]["title"], "s2": cvs[1]["title"], "F": "自由エネルギー (eV)"},
                    style="colormap", color="F", equal_axes=False))
            out.notes.append("バイアスなしの記録なので、自由エネルギーは MD 中に訪れた範囲の分布 (最初の 20% を除く) から出しています。"
                             "障壁を越えない変化は見えません (越えさせるにはメタダイナミクスを選びます)")
    out.attempt("COLVAR", colvar)
    if o["method"] != "metad":
        return

    def fes():
        hills = read_plumed_table(c.workdir / "HILLS")
        out.add("置いたガウス関数の数", len(hills))
        grids = []
        for cv in cvs:
            lo, hi = hills[cv["column"]].min(), hills[cv["column"]].max()
            pad = 3 * cv["sigma"]
            grids.append(np.linspace(lo - pad, hi + pad, 160 if len(cvs) == 1 else 60))
        if len(cvs) == 1:
            x = grids[0]
            frame = pd.DataFrame({"s": x})
            t_end = hills["time"].max()
            for part in (0.25, 0.5, 0.75):   # 収束の確認: 途中までのガウス関数で作った曲線
                frame[f"F_{int(part * 100)}"] = fes_from_hills(hills, columns, grids, until=t_end * part)
            frame["F"] = fes_from_hills(hills, columns, grids)
            out.datasets.append(Dataset("fes", "自由エネルギー曲線", frame, "s", ["F", "F_75", "F_50", "F_25"],
                                        {"s": cvs[0]["title"], "F": "自由エネルギー (eV) 最後まで",
                                         **{f"F_{p}": f"途中 ({p}%)" for p in (75, 50, 25)}}))
            f = frame["F"].to_numpy()
            found = minima_1d(x, f, depth=kt)
            out.add("最も低い谷の位置", float(x[np.argmin(f)]), "", 4)
            if len(found) >= 2:
                deepest = sorted(found, key=lambda i: f[i])[:2]
                a, b = sorted(deepest)
                out.add("2 つの谷の自由エネルギー差", float(abs(f[a] - f[b])), "eV", 3)
                out.add("2 つの谷のあいだの障壁 (低い谷から)", float(f[a:b + 1].max() - min(f[a], f[b])), "eV", 3)
                out.notes.append(f"谷: {x[a]:.4g} と {x[b]:.4g} ({cvs[0]['title']})")
            else:
                out.notes.append("はっきりした谷は 1 つだけです (もう一方の状態に届いていないか、谷が 1 つ)")
        else:
            f2 = fes_from_hills(hills, columns, grids)
            xx, yy = np.meshgrid(*grids, indexing="ij")
            # バイアスがほとんど積まれていない (訪れていない) ところは平らな台地になるだけなので、地図に出さない
            visited = f2.ravel() < f2.max() - 0.02 * max(f2.max(), 1e-12)
            out.datasets.append(Dataset("fes2d", "自由エネルギー面 (訪れた範囲)", pd.DataFrame(
                {"s1": xx.ravel()[visited], "s2": yy.ravel()[visited], "F": f2.ravel()[visited]}), "s1", ["s2"],
                {"s1": cvs[0]["title"], "s2": cvs[1]["title"], "F": "自由エネルギー (eV)"},
                style="colormap", color="F", equal_axes=False))
            i, j = np.unravel_index(np.argmin(f2), f2.shape)
            out.add(f"最も低い点 ({cvs[0]['title']})", float(grids[0][i]), "", 4)
            out.add(f"最も低い点 ({cvs[1]['title']})", float(grids[1][j]), "", 4)
            # それぞれの CV だけの自由エネルギー (もう一方で積分): F(s) = -kT ln ∫ exp(-F/kT)
            for axis, cv in enumerate(cvs):
                p = np.exp(-(f2 - f2.min()) / kt).sum(axis=1 - axis)
                proj = -kt * np.log(p / p.max())
                out.datasets.append(Dataset(f"fes_{cv['column']}", f"{cv['title']} だけの自由エネルギー",
                                            pd.DataFrame({"s": grids[axis], "F": proj}), "s", ["F"],
                                            {"s": cv["title"], "F": "自由エネルギー (eV)"}))
        bias = read_plumed_table(c.workdir / "COLVAR")["metad_bias"].to_numpy()
        out.add("バイアスの最大値", float(bias.max()), "eV", 3)
        out.notes.append("自由エネルギーは系全体の値です (1 原子あたりではありません)。途中までの曲線と最後の曲線の形が"
                         "そろっていれば収束しています。ずれが大きければ本計算を長くしてください")
    out.attempt("自由エネルギー面", fes)


PLUMED = Kind(
    "plumed", "自由エネルギー面 (PLUMED)", "発展",
    "PLUMED のメタダイナミクスで、配位数・Q6・結晶構造との類似度・セルの形・体積などの集団変数 (CV) に沿った"
    "自由エネルギー面を作ります。融解・結晶化・構造相転移の障壁や安定性を調べます。CV を記録するだけ (バイアスなし) もできます",
    [choice("method", "やり方", "metad",
            [{"value": "metad", "label": "メタダイナミクス (自由エネルギー面を作る)"},
             {"value": "watch", "label": "CV を記録するだけ (バイアスなし)"}]),
     choice("cv1", "CV 1", "q6", CVS, hint="融解・結晶化は配位数・Q6、構造相転移 (bcc → hcp など) は結晶構造との類似度・"
            "Q4・セルの比、密度の変わる相転移は体積"),
     choice("target1", "CV 1 の比べる結晶構造", "BCC", TARGETS, show_if={"cv1": "similarity"},
            hint="この結晶にどれだけ近いか (0〜1) を CV にします"),
     choice("cv2", "CV 2", "none", [{"value": "none", "label": "なし (1 次元)"}] + CVS,
            hint="2 つ選ぶと 2 次元の自由エネルギー面になります (時間がかかります)。例: bcc との類似度 × hcp との類似度"),
     choice("target2", "CV 2 の比べる結晶構造", "HCP", TARGETS, show_if={"cv2": "similarity"}),
     text("species_a", "配位数・Q6・Q4・類似度の中心の元素", "", hint="空欄ならすべての原子", max_length=3),
     text("species_b", "配位数・Q6・Q4 で数えるとなりの元素", "", hint="空欄ならすべての原子", max_length=3),
     num("cutoff", "となりと数える距離", None, min=1.0, max=8.0, unit="Å", optional=True,
         hint="空欄なら最近接距離の 1.2 倍 (fcc なら第 1 近接だけ、bcc なら第 2 近接まで)"),
     num("similarity_distance", "類似度で比べる結晶の最近接距離", None, min=1.0, max=6.0, unit="Å", optional=True,
         advanced=True, hint="空欄なら今の構造の最近接距離。転移先の結晶の値が分かっていれば入れます"),
     num("atom_i", "距離を測る原子 1", 1, min=1, max=settings.MAX_ATOMS, integer=True, advanced=True,
         hint="CV に「2 原子の距離」を選んだとき。スーパーセルにしたあとの番号 (1 から)"),
     num("atom_j", "距離を測る原子 2", 2, min=1, max=settings.MAX_ATOMS, integer=True, advanced=True),
     choice("ensemble", "アンサンブル", "npt", [{"value": "npt", "label": "NPT (圧力一定)"},
                                            {"value": "nvt", "label": "NVT (体積一定)"}]),
     num("temperature", "温度", 1300, min=1, max=20000, unit="K"),
     num("pressure", "圧力", 0, min=-100, max=1000, unit="GPa", show_if={"ensemble": "npt"}),
     choice("cell_mode", "セルの変え方", "auto",
            [{"value": "auto", "label": "CV に合わせる (構造の CV なら形も、それ以外は等方)"},
             {"value": "tri", "label": "形も変える (三斜晶。構造相転移に)"},
             {"value": "ortho", "label": "直方体のまま 3 辺を別々に"},
             {"value": "iso", "label": "形を保って伸び縮み (等方)"}], show_if={"ensemble": "npt"},
            hint="結晶の形が変わる転移では「形も変える」にします。液体では形が崩れやすいので等方にします"),
     steps("equil_steps", "平衡化 (バイアスなし)", 10000),
     steps("steps", "本計算", 200000),
     num("height", "ガウス関数の高さ", 0.05, min=1e-4, max=10, unit="eV", show_if={"method": "metad"},
         hint="系全体のエネルギー。原子が多いほど自由エネルギー差も大きいので、高めにします"),
     num("pace", "ガウス関数を置く間隔", 500, min=1, max=1_000_000, integer=True, unit="ステップ",
         show_if={"method": "metad"}),
     num("biasfactor", "バイアス係数 γ", 20, min=1.1, max=10000, show_if={"method": "metad"},
         hint="well-tempered の係数。障壁の高さ ÷ kT 程度にする (大きいほど高い障壁を越えやすい)"),
     num("sigma1", "CV 1 のガウス関数の幅", None, min=1e-6, max=100, optional=True, advanced=True,
         show_if={"method": "metad"},
         hint="空欄なら 配位数 0.1 / Q6・Q4 0.01 / 類似度 0.02 / セルの比 0.01 / 体積 0.05 Å³ / 距離 0.1 Å"),
     num("sigma2", "CV 2 のガウス関数の幅", None, min=1e-6, max=100, optional=True, advanced=True,
         show_if={"method": "metad"}),
     num("print_interval", "CV を記録する間隔", 100, min=1, max=1_000_000, integer=True, unit="ステップ", advanced=True)],
    build_plumed, collect_plumed)

KINDS = [PLUMED]
