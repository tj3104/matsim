"""原子ごとの結晶構造の判定 (adaptive common neighbor analysis, a-CNA)。

Stukowski (2012) の a-CNA。各原子について、近い順に 12 個 (fcc / hcp / 正二十面体) または 14 個 (bcc) の
となりを取り、その原子ごとに決めた距離 (となりまでの平均距離 × (1+√2)/2) の中で
「中心ととなり j の両方に結合している原子の数 n_cn」「その原子どうしの結合の数 n_b」「結合のつながりの最長 n_lc」
(CNA の指数) を数える。

    fcc       12 個すべてが (4,2,1)
    hcp       6 個が (4,2,1)、6 個が (4,2,2)
    正二十面体 12 個すべてが (5,5,5)
    bcc       8 個が (6,6,6)、6 個が (4,4,4)

温度で揺れた結晶でも判定できる (となりの距離を原子ごとに決めるので、格子定数や元素によらない)。
どれにも当てはまらない原子は「その他」(液体・粒界・表面・転位の芯など)。

    labels = classify(atoms)          # ["fcc", "bcc", ...]
    fractions(labels)                 # {"fcc": 0.95, ...}
"""
from __future__ import annotations

import numpy as np
from ase import Atoms
from ase.neighborlist import neighbor_list

TYPES = ["fcc", "hcp", "bcc", "正二十面体", "その他"]
COLORS = {"fcc": "#1baf7a", "hcp": "#d6457a", "bcc": "#2a78d6", "正二十面体": "#eda100", "その他": "#9ca3af"}
FACTOR = (1 + np.sqrt(2)) / 2


def _nearest(atoms: Atoms, count: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """各原子の近い順 count 個のとなりへのベクトル (N, count, 3) と距離 (N, count)。count 個ない原子は ok=False。"""
    n = len(atoms)
    i0, d0 = neighbor_list("id", atoms, 3.0)
    if len(d0) == 0:
        raise ValueError("3 Å 以内にとなりの原子がありません")
    nearest = np.full(n, np.inf)
    np.minimum.at(nearest, i0, d0)
    scale = float(np.median(nearest[np.isfinite(nearest)]))
    # bcc の 14 番目 (第 2 近接) は最近接の 1.155 倍、fcc の 13 番目は 1.414 倍。余裕をみて 1.6 倍まで探す
    i, d, vec = neighbor_list("idD", atoms, 1.6 * scale)
    order = np.lexsort((d, i))
    i, d, vec = i[order], d[order], vec[order]
    starts = np.searchsorted(i, np.arange(n))
    counts = np.bincount(i, minlength=n)
    ok = counts >= count
    rank = np.arange(len(i)) - starts[i]
    keep = rank < count
    vectors = np.zeros((n, count, 3))
    dists = np.full((n, count), np.inf)
    vectors[i[keep], rank[keep]] = vec[keep]
    dists[i[keep], rank[keep]] = d[keep]
    return vectors, dists, ok


def _signatures(vectors: np.ndarray, cutoff: np.ndarray) -> np.ndarray:
    """CNA の指数 (n_cn, n_b, n_lc) を、となりごとに (N, M, 3) で返す。"""
    n, m, _ = vectors.shape
    gap = np.linalg.norm(vectors[:, :, None, :] - vectors[:, None, :, :], axis=-1)
    bonded = gap < cutoff[:, None, None]
    bonded[:, np.arange(m), np.arange(m)] = False
    out = np.zeros((n, m, 3), dtype=np.int16)
    for j in range(m):
        common = bonded[:, j, :]                                   # 中心と j の両方に結合している原子
        n_cn = common.sum(axis=1)
        pair = bonded & common[:, :, None] & common[:, None, :]    # 共通の原子どうしの結合
        n_b = pair.sum(axis=(1, 2)) // 2
        out[:, j, 0] = n_cn
        out[:, j, 1] = n_b
        out[:, j, 2] = _longest_chain(pair, n_b)
    return out


def _longest_chain(pair: np.ndarray, n_b: np.ndarray) -> np.ndarray:
    """共通の原子どうしの結合のつながりの長さ n_lc。

    判定に要るのは fcc (4,2,1) と hcp (4,2,2) の区別 (2 本の結合が原子を共有するか) だけなので、
    結合が 2 本のときは共有の有無で 1 か 2 にし、それ以外は結合がすべてつながっているとみなして n_b にする
    (bcc の (6,6,6)・(4,4,4)、正二十面体の (5,5,5) は輪なので n_lc = n_b)。原子ごとのループを避けて速くするため。
    """
    shared = (pair.sum(axis=2) >= 2).any(axis=1)
    return np.where(n_b == 2, np.where(shared, 2, 1), n_b).astype(np.int16)


def _all(sig: np.ndarray, triple: tuple[int, int, int]) -> np.ndarray:
    return (sig == np.array(triple)).all(axis=-1).sum(axis=1)


def classify(atoms: Atoms) -> list[str]:
    """原子ごとの結晶構造 (TYPES のどれか)。"""
    n = len(atoms)
    labels = np.array(["その他"] * n, dtype=object)
    if n == 0:
        return []
    # 12 個のとなり: fcc / hcp / 正二十面体
    vectors, dists, ok = _nearest(atoms, 14)
    v12, d12 = vectors[:, :12], dists[:, :12]
    ok12 = np.isfinite(d12).all(axis=1)
    idx = np.flatnonzero(ok12)
    if len(idx):
        cutoff = FACTOR * d12[idx].mean(axis=1)
        sig = _signatures(v12[idx], cutoff)
        n421, n422, n555 = _all(sig, (4, 2, 1)), _all(sig, (4, 2, 2)), _all(sig, (5, 5, 5))
        labels[idx[n421 == 12]] = "fcc"
        labels[idx[(n421 == 6) & (n422 == 6)]] = "hcp"
        labels[idx[n555 == 12]] = "正二十面体"
    # 14 個のとなり: bcc (まだ決まっていない原子だけ)
    idx = np.flatnonzero(ok & (labels == "その他"))
    if len(idx):
        d = dists[idx]
        cutoff = FACTOR * (2 / np.sqrt(3) * d[:, :8].mean(axis=1) + d[:, 8:14].mean(axis=1)) / 2
        sig = _signatures(vectors[idx], cutoff)
        bcc = (_all(sig, (6, 6, 6)) == 8) & (_all(sig, (4, 4, 4)) == 6)
        labels[idx[bcc]] = "bcc"
    return labels.tolist()


def fractions(labels, mask: np.ndarray | None = None) -> dict[str, float]:
    """種類ごとの割合 (TYPES の順、すべての種類を含む)。"""
    values = np.asarray(labels, dtype=object)
    if mask is not None:
        values = values[mask]
    total = max(len(values), 1)
    return {t: float((values == t).sum()) / total for t in TYPES}


def analyze(final: Atoms, frames: list, progress=None) -> dict:
    """最終構造と、トラジェクトリのフレーム [(時刻 ps, 構造)] を判定する。progress(済んだ数) で進み具合を知らせる。"""
    labels = classify(final)
    symbols = np.array(final.get_chemical_symbols())
    species = list(dict.fromkeys(symbols.tolist()))
    total = fractions(labels)
    result = {
        "n_atoms": len(final),
        "fractions": [{"type": t, "fraction": total[t], "count": int(round(total[t] * len(final)))} for t in TYPES],
        "by_species": {sym: fractions(labels, symbols == sym) for sym in species},
        "labels": labels,
    }
    if progress:
        progress(1)
    if frames:
        evolution = {"time_ps": [], "frame": [], "fractions": {t: [] for t in TYPES}}
        for k, (time_ps, atoms) in enumerate(frames):
            f = fractions(classify(atoms))
            evolution["time_ps"].append(time_ps)
            evolution["frame"].append(k)
            for t in TYPES:
                evolution["fractions"][t].append(f[t])
            if progress:
                progress(k + 2)
        # 一度も現れない種類は線を描かない
        evolution["fractions"] = {t: v for t, v in evolution["fractions"].items() if any(v)}
        result["evolution"] = evolution
    return result
