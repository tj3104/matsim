"""化学的短範囲秩序: ウォーレン・カウリーの秩序度 (Warren–Cowley short-range order parameter)。

殻 k (第 k 近接) について、元素 i の原子のまわりの殻 k にいる原子のうち、元素 j の割合を p_ij(k) とすると

    α_ij(k) = 1 − p_ij(k) / c_j          (c_j は元素 j の全体の割合)

違う元素の組 (i ≠ j) で読む:
    α_ij < 0: i のまわりに j が多い (違う元素どうしが好まれる、規則化)
    α_ij = 0: ランダム
    α_ij > 0: i のまわりに j が少ない (同じ元素どうしが集まる、偏析・クラスター化)
同じ元素の組 (α_ii) は符号の意味が逆 (負なら同じ元素が集まる)。2 元系では c_i α_ij = c_j α_ji、α_ii = −(c_j/c_i) α_ij。

殻の境目は、全体の動径分布関数 g(r) の谷 (山と山のあいだの最も低い所)。熱でぼやけて山がつながると、殻は合わさる
(bcc の第 1・第 2 近接など)。原子数が有限だと、ランダムな並びでも α は 0 からずれるので、そのずれの目安
σ (2 項分布から) も出す: |α| が σ の 2〜3 倍より大きければ、意味のある秩序と言える。

原子ごとの局所的な秩序度 (第 1 殻): a_i = 1 − (同じ元素のとなりの割合) / c_self を 3D の色塗りに使う
(負: 違う元素に囲まれている、正: 同じ元素に囲まれている)。
"""
from __future__ import annotations

import math

import numpy as np
from ase import Atoms
from ase.neighborlist import neighbor_list

from .coordination import DR, RMAX, SMOOTH, _species, rdf

MAX_SHELLS = 3


def shell_edges(r: np.ndarray, g: np.ndarray, count: int = MAX_SHELLS) -> list[float]:
    """殻の外側の境目 (g(r) の谷) を内側から count 個まで。谷が平ら (完全な結晶) なら真ん中。"""
    dr = r[1] - r[0]
    kernel_r = np.arange(-3 * SMOOTH, 3 * SMOOTH + 1e-9, dr)
    kernel = np.exp(-0.5 * (kernel_r / SMOOTH) ** 2)
    s = np.convolve(g, kernel / kernel.sum(), mode="same")
    top = s.max()
    # 山: まわりより高く、全体の 5% より高い極大
    peaks = [k for k in range(1, len(s) - 1) if s[k] >= s[k - 1] and s[k] > s[k + 1] and s[k] > 0.05 * top]
    merged = []
    for k in peaks:   # 近すぎる山 (同じ山のぎざぎざ) はまとめる
        if merged and r[k] - r[merged[-1]] < 0.25:
            if s[k] > s[merged[-1]]:
                merged[-1] = k
            continue
        merged.append(k)
    edges = []
    for a, b in zip(merged, merged[1:]):
        low = s[a:b + 1].min()
        flat = np.flatnonzero(s[a:b + 1] <= low + 0.02 * top) + a
        # 谷が十分に深いときだけ境目にする (浅いくぼみは同じ殻の中)
        if low < 0.85 * min(s[a], s[b]):
            edges.append(float((r[flat[0]] + r[flat[-1]]) / 2))
        if len(edges) == count:
            break
    return edges


def warren_cowley(atoms: Atoms, edges: list[float]) -> dict:
    """殻ごとの α_ij・ランダムでのゆらぎ σ_ij・殻の配位数と、原子ごとの局所的な秩序度 (第 1 殻)。"""
    order, kind = _species(atoms)
    n = len(atoms)
    m = len(order)
    conc = np.bincount(kind, minlength=m) / n
    i, j, d = neighbor_list("ijd", atoms, float(edges[-1]))
    inner = 0.0
    shells = []
    local = None
    for k, outer in enumerate(edges):
        mask = (d >= inner) & (d < outer)
        pairs = np.zeros((m, m))
        np.add.at(pairs, (kind[i[mask]], kind[j[mask]]), 1)
        around = pairs.sum(axis=1)                    # 元素 i のまわりの殻 k の原子の数の合計
        with np.errstate(divide="ignore", invalid="ignore"):
            p = pairs / around[:, None]
            alpha = 1 - p / conc[None, :]
            sigma = np.sqrt(conc[None, :] * (1 - conc[None, :]) / around[:, None]) / conc[None, :]
        z = np.bincount(i[mask], minlength=n)
        shells.append({
            "shell": k + 1, "inner": inner, "outer": outer,
            "coordination": float(z.mean()),
            "alpha": {a: {b: _num(alpha[x, y]) for y, b in enumerate(order)} for x, a in enumerate(order)},
            "sigma": {a: {b: _num(sigma[x, y]) for y, b in enumerate(order)} for x, a in enumerate(order)},
        })
        if k == 0:   # 原子ごとの局所的な秩序度
            same = np.bincount(i[mask], weights=(kind[i[mask]] == kind[j[mask]]).astype(float), minlength=n)
            with np.errstate(divide="ignore", invalid="ignore"):
                local = 1 - (same / np.maximum(z, 1)) / conc[kind]
            local[z == 0] = 0
        inner = outer
    return {"species": order, "concentration": {s: float(c) for s, c in zip(order, conc)}, "shells": shells,
            "local": local}


def _num(v: float) -> float | None:
    return None if not math.isfinite(v) else float(v)


def analyze(final: Atoms, frames: list, *, shells: int = MAX_SHELLS, progress=None, view_stride: int = 1) -> dict:
    """最終構造の α_ij (殻ごと) と、フレームごとの第 1 殻の α_ij (殻の境目はフレームごとに g(r) から決め直す)。"""
    order, _ = _species(final)
    if len(order) < 2:
        raise ValueError("ウォーレン・カウリーの秩序度には、元素が 2 種類以上の構造が必要です")
    g = rdf(final, RMAX, DR)
    edges = shell_edges(g["r"], g["total"], shells)
    if not edges:
        raise ValueError("動径分布関数から殻の境目 (谷) が見つかりません")
    wc = warren_cowley(final, edges)
    local = wc.pop("local")
    result = {
        "n_atoms": len(final), **wc,
        "rdf": {"r": g["r"].round(4).tolist(), "total": g["total"].round(5).tolist()},
        "view_local": local[::view_stride].round(4).tolist(),
    }
    if progress:
        progress(1)
    if frames:
        pairs = [(a, b) for x, a in enumerate(order) for b in order[x:]]
        evolution = {"time_ps": [], "frame": [], "alpha": {f"{a}–{b}": [] for a, b in pairs}}
        for k, (time_ps, atoms) in enumerate(frames):
            fg = rdf(atoms, min(RMAX, 1.3 * edges[0] + 0.5), DR)
            fe = shell_edges(fg["r"], fg["total"], 1) or edges[:1]
            first = warren_cowley(atoms, fe[:1])["shells"][0]["alpha"]
            evolution["time_ps"].append(time_ps)
            evolution["frame"].append(k)
            for a, b in pairs:
                evolution["alpha"][f"{a}–{b}"].append(first[a][b])
            if progress:
                progress(k + 2)
        result["evolution"] = evolution
    return result
