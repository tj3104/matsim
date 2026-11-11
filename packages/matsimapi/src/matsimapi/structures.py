"""構造の取り込み (アップロード・Materials Project)・保存・表示用データ。"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path

import numpy as np
from ase import Atoms
from ase.build import bulk
from ase.data import chemical_symbols
from ase.io import read as ase_read
from ase.io import write as ase_write
from gpumd_toolkit.structure import StructureHandler

from . import settings

# 拡張子・ファイル名 → ASE の format 名。ここにない形式は受け付けない
FORMATS = {".cif": "cif", ".vasp": "vasp", ".poscar": "vasp", ".xyz": "extxyz", ".extxyz": "extxyz"}
VASP_NAMES = ("POSCAR", "CONTCAR")
VALID_SYMBOLS = set(chemical_symbols[1:])


def detect_format(filename: str) -> str:
    name = Path(filename).name
    suffix = Path(name).suffix.lower()
    if suffix in FORMATS:
        return FORMATS[suffix]
    if name.upper().startswith(VASP_NAMES):
        return "vasp"
    raise ValueError("対応していない形式です (cif / POSCAR / CONTCAR / .vasp / .xyz / .extxyz)")


def clean(atoms: Atoms) -> Atoms:
    """元素・座標・セルだけを持つ周期構造に作り直す (余計な配列や計算機を落とす)。"""
    symbols = atoms.get_chemical_symbols()
    if not symbols:
        raise ValueError("原子が 1 つもありません")
    unknown = sorted(set(symbols) - VALID_SYMBOLS)
    if unknown:
        raise ValueError(f"扱えない元素があります: {', '.join(unknown)}")
    cell = np.array(atoms.get_cell(), dtype=float)
    if not np.all(np.isfinite(cell)) or abs(np.linalg.det(cell)) < 1e-6:
        raise ValueError("セル (格子) の情報がありません。周期構造のファイルを使ってください")
    positions = np.array(atoms.get_positions(), dtype=float)
    if not np.all(np.isfinite(positions)):
        raise ValueError("座標に数値でない値があります")
    result = Atoms(symbols=symbols, positions=positions, cell=cell, pbc=True)
    result.wrap()
    return result


def parse_upload(filename: str, data: bytes) -> Atoms:
    """アップロードされたファイルを読む。読めなければ ValueError。"""
    fmt = detect_format(filename)
    with tempfile.NamedTemporaryFile(dir=settings.TMP_DIR, suffix=".upload") as handle:
        handle.write(data)
        handle.flush()
        try:
            atoms = ase_read(handle.name, format=fmt, index=-1)
        except Exception as exc:  # ASE のパーサは形式ごとに様々な例外を投げる
            raise ValueError(f"構造ファイルを読めませんでした ({type(exc).__name__})") from exc
    atoms = clean(atoms)
    if len(atoms) > settings.MAX_ATOMS:
        raise ValueError(f"原子数が多すぎます ({len(atoms):,} > {settings.MAX_ATOMS:,})")
    return atoms


# 動作確認用にすぐ読み込める構造 (慣用単位胞)。格子定数は実験値 [Å]
SAMPLES = {
    "Cu": {"label": "Cu (fcc)", "build": lambda: bulk("Cu", "fcc", a=3.615, cubic=True)},
    "MgO": {"label": "MgO (岩塩型)", "build": lambda: bulk("MgO", "rocksalt", a=4.212, cubic=True)},
}


def sample(key: str) -> tuple[Atoms, str]:
    """テスト構造を作る。戻り値は構造と表示名。"""
    if key not in SAMPLES:
        raise ValueError("テスト構造が見つかりません")
    return clean(SAMPLES[key]["build"]()), SAMPLES[key]["label"]


def path_for(structure_id: int) -> Path:
    return settings.STRUCTURE_DIR / f"{int(structure_id)}.xyz"


def save(db: sqlite3.Connection, atoms: Atoms, name: str, source: str, detail: str = "") -> int:
    count = db.execute("SELECT COUNT(*) FROM structures").fetchone()[0]
    if count >= settings.MAX_STRUCTURES:
        raise ValueError(f"構造は {settings.MAX_STRUCTURES} 件までです。不要なものを削除してください")
    info = StructureHandler.info(atoms).as_dict()
    structure_id = db.execute(
        "INSERT INTO structures (name, formula, n_atoms, info, source, source_detail) VALUES (?, ?, ?, ?, ?, ?)",
        (name[:60] or info["formula"], info["formula"], len(atoms), json.dumps(_jsonable(info)), source, detail[:200]),
    ).lastrowid
    ase_write(path_for(structure_id), atoms, format="extxyz")
    return structure_id


def load(structure_id: int) -> Atoms:
    path = path_for(structure_id)
    if not path.is_file():
        raise ValueError("構造が見つかりません")
    return clean(ase_read(path, format="extxyz", index=-1))


def delete(db: sqlite3.Connection, structure_id: int) -> bool:
    cursor = db.execute("DELETE FROM structures WHERE id = ?", (structure_id,))
    path_for(structure_id).unlink(missing_ok=True)
    return cursor.rowcount > 0


def default_repeat(thickness: list[float], n_atoms: int) -> list[int]:
    """スーパーセルの既定値。厚みを MIN_THICKNESS 以上にし、原子数が目安を超えるなら超えなくなるまで減らす。"""
    repeat = [max(1, math.ceil(settings.MIN_THICKNESS / t)) for t in thickness]
    while n_atoms * math.prod(repeat) > settings.WARN_ATOMS and any(r > 1 for r in repeat):
        repeat = [max(1, r - 1) for r in repeat]
    return repeat


def row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["info"] = json.loads(data["info"])
    data["default_repeat"] = default_repeat(data["info"]["thickness"], data["n_atoms"])
    return data


def view_data(atoms: Atoms, max_atoms: int = settings.MAX_VIEW_ATOMS) -> dict:
    """3D 表示用の元素・座標・セル。多すぎる場合は等間隔に間引く (セル全体の形は保たれる)。"""
    stride = max(1, -(-len(atoms) // max_atoms))
    symbols = np.array(atoms.get_chemical_symbols())[::stride]
    return {
        "symbols": symbols.tolist(),
        "positions": np.round(atoms.get_positions()[::stride], 3).tolist(),
        "cell": np.round(np.array(atoms.get_cell()), 4).tolist(),
        "n_atoms": len(atoms),
        "shown": len(symbols),
    }


def repeated_info(atoms: Atoms, repeat: tuple[int, int, int]) -> dict:
    """スーパーセルにしたときの原子数とセルの大きさ。"""
    na, nb, nc = repeat
    lengths = atoms.cell.lengths()
    thickness = StructureHandler.cell_thickness(atoms)
    return {
        "n_atoms": len(atoms) * na * nb * nc,
        "lengths": [round(float(lengths[i]) * r, 3) for i, r in enumerate(repeat)],
        "thickness": [round(float(thickness[i]) * r, 3) for i, r in enumerate(repeat)],
    }


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return round(float(value), 6)
    return value


# ---- 設定ファイル (Materials Project の API キー) ---------------------------------

def read_config() -> dict:
    try:
        return json.loads(settings.CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def write_config(config: dict) -> None:
    tmp = settings.CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(config, handle)
    os.replace(tmp, settings.CONFIG_PATH)


def mp_api_key() -> str:
    return read_config().get("mp_api_key", "")


# ---- Materials Project ------------------------------------------------------------

def _rester():
    key = mp_api_key()
    if not key:
        raise ValueError("Materials Project の API キーが未設定です。設定画面で入力してください")
    from mp_api.client import MPRester
    return MPRester(key, mute_progress_bars=True)


def mp_search(query: str, limit: int = 20) -> list[dict]:
    """組成式 (例: SiO2) か mp-id (例: mp-149) で検索する。安定なものから並べる。"""
    query = query.strip()
    fields = ["material_id", "formula_pretty", "symmetry", "energy_above_hull", "nsites"]
    try:
        with _rester() as mpr:
            if query.lower().startswith(("mp-", "mvc-")):
                docs = mpr.materials.summary.search(material_ids=[query.lower()], fields=fields)
            else:
                docs = mpr.materials.summary.search(formula=query, fields=fields)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Materials Project の検索に失敗しました ({type(exc).__name__})") from exc
    results = []
    for doc in docs:
        symmetry = doc.symmetry
        hull = doc.energy_above_hull
        results.append({
            "material_id": str(doc.material_id),
            "formula": doc.formula_pretty,
            "spacegroup": getattr(symmetry, "symbol", None) or "",
            "crystal_system": str(getattr(getattr(symmetry, "crystal_system", None), "value", "") or ""),
            "energy_above_hull": None if hull is None else round(float(hull), 4),
            "nsites": doc.nsites,
        })
    results.sort(key=lambda r: (r["energy_above_hull"] is None, r["energy_above_hull"] or 0.0, r["nsites"]))
    return results[:limit]


def mp_fetch(material_id: str, conventional: bool = True) -> Atoms:
    """mp-id の構造を取得する。既定は慣用単位胞。"""
    from pymatgen.io.ase import AseAtomsAdaptor
    try:
        with _rester() as mpr:
            structure = mpr.get_structure_by_material_id(material_id, conventional_unit_cell=conventional)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Materials Project からの取得に失敗しました ({type(exc).__name__})") from exc
    return clean(AseAtomsAdaptor.get_atoms(structure))
