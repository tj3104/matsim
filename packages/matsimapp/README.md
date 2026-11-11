# MatSimApp

スマホのブラウザから GPUMD の計算を投入・監視・結果確認できる web UI。計算そのものは [matsimapi](../matsimapi) が担当する。

```bash
matsimapp              # この PC だけ (http://localhost:8020)
matsimapp --lan        # 同じネットワークから
matsimapp --tailscale  # Tailscale (tailscale serve) 経由。Cookie を HTTPS 専用にする
```

データ置き場は `--data-dir` か `MATSIM_DATA_DIR`。使い方はアプリの中のマニュアル (`/manual/manual.html`)。
