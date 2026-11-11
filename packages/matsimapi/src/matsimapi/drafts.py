"""構造の編集の下書き。編集を何回か重ね (一つ前に戻すこともでき)、最後に 1 回だけ構造として登録する。

下書きは一時フォルダ (TMP_DIR) に置く: draft-<名札>-<段>.xyz (段 0 が元の構造) と、手順を書いた draft-<名札>.json。
登録するまで構造の一覧には出ない。古い下書きは DRAFT_DAYS 日で消す。
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

from ase.io import read as ase_read
from ase.io import write as ase_write

from . import modeling, settings, structures

TOKEN = re.compile(r"^[0-9a-f]{16}$")
DRAFT_DAYS = 2


def _paths(token: str) -> tuple[Path, Path]:
    if not TOKEN.match(token or ""):
        raise ValueError("下書きが見つかりません")
    return settings.TMP_DIR / f"draft-{token}.json", settings.TMP_DIR / f"draft-{token}"


def _load(token: str) -> dict:
    meta, _ = _paths(token)
    if not meta.is_file():
        raise ValueError("下書きが見つかりません (古い下書きは消えます)")
    return json.loads(meta.read_text())


def _atoms(token: str, step: int):
    _, stem = _paths(token)
    return structures.clean(ase_read(f"{stem}-{step}.xyz", format="extxyz", index=-1))


def _state(token: str, info: dict, atoms) -> dict:
    return {"draft": token, "step": len(info["steps"]), "base_id": info["base_id"], "base_name": info["base_name"],
            "steps": info["steps"], "formula": atoms.get_chemical_formula(mode="metal"), "n_atoms": len(atoms),
            "view": structures.view_data(atoms)}


def _cleanup() -> None:
    limit = time.time() - DRAFT_DAYS * 86400
    for path in settings.TMP_DIR.glob("draft-*"):
        if path.stat().st_mtime < limit:
            path.unlink(missing_ok=True)


def start(db: sqlite3.Connection, structure_id: int) -> dict:
    """構造から下書きを始める。"""
    row = db.execute("SELECT * FROM structures WHERE id = ?", (structure_id,)).fetchone()
    if row is None:
        raise ValueError("構造が見つかりません")
    _cleanup()
    token = secrets.token_hex(8)
    meta, stem = _paths(token)
    atoms = structures.load(structure_id)
    ase_write(f"{stem}-0.xyz", atoms, format="extxyz")
    info = {"base_id": structure_id, "base_name": row["name"], "steps": []}
    meta.write_text(json.dumps(info, ensure_ascii=False))
    return _state(token, info, atoms)


def apply(db: sqlite3.Connection, token: str, op_key: str, options: dict) -> dict:
    """いまの下書きに編集を 1 つ重ねる。"""
    info = _load(token)
    op = modeling.get_op(op_key)
    atoms = _atoms(token, len(info["steps"]))
    others = {}
    for key in modeling.structure_refs(op):   # ほかの構造を使う編集 (重ねる・多結晶など)
        ref = (options or {}).get(key)
        if ref in (None, ""):
            continue
        if db.execute("SELECT 1 FROM structures WHERE id = ?", (int(ref),)).fetchone() is None:
            raise ValueError("選んだ構造が見つかりません")
        others[key] = structures.load(int(ref))
    result, warnings, values = modeling.build(op, atoms if op.needs_structure else None, options or {}, others)
    result = structures.clean(result)
    close = modeling.too_close(result)
    if close:
        warnings = [*warnings, close]
    info["steps"].append({"op": op.key, "label": op.label, "summary": modeling.summary_text(op, values),
                          "warnings": warnings})
    meta, stem = _paths(token)
    ase_write(f"{stem}-{len(info['steps'])}.xyz", result, format="extxyz")
    meta.write_text(json.dumps(info, ensure_ascii=False))
    return {**_state(token, info, result), "warnings": warnings}


def undo(token: str) -> dict:
    """一つ前に戻す (元の構造より前には戻らない)。"""
    info = _load(token)
    meta, stem = _paths(token)
    if info["steps"]:
        Path(f"{stem}-{len(info['steps'])}.xyz").unlink(missing_ok=True)
        info["steps"].pop()
        meta.write_text(json.dumps(info, ensure_ascii=False))
    return _state(token, info, _atoms(token, len(info["steps"])))


def view(token: str) -> dict:
    info = _load(token)
    return _state(token, info, _atoms(token, len(info["steps"])))


def save(db: sqlite3.Connection, token: str, name: str = "") -> dict:
    """いまの下書きを構造として登録する。手順は取得元に残す。"""
    info = _load(token)
    if not info["steps"]:
        raise ValueError("まだ編集していません")
    atoms = _atoms(token, len(info["steps"]))
    labels = " → ".join(step["label"] for step in info["steps"])
    detail = " / ".join(step["summary"] for step in info["steps"])
    structure_id = structures.save(db, atoms, name.strip() or f"{info['base_name']} → {labels}"[:60], "model",
                                   f"{detail} (元: {info['base_name']})")
    return {"id": structure_id, "steps": len(info["steps"]), "formula": atoms.get_chemical_formula(mode="metal")}
