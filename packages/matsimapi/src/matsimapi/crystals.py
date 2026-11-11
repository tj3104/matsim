"""Materials Project の結晶構造を OPTIMADE (データベース共通の検索の決まり) で取り込む。API キーは要らない。

    問い合わせ: 組成式 (TiO2)・元素の組 (Ti-O、その元素だけからなる組成すべて)・ID (mp-149 など)

候補には 組成式・原子数・空間群 (spglib で求める)・凸包からの距離 (安定性、eV/原子) を付け、安定な順に並べる。
OPTIMADE の MP は基本単位胞を任意の向きで返すので、取り込むときは既定で慣用の単位胞にし、どちらでも標準の向き
(a を x 軸に、b を xy 面に。Materials Project の API や pymatgen の慣用の単位胞と同じ) に置き直す。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from functools import reduce
from math import gcd

import numpy as np
from ase import Atoms
from ase.data import chemical_symbols
from ase.geometry import cellpar_to_cell

BASE = "https://optimade.materialsproject.org/v1/structures"
TIMEOUT = 40
MAX_RESULTS = 20
MAX_SITES = 2000
FIELDS = ("lattice_vectors,cartesian_site_positions,species_at_sites,species,structure_features,nsites,dimension_types,"
          "_mp_stability")


def _get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "matsimapi", "Accept": "application/vnd.api+json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read()).get("errors", [{}])[0].get("detail", "")
        except Exception:   # HTML のエラーの画面など
            pass
        if exc.code == 404:
            raise ValueError("Materials Project に見つかりませんでした") from None
        if exc.code >= 500:
            raise ValueError(f"Materials Project のサーバーが応答しません ({exc.code})。しばらくしてから試してください") from None
        raise ValueError(f"Materials Project に断られました ({exc.code}{f': {detail}' if detail else ''})") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ValueError(f"Materials Project につながりません ({getattr(exc, 'reason', exc)})") from None
    except ValueError:
        raise ValueError("Materials Project の応答を読めません") from None


# ---- 問い合わせ ---------------------------------------------------------------------------------

def _counts(formula: str) -> dict[str, int]:
    """TiO2 → {"Ti": 1, "O": 2}。括弧は扱わない。"""
    counts: dict[str, int] = {}
    for symbol, number in re.findall(r"([A-Z][a-z]?)(\d*)", formula):
        if symbol not in chemical_symbols[1:]:
            raise ValueError(f"{symbol} は元素記号ではありません")
        counts[symbol] = counts.get(symbol, 0) + int(number or 1)
    return counts


def reduced(counts: dict[str, int]) -> str:
    """OPTIMADE の簡約した組成式 (元素はアルファベット順、1 は書かない): {"Ti": 2, "O": 4} → O2Ti"""
    divisor = reduce(gcd, counts.values())
    return "".join(f"{s}{n // divisor if n // divisor > 1 else ''}" for s, n in sorted(counts.items()))


def _filter(query: str) -> str | None:
    """OPTIMADE の絞り込み。ID のときは None。"""
    query = query.strip()
    if not query or len(query) > 100:
        raise ValueError("組成式 (TiO2)・元素の組 (Ti-O)・ID (mp-149) を 1〜100 文字で入れてください")
    if re.fullmatch(r"[A-Z][a-z]?(?:\s*[-,\s]\s*[A-Z][a-z]?)+", query):   # Ti-O・Ti,O
        elements = sorted(set(re.split(r"\s*[-,\s]\s*", query)))
        for symbol in elements:
            if symbol not in chemical_symbols[1:]:
                raise ValueError(f"{symbol} は元素記号ではありません")
        return f"elements HAS ALL {','.join(f'{chr(34)}{e}{chr(34)}' for e in elements)} AND nelements={len(elements)}"
    if re.fullmatch(r"(?:[A-Z][a-z]?\d*)+", query):
        return f'chemical_formula_reduced="{reduced(_counts(query))}"'
    if re.fullmatch(r"mp-\d+", query):
        return None
    raise ValueError("組成式 (TiO2)・元素の組 (Ti-O)・ID (mp-149) のどれかで入れてください")


# ---- 構造 ---------------------------------------------------------------------------------------

def _atoms(attributes: dict) -> tuple[Atoms | None, str]:
    """OPTIMADE の構造 → ASE の Atoms。使えないときは (None, 理由)。"""
    if "disorder" in (attributes.get("structure_features") or []):
        return None, "部分占有 (乱れ) のある構造なので、計算に使えません"
    species = {s["name"]: s for s in attributes.get("species") or []}
    symbols = []
    for name in attributes.get("species_at_sites") or []:
        spec = species.get(name, {"chemical_symbols": [name]})
        if len(spec["chemical_symbols"]) != 1 or spec["chemical_symbols"][0] not in chemical_symbols[1:]:
            return None, "部分占有 (乱れ) や空孔のある構造なので、計算に使えません"
        symbols.append(spec["chemical_symbols"][0])
    lattice, positions = attributes.get("lattice_vectors"), attributes.get("cartesian_site_positions")
    if not symbols or lattice is None or positions is None:
        return None, "原子の座標かセルがありません"
    if len(symbols) > MAX_SITES:
        return None, f"原子が多すぎます ({len(symbols)} 個)"
    pbc = [bool(d) for d in attributes.get("dimension_types") or [1, 1, 1]]
    return Atoms(symbols, positions=positions, cell=lattice, pbc=pbc), ""


def spacegroup(atoms: Atoms) -> str | None:
    try:
        import spglib
        data = spglib.get_symmetry_dataset((atoms.cell[:], atoms.get_scaled_positions(), atoms.numbers), symprec=1e-3)
        return f"{data.international} ({data.number})" if data else None
    except Exception:   # spglib がない・判定できない
        return None


def conventional(atoms: Atoms) -> Atoms:
    """慣用 (通常) の単位胞にする (spglib の標準化。位置は丸めない)。"""
    import spglib
    cell = spglib.standardize_cell((atoms.cell[:], atoms.get_scaled_positions(), atoms.numbers),
                                   to_primitive=False, no_idealize=True, symprec=1e-3)
    if cell is None:
        return atoms
    lattice, scaled, numbers = cell
    return Atoms(numbers=numbers, scaled_positions=scaled, cell=lattice, pbc=True)


def standard_orientation(atoms: Atoms) -> Atoms:
    """セルを標準の向き (a を x 軸、b を xy 面) に回す。原子の分率座標は変えない。左手系のセルは c を反転して右手系にする。"""
    cell, scaled = atoms.cell[:].copy(), atoms.get_scaled_positions(wrap=False)
    if np.linalg.det(cell) < 0:   # 同じ結晶のまま右手系に (c → -c、分率座標の z → -z)
        cell[2] *= -1
        scaled[:, 2] *= -1
    from ase.cell import Cell
    turned = Atoms(atoms.numbers, cell=cellpar_to_cell(Cell(cell).cellpar()), pbc=atoms.pbc)
    turned.set_scaled_positions(scaled)
    turned.wrap()
    return turned


def _summary(record: dict) -> dict:
    attributes = record["attributes"]
    atoms, problem = _atoms(attributes)
    stability = attributes.get("_mp_stability") or {}
    hull = (stability.get("gga_gga+u_r2scan") or stability.get("gga_gga+u") or {}).get("energy_above_hull")
    return {"id": str(record["id"]),
            "formula": reduced(_counts(atoms.get_chemical_formula())) if atoms is not None else None,
            "n_atoms": len(atoms) if atoms is not None else attributes.get("nsites"),
            "spacegroup": spacegroup(atoms) if atoms is not None else None,
            "e_above_hull": round(hull, 4) if isinstance(hull, (int, float)) else None,
            "importable": atoms is not None, "problem": problem or None}


def search(query: str, limit: int = MAX_RESULTS) -> list[dict]:
    """候補を安定な順 (凸包からの距離が小さい順) に返す。"""
    flt = _filter(query)
    if flt is None:
        return [_summary(_get(f"{BASE}/{query.strip()}?response_fields={FIELDS}")["data"])]
    data = _get(f"{BASE}?{urllib.parse.urlencode({'filter': flt, 'response_fields': FIELDS, 'page_limit': max(limit * 3, 30)})}")
    found = [_summary(record) for record in data.get("data", [])]
    if not found:
        raise ValueError(f"Materials Project に {query} の構造は見つかりませんでした")
    found.sort(key=lambda f: (not f["importable"], f["e_above_hull"] if f["e_above_hull"] is not None else 99, f["n_atoms"] or 0))
    return found[:limit]


def fetch(entry_id: str, use_conventional: bool = True) -> tuple[Atoms, dict]:
    """1 つの構造を取ってくる。use_conventional で慣用の単位胞にする (OPTIMADE の MP は基本単位胞で返すため)。"""
    if not re.fullmatch(r"mp-\d{1,12}", entry_id):
        raise ValueError("ID は mp-149 のように書いてください")
    record = _get(f"{BASE}/{entry_id}?response_fields={FIELDS}")["data"]
    atoms, problem = _atoms(record["attributes"])
    if atoms is None:
        raise ValueError(problem)
    if all(atoms.pbc):
        atoms = standard_orientation(conventional(atoms) if use_conventional else atoms)
    info = _summary(record)
    info["n_atoms"] = len(atoms)
    return atoms, info
