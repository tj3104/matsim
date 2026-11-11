"""atomsk_kit 単体のテスト。Atomsk を実際に呼んで、結果の原子数・組成・セル・再現性を確かめる。

    ~/.venv/gpumd312/bin/python 01_test/test_atomsk_kit.py
"""
import sys

import numpy as np
from ase.build import bulk

import atomsk_kit as ak

checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    detail = str(detail)
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def raises(fn, kind=Exception):
    try:
        fn()
    except kind as exc:
        return str(exc)
    return None


def main():
    check("Atomsk が見つかる", ak.available(), str(ak.config.binary))
    cu = ak.create("fcc", 3.615, "Cu")
    check("create fcc", len(cu) == 4 and np.allclose(cu.cell.lengths(), 3.615))
    nacl = ak.create("rocksalt", 5.64, "Na Cl", duplicate=(2, 2, 2))
    check("create 岩塩型 (元素 2 つ)", len(nacl) == 64 and nacl.get_chemical_formula() == "Cl32Na32")
    hcp = ak.create("hcp", 3.21, ["Mg"], c=5.21)
    check("create hcp (c あり)", len(hcp) == 2, hcp.get_chemical_formula())
    oriented = ak.create("fcc", 3.615, "Cu", orient=("[11-2]", "[1-10]", "[111]"))
    check("create 方位つき", len(oriented) == 6)
    check("create 元素数の誤り", raises(lambda: ak.create("perovskite", 4.0, "Sr"), ValueError))
    check("create c がない", raises(lambda: ak.create("hcp", 3.2, "Mg"), ValueError))
    check("nanotube", len(ak.nanotube(2.46, 5, 5, "C", nz=2)) == 40)

    p = ak.Pipeline(cu).duplicate(2, 2, 2)
    check("Pipeline duplicate", len(p.run()) == 32 and p.args[0] == "-duplicate")
    slab = ak.Pipeline(cu).duplicate(3, 3, 3).cell("z", "add", 15).center().run()
    check("Pipeline 真空層", abs(slab.cell.lengths()[2] - (3 * 3.615 + 15)) < 1e-3)
    o = ak.Pipeline(cu).orient(("[100]", "[010]", "[001]"), ("[11-2]", "[1-10]", "[111]")).orthogonal_cell().run()
    check("Pipeline orient + orthogonal_cell", len(o) == 24, str(np.round(o.cell.lengths(), 2)))
    big = cu.repeat([8, 8, 8])
    ball = ak.Pipeline(big).select_sphere(False, ak.box(0.5), ak.box(0.5), ak.box(0.5), 8).remove_selected().run()
    check("Pipeline 球で切り抜き", 100 < len(ball) < 250, len(ball))
    disl = ak.Pipeline(big).dislocation(ak.box(0.501), ak.box(0.501), "screw", "z", "y", 2.556).run()
    check("Pipeline らせん転位 (原子数は変わらない)", len(disl) == len(big))
    check("Pipeline 刃状転位にポアソン比が要る",
          raises(lambda: ak.Pipeline(big).dislocation(0, 0, "edge", "z", "y", 2.5), ValueError))
    log = []
    check("Atomsk のエラーは AtomskError",
          raises(lambda: ak.Pipeline(cu).option("-nonexistent-option").run(log=log), ak.AtomskError))

    cu3 = cu.repeat(3)
    a = ak.substitute(cu3, "Cu", "Ni", fraction=0.25, seed=3)
    b = ak.substitute(cu3, "Cu", "Ni", fraction=0.25, seed=3)
    check("substitute 25%", a.get_chemical_formula() == "Cu81Ni27")
    check("同じシードなら同じ結果", a.get_chemical_symbols() == b.get_chemical_symbols())
    check("元の構造は変えない", cu3.get_chemical_formula() == "Cu108")
    check("vacancy count", len(ak.vacancy(cu3, count=5, seed=1)) == 103)
    hea = ak.random_alloy(cu3, "Cu:1,Ni:1,Co:1,Fe:1", seed=1)
    check("random_alloy (個数がちょうど合う)", hea.get_chemical_formula() == "Co27Cu27Fe27Ni27")
    check("interstitial", len(ak.interstitial(cu3, "H", 3, min_dist=1.5, seed=1)) == 111)
    mgo = bulk("MgO", "rocksalt", a=4.212, cubic=True)
    swapped = ak.swap_species(mgo, "Mg", "O")
    check("swap_species", swapped.get_chemical_symbols() == [{"Mg": "O", "O": "Mg"}[s] for s in mgo.get_chemical_symbols()])

    fe = bulk("Fe", "bcc", a=2.863, cubic=True)
    rot = ak.rotate(fe, "z", 16)
    from ase.neighborlist import neighbor_list
    d = neighbor_list("d", rot.repeat((3, 3, 3)), 2.6)
    check("rotate はセルごと回す (結晶が崩れない)", np.allclose(d, 2.4794, atol=1e-3) and abs(rot.cell[0, 1]) > 0.1,
          f"最近接 {d.min():.4f}〜{d.max():.4f} Å")

    merged = ak.merge([cu.repeat([3, 3, 2]), bulk("Ni", "fcc", a=3.52, cubic=True).repeat([3, 3, 2])], stack="z", scale=True)
    check("merge (積む)", len(merged) == 144 and merged.cell.lengths()[2] > 13)
    bi = ak.bicrystal(cu, ("[100]", "[013]", "[0-31]"), ("[100]", "[0-13]", "[031]"), stack="y", size=(20, 60, 20))
    check("bicrystal", len(bi) > 1000 and ak.close_pairs(bi) == 0, f"{len(bi)} 原子")
    p1 = ak.polycrystal(cu, (40, 40, 40), 4, seed_rng=5)
    p2 = ak.polycrystal(cu, (40, 40, 40), 4, seed_rng=5)
    check("polycrystal (同じシードなら同じ)", len(p1) == len(p2) and np.allclose(p1.positions, p2.positions), len(p1))
    col = ak.polycrystal(cu, (50, 50, 0), 4, columnar=True, nz=2, seed_rng=1)
    check("polycrystal 柱状", abs(col.cell.lengths()[2] - 2 * 3.615) < 1e-3)

    np_ = ak.nanoparticle(cu, 8, vacuum=5)
    check("nanoparticle", 100 < len(np_) < 250 and np.allclose(np_.cell.lengths(), 26))
    log = []
    g = ak.granular(cu, (80, 80, 10), grains=4, radius=14, seed=2, log=log)
    check("granular", len(g) > 4000 and ak.close_pairs(g) == 0, f"{len(g)} 原子")
    g2 = ak.granular(cu, (80, 80, 10), grains=4, radius=14, seed=2)
    check("granular (同じシードなら同じ)", np.allclose(g.positions, g2.positions))
    check("granular の注意書き", any("面積率" in m for m in log), log[:1])
    dense = ak.granular(cu, (100, 100, 10), grains=6, radius=15, filler="none", seed=1)
    check("granular 詰めた配置 (押し広げ)", len(dense) > 2000)
    check("granular 詰めすぎはエラー",
          raises(lambda: ak.granular(cu, (60, 60, 10), grains=10, radius=15, seed=1), ValueError))

    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} OK" + (f"  失敗: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
