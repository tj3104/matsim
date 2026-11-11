"""Atomsk のモード (--create / --merge / --polycrystal) と、それを組み合わせた双結晶。

    from atomsk_kit import create, merge, polycrystal, bicrystal
    cu = create("fcc", 3.615, "Cu")
    nacl = create("rocksalt", 5.64, ["Na", "Cl"], duplicate=(2, 2, 2))
    poly = polycrystal(cu, box=(60, 60, 60), grains=8, seed=1)
"""
from __future__ import annotations

import numpy as np
from ase import Atoms
from scipy.spatial.transform import Rotation

from .edit import as_rng
from .geometry import VALID_SYMBOLS, is_orthorhombic, number_density, sort_atoms
from .pipeline import AXES, miller
from .runner import OUT, run

# --create で作れる格子: (表示名, c が要るか, 元素数の範囲)
LATTICES = {
    "sc": ("単純立方 (sc)", False, (1, 1)), "bcc": ("体心立方 (bcc) / CsCl", False, (1, 2)),
    "fcc": ("面心立方 (fcc)", False, (1, 2)), "L12": ("L1₂", False, (2, 2)),
    "fluorite": ("蛍石型", False, (2, 2)), "diamond": ("ダイヤモンド / 閃亜鉛鉱", False, (1, 2)),
    "rocksalt": ("岩塩型", False, (2, 2)), "perovskite": ("ペロブスカイト", False, (3, 3)),
    "A15": ("A15 (Cr₃Si)", False, (2, 2)), "C15": ("C15 ラーベス相", False, (2, 2)),
    "st": ("単純正方 (st)", True, (1, 1)), "bct": ("体心正方 (bct)", True, (1, 2)),
    "fct": ("面心正方 (fct) / L1₀", True, (1, 2)), "hcp": ("六方最密 (hcp)", True, (1, 2)),
    "wurtzite": ("ウルツ鉱", True, (2, 2)), "graphite": ("グラファイト", True, (1, 2)),
    "BN": ("六方晶 BN", True, (2, 2)), "C14": ("C14 ラーベス相", True, (2, 2)), "C36": ("C36 ラーベス相", True, (2, 2)),
}


def _species(species: str | list[str]) -> list[str]:
    items = species.split() if isinstance(species, str) else list(species)
    for s in items:
        if s not in VALID_SYMBOLS:
            raise ValueError(f"元素記号が正しくありません: {s}")
    return items


def create(lattice: str, a: float, species: str | list[str], *, c: float | None = None,
           orient: tuple[str, str, str] | None = None, duplicate: tuple[int, int, int] | None = None,
           log: list[str] | None = None) -> Atoms:
    """格子の種類・格子定数・元素から結晶を作る (atomsk --create)。

    orient=("[11-2]", "[1-10]", "[111]") で x, y, z の結晶方位を指定できる (立方晶・六方晶のみ)。
    """
    if lattice not in LATTICES:
        raise ValueError(f"格子の種類が正しくありません: {lattice}")
    label, needs_c, (lo, hi) = LATTICES[lattice]
    elems = _species(species)
    if not lo <= len(elems) <= hi:
        raise ValueError(f"{label} には元素を {lo}{'' if lo == hi else f'〜{hi}'} 個指定してください")
    if needs_c and c is None:
        raise ValueError(f"{label} には格子定数 c が要ります")
    args = ["--create", lattice, a, *([c] if needs_c else []), *elems]
    if orient:
        args += ["orient", *[miller(v) for v in orient]]
    if duplicate and tuple(duplicate) != (1, 1, 1):
        args += ["-duplicate", *duplicate]
    return run([*args, OUT], log=log)


def nanotube(a0: float, m: int, n: int, species: str | list[str] = "C", *, nz: int = 1,
             log: list[str] | None = None) -> Atoms:
    """カイラル指数 (m, n) のナノチューブ (軸は z)。a0 はグラフェンの格子定数 (炭素なら 2.46 Å)。"""
    elems = _species(species)
    if not 1 <= len(elems) <= 2:
        raise ValueError("ナノチューブの元素は 1〜2 個です")
    args = ["--create", "nanotube", a0, int(m), int(n), *elems]
    if nz > 1:
        args += ["-duplicate", 1, 1, int(nz)]
    return run([*args, OUT], log=log)


def merge(structures: list[Atoms], *, stack: str | None = None, scale: bool = False,
          remove_doubles: float | None = None, log: list[str] | None = None) -> Atoms:
    """複数の構造を 1 つにまとめる (atomsk --merge)。

    stack="z" ならその方向に積む。scale=True なら積む方向以外の大きさを最初の構造に合わせて伸縮する。
    stack を省くと、すべて最初の構造の箱に入れる。
    """
    if len(structures) < 2:
        raise ValueError("2 つ以上の構造を渡してください")
    args: list = ["--merge"]
    if stack:
        if stack not in AXES:
            raise ValueError("stack は x / y / z です")
        args += ["stack", stack]
        if scale:
            args += ["scale", "".join(a for a in AXES if a != stack)]
    names = [f"s{i}.vasp" for i in range(len(structures))]
    args += [len(structures), *names, OUT]
    if remove_doubles:
        args += ["-remove-doubles", remove_doubles]
    return run(args, inputs=dict(zip(names, structures)), log=log)


def _repeat_to(atoms: Atoms, lengths) -> Atoms:
    reps = [max(1, int(round(target / length))) for target, length in zip(lengths, atoms.cell.lengths())]
    return atoms.repeat(reps)


def bicrystal(seed: Atoms, orient_a: tuple[str, str, str], orient_b: tuple[str, str, str], *, stack: str = "y",
              size: tuple[float, float, float] = (20, 60, 20), remove_doubles: float | None = 1.0,
              log: list[str] | None = None) -> Atoms:
    """立方晶の慣用単位胞 seed を 2 つの方位に回して重ね、粒界を作る。

    orient_a / orient_b は結晶 A / B の (x, y, z) の方位。stack 方向に半分ずつ積む (size はおよその全体の大きさ [Å])。
    周期境界があるので、粒界は中央とセルの端の 2 か所にできる。B は面内の大きさを A に合わせて伸縮する。
    """
    std = ("[100]", "[010]", "[001]")
    grains = [run(["in.vasp", "-orient", *std, *[miller(v) for v in o], "-orthogonal-cell", OUT],
                  inputs={"in.vasp": seed}, log=log) for o in (orient_a, orient_b)]
    k = AXES.index(stack)
    halves = [size[i] / 2 if i == k else size[i] for i in range(3)]
    grains = [_repeat_to(g, halves) for g in grains]
    la, lb = grains[0].cell.lengths(), grains[1].cell.lengths()
    mismatch = max(abs(lb[i] / la[i] - 1) for i in range(3) if i != k) * 100
    if mismatch > 2 and log is not None:
        log.append(f"2 つの結晶の面内の大きさが {mismatch:.1f}% ずれています (B を伸縮して合わせます)")
    result = merge(grains, stack=stack, scale=True, remove_doubles=remove_doubles, log=log)
    if log is not None:
        log.append("周期境界のため、粒界は中央とセルの端の 2 か所にできます")
    return result


def spread_points(rng: np.random.Generator, n: int, lengths: np.ndarray, dims: int) -> np.ndarray:
    """周期的な箱の中に、互いにある程度離れた点を n 個置く (極端に小さな粒ができないように)。"""
    volume = float(np.prod(lengths[:dims]))
    dmin = 0.6 * (volume / n) ** (1 / dims)
    points: list[np.ndarray] = []
    for _ in range(200 * n):
        if len(points) == n:
            break
        p = rng.random(3) * lengths
        if dims == 2:
            p[2] = 0.0
        if points:
            delta = np.array(points)[:, :dims] - p[:dims]
            delta -= np.round(delta / lengths[:dims]) * lengths[:dims]
            if np.min(np.linalg.norm(delta, axis=1)) < dmin:
                continue
        points.append(p)
    while len(points) < n:
        points.append(rng.random(3) * lengths)
    return np.array(points)


def polycrystal(seed: Atoms, box: tuple[float, float, float], grains: int, *, columnar: bool = False, nz: int = 1,
                matrix: Atoms | None = None, clearance: float | None = None, remove_doubles: float | None = 1.0,
                seed_rng=None, max_atoms: int | None = None, log: list[str] | None = None) -> Atoms:
    """Voronoi 多結晶 (atomsk --polycrystal)。

    粒の位置と方位は seed_rng (整数か numpy の Generator) から決めて node として渡すので、同じ入力なら同じ構造になる。
    columnar=True なら z 方向にそろった柱状 (2D Voronoi)。z の厚さは seed の c になり、nz 回繰り返す。
    matrix を渡すと粒のすき間 (幅 2×clearance) をその構造で埋める (粒界相)。
    """
    rng = as_rng(seed_rng)
    lengths = np.array(box, dtype=float)
    if columnar:
        if not is_orthorhombic(seed):
            raise ValueError("柱状にするには直方体のセルの結晶を使ってください (先に orthogonal_cell)")
        lengths[2] = seed.cell.lengths()[2]
    est = number_density(seed) * float(np.prod(lengths)) * (nz if columnar else 1)
    if max_atoms is not None and est > max_atoms:
        raise ValueError(f"原子数が多すぎます (約 {est:,.0f})。箱を小さくしてください")
    nodes = spread_points(rng, grains, lengths, 2 if columnar else 3)
    lines = [f"box {lengths[0]:.6f} {lengths[1]:.6f} {lengths[2]:.6f}"]
    for p in nodes:
        angles = [0.0, 0.0, rng.uniform(-180, 180)] if columnar else \
            Rotation.random(random_state=rng).as_euler("xyz", degrees=True)
        lines.append(f"node {p[0]:.4f} {p[1]:.4f} {p[2]:.4f} {angles[0]:.3f}° {angles[1]:.3f}° {angles[2]:.3f}°")
    inputs = {"seed.vasp": seed}
    if matrix is not None:
        inputs["matrix.vasp"] = matrix
        # Atomsk は node の行より前に matrix があると node を読まないので、後ろに書く
        lines += ["matrix matrix.vasp", f"clearance {clearance if clearance is not None else 3.0}"]
    elif clearance is not None:
        lines.append(f"clearance {clearance}")
    args = ["--polycrystal", "seed.vasp", "poly.txt", OUT, "-wrap"]
    if remove_doubles:
        args += ["-remove-doubles", remove_doubles]
    if columnar and nz > 1:
        args += ["-duplicate", 1, 1, int(nz)]
    # Atomsk は並列で動くと原子の順番が毎回変わるので、並べ替えて同じ入力なら同じ出力にする
    return sort_atoms(run(args, inputs=inputs, files={"poly.txt": "\n".join(lines) + "\n"}, log=log))
