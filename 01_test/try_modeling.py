"""モデリングの全操作を既定値 (と一部の代表的な値) で実行して、構造ができるかを確かめる。

    MATSIM_DATA_DIR=/tmp/matsim_try ~/.venv/gpumd312/bin/python 01_test/try_modeling.py
"""
import base64
import os
import sys
import time
from pathlib import Path

import numpy as np
from ase.build import bulk

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("MATSIM_DATA_DIR", str(ROOT / "01_test" / "data_model"))

from matsimapi import modeling, settings  # noqa: E402

settings.TMP_DIR.mkdir(parents=True, exist_ok=True)

cu = bulk("Cu", "fcc", a=3.615, cubic=True)
mgo = bulk("MgO", "rocksalt", a=4.212, cubic=True)
big = cu.repeat([8, 8, 8])                     # 転位・き裂など、ある程度の大きさが要るもの
cube_stl = "solid c\n" + "".join(
    f"facet normal 0 0 0\nouter loop\n" + "".join(f"vertex {v[0]} {v[1]} {v[2]}\n" for v in tri) + "endloop\nendfacet\n"
    for tri in [  # 1×1×1 の立方体 (12 枚の三角形)
        [(0, 0, 0), (1, 1, 0), (1, 0, 0)], [(0, 0, 0), (0, 1, 0), (1, 1, 0)],
        [(0, 0, 1), (1, 0, 1), (1, 1, 1)], [(0, 0, 1), (1, 1, 1), (0, 1, 1)],
        [(0, 0, 0), (1, 0, 0), (1, 0, 1)], [(0, 0, 0), (1, 0, 1), (0, 0, 1)],
        [(0, 1, 0), (1, 1, 1), (1, 1, 0)], [(0, 1, 0), (0, 1, 1), (1, 1, 1)],
        [(0, 0, 0), (0, 0, 1), (0, 1, 1)], [(0, 0, 0), (0, 1, 1), (0, 1, 0)],
        [(1, 0, 0), (1, 1, 0), (1, 1, 1)], [(1, 0, 0), (1, 1, 1), (1, 0, 1)]]) + "endsolid c\n"

CASES = {
    "substitute": (cu.repeat(3), {"from": "Cu", "to": "Ni", "amount": "percent", "percent": 25}),
    "vacancy": (cu.repeat(3), {}),
    "random_alloy": (cu.repeat(3), {}),
    "interstitial": (cu.repeat(3), {"count": 3}),
    "swap_species": (mgo, {"sp1": "Mg", "sp2": "O"}),
    "disturb": (cu, {}),
    "remove_doubles": (cu, {}),
    "separate": (cu, {}),
    "create": (None, {}),
    "create#hcp": (None, {"lattice": "hcp", "a": 3.21, "c": 5.21, "elements": "Mg", "nx": 2, "ny": 2, "nz": 2}),
    "create#orient": (None, {"orient": True}),
    "nanotube": (None, {}),
    "supercell": (cu, {}),
    "orient": (cu, {}),
    "orthogonal": (bulk("Cu", "fcc", a=3.615), {}),
    "tidy": (cu, {"action": "primitive"}),
    "vacuum": (cu.repeat([3, 3, 4]), {}),
    "deform": (cu, {"poisson": 0.3}),
    "rotate": (cu, {}),
    "shift": (cu, {}),
    "mirror": (cu, {}),
    "swap_axes": (cu, {}),
    "torsion": (cu.repeat([4, 4, 10]), {}),
    "roll": (cu.repeat([12, 2, 1]), {}),
    "cut": (cu.repeat(4), {}),
    "region": (cu.repeat(6), {"radius": 6}),
    "region#box": (cu.repeat(6), {"shape": "box", "action": "keep"}),
    "nanoparticle": (cu, {"radius": 8}),
    "nanoparticle#wire": (cu, {"shape": "wire", "radius": 8}),
    "stl": (cu.repeat(6), {"stl": {"name": "cube.stl", "data": base64.b64encode(cube_stl.encode()).decode()},
                           "placement": "fill", "action": "remove"}),
    "dislocation": (big, {}),
    "dislocation#edge": (big, {"dtype": "edge"}),
    "loop": (big, {}),
    "crack": (big, {}),
    "bicrystal": (cu, {}),
    "merge": (cu.repeat([3, 3, 2]), {"other": 0}),
    "polycrystal": (cu, {"lx": 40, "ly": 40, "lz": 40, "grains": 4}),
    "polycrystal#2d": (cu, {"dim": "2d", "grains": 4, "lx": 50, "ly": 50, "nz": 2}),
    "granular": (cu, {"lx": 80, "ly": 80, "lz": 10, "grains": 4, "radius": 14}),
    "granular#hex": (cu, {"layout": "hex", "lx": 70, "ly": 70, "lz": 8, "radius": 12, "filler": "none"}),
    "granular#crystal": (cu, {"lx": 70, "ly": 70, "lz": 8, "grains": 3, "radius": 12, "filler": "crystal", "matrix": 0}),
    "granular#3d": (cu, {"lx": 70, "ly": 70, "lz": 15, "grains": 3, "radius": 12, "orientation": "random3d"}),
}
OTHERS = {"merge": {"other": bulk("Ni", "fcc", a=3.52, cubic=True).repeat([3, 3, 2])},
          "granular#crystal": {"matrix": bulk("Ni", "fcc", a=3.52, cubic=True)}}

failed = []
for name, (atoms, raw) in CASES.items():
    key = name.split("#")[0]
    op = modeling.get_op(key)
    start = time.time()
    try:
        result, warnings, o = modeling.build(op, atoms, raw, OTHERS.get(name, {}))
        close = modeling.close_pairs(result)
        print(f"OK  {name:22s} {len(result):7d} 原子 {result.get_chemical_formula(mode='metal'):14s} "
              f"セル {np.round(result.cell.lengths(), 1)} 近接{close} {time.time() - start:5.1f}s")
        for w in warnings:
            print("      ・", w)
        if key == "dislocation":
            moved = np.abs(result.positions - atoms.positions).max() if len(result) == len(atoms) else None
            print("      最大変位", moved)
    except Exception as exc:
        failed.append(name)
        print(f"NG  {name:22s} {type(exc).__name__}: {exc}")
print("失敗:", failed or "なし")
