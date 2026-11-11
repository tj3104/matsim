"""atomsk_kit: Atomsk を Python (ASE の Atoms) から使いやすくするキット。

構造は ase.Atoms で受け取って ase.Atoms で返す。Atomsk の呼び出し (一時ファイル・エラー処理) はキットが受け持つ。

    from atomsk_kit import Pipeline, box, create, granular, substitute
    cu = create("fcc", 3.615, "Cu")                                     # atomsk --create
    film = (Pipeline(cu).duplicate(10, 10, 4).cell("z", "add", 15)       # オプションをつないで 1 回で実行
            .center().run())
    nicu = substitute(cu.repeat(3), "Cu", "Ni", fraction=0.25, seed=1)  # Python 実装 (シードで再現)
    grains = granular(cu, (150, 150, 20), grains=10, radius=18, seed=1)

Atomsk の場所は環境変数 ATOMSK_BIN、PATH の atomsk、~/repos/atomsk/src/atomsk の順に探す。
configure(binary=..., timeout=..., workdir=...) で変えられる。
"""
from .edit import disturb, interstitial, random_alloy, rotate, substitute, swap_species, vacancy
from .geometry import (close_pairs, composition_of, is_orthorhombic, nearest_distance, number_density,
                       parse_composition, sort_atoms, split_counts)
from .micro import granular, nanoparticle
from .modes import LATTICES, bicrystal, create, merge, nanotube, polycrystal
from .pipeline import Pipeline, box, miller
from .runner import OUT, AtomskError, available, configure, config, run, version

__version__ = "0.1.0"

__all__ = [
    "AtomskError", "LATTICES", "OUT", "Pipeline", "available", "bicrystal", "box", "close_pairs", "composition_of",
    "config", "configure", "create", "disturb", "granular", "interstitial", "is_orthorhombic", "merge", "miller",
    "nanoparticle", "nanotube", "nearest_distance", "number_density", "parse_composition", "polycrystal",
    "random_alloy", "rotate", "run", "sort_atoms", "split_counts", "substitute", "swap_species", "vacancy", "version",
]
