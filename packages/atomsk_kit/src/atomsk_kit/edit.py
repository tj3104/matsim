"""原子の編集 (置換・空孔・ランダム合金・格子間原子など)。Python で実装し、シードで再現できる。

Atomsk の -select random は毎回結果が変わるので、乱数を使う編集はこちらを使う。

    from atomsk_kit import substitute, vacancy, random_alloy
    nicu = substitute(cu, "Cu", "Ni", fraction=0.1, seed=1)        # Cu の 10% を Ni に
    vac = vacancy(cu, count=3, seed=1)                             # 3 個の空孔
    hea = random_alloy(cu, "Cu:1,Ni:1,Co:1,Fe:1", seed=1)          # 等モルの高エントロピー合金
"""
from __future__ import annotations

import numpy as np
from ase import Atoms
from ase.geometry import get_distances

from .geometry import VALID_SYMBOLS, parse_composition, split_counts


def as_rng(seed: int | np.random.Generator | None) -> np.random.Generator:
    return seed if isinstance(seed, np.random.Generator) else np.random.default_rng(seed)


def _check_symbol(symbol: str) -> str:
    if symbol not in VALID_SYMBOLS:
        raise ValueError(f"元素記号が正しくありません: {symbol}")
    return symbol


def _indices(atoms: Atoms, species: str | None) -> np.ndarray:
    """対象の原子の番号。species が None か "*" ならすべて。"""
    if species in (None, "*"):
        return np.arange(len(atoms))
    return np.flatnonzero(np.array(atoms.get_chemical_symbols()) == species)


def how_many(available: int, count: int | None = None, fraction: float | None = None) -> int:
    """選ぶ個数。count (個数) か fraction (0〜1 の割合) のどちらか。どちらもなければすべて。"""
    if available == 0:
        raise ValueError("対象の原子がありません")
    if count is not None and fraction is not None:
        raise ValueError("count と fraction はどちらか一方だけ指定してください")
    if count is not None:
        if count > available:
            raise ValueError(f"対象の原子は {available} 個しかありません")
        n = int(count)
    elif fraction is not None:
        if not 0 <= fraction <= 1:
            raise ValueError("fraction は 0〜1 です")
        n = int(round(available * fraction))
    else:
        n = available
    if n <= 0:
        raise ValueError(f"対象の原子が {available} 個なので、この割合では 0 個になります")
    return n


def substitute(atoms: Atoms, old: str, new: str, *, count: int | None = None, fraction: float | None = None,
               seed=None, log: list[str] | None = None) -> Atoms:
    """old の一部 (count 個 / fraction の割合 / 指定がなければすべて) をランダムに new に置き換える。"""
    if old == new:
        raise ValueError("置換前と置換後が同じ元素です")
    _check_symbol(new)
    result = atoms.copy()
    idx = _indices(result, old)
    if not len(idx):
        raise ValueError(f"構造に {old} がありません")
    chosen = as_rng(seed).choice(idx, how_many(len(idx), count, fraction), replace=False)
    symbols = np.array(result.get_chemical_symbols(), dtype=object)
    symbols[chosen] = new
    result.set_chemical_symbols(list(symbols))
    if log is not None:
        log.append(f"{old} {len(idx)} 個のうち {len(chosen)} 個を {new} にしました")
    return result


def vacancy(atoms: Atoms, species: str | None = None, *, count: int | None = None, fraction: float | None = None,
            seed=None, log: list[str] | None = None) -> Atoms:
    """原子をランダムに取り除く。species を指定するとその元素だけから選ぶ。"""
    idx = _indices(atoms, species)
    chosen = as_rng(seed).choice(idx, how_many(len(idx), count, fraction), replace=False)
    if len(chosen) >= len(atoms):
        raise ValueError("すべての原子が消えてしまいます")
    result = atoms.copy()
    del result[sorted(chosen.tolist())]
    if log is not None:
        log.append(f"{len(chosen)} 個の原子を取り除きました (空孔濃度 {len(chosen) / len(idx) * 100:.2f}%)")
    return result


def random_alloy(atoms: Atoms, composition: str | dict, sites: str | None = None, *, seed=None,
                 log: list[str] | None = None) -> Atoms:
    """sites (既定はすべて) のサイトを、組成どおりの個数の元素でランダムに埋める。

    composition は "Cu:50,Ni:50" か {"Cu": 50, "Ni": 50}。比率なので合計は 100 でなくてよい。
    個数は最大剰余法で整数に丸めるので、合計はサイト数にちょうど一致する。
    """
    result = atoms.copy()
    idx = _indices(result, sites)
    if not len(idx):
        raise ValueError("対象のサイトがありません")
    counts = split_counts(len(idx), parse_composition(composition))
    symbols = np.array(result.get_chemical_symbols(), dtype=object)
    symbols[as_rng(seed).permutation(idx)] = np.concatenate([[k] * n for k, n in counts.items()])
    result.set_chemical_symbols(list(symbols))
    if log is not None:
        log.append("配置した数: " + ", ".join(f"{k} {n}" for k, n in counts.items()))
    return result


def interstitial(atoms: Atoms, element: str, count: int = 1, *, min_dist: float = 1.5, seed=None,
                 max_tries_per_atom: int = 2000) -> Atoms:
    """既存の原子 (と加えた原子) から min_dist [Å] 以上離れたランダムな場所に原子を加える。"""
    _check_symbol(element)
    rng = as_rng(seed)
    result = atoms.copy()
    added: list[np.ndarray] = []
    for _ in range(max_tries_per_atom * count):
        if len(added) == count:
            break
        point = rng.random(3) @ np.array(result.cell)
        others = np.vstack([result.positions, *added]) if added else result.positions
        if get_distances([point], others, cell=result.cell, pbc=True)[1].min() >= min_dist:
            added.append(point)
    if len(added) < count:
        raise ValueError(f"{min_dist} Å 以上離れた場所が {len(added)} 個しか見つかりませんでした。距離を小さくしてください")
    result += Atoms([element] * count, positions=added)
    return result


def disturb(atoms: Atoms, amplitude: float, *, seed=None) -> Atoms:
    """各原子を各方向に ±amplitude [Å] の範囲で一様乱数だけずらす。"""
    result = atoms.copy()
    result.positions += as_rng(seed).uniform(-amplitude, amplitude, (len(result), 3))
    return result


def rotate(atoms: Atoms, axis: str, angle: float, *, com: bool = True, log: list[str] | None = None) -> Atoms:
    """セルと原子を一緒に回す (結晶は同じまま、座標軸に対する向きだけが変わる)。

    Atomsk の -rotate (Pipeline.rotate) は原子だけを回してセルを軸にそろえたまま残すことがあり、
    周期的な結晶では周期境界で原子の並びが合わなくなる。結晶を回すときはこちらを使う。
    """
    if axis not in ("x", "y", "z"):
        raise ValueError("回転軸は x / y / z です")
    result = atoms.copy()
    center = result.get_center_of_mass() if com else (0.0, 0.0, 0.0)
    result.rotate(float(angle), axis, center=center, rotate_cell=True)
    if log is not None and float(angle) % 90:
        log.append("セルも回したので、セルの辺は座標軸から傾いています。回した結晶を別の結晶と積み重ねるときは、"
                   "「双結晶 (粒界)」か「結晶方位の変更」で、両方に共通の周期を持つ方位 (対応格子 Σ) を選んでください")
    return result


def swap_species(atoms: Atoms, a: str, b: str) -> Atoms:
    """元素 a と b を入れ替える。"""
    if a == b:
        raise ValueError("同じ元素どうしは入れ替えられません")
    result = atoms.copy()
    symbols = np.array(result.get_chemical_symbols(), dtype=object)
    first, second = symbols == a, symbols == b
    symbols[first], symbols[second] = b, a
    result.set_chemical_symbols(list(symbols))
    return result
