"""構造相転移 (温度・圧力スキャン)。

温度か圧力を段階的に変えながら、セルの形も自由に変えられる NPT (三斜晶) を流し、各段の終わりの構造を
結晶構造の判定 (cna.py の adaptive CNA) にかけて、fcc / hcp / bcc などの比率・体積・格子の変化を追う。
行き (昇温・加圧) と帰り (降温・減圧) の両方を流すと、転移の起こる温度・圧力のずれ (ヒステリシス) が分かる。

    例: Ti・Zr の hcp → bcc (昇温)、Fe の bcc → hcp (加圧、約 13 GPa)、Fe の bcc ↔ fcc (昇温)

MD の転移は過熱・過冷却で本当の転移点からずれる (小さい系・短い時間ほど大きくずれる)。行きと帰りの真ん中が目安。
正確な転移点は、自由エネルギー面 (PLUMED) や、2 相を並べた計算で決める。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from ase.io import read as read_any
from gpumd_toolkit import workflows as wf

from . import cna, structures
from .kinds import Collected, Context, Dataset, Kind, choice, flag, num, steps
from .tutorial_kinds import _lengths, _thermo_frame, calc_atoms

MAX_POINTS = 30
FRAMES_PER_POINT = 5            # 各段の後半から等間隔に何枚の構造を判定するか (熱ゆらぎをならす)
MAX_JUDGED_ATOMS = 4_000_000    # 判定する 原子数 × 枚数 の上限 (CNA で数分)
CRYSTAL = [t for t in cna.TYPES if t != "その他"]


def frames_per_point(n_atoms: int, n_stages: int) -> int:
    """各段で判定する構造の枚数。原子が多いときは減らす (1〜FRAMES_PER_POINT)。"""
    return int(max(1, min(FRAMES_PER_POINT, MAX_JUDGED_ATOMS // max(n_atoms * n_stages, 1))))


def dominant_type(fractions: dict[str, float]) -> str:
    """その段の構造。結晶 (fcc・hcp・bcc・正二十面体) のうち最も多いもの。

    高温の bcc などは熱ゆらぎで「その他」に判定される原子が半分を超えることがあるので、「その他」は
    結晶らしい原子が 2 割に満たないとき (液体・アモルファスなど) だけにする。
    """
    if sum(fractions[t] for t in CRYSTAL) < 0.2:
        return "その他"
    return max(CRYSTAL, key=lambda t: fractions[t])


def scan_path(o: dict) -> tuple[list[float], list[str]]:
    """各段の温度 (または圧力) と向き ("行き" / "帰り")。"""
    key = "t" if o["control"] == "temperature" else "p"
    values = np.linspace(o[f"{key}_start"], o[f"{key}_end"], o["n_points"]).tolist()
    if o["round_trip"]:
        back = values[-2::-1]
        return values + back, ["行き"] * len(values) + ["帰り"] * len(back)
    return values, ["行き"] * len(values)


def build_transition(c: Context):
    o = c.o
    if o["n_points"] < 2:
        raise ValueError("点の数は 2 以上にしてください")
    if (o["t_start"] if o["control"] == "temperature" else o["p_start"]) == \
            (o["t_end"] if o["control"] == "temperature" else o["p_end"]):
        raise ValueError("始めと終わりを別の値にしてください")
    calc = c.make(wf.MechanicalCalculation)
    values, _ = scan_path(o)
    # 各段で k 枚ずつ構造を保存する (後半から等間隔。結晶構造の判定に使う)。GPUMD は run ごとに数え直すので、
    # 各段の枚数は steps // 間隔 で決まる
    k = frames_per_point(calc.n_atoms, len(values))
    calc.set_dump(thermo=c.thermo_interval, traj=max(1, o["steps"] // (2 * k)), traj_properties=())

    def stage(value, n):
        t = value if o["control"] == "temperature" else o["temperature"]
        p = value if o["control"] == "pressure" else o["pressure"]
        calc.npt(temperature=t, steps=n, pressure=p, barostat="npt_scr", cell_mode=o["cell_mode"])
    stage(values[0], o["equil_steps"])        # 最初の点で平衡化
    for value in values:
        stage(value, o["steps"])
    return calc


def _stage_table(c: Context) -> pd.DataFrame:
    """平衡化を除いた各段の後半 50% の平均。"""
    frame = _thermo_frame(c)
    lengths = _lengths(frame)
    cell = frame[["ax", "ay", "az", "bx", "by", "bz", "cx", "cy", "cz"]].to_numpy().reshape(-1, 3, 3)

    def angle(u, v):
        return np.degrees(np.arccos(np.clip((u * v).sum(1) / np.linalg.norm(u, axis=1) / np.linalg.norm(v, axis=1),
                                            -1, 1)))
    frame["a"], frame["b"], frame["c"] = lengths[:, 0], lengths[:, 1], lengths[:, 2]
    frame["alpha"] = angle(cell[:, 1], cell[:, 2])
    frame["beta"] = angle(cell[:, 0], cell[:, 2])
    frame["gamma"] = angle(cell[:, 0], cell[:, 1])
    n = calc_atoms(c)
    rows = []
    for stage, group in frame.groupby("stage"):
        if stage == 0:
            continue
        tail = group.iloc[len(group) // 2:]
        rows.append({"stage": int(stage), "temperature": tail["temperature"].mean(),
                     "pressure": tail["pressure"].mean(), "volume_per_atom": tail["volume"].mean() / n,
                     "pe_per_atom": tail["potential_energy"].mean() / n,
                     **{k: tail[k].mean() for k in ("a", "b", "c", "alpha", "beta", "gamma")}})
    return pd.DataFrame(rows)


def _stage_fractions(c: Context, n_stages: int) -> list[dict[str, float]]:
    """各段の後半の構造 (k 枚) を判定して平均した、結晶構造の比率。"""
    o = c.o
    n_atoms = len(c.atoms) * c.repeat[0] * c.repeat[1] * c.repeat[2]
    k = frames_per_point(n_atoms, n_stages)
    per_stage = o["steps"] // max(1, o["steps"] // (2 * k))      # 各段で書かれる枚数
    frames = read_any(c.workdir / "dump.xyz", index=":", format="extxyz")
    if len(frames) < per_stage * n_stages:
        raise ValueError(f"トラジェクトリの枚数が足りません ({len(frames)} < {per_stage * n_stages})")
    frames = frames[len(frames) - per_stage * n_stages:]
    result = []
    for s in range(n_stages):
        stage = frames[s * per_stage:(s + 1) * per_stage][-k:]     # その段の後半 k 枚
        fracs = [cna.fractions(cna.classify(structures.clean(a))) for a in stage]
        result.append({t: float(np.mean([f[t] for f in fracs])) for t in cna.TYPES})
    return result


def transitions(values: list[float], dominant: list[str]) -> list[tuple[str, str, float, float]]:
    """最も多い構造が入れ替わった所 (前の構造, 後の構造, 前の値, 後の値)。"""
    found = []
    for k in range(1, len(values)):
        if dominant[k] != dominant[k - 1]:
            found.append((dominant[k - 1], dominant[k], values[k - 1], values[k]))
    return found


def collect_transition(calc, c: Context, out: Collected):
    o = c.o
    values, legs = scan_path(o)
    is_t = o["control"] == "temperature"
    unit = "K" if is_t else "GPa"
    x_label = "温度 (K)" if is_t else "圧力 (GPa)"

    def run():
        table = _stage_table(c)
        if len(table) != len(values):
            raise ValueError(f"段の数が合いません ({len(table)} / {len(values)})")
        table["target"] = values
        table["leg"] = legs
        fracs = _stage_fractions(c, len(values))
        for t in cna.TYPES:
            table[t] = [f[t] for f in fracs]
        dominant = [dominant_type(f) for f in fracs]
        table["dominant"] = dominant
        a0, b0, c0 = table[["a", "b", "c"]].iloc[0]
        table["a_rel"], table["b_rel"], table["c_rel"] = table["a"] / a0, table["b"] / b0, table["c"] / c0
        (c.workdir / "analysis").mkdir(exist_ok=True)
        table.to_csv(c.workdir / "analysis" / "transition_table.csv", index=False)

        up = table[table["leg"] == "行き"]
        down = table[table["leg"] == "帰り"]
        out.add("始めの構造", f"{dominant[0]} ({fracs[0][dominant[0]] * 100:.0f}%)")
        turn = len(up) - 1
        out.add("折り返し点の構造", f"{dominant[turn]} ({fracs[turn][dominant[turn]] * 100:.0f}%)")
        if len(down):
            out.add("終わりの構造", f"{dominant[-1]} ({fracs[-1][dominant[-1]] * 100:.0f}%)")
        mids = {}
        for leg, part in (("行き", up), ("帰り", down)):
            for before, after, v1, v2 in transitions(part["target"].tolist(), part["dominant"].tolist()):
                lo, hi = sorted((v1, v2))
                out.add(f"{leg}の転移 {before} → {after}", f"{lo:g}〜{hi:g} {unit} のあいだ")
                mids.setdefault(leg, []).append(((v1 + v2) / 2, before, after))
                rows = part.set_index("target")
                dv = (rows.loc[v2, "volume_per_atom"] - rows.loc[v1, "volume_per_atom"]) / rows.loc[v1, "volume_per_atom"]
                out.add(f"{leg}の転移での体積の変化", float(dv * 100), "%", 3)
        if "行き" in mids and "帰り" in mids:
            forward, backward = mids["行き"][0], mids["帰り"][-1]
            if {forward[1], forward[2]} == {backward[1], backward[2]}:
                out.add("ヒステリシスの幅 (行きと帰りの転移の差)", abs(forward[0] - backward[0]), unit, 4)
                out.add("転移点の目安 (行きと帰りの真ん中)", (forward[0] + backward[0]) / 2, unit, 5)
        if not mids:
            out.notes.append("この範囲では、最も多い結晶構造は入れ替わりませんでした (範囲を広げるか、各段を長くしてください)")
        out.notes.append("各段の構造は、後半の構造を数枚判定して平均しています。高温では熱ゆらぎで「その他」に判定される原子が"
                         "増えるので、段の構造は結晶 (fcc・hcp・bcc・正二十面体) のうち最も多いもので決めています "
                         "(結晶らしい原子が 2 割未満なら「その他」= 液体など)")
        out.notes.append("MD の転移は過熱・過冷却でずれるので、行きと帰りの真ん中を目安にしてください。"
                         "転移が起こるか・起こる温度や圧力は、ポテンシャルの精度に強く左右されます (汎用のポテンシャルでは、"
                         "相の安定性の順番が第一原理計算と違うことがあります)")

        def by_leg(columns, labels):
            frame = pd.DataFrame({"x": sorted(set(values))})
            names = {"x": x_label}
            for leg, part in (("行き", up), ("帰り", down)):
                if not len(part):
                    continue
                rows = part.set_index("target")
                for col in columns:
                    key = f"{col}_{'up' if leg == '行き' else 'down'}"
                    frame[key] = frame["x"].map(rows[col])
                    names[key] = f"{labels[col]} ({leg})"
            return frame, names
        frame, names = by_leg(["volume_per_atom"], {"volume_per_atom": "体積 (Å³/原子)"})
        out.datasets.append(Dataset("volume", "体積", frame, "x", [k for k in frame.columns if k != "x"], names))
        frame, names = by_leg(["pe_per_atom"], {"pe_per_atom": "ポテンシャルエネルギー (eV/原子)"})
        out.datasets.append(Dataset("energy", "ポテンシャルエネルギー", frame, "x", [k for k in frame.columns if k != "x"],
                                    names))
        shown = [t for t in cna.TYPES if table[t].max() > 0.02]
        for leg, part in (("行き", up), ("帰り", down)):
            if not len(part):
                continue
            key = "up" if leg == "行き" else "down"
            frame = part[["target", *shown]].reset_index(drop=True)
            out.datasets.append(Dataset(f"structure_{key}", f"結晶構造の比率 ({leg}: {'昇温' if is_t else '加圧'}"
                                        f"{'' if leg == '行き' else ' の帰り'})", frame, "target", shown,
                                        {"target": x_label, **{t: f"{t} の比率" for t in shown}}))
        frame, names = by_leg(["a_rel", "b_rel", "c_rel"], {"a_rel": "a / a(始め)", "b_rel": "b / b(始め)",
                                                            "c_rel": "c / c(始め)"})
        out.datasets.append(Dataset("cell", "セルの辺の長さ (始めを 1)", frame, "x", [k for k in frame.columns if k != "x"],
                                    names))
        frame, names = by_leg(["alpha", "beta", "gamma"], {"alpha": "α (°)", "beta": "β (°)", "gamma": "γ (°)"})
        out.datasets.append(Dataset("angles", "セルの角度", frame, "x", [k for k in frame.columns if k != "x"], names))
    out.attempt("構造相転移", run)
    out.notes.append("各段の終わりの構造は、ジョブの「結晶構造の判定」でも時間変化として見られます")


TRANSITION = Kind(
    "transition", "構造相転移 (温度・圧力スキャン)", "物性",
    "温度か圧力を段階的に変えながら、セルの形も自由に変えられる NPT を流し、結晶構造 (fcc・hcp・bcc など) の比率・"
    "体積・格子の変化から転移を探します。行きと帰りを流してヒステリシスも見ます (例: Ti の hcp → bcc、Fe の bcc → hcp)",
    [choice("control", "変えるもの", "temperature", [{"value": "temperature", "label": "温度 (昇温 → 降温)"},
                                                  {"value": "pressure", "label": "圧力 (加圧 → 減圧)"}]),
     num("t_start", "始めの温度", 300, min=1, max=20000, unit="K", show_if={"control": "temperature"}),
     num("t_end", "折り返しの温度", 1500, min=1, max=20000, unit="K", show_if={"control": "temperature"}),
     num("pressure", "圧力 (一定)", 0, min=-100, max=1000, unit="GPa", show_if={"control": "temperature"}),
     num("p_start", "始めの圧力", 0, min=-100, max=1000, unit="GPa", show_if={"control": "pressure"}),
     num("p_end", "折り返しの圧力", 30, min=-100, max=1000, unit="GPa", show_if={"control": "pressure"}),
     num("temperature", "温度 (一定)", 300, min=1, max=20000, unit="K", show_if={"control": "pressure"}),
     num("n_points", "点の数 (行き)", 13, min=2, max=MAX_POINTS, integer=True,
         hint="始めから折り返しまでを等間隔に。帰りも同じ点を逆にたどります"),
     flag("round_trip", "帰り (降温・減圧) も流す", True, hint="行きと帰りの転移のずれ (ヒステリシス) が分かります"),
     steps("equil_steps", "最初の平衡化", 10000),
     steps("steps", "各点のステップ数", 20000, hint="長いほど過熱・過冷却が小さくなり、転移点が正確になります"),
     choice("cell_mode", "セルの変え方", "tri", [{"value": "tri", "label": "形も変える (三斜晶)"},
                                              {"value": "ortho", "label": "直方体のまま 3 辺を別々に"}],
            advanced=True, hint="結晶の形が変わる転移 (bcc → hcp など) は「形も変える」が必要です")],
    build_transition, collect_transition)

KINDS = [TRANSITION]
