"""ナノ粒子・ナノワイヤーと、グラニュラー構造 (円柱粒の柱状組織)。

    from atomsk_kit import granular, nanoparticle
    ball = nanoparticle(cu, radius=12, vacuum=10)
    film = granular(cu, box=(150, 150, 20), grains=10, radius=18, filler="random", seed=1)
"""
from __future__ import annotations

import math
from typing import Callable

import numpy as np
from ase import Atoms
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

from .edit import as_rng
from .geometry import (composition_of, is_orthorhombic, nearest_distance, number_density, parse_composition,
                       species_mix)
from .pipeline import Pipeline, box


def nanoparticle(crystal: Atoms, radius: float, *, vacuum: float = 10.0, shape: str = "sphere", nz: int = 6,
                 max_atoms: int | None = None, log: list[str] | None = None) -> Atoms:
    """結晶から球 (shape="sphere") または z 方向に周期的な円柱 (shape="wire", 長さは z を nz 回繰り返す) を切り出し、
    まわりに vacuum [Å] の真空を付ける。"""
    if shape not in ("sphere", "wire"):
        raise ValueError("shape は sphere / wire です")
    thick = np.array([np.linalg.norm(v) for v in crystal.cell])
    reps = [max(1, math.ceil((2 * radius + 2) / t)) for t in thick]
    if shape == "wire":
        reps[2] = int(nz)
    est = len(crystal) * reps[0] * reps[1] * reps[2]
    if max_atoms is not None and est > max_atoms:
        raise ValueError(f"途中の原子数が多すぎます ({est:,})。半径を小さくしてください")
    p = Pipeline(crystal).duplicate(*reps)
    if shape == "sphere":
        p.select_sphere(False, box(0.5), box(0.5), box(0.5), radius)
    else:
        p.select_cylinder(False, "z", box(0.5), box(0.5), radius)
    atoms = p.remove_selected().run(log=log)
    size = 2 * radius + 2 * vacuum
    length_z = size if shape == "sphere" else float(np.linalg.norm(atoms.cell[2]))
    center = atoms.positions.mean(axis=0)
    atoms.positions += np.array([size, size, length_z]) / 2 - center
    if shape == "wire":   # ワイヤーは z 方向に周期的なので z はずらさない
        atoms.positions[:, 2] += center[2] - length_z / 2
    atoms.set_cell(np.diag([size, size, length_z]))
    atoms.pbc = True
    return atoms


# ---- グラニュラー構造 ---------------------------------------------------------------------

def place_disks(rng: np.random.Generator, radii: np.ndarray, lx: float, ly: float, gap: float) -> np.ndarray:
    """周期的な長方形の中に、互いに gap 以上離して円を置く。中心 (N, 2) を返す。

    まず大きい順に乱択で置き (面積率 50% 前後まで)、置けなければ重なりを押し広げて解消する
    (円どうしを少しずつ離す反復。面積率 80% 程度まで詰められる)。
    """
    size = np.array([lx, ly])
    need = radii + gap / 2
    fraction = float(np.sum(np.pi * need ** 2)) / (lx * ly)
    if fraction > 0.85:
        raise ValueError(f"粒 (すき間込み) の面積が箱の {fraction * 100:.0f}% になり、詰めきれません。"
                         "数・半径・すき間を小さくするか、箱を大きくしてください")
    centers = np.zeros((len(radii), 2))
    placed: list[int] = []
    for i in np.argsort(-radii):
        for _ in range(3000):
            p = rng.random(2) * size
            if placed:
                delta = centers[placed] - p
                delta -= np.round(delta / size) * size
                if np.any(np.linalg.norm(delta, axis=1) < need[placed] + need[i]):
                    continue
            centers[i] = p
            placed.append(i)
            break
        else:
            break
    if len(placed) == len(radii):
        return centers
    rest = [i for i in range(len(radii)) if i not in placed]
    centers[rest] = rng.random((len(rest), 2)) * size
    for _ in range(5000):
        delta = centers[:, None, :] - centers[None, :, :]
        delta -= np.round(delta / size) * size
        dist = np.linalg.norm(delta, axis=2)
        overlap = need[:, None] + need[None, :] - dist
        np.fill_diagonal(overlap, 0)
        overlap = np.clip(overlap, 0, None)
        if overlap.max() < 1e-3:
            return np.mod(centers, size)
        with np.errstate(invalid="ignore", divide="ignore"):
            unit = np.where(dist[..., None] > 1e-9, delta / dist[..., None], rng.normal(size=delta.shape))
        centers = np.mod(centers + 0.5 * np.sum(unit * overlap[..., None], axis=1), size)
    raise ValueError("粒を重ならないように置けませんでした。数・半径・すき間を小さくするか、箱を大きくしてください")


def hex_disks(r: float, lx: float, ly: float, gap: float) -> tuple[np.ndarray, float, float]:
    """六方 (三角格子) 配置。周期的になるように箱の大きさを合わせ、(中心, lx, ly) を返す。"""
    pitch = 2 * r + gap
    nx = max(1, int(lx // pitch))
    rows = max(2, int(ly // (pitch * math.sqrt(3) / 2)) // 2 * 2)
    lx, ly = nx * pitch, rows * pitch * math.sqrt(3) / 2
    centers = [((i + 0.5 * (j % 2) + 0.25) * pitch, (j + 0.5) * pitch * math.sqrt(3) / 2)
               for j in range(rows) for i in range(nx)]
    return np.array(centers), lx, ly


def periodic_disk_distance(points: np.ndarray, centers: np.ndarray, size: np.ndarray) -> np.ndarray:
    """各点 (M,2) から各円の中心 (G,2) までの周期的な距離 (M,G)。"""
    delta = points[:, None, :] - centers[None, :, :]
    delta -= np.round(delta / size) * size
    return np.linalg.norm(delta, axis=2)


def fill_random(rng: np.random.Generator, existing: np.ndarray, lengths: np.ndarray, target: int, dmin: float,
                allowed: Callable[[np.ndarray], np.ndarray]) -> np.ndarray:
    """直方体の周期的な箱に、既存の原子・互いから dmin 以上離して点を target 個まで置く (乱択逐次付加)。

    allowed(点の配列) は置いてよい点なら True を返す関数。詰まって置けなくなったら、そこで止めて返す。
    """
    accepted = np.empty((0, 3))
    tree = cKDTree(existing % lengths, boxsize=lengths) if len(existing) else None
    stale = 0
    batch = max(2000, min(200000, target // 4 + 1000))
    for _ in range(400):
        if len(accepted) >= target or stale >= 6:
            break
        cand = rng.random((batch, 3)) * lengths
        cand = cand[allowed(cand)]
        if len(cand) and tree is not None:
            d, _ = tree.query(cand, k=1, distance_upper_bound=dmin)
            cand = cand[~np.isfinite(d)]
        if len(cand):
            alive = np.ones(len(cand), dtype=bool)
            for i, j in cKDTree(cand, boxsize=lengths).query_pairs(dmin):
                if alive[i] and alive[j]:
                    alive[j] = False
            cand = cand[alive][: target - len(accepted)]
        if not len(cand):
            stale += 1
            continue
        stale = 0
        accepted = np.vstack([accepted, cand])
        pool = np.vstack([existing, accepted]) if len(existing) else accepted
        tree = cKDTree(pool % lengths, boxsize=lengths)
    return accepted


def granular(crystal: Atoms, box_size: tuple[float, float, float], *, grains: int = 10, radius: float = 18.0,
             spread: float = 0.2, gap: float = 4.0, layout: str = "random", orientation: str = "columnar",
             filler: str = "random", fill_composition: str | dict | None = None, fill_density: float = 0.9,
             matrix: Atoms | None = None, clearance: float = 1.5, dmin: float | None = None, seed=None,
             max_atoms: int | None = None, log: list[str] | None = None) -> Atoms:
    """円柱の結晶粒を並べ、すき間を埋めたグラニュラー構造 (z 方向に延びる柱状組織) を作る。

    box_size     (lx, ly, lz) [Å]。orientation="columnar" なら lz は結晶の周期の整数倍に丸める
    grains       粒の数 (layout="random" のとき)。layout="hex" なら六方配置で、箱に入るだけ並べる
    radius       粒の平均半径 [Å]。spread は半径のばらつき (±割合、0〜1)
    gap          粒どうしの最小のすき間 [Å]
    orientation  "columnar" (z 軸まわりにランダム。z 方向は周期的) / "random3d" (3 次元でランダム)
    filler       "random" (すき間に原子をランダムに詰める) / "crystal" (matrix の結晶で埋める) / "none"
    fill_composition  詰める原子の組成 (None なら粒と同じ)。fill_density は結晶の数密度に対する割合
    clearance    粒の表面からあける距離 [Å]。dmin は原子どうしの最小距離 (None なら最近接距離の 0.75 倍)

    粒は、結晶を大きく並べて重心のまわりに回し、Atomsk の -select cylinder で円柱に切り出す。
    """
    rng = as_rng(seed)
    if not is_orthorhombic(crystal):
        crystal = Pipeline(crystal).orthogonal_cell().run(log=log)
        if log is not None:
            log.append("元の結晶を直交セルに直してから使いました")
    a_len, b_len, c_len = crystal.cell.lengths()
    columnar = orientation == "columnar"
    lx, ly, lz_in = map(float, box_size)
    nz = max(1, int(round(lz_in / c_len)))
    lz = nz * c_len if columnar else lz_in

    if layout == "hex":
        centers, lx, ly = hex_disks(radius, lx, ly, gap)
        radii = np.full(len(centers), float(radius))
        if log is not None:
            log.append(f"六方配置に合わせて箱を {lx:.1f} × {ly:.1f} Å にしました ({len(centers)} 粒)")
    elif layout == "random":
        radii = radius * (1 + spread * rng.uniform(-1, 1, grains))
        centers = place_disks(rng, radii, lx, ly, gap)
    else:
        raise ValueError("layout は random / hex です")
    size2 = np.array([lx, ly])
    lengths = np.array([lx, ly, lz])
    n0 = number_density(crystal)
    if max_atoms is not None and n0 * lx * ly * lz > max_atoms:
        raise ValueError(f"原子数が多すぎます (約 {n0 * lx * ly * lz:,.0f})。箱を小さくしてください")

    parts = []
    margin = 2 * max(a_len, b_len, c_len)
    for center, r in zip(centers, radii):
        if columnar:
            half = r + margin
            reps = [math.ceil(2 * half / a_len), math.ceil(2 * half / b_len), nz]
        else:
            half = math.sqrt(r ** 2 + (lz / 2) ** 2) + margin
            reps = [math.ceil(2 * half / a_len), math.ceil(2 * half / b_len), math.ceil(2 * half / c_len)]
        block = crystal.repeat(reps)
        com = block.get_center_of_mass()
        p = Pipeline(block)
        if columnar:
            p.rotate("z", f"{rng.uniform(-180, 180):.4f}")
        else:
            for axis, angle in zip("xyz", Rotation.random(random_state=rng).as_euler("xyz", degrees=True)):
                p.rotate(axis, f"{angle:.4f}")
        grain = p.select_cylinder(False, "z", f"{com[0]:.5f}", f"{com[1]:.5f}", r).remove_selected().run(log=log)
        pos = grain.positions.copy()
        if columnar:
            pos[:, 2] = np.mod(pos[:, 2], lz)
        else:
            keep = np.abs(pos[:, 2] - com[2]) < lz / 2
            pos, grain = pos[keep], grain[keep]
            pos[:, 2] -= com[2] - lz / 2
        pos[:, :2] += center - com[:2]
        parts.append(Atoms(grain.get_chemical_symbols(), positions=np.mod(pos, lengths)))
        if max_atoms is not None and sum(len(x) for x in parts) > max_atoms:
            raise ValueError("原子数が多すぎます。箱を小さくしてください")
    atoms = parts[0]
    for part in parts[1:]:
        atoms += part
    atoms.set_cell(np.diag(lengths))
    atoms.pbc = True

    nn = nearest_distance(crystal)
    dmin = dmin if dmin is not None else round(0.75 * nn, 3)
    pairs = cKDTree(atoms.positions % lengths, boxsize=lengths).query_pairs(0.6 * nn, output_type="ndarray")
    if len(pairs):
        drop = np.unique(pairs[:, 1])
        del atoms[drop.tolist()]
        if log is not None:
            log.append(f"粒の境目で近すぎる原子を {len(drop)} 個取り除きました")
    if log is not None:
        area = float(np.sum(np.pi * radii ** 2))
        log.append(f"粒 {len(radii)} 個 (面積率 {area / (lx * ly) * 100:.1f}%)・粒の原子 {len(atoms):,} 個")

    def outside(pts: np.ndarray) -> np.ndarray:
        return np.all(periodic_disk_distance(pts[:, :2], centers, size2) >= radii + clearance, axis=1)

    if filler == "random":
        comp = composition_of(crystal) if fill_composition is None else parse_composition(fill_composition)
        free_area = lx * ly - float(np.sum(np.pi * (radii + clearance) ** 2))
        target = int(round(fill_density * n0 * max(free_area, 0) * lz))
        if target > 0:
            pts = fill_random(rng, atoms.positions, lengths, target, dmin, outside)
            atoms += Atoms(species_mix(len(pts), comp, rng), positions=pts)
            if log is not None:
                if len(pts) < target * 0.98:
                    log.append(f"すき間に {len(pts):,} 個しか置けませんでした (目標 {target:,} 個)。"
                               "最小距離を小さくするか、充填率を下げてください")
                log.append(f"すき間に {len(pts):,} 個の原子をランダムに詰めました (最小距離 {dmin} Å)。"
                           "MD でアニールして緩和してから使ってください")
    elif filler == "crystal":
        if matrix is None:
            raise ValueError("すき間を埋める結晶 (matrix) を渡してください")
        if not is_orthorhombic(matrix):
            matrix = Pipeline(matrix).orthogonal_cell().run(log=log)
        big = matrix.repeat([math.ceil(L / l) for L, l in zip(lengths, matrix.cell.lengths())])
        keep = np.all(big.positions < lengths, axis=1) & outside(big.positions)
        if keep.any():
            fill = big[np.flatnonzero(keep)]
            d, _ = cKDTree(atoms.positions % lengths, boxsize=lengths).query(fill.positions, k=1,
                                                                             distance_upper_bound=dmin)
            fill = fill[np.flatnonzero(~np.isfinite(d))]
            # 箱の大きさが結晶の周期と合わないと、セルの端 (周期境界の継ぎ目) で原子が重なるので間引く
            seam = cKDTree(fill.positions % lengths, boxsize=lengths).query_pairs(dmin, output_type="ndarray")
            if len(seam):
                del fill[np.unique(seam[:, 1]).tolist()]
            atoms += Atoms(fill.get_chemical_symbols(), positions=fill.positions)
            if log is not None:
                log.append(f"すき間を結晶 ({len(fill):,} 個) で埋めました")
        if log is not None and any(abs(L / l - round(L / l)) > 1e-3 for L, l in zip(lengths, matrix.cell.lengths())):
            log.append("すき間の結晶は箱の大きさと周期が合わないので、セルの端で格子がずれます")
    elif filler != "none":
        raise ValueError("filler は random / crystal / none です")
    return atoms
