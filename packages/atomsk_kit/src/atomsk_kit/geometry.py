"""構造を調べる小さな道具 (セルの形・最近接距離・数密度・組成)。"""
from __future__ import annotations

import math

import numpy as np
from ase import Atoms
from ase.data import chemical_symbols
from ase.geometry import get_distances

VALID_SYMBOLS = set(chemical_symbols[1:])


def is_orthorhombic(atoms: Atoms, tol: float = 1e-4) -> bool:
    cell = np.array(atoms.cell)
    return bool(np.all(np.abs(cell - np.diag(np.diag(cell))) < tol))


def nearest_distance(atoms: Atoms) -> float:
    """結晶の最近接距離の目安 (先頭 50 原子から調べる)。"""
    sample = atoms if len(atoms) >= 8 else atoms.repeat([2, 2, 2])
    d = get_distances(sample.positions[: min(len(sample), 50)], sample.positions, cell=sample.cell, pbc=True)[1]
    d = d[d > 1e-6]
    return float(d.min()) if d.size else 2.5


def number_density(atoms: Atoms) -> float:
    """数密度 [原子/Å³]。"""
    return len(atoms) / atoms.get_volume()


def composition_of(atoms: Atoms) -> dict[str, float]:
    symbols, counts = np.unique(atoms.get_chemical_symbols(), return_counts=True)
    return {str(s): float(c / counts.sum()) for s, c in zip(symbols, counts)}


def parse_composition(text: str | dict) -> dict[str, float]:
    """"Cu:50,Ni:50" または {"Cu": 50, "Ni": 50} を比率 (合計 1) にする。"""
    if isinstance(text, dict):
        items = list(text.items())
    else:
        items = []
        for part in str(text).split(","):
            sym, value = [s.strip() for s in part.split(":")]
            items.append((sym, float(value)))
    comp: dict[str, float] = {}
    for sym, value in items:
        if sym not in VALID_SYMBOLS:
            raise ValueError(f"組成の元素記号が正しくありません: {sym}")
        if value < 0:
            raise ValueError("組成の比率は 0 以上にしてください")
        comp[sym] = comp.get(sym, 0.0) + float(value)
    total = sum(comp.values())
    if total <= 0:
        raise ValueError("組成の比率の合計が 0 です")
    return {k: v / total for k, v in comp.items()}


def split_counts(n: int, comp: dict[str, float]) -> dict[str, int]:
    """n 個を組成比に従って整数に分ける (最大剰余法なので合計はちょうど n)。"""
    raw = {k: n * v for k, v in comp.items()}
    counts = {k: int(math.floor(v)) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - counts[k], reverse=True)[: n - sum(counts.values())]:
        counts[k] += 1
    return counts


def species_mix(n: int, comp: dict[str, float], rng: np.random.Generator) -> np.ndarray:
    """組成どおりの元素記号を n 個、ランダムな順に並べる。"""
    if n == 0:
        return np.array([], dtype=str)
    return rng.permutation(np.concatenate([[k] * c for k, c in split_counts(n, comp).items()]))


def sort_atoms(atoms: Atoms) -> Atoms:
    """元素 → z → y → x の順に並べ替える。Atomsk は並列で動くと出力の順番が毎回変わるので、そろえるのに使う。"""
    pos = np.round(atoms.positions, 4)
    order = np.lexsort((pos[:, 0], pos[:, 1], pos[:, 2], atoms.numbers))
    return atoms[order]


def close_pairs(atoms: Atoms, limit: float = 0.8) -> int:
    """limit [Å] より近い原子の組の数 (重なりの目安)。30 万原子を超える構造は調べず 0 を返す。"""
    if len(atoms) > 300_000 or len(atoms) < 2:
        return 0
    from ase.neighborlist import neighbor_list
    return len(neighbor_list("i", atoms, limit)) // 2
