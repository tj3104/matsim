"""配位多面体 (ポリヘドラル) の検出・形の判定・つながり方。

中心の元素 (例: Si) の各原子について、配位する元素 (例: O) の原子のうち距離 r_c 以内にあるものを頂点とする
多面体を作る。r_c は、既定では部分動径分布関数 g_AB(r) の第 1 の谷 (coordination.cutoffs と同じ)。

形の判定: 中心から見た頂点どうしのなす角 (n(n−1)/2 個) を小さい順に並べ、理想的な多面体の角の並びと比べる。
角の並びは多面体の向きにも頂点の番号の付け方にもよらないので、回転や対応づけを探さずにまとめて比べられる。
ずれ (角度の二乗平均平方根、度) が最も小さい形にし、それが DISTORTED_DEG より大きければ「n 配位 (不規則)」。

つながり方: 2 つの多面体が共有する頂点の数で、頂点共有 (1)・辺共有 (2)・面共有 (3 以上)。
多面体ごとに、頂点を共有している別の多面体の数を Qⁿ の n とする (SiO4 のケイ酸塩なら、架橋酸素の数に当たる)。
"""
from __future__ import annotations

import itertools
import math

import numpy as np
from ase import Atoms
from ase.neighborlist import neighbor_list

from .coordination import _species, cutoffs

DISTORTED_DEG = 15.0
MAX_DRAW = 1500            # 3D 表示に描く多面体の数の上限
ANIONS = ["O", "F", "S", "N", "Cl", "Se", "Br", "Te", "I", "H"]


def _unit(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def _ring(n, z=0.0, phase=0.0):
    return [(math.cos(2 * math.pi * k / n + phase), math.sin(2 * math.pi * k / n + phase), z) for k in range(n)]


def _references() -> dict[int, list[tuple[str, np.ndarray]]]:
    """配位数ごとの理想的な多面体 (頂点の向きの単位ベクトル)。"""
    t = 1 / math.sqrt(3)
    phi = (1 + math.sqrt(5)) / 2
    octa = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    tetra = [(t, t, t), (t, -t, -t), (-t, t, -t), (-t, -t, t)]
    prism_h = math.sqrt(3) / 2          # 辺の長さがすべて等しい三角柱 (三角形の外接円の半径 1)
    anti_h = 2 ** -0.25                 # 辺の長さがすべて等しい正方逆柱
    cubo = [p for p in itertools.product((-1, 0, 1), repeat=3) if sorted(map(abs, p)) == [0, 1, 1]]
    # hcp (理想的な c/a) の第 1 近接: 同じ面の 6 個と、上下の面の同じ向きの 3 個ずつ (上下が重なる = 反立方八面体)
    up = [(math.cos(a) / math.sqrt(3), math.sin(a) / math.sqrt(3)) for a in (math.pi / 6, 5 * math.pi / 6, 3 * math.pi / 2)]
    anticubo = _ring(6) + [(x, y, z) for z in (math.sqrt(2 / 3), -math.sqrt(2 / 3)) for x, y in up]
    ico = [(0, s1, s2 * phi) for s1 in (-1, 1) for s2 in (-1, 1)]
    ico = ico + [(y, z, x) for x, y, z in ico] + [(z, x, y) for x, y, z in ico]
    refs = {
        2: [("直線", [(0, 0, 1), (0, 0, -1)])],
        3: [("平面三角形", _ring(3)), ("三角錐", tetra[:3])],
        4: [("四面体", tetra), ("平面四角形", _ring(4)), ("シーソー", [(0, 0, 1), (0, 0, -1)] + _ring(3)[:2])],
        5: [("三方両錐", [(0, 0, 1), (0, 0, -1)] + _ring(3)), ("四角錐", octa[:5])],
        6: [("八面体", octa), ("三角柱", _ring(3, prism_h / 2) + _ring(3, -prism_h / 2))],
        7: [("五方両錐", [(0, 0, 1), (0, 0, -1)] + _ring(5)), ("一冠八面体", octa + [(t, t, t)])],
        8: [("立方体", list(itertools.product((-1, 1), repeat=3))),
            ("正方逆柱", _ring(4, anti_h / 2) + _ring(4, -anti_h / 2, math.pi / 4))],
        9: [("三冠三角柱", _ring(3, prism_h / 2) + _ring(3, -prism_h / 2) + _ring(3, 0, math.pi / 3))],
        12: [("立方八面体 (fcc)", cubo), ("反立方八面体 (hcp)", anticubo), ("正二十面体", ico)],
    }
    return {n: [(name, _unit(v)) for name, v in shapes] for n, shapes in refs.items()}


REFERENCES = _references()


def _angle_signature(vectors: np.ndarray) -> np.ndarray:
    """(m, n, 3) の頂点の向き → (m, n(n−1)/2) の小さい順の角度 [度]。"""
    u = _unit(vectors)
    cos = np.einsum("mik,mjk->mij", u, u)
    n = u.shape[1]
    iu = np.triu_indices(n, 1)
    return np.sort(np.degrees(np.arccos(np.clip(cos[:, iu[0], iu[1]], -1, 1))), axis=1)


def classify(vectors: np.ndarray) -> tuple[list[str], np.ndarray]:
    """同じ配位数 n の多面体 (m, n, 3) の形と、理想形からのずれ (度)。"""
    m, n, _ = vectors.shape
    if n < 2 or n not in REFERENCES:
        return [f"{n} 配位"] * m, np.full(m, np.nan)
    sig = _angle_signature(vectors)
    best = np.full(m, np.inf)
    names = np.array([""] * m, dtype=object)
    for name, ref in REFERENCES[n]:
        ref_sig = _angle_signature(ref[None])[0]
        rms = np.sqrt(((sig - ref_sig[None]) ** 2).mean(axis=1))
        better = rms < best
        best[better] = rms[better]
        names[better] = name
    names[best > DISTORTED_DEG] = f"{n} 配位 (不規則)"
    return names.tolist(), best


def default_pair(order: list[str]) -> tuple[str, str]:
    """中心と配位の元素の既定: 陰イオン (O など) があればそれを配位に、ほかの元素を中心に。なければ全元素を配位に。"""
    anions = [a for a in ANIONS if a in order]
    if anions and len(order) > 1:
        ligand = anions[0]
        return next(s for s in order if s != ligand), ligand
    return order[0], "*"


def _faces(points: np.ndarray, center: np.ndarray) -> list:
    """多面体の面 (三角形の頂点の座標) のリスト。平ら (平面三角形・平面四角形) なら扇形に分ける。"""
    from scipy.spatial import ConvexHull, QhullError
    n = len(points)
    if n < 3:
        return []
    if n >= 4:
        try:
            hull = ConvexHull(points)
            return [points[s].round(3).tolist() for s in hull.simplices]
        except (QhullError, ValueError):
            pass
    # 平ら: 重心のまわりの角度の順に並べて扇形
    c = points.mean(axis=0)
    normal = np.cross(points[1] - points[0], points[2] - points[0])
    if np.linalg.norm(normal) < 1e-9:
        return []
    normal /= np.linalg.norm(normal)
    e1 = _unit(points[0] - c)
    e2 = np.cross(normal, e1)
    order = np.argsort([math.atan2(np.dot(p - c, e2), np.dot(p - c, e1)) for p in points])
    ring = points[order]
    return [[c.round(3).tolist(), ring[k].round(3).tolist(), ring[(k + 1) % n].round(3).tolist()] for k in range(n)]


def detect(atoms: Atoms, center: str, ligand: str, rc: float) -> dict:
    """中心の元素の各原子のまわりの多面体: 頂点 (配位原子の番号)・頂点の向き・形・ずれ・長さのゆがみ。"""
    symbols = np.array(atoms.get_chemical_symbols())
    centers = np.flatnonzero(symbols == center)
    if not len(centers):
        raise ValueError(f"構造に {center} がありません")
    if ligand != "*" and ligand not in symbols:
        raise ValueError(f"構造に {ligand} がありません")
    i, j, d, vec = neighbor_list("ijdD", atoms, rc)
    # 小さなセルでは、となりが周期境界の向こうの自分自身の像のこともあるので、番号ではなく距離 0 (自分) だけを除く
    keep = np.isin(i, centers) & (d > 1e-6)
    if ligand != "*":
        keep &= symbols[j] == ligand
    i, j, d, vec = i[keep], j[keep], d[keep], vec[keep]
    order = np.argsort(i, kind="stable")
    i, j, d, vec = i[order], j[order], d[order], vec[order]
    starts = np.searchsorted(i, centers)
    ends = np.searchsorted(i, centers, side="right")
    cn = ends - starts
    shape = np.array([""] * len(centers), dtype=object)
    rms = np.full(len(centers), np.nan)
    for n in np.unique(cn):
        sel = np.flatnonzero(cn == n)
        if n == 0:
            shape[sel] = "配位なし"
            continue
        vectors = np.stack([vec[starts[k]:ends[k]] for k in sel])
        names, dev = classify(vectors)
        shape[sel] = names
        rms[sel] = dev
    lengths = [d[starts[k]:ends[k]] for k in range(len(centers))]
    distortion = np.array([float(np.mean(np.abs(x - x.mean())) / x.mean()) if len(x) else np.nan for x in lengths])
    return {"centers": centers, "cn": cn, "shape": shape, "rms": rms, "distortion": distortion,
            "ligands": [j[starts[k]:ends[k]] for k in range(len(centers))],
            "vectors": [vec[starts[k]:ends[k]] for k in range(len(centers))]}


def connectivity(poly: dict) -> dict:
    """多面体どうしの共有 (頂点・辺・面) と、多面体ごとのつながっている数 (Qⁿ)。"""
    owners: dict[int, list[int]] = {}
    for k, ligands in enumerate(poly["ligands"]):
        for lig in ligands:
            owners.setdefault(int(lig), []).append(k)
    shared: dict[tuple[int, int], int] = {}
    for ks in owners.values():
        for a, b in itertools.combinations(sorted(set(ks)), 2):
            shared[(a, b)] = shared.get((a, b), 0) + 1
    modes = {"頂点共有": 0, "辺共有": 0, "面共有": 0}
    partners = np.zeros(len(poly["ligands"]), dtype=int)
    for (a, b), count in shared.items():
        modes["頂点共有" if count == 1 else "辺共有" if count == 2 else "面共有"] += 1
        partners[a] += 1
        partners[b] += 1
    return {"modes": modes, "partners": partners}


def _fractions(values) -> list[dict]:
    names, counts = np.unique(np.asarray(values, dtype=object).astype(str), return_counts=True)
    total = counts.sum()
    rows = [{"name": str(n), "count": int(c), "fraction": float(c / total)} for n, c in zip(names, counts)]
    return sorted(rows, key=lambda r: -r["count"])


def analyze(final: Atoms, frames: list, *, center: str | None = None, ligand: str | None = None,
            cutoff: float | None = None, progress=None) -> dict:
    order, _ = _species(final)
    auto_center, auto_ligand = default_pair(order)
    center = center or auto_center
    ligand = ligand or auto_ligand
    if center not in order:
        raise ValueError(f"構造に {center} がありません ({'・'.join(order)})")
    if cutoff:
        rc = float(cutoff)
        auto = False
    else:
        table, _ = cutoffs(final)
        rc = max(table[f"{center}-{s}"] for s in order) if ligand == "*" else table[f"{center}-{ligand}"]
        auto = True
    poly = detect(final, center, ligand, rc)
    # 共有のしかたは、配位する元素が中心と別の 1 元素のとき (SiO4 の O など) だけ意味がある
    conn = (connectivity(poly) if ligand not in ("*", center)
            else {"modes": {}, "partners": np.zeros(len(poly["cn"]), int)})
    types = _fractions(poly["shape"])
    for row in types:
        mask = poly["shape"] == row["name"]
        row["cn"] = int(poly["cn"][mask][0])
        row["rms"] = None if np.all(np.isnan(poly["rms"][mask])) else float(np.nanmean(poly["rms"][mask]))
        row["distortion"] = float(np.nanmean(poly["distortion"][mask]))
    # 3D 表示に描く多面体 (多いときは等間隔に選ぶ)
    pick = np.linspace(0, len(poly["centers"]) - 1, min(MAX_DRAW, len(poly["centers"]))).round().astype(int)
    pick = np.unique(pick)
    positions = final.get_positions()
    draw = []
    for k in pick:
        c = positions[poly["centers"][k]]
        pts = c + poly["vectors"][k]
        draw.append({"center": int(poly["centers"][k]), "shape": str(poly["shape"][k]), "faces": _faces(pts, c)})
    hist_edges = np.arange(0, 31, 2.5)
    rms_valid = poly["rms"][np.isfinite(poly["rms"])]
    result = {
        "n_atoms": len(final), "species": order, "center": center, "ligand": ligand, "cutoff": rc, "cutoff_auto": auto,
        "n_polyhedra": int(len(poly["centers"])), "mean_cn": float(poly["cn"].mean()),
        "types": types,
        "cn_distribution": [{"cn": int(v), "count": int(c), "fraction": float(c / len(poly["cn"]))}
                            for v, c in zip(*np.unique(poly["cn"], return_counts=True))],
        "sharing": conn["modes"],
        "qn": [{"n": int(v), "count": int(c), "fraction": float(c / len(poly["cn"]))}
               for v, c in zip(*np.unique(conn["partners"], return_counts=True))] if conn["modes"] else [],
        "rms_hist": {"edges": hist_edges.tolist(), "counts": np.histogram(np.clip(rms_valid, 0, 29.99), bins=hist_edges)[0].tolist()},
        "mean_rms": float(rms_valid.mean()) if len(rms_valid) else None,
        "mean_distortion": float(np.nanmean(poly["distortion"])),
        "polyhedra": draw,
        "drawn": len(draw),
        "center_shape": {int(c): str(s) for c, s in zip(poly["centers"], poly["shape"])},
    }
    if progress:
        progress(1)
    if frames:
        evolution = {"time_ps": [], "frame": [], "fractions": {}, "mean_cn": []}
        names = [t["name"] for t in types]
        for k, (time_ps, atoms) in enumerate(frames):
            fp = detect(atoms, center, ligand, rc)
            fr = {row["name"]: row["fraction"] for row in _fractions(fp["shape"])}
            evolution["time_ps"].append(time_ps)
            evolution["frame"].append(k)
            evolution["mean_cn"].append(float(fp["cn"].mean()))
            for name in set(names) | set(fr):
                evolution["fractions"].setdefault(name, [0.0] * k).append(fr.get(name, 0.0))
            for name in evolution["fractions"]:
                if len(evolution["fractions"][name]) < k + 1:
                    evolution["fractions"][name].append(0.0)
            if progress:
                progress(k + 2)
        # 少ない形 (いつも 2% 未満) は線を描かない
        evolution["fractions"] = {n: v for n, v in evolution["fractions"].items() if max(v) >= 0.02}
        result["evolution"] = evolution
    return result
