#!/usr/bin/env bash
# MatSimApp の起動スクリプト
#   ./run.sh              この PC からのみアクセス可 (http://localhost:8020)
#   ./run.sh --lan        同じネットワークのスマホからもアクセス可
#   ./run.sh --tailscale  Tailscale (tailscale serve) 経由で使う。この PC だけで待ち受け、
#                         Cookie を HTTPS 専用にする
#   ./run.sh doctor       paths.env のパスと仮想環境で動くか (GPUMD がなければ CPU の NEP) を確かめる
#
# 連携するもの (venv・GPUMD・Atomsk・NEP89 など) の場所は paths.env に書く (paths.example.env を参照)。秘密は .env に書く。
# ポートは他のアプリ (マネオンズ 8000 / ゆめぐりGO 8010) と重ならない 8020 を使う。
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# 連携するものの場所と venv (paths.env)。.env と環境変数にある値が優先
source ./paths.sh
matsim_load_paths

# Python は MATSIM_VENV → 既定の仮想環境 → このフォルダの .venv → PATH の python3 の順に探す
if [[ -n "${MATSIM_VENV:-}" ]]; then
  PYTHON="$MATSIM_VENV/bin/python"
elif [[ -x "$HOME/.venv/gpumd312/bin/python" ]]; then
  PYTHON="$HOME/.venv/gpumd312/bin/python"
elif [[ -x .venv/bin/python ]]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="python3"
fi
if ! "$PYTHON" -c "import fastapi, uvicorn, matsimapp, matsimapi" 2>/dev/null; then
  echo "必要なパッケージが見つかりません ($PYTHON)。" >&2
  echo "仮想環境で 'uv pip install -r requirements.txt' を実行し、その場所を .env の MATSIM_VENV に書いてください。" >&2
  exit 1
fi

# データ置き場は MATSIM_DATA_DIR。この場所のアプリとして動かすので、既定はこのフォルダの data/
export MATSIM_DATA_DIR="${MATSIM_DATA_DIR:-$PWD/data}"
if [[ "${1:-}" == "doctor" ]]; then
  exec "$PYTHON" -m matsimapi.cli doctor
fi
exec "$PYTHON" -m matsimapp "$@"
