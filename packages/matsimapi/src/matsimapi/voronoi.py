"""ボロノイ多面体の指数による局所構造の解析。

各原子のボロノイ多面体の面を辺の数で数え、<n3,n4,n5,n6> (3〜6 角形の面の数) で分類する。
例: <0,0,12,0> は正二十面体 (金属ガラス・液体に多い)、<0,6,0,8> は bcc、<0,12,0,0> は fcc/hcp。

周期境界は、セルの外側に薄く原子を写して (像を置いて) からボロノイ分割することで扱う。
熱ゆらぎがあると多面体の頂点が分裂して、ごく小さな面やごく短い辺ができ、分類がばらつく。そこで
  ・多面体の全表面積に対する割合が area_threshold 未満の面は数えない (よく使われる値は 1%)
  ・平均原子間隔 (V/N)^(1/3) の edge_fraction 倍より短い辺は数えない
とする。既定 (1%・0.18 倍) で、300 K 程度に揺らした fcc の 9 割・bcc のすべてが正しい指数になる。
"""
from __future__ import annotations

import itertools
import math
from collections import Counter

import numpy as np
from ase import Atoms
from scipy.spatial import Voronoi

MAX_EDGES = 8          # 数える面の辺の数の上限 (これより多い面は MAX_EDGES に入れる)
SEP = ","


def _periodic_points(atoms: Atoms, pad: float) -> tuple[np.ndarray, np.ndarray]:
    """セルの中の原子と、セルの外側 pad [Å] 以内に写した像。戻り値は (座標, 元の原子の番号)。"""
    cell = np.array(atoms.cell)
    scaled = atoms.get_scaled_positions(wrap=True)
    volume = abs(np.linalg.det(cell))
    thickness = np.array([volume / np.linalg.norm(np.cross(cell[(i + 1) % 3], cell[(i + 2) % 3])) for i in range(3)])
    margin = np.minimum(pad / thickness, 0.5)
    points = [scaled]
    owners = [np.arange(len(atoms))]
    for shift in itertools.product((-1, 0, 1), repeat=3):
        if shift == (0, 0, 0):
            continue
        moved = scaled + np.array(shift)
        keep = np.all((moved > -margin) & (moved < 1 + margin), axis=1)
        if keep.any():
            points.append(moved[keep])
            owners.append(np.flatnonzero(keep))
    return np.vstack(points) @ cell, np.concatenate(owners)


def _face(vertices: np.ndarray, normal: np.ndarray, edge_threshold: float) -> tuple[float, int]:
    """面 (凸多角形) の面積と、edge_threshold より長い辺の数。頂点は面の中で角度順に並べ直す。"""
    center = vertices.mean(axis=0)
    rel = vertices - center
    n = normal / np.linalg.norm(normal)
    u = rel[0] - np.dot(rel[0], n) * n
    if np.linalg.norm(u) < 1e-12:
        u = np.cross(n, [1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.cross(n, [0.0, 1.0, 0.0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    order = np.argsort(np.arctan2(rel @ v, rel @ u))
    ring = vertices[order]
    nxt = np.roll(ring, -1, axis=0)
    area = 0.5 * float(np.linalg.norm(np.cross(ring - center, nxt - center).sum(axis=0)))
    edges = int(np.sum(np.linalg.norm(nxt - ring, axis=1) > edge_threshold))
    return area, edges


def voronoi_counts(atoms: Atoms, *, area_threshold: float = 0.01, edge_fraction: float = 0.18) -> np.ndarray:
    """原子ごとの面の数 (N, MAX_EDGES - 2)。列 k は (k + 3) 角形の面の数。"""
    n_atoms = len(atoms)
    if n_atoms == 0:
        raise ValueError("原子がありません")
    spacing = (atoms.get_volume() / n_atoms) ** (1 / 3)
    edge_threshold = edge_fraction * spacing
    pad = max(6.0, 3.0 * spacing)
    # 単位胞のように小さなセルは、像が 1 周ぶんでは足りないので、先に繰り返してから分割する
    # (ase の repeat は元の原子を先頭に並べるので、先頭 n_atoms 個の結果が元の原子のもの)
    cell = np.array(atoms.cell)
    volume = abs(np.linalg.det(cell))
    thickness = np.array([volume / np.linalg.norm(np.cross(cell[(i + 1) % 3], cell[(i + 2) % 3])) for i in range(3)])
    reps = [max(1, math.ceil(2 * pad / t)) for t in thickness]
    if reps != [1, 1, 1]:
        work = atoms.repeat(reps)
        return voronoi_counts(work, area_threshold=area_threshold, edge_fraction=edge_fraction)[:n_atoms]
    points, owners = _periodic_points(atoms, pad=pad)
    vor = Voronoi(points)
    faces: list[list[tuple[float, int]]] = [[] for _ in range(n_atoms)]
    for (p, q), ridge in zip(vor.ridge_points, vor.ridge_vertices):
        if p >= n_atoms and q >= n_atoms:
            continue
        if -1 in ridge or len(ridge) < 3:
            continue
        area, edges = _face(vor.vertices[ridge], points[q] - points[p], edge_threshold)
        for atom in (p, q):
            if atom < n_atoms:
                faces[atom].append((area, edges))
    counts = np.zeros((n_atoms, MAX_EDGES - 2), dtype=int)
    for i, items in enumerate(faces):
        if not items:
            continue
        total = sum(a for a, _ in items)
        for area, edges in items:
            if area >= area_threshold * total and edges >= 3:
                counts[i, min(edges, MAX_EDGES) - 3] += 1
    return counts


def label(row: np.ndarray) -> str:
    """面の数の行を <n3,n4,n5,n6> の文字列にする。7 角形以上の面があれば後ろに足す。"""
    head = row[:4].tolist()
    tail = row[4:].tolist()
    while tail and tail[-1] == 0:
        tail.pop()
    return "<" + SEP.join(str(v) for v in head + tail) + ">"


def classify(atoms: Atoms, *, area_threshold: float = 0.01, edge_fraction: float = 0.18) -> list[str]:
    """原子ごとのボロノイ指数の文字列。"""
    return [label(row) for row in voronoi_counts(atoms, area_threshold=area_threshold, edge_fraction=edge_fraction)]


KNOWN = {
    "<0,0,12,0>": "正二十面体", "<0,2,8,2>": "歪んだ二十面体", "<0,1,10,2>": "歪んだ二十面体", "<0,2,8,1>": "",
    "<0,12,0,0>": "fcc / hcp (立方八面体など)", "<0,6,0,8>": "bcc", "<0,6,0,0>": "単純立方・岩塩型 (立方体)", "<0,3,6,3>": "", "<0,3,6,4>": "",
    "<0,2,8,4>": "", "<0,0,12,2>": "Z14 (Frank–Kasper)", "<0,0,12,3>": "Z15 (Frank–Kasper)",
    "<0,0,12,4>": "Z16 (Frank–Kasper)", "<0,4,4,6>": "",
}


def describe(index: str) -> str:
    return KNOWN.get(index, "")


def counts_of(index: str) -> list[int]:
    return [int(v) for v in index.strip("<>").split(SEP)]


# 指数のまとまり。表示の順番もこの順 (金属ガラスの解析でよく使う分け方)
FAMILIES = ["正二十面体", "二十面体に近い", "Frank–Kasper型", "fcc・hcp", "bcc", "結晶に近い", "乱れた多面体"]


def family(index: str) -> str:
    """指数をまとまりに分ける。

    正二十面体 <0,0,12,0> / Frank–Kasper型 <0,0,12,n6≥1> / fcc・hcp <0,12,0,0> / bcc <0,6,0,8> は指数そのもの。
    それ以外は面の形の割合で分ける: 五角形が 8 枚以上で 6 割以上なら「二十面体に近い」、
    四角形が五角形より多ければ「結晶に近い」、どちらでもなければ「乱れた多面体」。
    """
    c = counts_of(index) + [0] * 4
    n3, n4, n5, n6 = c[:4]
    cn = sum(c)
    if index == "<0,0,12,0>":
        return "正二十面体"
    if n3 == 0 and n4 == 0 and n5 == 12 and n6 >= 1:
        return "Frank–Kasper型"
    if index == "<0,12,0,0>":
        return "fcc・hcp"
    if index == "<0,6,0,8>":
        return "bcc"
    if n5 >= 8 and cn and n5 / cn >= 0.6:
        return "二十面体に近い"
    if n4 > n5:
        return "結晶に近い"
    return "乱れた多面体"


SHAPES = ["三角形", "四角形", "五角形", "六角形", "七角形", "八角形"]


def explain(index: str) -> str:
    """指数を言葉にする。例: 配位数 12: 三角形 1・四角形 5・五角形 5・六角形 1"""
    c = counts_of(index)
    faces = "・".join(f"{SHAPES[k]} {n}" for k, n in enumerate(c) if n and k < len(SHAPES))
    return f"配位数 {sum(c)}: {faces or '面なし'}"


def enrich(result: dict) -> dict:
    """まとまり・説明を足す (前の版で保存した結果にも、読み出すときに足す)。"""
    if "families" in result:
        return result
    total = result["n_center"] or 1
    fam_counts: dict[str, int] = {}
    for item in result["ranking"]:
        item["family"] = family(item["index"])
        item["detail"] = explain(item["index"])
        fam_counts[item["family"]] = fam_counts.get(item["family"], 0) + item["count"]
    result["families"] = [{"family": f, "count": fam_counts[f], "fraction": fam_counts[f] / total}
                          for f in FAMILIES if f in fam_counts]
    result["family_of"] = {item["index"]: item["family"] for item in result["ranking"]}
    return result


def fractions(labels: list[str], mask: np.ndarray | None = None) -> Counter:
    chosen = labels if mask is None else [lab for lab, keep in zip(labels, mask) if keep]
    return Counter(chosen)


def coordination(labels: list[str]) -> list[int]:
    """ボロノイ指数から配位数 (数えた面の総数)。"""
    return [sum(int(v) for v in lab.strip("<>").split(SEP)) for lab in labels]


def budget_frames(n_atoms: int, wanted: int, limit: int = 400_000) -> int:
    """解析するフレーム数を、原子数 × フレーム数 が limit 以内になるように抑える。"""
    return max(0, min(wanted, math.floor(limit / max(n_atoms, 1))))


# ---- ジョブ・構造の解析 (API から呼ぶ) ----------------------------------------------------

def trajectory_frames(path, n_atoms: int, wanted: int, *, last: bool = False) -> list[tuple[float | None, Atoms]]:
    """トラジェクトリ (extxyz) から等間隔に wanted 枚 (last=True なら最後の wanted 枚) を読む。戻り値は (時刻 [ps], 構造)。"""
    from ase.io import read
    from .results import _count_lines
    if wanted <= 0 or not path.is_file() or path.stat().st_size == 0:
        return []
    total = _count_lines(path) // (n_atoms + 2)
    if total == 0:
        return []
    if last:
        picks = list(range(max(0, total - wanted), total))
    else:
        picks = sorted({round(i * (total - 1) / max(wanted - 1, 1)) for i in range(min(wanted, total))})
    frames = []
    for index in picks:
        atoms = read(path, index=index, format="extxyz")
        if len(atoms) != n_atoms:
            continue
        time_fs = atoms.info.get("Time", atoms.info.get("time"))
        frames.append((float(time_fs) / 1000 if time_fs is not None else None, atoms))
    return frames


def analyze(final: Atoms, frames: list[tuple[float | None, Atoms]], *, center: str = "*", area_threshold: float = 0.01,
            edge_fraction: float = 0.18, top: int = 10, progress=None) -> dict:
    """最終構造の分類 (比率・原子ごとの指数) と、フレームごとの比率の推移。

    center を元素記号にすると、その元素を中心とする多面体だけを数える (色塗りは全原子分を返す)。
    progress(解析した枚数) を渡すと、1 枚 (最終構造・各フレーム) ごとに呼ぶ (中止するときは例外を投げる)。
    """
    labels = classify(final, area_threshold=area_threshold, edge_fraction=edge_fraction)
    if progress:
        progress(1)
    symbols = np.array(final.get_chemical_symbols())
    mask = np.ones(len(final), dtype=bool) if center == "*" else symbols == center
    if not mask.any():
        raise ValueError(f"構造に {center} がありません")
    counts = fractions(labels, mask)
    total = int(mask.sum())
    ranking = [{"index": index, "count": n, "fraction": n / total, "name": describe(index)}
               for index, n in counts.most_common()]
    cn = Counter(c for c, keep in zip(coordination(labels), mask) if keep)
    shown = [r["index"] for r in ranking[:top]]
    if frames:
        # 最初のフレームで多かった種類 (融ける前の結晶など) も、最終構造で上位でなくても追う
        first = fractions(classify(frames[0][1], area_threshold=area_threshold, edge_fraction=edge_fraction),
                          mask if len(frames[0][1]) == len(final) else None)
        shown += [index for index, _ in first.most_common(3) if index not in shown]
    evolution = {"time_ps": [], "frame": [], "fractions": {index: [] for index in shown}, "other": [],
                 "families": {f: [] for f in FAMILIES}}
    for i, (time_ps, atoms) in enumerate(frames):
        frame_labels = classify(atoms, area_threshold=area_threshold, edge_fraction=edge_fraction)
        frame_mask = mask if len(atoms) == len(final) else np.ones(len(atoms), dtype=bool)
        frame_counts = fractions(frame_labels, frame_mask)
        n = int(frame_mask.sum())
        evolution["time_ps"].append(time_ps)
        evolution["frame"].append(i)
        for index in shown:
            evolution["fractions"][index].append(frame_counts.get(index, 0) / n)
        evolution["other"].append(1 - sum(frame_counts.get(index, 0) for index in shown) / n)
        fam = Counter()
        for index, k in frame_counts.items():
            fam[family(index)] += k
        for f in FAMILIES:
            evolution["families"][f].append(fam.get(f, 0) / n)
        if progress:
            progress(i + 2)
    if frames:   # 一度も現れなかったまとまりは時間変化に出さない
        evolution["families"] = {f: v for f, v in evolution["families"].items() if any(v)}
    return enrich({"n_atoms": len(final), "n_center": total, "center": center, "labels": labels, "ranking": ranking,
            "coordination": [{"cn": k, "count": v, "fraction": v / total} for k, v in sorted(cn.items())],
            "evolution": evolution if frames else None,
            "settings": {"area_threshold": area_threshold, "edge_fraction": edge_fraction}})
