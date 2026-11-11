"""原子分布: 選んだ元素の原子を 1 つの軸に射影し、分割幅ごとの原子数を数える (高さ方向の濃度分布など)。

座標はセルの中に戻した (wrap した) デカルト座標。直方体のセルなら 0〜セルの長さ、斜めのセルなら
原子の座標の最小〜最大を、分割幅で区切って数える。トラジェクトリの複数フレームで平均もできる。
"""
from __future__ import annotations

import math

import numpy as np
from ase import Atoms

AXIS = {"x": 0, "y": 1, "z": 2}
MAX_BINS = 5000


def _range(atoms: Atoms, axis: int) -> tuple[float, float]:
    cell = np.array(atoms.cell)
    off = np.delete(cell[axis], axis)
    others = np.delete(cell[:, axis], axis)
    if np.allclose(off, 0, atol=1e-6) and np.allclose(others, 0, atol=1e-6):
        return 0.0, float(cell[axis, axis])
    pos = atoms.get_positions(wrap=True)[:, axis]
    return float(pos.min()), float(pos.max())


def profile(frames: list[Atoms], species: list[str], axis: str, bin_width: float) -> dict:
    """フレームごとに数えて平均した原子数の分布。species が空ならすべての元素。"""
    if axis not in AXIS or bin_width <= 0:
        raise ValueError("軸と分割幅を確かめてください")
    k = AXIS[axis]
    present = sorted(set(frames[0].get_chemical_symbols()))
    chosen = [s for s in species if s in present] or present
    missing = [s for s in species if s not in present]
    lo, hi = _range(frames[0], k)
    n_bins = max(1, math.ceil((hi - lo) / bin_width - 1e-9))
    if n_bins > MAX_BINS:
        raise ValueError(f"分割が多すぎます ({n_bins:,})。分割幅を大きくしてください")
    # 区間の境目にちょうど乗った原子 (完全な結晶の原子面など) が、浮動小数点の誤差で上下にばらけないよう、
    # 座標と境目を 1e-6 Å に丸めてから数える (境目の原子は上の区間に入る)
    edges = np.round(lo + np.arange(n_bins + 1) * bin_width, 6)
    counts = {s: np.zeros(n_bins) for s in chosen}
    for atoms in frames:
        pos = np.round(atoms.get_positions(wrap=True)[:, k], 6)
        symbols = np.array(atoms.get_chemical_symbols())
        for s in chosen:
            counts[s] += np.histogram(pos[symbols == s], bins=edges)[0]
    for s in chosen:
        counts[s] /= len(frames)
    total = np.sum([counts[s] for s in chosen], axis=0)
    return {"axis": axis, "bin_width": bin_width, "range": [lo, float(edges[-1])], "n_frames": len(frames),
            "centers": ((edges[:-1] + edges[1:]) / 2).round(4).tolist(),
            "counts": {s: counts[s].round(4).tolist() for s in chosen}, "total": total.round(4).tolist(),
            "species": chosen, "missing": missing,
            "n_atoms": {s: int(round(float(counts[s].sum()))) for s in chosen}}
