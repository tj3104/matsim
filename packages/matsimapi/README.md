# MatSimAPI

GPUMD・SevenNet による材料シミュレーションの実行エンジン。構造の用意、計算の投入、ジョブのキュー、結果の解析を担当する。

```python
from matsimapi import Client

client = Client()                          # データ置き場は MATSIM_DATA_DIR または Client(data_dir=...)
cu = client.add_structure("POSCAR")        # ファイル・ASE の Atoms・client.add_sample("Cu")
print(client.preview("md", cu, stages=[{"ensemble": "nvt", "steps": 1000, "temperature": 300}]))

job = client.submit("md", cu, repeat=(3, 3, 3), stages=[{"ensemble": "nvt", "steps": 1000, "temperature": 300}])
job.wait()
print(job.results())                       # 要点の数値と表の一覧
print(job.dataset("thermo")["data"].keys())
print(job.analyze("cna").wait().result())  # 解析もジョブとして動かす
```

- 計算の種類と入力欄: `client.kinds()`。MD は `stages`、それ以外は `options` で条件を渡す。
- 温度スキャンの `submit` は `Job` のリストを返す。
- 解析: `voronoi` / `cna` / `coord` / `sro` / `poly`、`job.distribution(...)`、`client.arrhenius([...])`。

## コマンド

`matsimapi info | kinds | structures | potentials | jobs | job <id> | worker | mcp`

## ワーカー

GPU は 1 枚なので、ジョブを動かす係はデータ置き場ごとに 1 プロセスだけ (`worker.lock` で決める)。常に独立した
`matsimapi worker` が務め、画面・Python・MCP はキューに入れるだけ。画面のサーバーが起動したワーカーは、画面を止めても残る。
画面がなければ Python や MCP が裏で起動する (こちらはジョブがなくなって 5 分たつと終わる)。

## MCP サーバー

`matsimapi mcp` で標準入出力の MCP サーバーが動く。`pip install matsimapi[mcp]` が必要。

```json
{"mcpServers": {"matsimapi": {"command": "matsimapi", "args": ["mcp"],
                              "env": {"MATSIM_DATA_DIR": "/path/to/data"}}}}
```

認証がないのでネットワークには公開しない。接続した LLM は計算の投入・削除と、このマシンのファイルの読み込みができる。

## 必要なもの

- GPUMD (`MATSIM_GPUMD_BIN`、既定は `~/repos/GPUMD/src/gpumd`)
- gpumd-toolkit、atomsk-kit
- 任意: Atomsk、PLUMED 対応の gpumd、SevenNet (`matsimapi[ml]`)、Materials Project (`matsimapi[mp]`)
