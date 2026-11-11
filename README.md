# MatSim

GPUMD・SevenNet による材料シミュレーションのツール群。

| パッケージ | 役割 | 入口 |
|---|---|---|
| [`matsimapi`](packages/matsimapi) | 計算の実行 (構造・ジョブ・解析) | Python `Client`、コマンド `matsimapi`、MCP `matsimapi mcp` |
| [`matsimapp`](packages/matsimapp) | 画面 (スマホ・PC のブラウザ) | コマンド `matsimapp`、`./run.sh` |
| [`atomsk-kit`](packages/atomsk_kit) | 構造モデリングに使う Atomsk のキット | Python |

```
matsimapp (画面・ログイン) ─┐
Python スクリプト ──────────┼─→ matsimapi ─→ キュー (SQLite) ─→ ワーカー ─→ gpumd / SevenNet
LLM (MCP サーバー) ─────────┘
```

## インストール

GPUMD 本体・Atomsk・PLUMED は別にビルドする。`gpumd-toolkit` と `atomsk-kit` は PyPI にないので一緒に入れる。

```bash
# pip / uv
pip install ~/repos/GPUMD_toolkit ./packages/atomsk_kit "./packages/matsimapi[mcp,mp]" ./packages/matsimapp

# conda
conda env create -f conda/environment.yml && conda activate matsim

# 開発 (編集可能な形)
uv pip install -r requirements.txt --python ~/.venv/gpumd312/bin/python
```

配布用のファイルは `tools/build_dist.sh` で `dist/` に、conda パッケージのレシピは `conda/` にある。

## パスの設定 (paths.env) と GPUMD がないとき

連携するもの (venv・GPUMD・nep・PLUMED 版 gpumd・Atomsk・SevenNet の Python・NEP89 のファイル・データ置き場) の場所は
`paths.env` の 1 か所に書く (ひな形は `paths.example.env`)。`run.sh` と `matsimapi` (MCP・CLI・ワーカー) が読み、シェルの
環境変数と `.env` にある値が優先する。場所は `MATSIM_PATHS`、なければソースから使うときはこのフォルダ、インストールしたときは
`~/.config/matsim/paths.env`。秘密 (MP_API_KEY など) は `.env` に書く。書いた値で動くかは `./run.sh doctor` (`matsimapi doctor`)。

GPUMD がない人 (GPU がない PC など) も使える。`MATSIM_MD_ENGINE=auto` (既定) で GPUMD が見つからなければ、NEP の MD・静的計算
(構造緩和・弾性定数・状態方程式)・フォノン分散を CPU (calorine の CPUNEP) で ASE を使って計算する (`cpu` で常に CPU)。
CPU は GPUMD より桁違いに遅い (Cu 108 原子で約 2 万 原子×ステップ/秒) ので、数百原子・数千〜数万ステップ程度に。拡散・熱伝導・
融点などの GPUMD だけの計算と、ファインチューニング・PLUMED は、理由をつけて止まる。結果のグラフ・最終構造・構造の時間変化は
GPUMD のときと同じに使える。試験は `01_test/test_cpu_nep.py`。

## 使い方

```bash
./run.sh                     # 画面 (http://localhost:8020)
matsimapi info               # 計算環境の確認
matsimapi mcp                # MCP サーバー
```

```python
from matsimapi import Client

client = Client()
cu = client.add_sample("Cu")
job = client.submit("md", cu, repeat=(3, 3, 3), stages=[{"ensemble": "nvt", "steps": 1000, "temperature": 300}])
print(job.wait().thermo()["summary"])
```

画面・Python・MCP はデータ置き場 (`MATSIM_DATA_DIR`) を共有する。詳しくはアプリのマニュアル第 6 部。
