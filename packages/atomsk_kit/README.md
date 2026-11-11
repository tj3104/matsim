# atomsk_kit

[Atomsk](https://atomsk.univ-lille.fr/) を Python (ASE の `Atoms`) から使いやすくするキットです。
MatSimApp の構造モデリングはこのキットを通して Atomsk を使っています。

## インストール

```bash
uv pip install -e packages/atomsk_kit --python ~/.venv/gpumd312/bin/python
```

Atomsk 本体は別に用意します (例: `~/repos/atomsk` で `make atomsk`)。場所は次の順に探します。

1. 環境変数 `ATOMSK_BIN`
2. PATH の `atomsk`
3. `~/repos/atomsk/src/atomsk`

`atomsk_kit.configure(binary=..., timeout=..., workdir=...)` でも変えられます。

## 使い方

```python
from ase.build import bulk
from atomsk_kit import Pipeline, box, create, granular, polycrystal, random_alloy, substitute

cu = create("fcc", 3.615, "Cu")                          # atomsk --create
nacl = create("rocksalt", 5.64, "Na Cl", duplicate=(2, 2, 2))

# Atomsk のオプションをつないで 1 回の呼び出しで実行する
slab = (Pipeline(cu).duplicate(8, 8, 8)
        .dislocation(box(0.501), box(0.501), "screw", xi="z", n="y", b=2.556)
        .cell("z", "add", 15).center()
        .run())

# 乱数を使う編集は Python 実装 (seed で再現できる)
nicu = substitute(cu.repeat(3), "Cu", "Ni", fraction=0.25, seed=1)
hea = random_alloy(cu.repeat(3), "Cu:1,Ni:1,Co:1,Fe:1", seed=1)

# Voronoi 多結晶 (粒の位置・方位をシードから決めて Atomsk に渡す)
poly = polycrystal(cu, box=(60, 60, 60), grains=8, seed_rng=1)

# グラニュラー構造: 円柱の結晶粒 (Atomsk の -select cylinder で切り出す) + すき間に原子を充填
film = granular(cu, (150, 150, 20), grains=10, radius=18, filler="random", seed=1)
```

多くの関数は `log=[]` を渡すと、注意書き (Atomsk の警告・置いた原子数など) をそのリストに書き足します。
失敗したときは `atomsk_kit.AtomskError` (Atomsk のエラー) か `ValueError` (入力の誤り) を投げます。

## 中身

| モジュール | 内容 |
|---|---|
| `runner` | Atomsk の呼び出し (`run`)、設定 (`configure`)、`AtomskError` |
| `pipeline` | `Pipeline`: `-duplicate` `-orient` `-select` `-dislocation` `-crack` などをつなぐ |
| `modes` | `create` `nanotube` `merge` `polycrystal` `bicrystal` |
| `edit` | `substitute` `vacancy` `random_alloy` `interstitial` `disturb` `swap_species` |
| `micro` | `nanoparticle` `granular` |
| `geometry` | 組成・最近接距離・数密度などの道具 |

テストは MatSimApp の `01_test/test_atomsk_kit.py` にあります。
