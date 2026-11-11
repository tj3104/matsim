"""フォノンの振動のようす (固有ベクトル)。フォノン分散のジョブが書く D.out (q 点ごとの動的行列) を対角化する。

GPUMD (phonon/hessian.cu) の決まりに合わせている:
  - q 点: kpoints.in の高対称点のあいだを、逆格子の直交座標で 100 等分してつなぐ (区間の始点は前の区間の終点)
  - 動的行列: D_ab(q) = Σ Φ_ab exp(i q·(r_b − r_a)) / √(m_a m_b)  (原子の位置で位相を付ける)
    D.out は q 点ごとに 3N 行、各行は 3N 個の実部と 3N 個の虚部 [eV/Å²/amu]
  - 振動数: 固有値 ω² を ×1e6 / TIME_UNIT² で (rad/ps)² にし、ν = √ω² / 2π [THz] (負の ω² は虚振動、負の ν で表す)
固有ベクトル e から、セル R にある原子 a の変位は u = Re[ e_a / √m_a · exp(i (q·(R + r_a) − ωt)) ]。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from ase.data import atomic_masses, atomic_numbers

TIME_UNIT = 1.018051e+1             # GPUMD の時間の単位 [fs]
TO_OMEGA2 = 1.0e6 / TIME_UNIT ** 2  # eV/Å²/amu → (rad/ps)²
CACHE = "analysis/phonon_D.npy"


def _kpoints(directory: Path, cell: np.ndarray) -> tuple[np.ndarray, list[dict]]:
    """GPUMD と同じ q 点の並び (直交座標、Å⁻¹) と、高対称点 [{index, label}]。"""
    frac, labels = [], []
    for line in (directory / "kpoints.in").read_text().splitlines():
        tokens = line.split()
        if len(tokens) >= 4:
            frac.append([float(v) for v in tokens[:3]])
            labels.append("Γ" if tokens[3] in ("G", "Gamma") else tokens[3])
    recip = 2 * math.pi * np.linalg.inv(cell).T          # 行が逆格子ベクトル
    hsp = np.array(frac) @ recip
    points = [hsp[0]]
    ticks = [{"index": 0, "label": labels[0]}]
    for a in range(1, len(hsp)):
        for j in range(1, 101):
            points.append(hsp[a - 1] + (hsp[a] - hsp[a - 1]) * j * 0.01)
        ticks.append({"index": len(points) - 1, "label": labels[a]})
    return np.array(points), ticks


def _masses(symbols: list[str]) -> np.ndarray:
    return np.array([atomic_masses[atomic_numbers[s]] for s in symbols])


def load_dynamical(directory: Path, n_basis: int) -> np.ndarray:
    """D.out → (q 点の数, 3N, 3N) の複素行列。初回に numpy の形式で保存して、2 回目からはそれを読む。"""
    cache = directory / CACHE
    source = directory / "D.out"
    if cache.is_file() and cache.stat().st_mtime >= source.stat().st_mtime:
        return np.load(cache)
    dim = 3 * n_basis
    raw = np.loadtxt(source, ndmin=2)
    if raw.shape[1] != 2 * dim or raw.shape[0] % dim:
        raise ValueError(f"D.out の形が想定と違います ({raw.shape}、基本単位胞 {n_basis} 原子)")
    raw = raw.reshape(-1, dim, 2 * dim)
    d = raw[:, :, :dim] + 1j * raw[:, :, dim:]
    cache.parent.mkdir(exist_ok=True)
    np.save(cache, d)
    return d


def overview(directory: Path) -> dict:
    """q 点の数・分枝の数・高対称点・基本単位胞 (表示用)。"""
    from gpumd_toolkit.structure import StructureHandler
    atoms = StructureHandler.read_model(directory / "model.xyz")
    cell = atoms.cell.array
    kpts, ticks = _kpoints(directory, cell)
    d = load_dynamical(directory, len(atoms))
    if len(d) != len(kpts):
        raise ValueError(f"q 点の数が合いません (D.out {len(d)}、kpoints.in から {len(kpts)})")
    return {"n_q": len(kpts), "n_bands": 3 * len(atoms), "ticks": ticks,
            "cell": cell.round(6).tolist(), "symbols": atoms.get_chemical_symbols(),
            "positions": atoms.positions.round(6).tolist()}


def mode(directory: Path, q_index: int, band: int) -> dict:
    """q 点 q_index・分枝 band (0 始まり、振動数の低い順) の振動数と、原子ごとの変位の振幅 (複素、1/√m をかけたもの)。"""
    from gpumd_toolkit.structure import StructureHandler
    atoms = StructureHandler.read_model(directory / "model.xyz")
    n = len(atoms)
    kpts, ticks = _kpoints(directory, atoms.cell.array)
    d = load_dynamical(directory, n)
    if not 0 <= q_index < len(d):
        raise ValueError(f"q 点の番号は 0〜{len(d) - 1} です")
    if not 0 <= band < 3 * n:
        raise ValueError(f"分枝の番号は 1〜{3 * n} です")
    w2, vectors = np.linalg.eigh(d[q_index])
    nu = np.sign(w2) * np.sqrt(np.abs(w2) * TO_OMEGA2) / (2 * math.pi)
    e = vectors[:, band].reshape(n, 3)
    u = e / np.sqrt(_masses(atoms.get_chemical_symbols()))[:, None]
    # 位相をそろえる (最も大きく動く成分が実数になるように) と、矢印が見やすい
    big = np.unravel_index(np.argmax(np.abs(u)), u.shape)
    u = u * np.exp(-1j * np.angle(u[big]))
    u = u / np.abs(u).max()
    near = min(ticks, key=lambda tk: abs(tk["index"] - q_index))
    frac = kpts[q_index] @ atoms.cell.array.T / (2 * math.pi)   # 逆格子の分数座標
    return {"q_index": q_index, "band": band, "frequency": float(nu[band]), "frequencies": nu.round(5).tolist(),
            "q_cart": kpts[q_index].round(6).tolist(), "q_frac": frac.round(4).tolist(),
            "near": near["label"] if abs(near["index"] - q_index) <= 2 else None,
            "u_re": u.real.round(5).tolist(), "u_im": u.imag.round(5).tolist()}
