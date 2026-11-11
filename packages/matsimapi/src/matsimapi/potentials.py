"""ポテンシャルの登録と、構造の元素に対応しているかの確認。

NEP (nep.txt、GPUMD で計算) と、SevenNet の学習済みモデル (パスが "sevennet:<モデル>[:<modal>]"、ASE で計算。
ase_engine.py) を扱う。SevenNet はファイルではなく、SevenNet の入った Python (settings.ML_PYTHON) があれば使える。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ase.data import chemical_symbols

from . import settings

VALID_SYMBOLS = set(chemical_symbols[1:])


def parse_elements(path: Path) -> list[str]:
    """nep.txt の 1 行目 (例: ``nep4_zbl 2 Cu Ni``) から対応元素を読む。"""
    try:
        with open(path, "r", encoding="ascii", errors="strict") as handle:
            tokens = handle.readline(4096).split()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("NEP ポテンシャルのファイルとして読めません") from exc
    if len(tokens) < 3 or not tokens[0].startswith("nep") or not tokens[1].isdigit():
        raise ValueError("NEP ポテンシャル (nep.txt) の形式ではありません")
    count = int(tokens[1])
    elements = tokens[2:2 + count]
    if count < 1 or len(elements) != count or not set(elements) <= VALID_SYMBOLS:
        raise ValueError("NEP ポテンシャルの元素の行を読めません")
    return elements


# SevenNet の学習済みモデルが対応する元素: 学習データ (MPtrj・sAlex・OMat24) の 89 元素 (H〜Pu から Po・At・Rn・Fr・Ra を除く)
SEVENNET_ELEMENTS = [s for s in chemical_symbols[1:95] if s not in ("Po", "At", "Rn", "Fr", "Ra")]


def engine_of(path: str) -> str:
    return "sevennet" if str(path).startswith("sevennet:") else "gpumd"


def is_available(path: str) -> bool:
    if engine_of(path) == "sevennet":
        return settings.ML_PYTHON.is_file()
    return Path(path).is_file()


def sync_builtin(db: sqlite3.Connection) -> None:
    """設定にある既定のポテンシャルを登録する。ファイルがなければ登録しない。"""
    for name, path in settings.BUILTIN_POTENTIALS.items():
        if not path.is_file():
            continue
        try:
            elements = parse_elements(path)
        except ValueError:
            continue
        db.execute(
            "INSERT INTO potentials (name, path, elements, builtin) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(name) DO UPDATE SET path = excluded.path, elements = excluded.elements",
            (name, str(path), json.dumps(elements)))
    # SevenNet の学習済みモデル (NEP のあとに並べる: 既定で選ばれるのは NEP のまま)
    if settings.ML_PYTHON.is_file():
        for name, spec in settings.SEVENNET_MODELS.items():
            db.execute(
                "INSERT INTO potentials (name, path, elements, builtin) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(name) DO UPDATE SET path = excluded.path, elements = excluded.elements",
                (name, spec, json.dumps(SEVENNET_ELEMENTS)))


def add_upload(db: sqlite3.Connection, name: str, tmp_path: Path) -> int:
    """アップロードされた nep.txt を検証して登録する。tmp_path は登録後に移動される。"""
    elements = parse_elements(tmp_path)
    name = name.strip()[:60]
    if not name:
        raise ValueError("ポテンシャルの名前を入力してください")
    if db.execute("SELECT 1 FROM potentials WHERE name = ?", (name,)).fetchone():
        raise ValueError("同じ名前のポテンシャルがあります")
    potential_id = db.execute("INSERT INTO potentials (name, path, elements) VALUES (?, '', ?)",
                              (name, json.dumps(elements))).lastrowid
    path = settings.POTENTIAL_DIR / f"{potential_id}.txt"
    tmp_path.replace(path)
    db.execute("UPDATE potentials SET path = ? WHERE id = ?", (str(path), potential_id))
    return potential_id


def delete(db: sqlite3.Connection, potential_id: int) -> None:
    row = db.execute("SELECT path, builtin FROM potentials WHERE id = ?", (potential_id,)).fetchone()
    if row is None:
        raise ValueError("ポテンシャルが見つかりません")
    if row["builtin"]:
        raise ValueError("既定のポテンシャルは削除できません")
    db.execute("DELETE FROM potentials WHERE id = ?", (potential_id,))
    path = Path(row["path"])
    # アプリが保存したファイルだけを消す
    if path.parent == settings.POTENTIAL_DIR:
        path.unlink(missing_ok=True)
        path.with_suffix(".restart").unlink(missing_ok=True)   # ファインチューニングでできたもの (学び直しの出発点)


def row_to_dict(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"], "elements": json.loads(row["elements"]),
            "builtin": bool(row["builtin"]), "available": is_available(row["path"]), "engine": engine_of(row["path"])}


def get(db: sqlite3.Connection, potential_id: int) -> dict:
    row = db.execute("SELECT * FROM potentials WHERE id = ?", (potential_id,)).fetchone()
    if row is None:
        raise ValueError("ポテンシャルが見つかりません")
    data = row_to_dict(row)
    data["path"] = row["path"]
    if not data["available"]:
        raise ValueError("SevenNet の入った Python が見つかりません (settings.ML_PYTHON)" if data["engine"] == "sevennet"
                         else "ポテンシャルのファイルが見つかりません")
    return data


def missing_elements(symbols: list[str], elements: list[str]) -> list[str]:
    return sorted(set(symbols) - set(elements))
