"""配位数 (距離で数える) と動径分布関数 g(r)。

原子 i のまわりで、距離 r_c (カットオフ) の内側にある原子の数を数える。r_c は、既定では元素の組み合わせごとの
部分動径分布関数 g_AB(r) の「第 1 の谷」(最初の山のあとの最も低い所) にする。結晶では第 1 近接の殻と第 2 近接の殻の
あいだ、液体では第 1 配位殻の外側になる。

    result = analyze(atoms, frames)        # 最終構造と、トラジェクトリのフレーム [(時刻 ps, 構造)]
    result["matrix"]["Mg"]["O"]            # Mg のまわりの O の数 (平均)

ボロノイの配位数 (面の数) とは数え方が違う: bcc は、ボロノイでは第 2 近接まで面を持つので 14、
距離では第 1 の谷の位置しだいで 8 (完全な結晶) か 14 (熱でぼやけて 2 つの山がつながったとき)。
"""
from __future__ import annotations

import math

import numpy as np
from ase import Atoms
from ase.neighborlist import neighbor_list

RMAX = 6.0          # g(r) を描く範囲 [Å]
DR = 0.02           # g(r) の刻み [Å]
SMOOTH = 0.06       # 谷を探すときにならす幅 [Å] (完全な結晶の g(r) は針のようなので)
MAX_RDF_PAIRS = 20_000_000


def _species(atoms: Atoms) -> tuple[list[str], np.ndarray]:
    symbols = atoms.get_chemical_symbols()
    order = list(dict.fromkeys(symbols))
    index = {s: k for k, s in enumerate(order)}
    return order, np.array([index[s] for s in symbols])


def rdf(atoms: Atoms, rmax: float = RMAX, dr: float = DR) -> dict:
    """全体と、元素の組み合わせごとの動径分布関数。"""
    n = len(atoms)
    volume = atoms.get_volume()
    order, kind = _species(atoms)
    # 原子が多いときは、数える範囲を狭めて組の数を抑える (1 原子あたりの組の数 ≒ 密度 × 球の体積)
    density = n / volume
    per_atom = density * 4 / 3 * math.pi * rmax ** 3
    if n * per_atom > MAX_RDF_PAIRS:
        rmax = max(3.5, (MAX_RDF_PAIRS / n / density / (4 / 3 * math.pi)) ** (1 / 3))
    i, j, d = neighbor_list("ijd", atoms, rmax)
    edges = np.arange(0, rmax + dr, dr)
    r = (edges[:-1] + edges[1:]) / 2
    shell = 4 * math.pi * r ** 2 * dr
    counts = np.bincount(kind, minlength=len(order))
    total = np.histogram(d, bins=edges)[0] / (n * density * shell)
    partial = {}
    for a, sa in enumerate(order):
        for b, sb in enumerate(order):
            if b < a:
                continue
            mask = (kind[i] == a) & (kind[j] == b)
            # g_AB: A の原子のまわりの B の数密度 ÷ B の平均の数密度
            partial[f"{sa}-{sb}"] = np.histogram(d[mask], bins=edges)[0] / (counts[a] * counts[b] / volume * shell)
    return {"r": r, "total": total, "partial": partial, "rmax": rmax}


def first_minimum(r: np.ndarray, g: np.ndarray) -> float | None:
    """最初の山のあとの谷の位置。谷が平ら (完全な結晶で 0 が続く) なら、その真ん中。"""
    if not np.any(g > 0):
        return None
    kernel_r = np.arange(-3 * SMOOTH, 3 * SMOOTH + 1e-9, r[1] - r[0])
    kernel = np.exp(-0.5 * (kernel_r / SMOOTH) ** 2)
    smooth = np.convolve(g, kernel / kernel.sum(), mode="same")
    start = int(np.argmax(g > 0.05 * g.max()))           # 最初に原子が現れる所
    window = smooth[start:start + int(1.0 / (r[1] - r[0]))]   # そこから 1 Å の中で最初の山
    peak = start + int(np.argmax(window))
    k = peak + 1
    while k < len(smooth) - 1 and smooth[k + 1] <= smooth[k] + 1e-12:
        k += 1
    if k >= len(smooth) - 1:
        return None
    low = smooth[k]
    lo = hi = k
    while lo > peak and smooth[lo - 1] <= low + 0.02 * smooth[peak]:
        lo -= 1
    while hi < len(smooth) - 1 and smooth[hi + 1] <= low + 0.02 * smooth[peak]:
        hi += 1
    return float((r[lo] + r[hi]) / 2)


def cutoffs(atoms: Atoms, given: float | None = None, rmax: float = RMAX) -> tuple[dict[str, float], dict]:
    """元素の組み合わせごとのカットオフ。given があればすべてそれ。戻り値は (カットオフ, g(r))。"""
    g = rdf(atoms, rmax)
    order, _ = _species(atoms)
    if given:
        return {f"{a}-{b}": float(given) for a in order for b in order}, g
    base = first_minimum(g["r"], g["total"])
    if base is None:
        raise ValueError("動径分布関数に山がありません (原子が離れすぎています)")
    result = {}
    for key, values in g["partial"].items():
        a, b = key.split("-")
        rc = first_minimum(g["r"], values) if values.max() > 0 else None
        # 組の数が少なく谷が見つからない・全体の谷から大きく外れるときは、全体の谷を使う
        if rc is None or not 0.6 * base < rc < 1.5 * base:
            rc = base
        result[f"{a}-{b}"] = result[f"{b}-{a}"] = float(rc)
    return result, g


def per_atom(atoms: Atoms, rc: dict[str, float]) -> tuple[np.ndarray, list[str]]:
    """原子ごと・となりの元素ごとの配位数 (N, 元素の数) と、元素の並び。"""
    order, kind = _species(atoms)
    table = np.array([[rc[f"{a}-{b}"] for b in order] for a in order])
    i, j, d = neighbor_list("ijd", atoms, float(table.max()))
    keep = d < table[kind[i], kind[j]]
    cn = np.zeros((len(atoms), len(order)), dtype=int)
    np.add.at(cn, (i[keep], kind[j[keep]]), 1)
    return cn, order


def _dist(values: np.ndarray) -> list[dict]:
    if not len(values):
        return []
    v, c = np.unique(values, return_counts=True)
    return [{"cn": int(x), "count": int(n), "fraction": float(n / len(values))} for x, n in zip(v, c)]


def summarize(atoms: Atoms, cn: np.ndarray, order: list[str]) -> dict:
    """元素の組み合わせごとの平均と分布。"*" は「すべて」(中心ならすべての原子、となりならすべての元素を数える)。

    matrix[中心][となり] = 平均の配位数、distribution[中心][となり] = [{cn, count, fraction}]
    """
    _, kind = _species(atoms)
    centers = {"*": np.ones(len(atoms), bool), **{sa: kind == a for a, sa in enumerate(order)}}
    neighbors = {"*": cn.sum(axis=1), **{sb: cn[:, b] for b, sb in enumerate(order)}}
    matrix, distribution = {}, {}
    for ca, mask in centers.items():
        matrix[ca] = {nb: float(values[mask].mean()) for nb, values in neighbors.items()}
        distribution[ca] = {nb: _dist(values[mask]) for nb, values in neighbors.items()}
    return {"mean": matrix["*"]["*"], "matrix": matrix, "distribution": distribution}


def _series(atoms: Atoms, cn: np.ndarray, order: list[str]) -> dict[str, float]:
    """時間変化に描く平均: 中心の元素ごとの合計と、2 元素以上なら組み合わせごと。"""
    _, kind = _species(atoms)
    total = cn.sum(axis=1)
    out = {"全体": float(total.mean())}
    if len(order) > 1:
        for a, sa in enumerate(order):
            for b, sb in enumerate(order):
                out[f"{sa} のまわりの {sb}"] = float(cn[kind == a, b].mean())
    return out


def analyze(final: Atoms, frames: list, *, cutoff: float | None = None, per_frame: bool = True, progress=None,
            view_stride: int = 1) -> dict:
    """最終構造の配位数・g(r)・分布と、フレームごとの平均の配位数。

    時間変化の数える距離: cutoff を指定したときはそれ。自動のときは per_frame なら各フレームの g(r) の谷から決め直し
    (途中で構造が変わる計算でも、その時点の構造に合った距離で数える)、そうでなければ最終構造で決めたものを使う。

    view_atoms は 3D 表示に送る原子 (view_stride ごと) の、となりの元素ごとの配位数 [[n_A, n_B, ...], ...]。
    """
    rc, g = cutoffs(final, cutoff)
    cn, order = per_atom(final, rc)
    result = {
        "n_atoms": len(final),
        "species": order,
        "cutoffs": {key: rc[key] for key in g["partial"]},   # 組み合わせごとに 1 つ (A-B と B-A は同じ)
        "cutoff_auto": cutoff is None,
        "rdf": {"r": g["r"].round(4).tolist(), "total": g["total"].round(5).tolist(),
                "partial": {k: v.round(5).tolist() for k, v in g["partial"].items()} if len(order) > 1 else {}},
        **summarize(final, cn, order),
        "view_atoms": cn[::view_stride].tolist(),
    }
    if progress:
        progress(1)
    if frames:
        evolution = {"time_ps": [], "frame": [], "mean": {}, "cutoff": {}, "per_frame": bool(per_frame and not cutoff)}
        # 各フレームの g(r) は、谷を探せる所 (最終構造の距離の 1.6 倍) までで十分
        frame_rmax = min(RMAX, 1.6 * max(rc.values()))
        for k, (time_ps, atoms) in enumerate(frames):
            frame_rc = rc
            if evolution["per_frame"]:
                try:
                    frame_rc, _ = cutoffs(atoms, None, frame_rmax)
                except ValueError:
                    frame_rc = rc
            frame_cn, frame_order = per_atom(atoms, frame_rc)
            for key in result["cutoffs"]:
                evolution["cutoff"].setdefault(key, []).append(float(frame_rc.get(key, rc[key])))
            evolution["time_ps"].append(time_ps)
            evolution["frame"].append(k)
            for name, value in _series(atoms, frame_cn, frame_order).items():
                evolution["mean"].setdefault(name, []).append(value)
            if progress:
                progress(k + 2)
        result["evolution"] = evolution
    return result
