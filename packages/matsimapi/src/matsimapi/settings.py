"""パスや上限などの設定。

連携するもの (GPUMD・nep・PLUMED 版 gpumd・Atomsk・SevenNet の Python・NEP89 のファイル) の場所と仮想環境は
paths.env (ひな形は MatSim のフォルダの paths.example.env) に書く。読む順は 環境変数 (.env を含む) → paths.env → 下の既定。
paths.env の場所は MATSIM_PATHS、なければソースから使うときはリポジトリの paths.env、インストールしたときは
~/.config/matsim/paths.env。確かめるのは matsimapi doctor。
GPUMD がなければ (MATSIM_MD_ENGINE=auto)、NEP の MD・静的計算・フォノン分散は CPU (calorine の CPUNEP) で計算する。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _paths_file() -> Path:
    if os.environ.get("MATSIM_PATHS"):
        return Path(os.environ["MATSIM_PATHS"]).expanduser()
    repo = PACKAGE_DIR.parents[3]   # <repo>/packages/matsimapi/src/matsimapi
    if (repo / "packages" / "matsimapi").is_dir():
        return repo / "paths.env"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "matsim" / "paths.env"


PATHS_FILE = _paths_file()


def load_paths(path: Path = PATHS_FILE) -> dict[str, str]:
    """paths.env を読み、まだ環境変数にない値だけ os.environ に入れる (計算の子プロセスにも渡る)。読んだ値を返す。
    書き方は KEY=値。# の行と空行は飛ばし、~ と $変数 (上の行のものも) を展開する。"""
    found: dict[str, str] = {}
    if not path.is_file():
        return found
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            raise ValueError(f"{path}:{number}: KEY=値 の形ではありません: {raw}")
        value = value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()   # 行の終わりのコメント
        value = os.path.expanduser(os.path.expandvars(value))
        found[key] = value
        os.environ.setdefault(key, value)
    return found


load_paths()


def _default_data_dir() -> Path:
    """データ置き場の既定。MATSIM_DATA_DIR がなければ、ソースから使うときはリポジトリの data/、
    pip / conda でインストールしたときは ~/.local/share/matsim にする。"""
    repo = PACKAGE_DIR.parents[3]   # <repo>/packages/matsimapi/src/matsimapi
    if (repo / "packages" / "matsimapi").is_dir():
        return repo / "data"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "matsim"


# 書き出すものはすべてこの下に置く (set_data_dir が下の 7 つをまとめて決める)
DATA_DIR = DB_PATH = STRUCTURE_DIR = POTENTIAL_DIR = JOB_DIR = TMP_DIR = CONFIG_PATH = Path()


def set_data_dir(path: str | Path) -> None:
    """データ置き場を切り替える。MatSimApp・Python・MCP で同じ場所を指せば、構造やジョブを共有できる。"""
    global DATA_DIR, DB_PATH, STRUCTURE_DIR, POTENTIAL_DIR, JOB_DIR, TMP_DIR, CONFIG_PATH
    DATA_DIR = Path(path).expanduser().resolve()
    os.environ["MATSIM_DATA_DIR"] = str(DATA_DIR)   # 裏で起動するワーカーなどの子プロセスも同じ場所を使う
    DB_PATH = DATA_DIR / "app.db"
    STRUCTURE_DIR = DATA_DIR / "structures"
    POTENTIAL_DIR = DATA_DIR / "potentials"
    JOB_DIR = DATA_DIR / "jobs"
    TMP_DIR = DATA_DIR / "tmp"
    CONFIG_PATH = DATA_DIR / "config.json"


set_data_dir(os.environ.get("MATSIM_DATA_DIR") or _default_data_dir())

# GPUMD 本体。読むだけで、ビルドや書き換えはしない
GPUMD_DIR = Path(os.environ.get("MATSIM_GPUMD_DIR", Path.home() / "repos" / "GPUMD"))
GPUMD_BIN = Path(os.environ.get("MATSIM_GPUMD_BIN", GPUMD_DIR / "src" / "gpumd"))
# PLUMED 対応でビルドした gpumd (~/repos/plumed/README.md)。PLUMED を使う種類だけこちらで動かす
GPUMD_PLUMED_BIN = Path(os.environ.get("MATSIM_GPUMD_PLUMED_BIN",
                                       Path.home() / "repos" / "plumed" / "gpumd-plumed" / "src" / "gpumd"))
# PLUMED の CV の計算に使うスレッド数 (GPU の MD に対して CPU 側が遅くなりやすいので並列にする)
PLUMED_THREADS = int(os.environ.get("MATSIM_PLUMED_THREADS", max(1, min(14, (os.cpu_count() or 2) - 2))))

# GNN 系の汎用力場 (SevenNet) の計算に使う Python。torch・sevenn・phonopy の入った環境 (既定はアプリと同じ環境)。
# SevenNet のポテンシャルを選んだ MD・静的計算・フォノン分散は、この Python で engines/ase_runner.py を別のプロセスとして動かす
ML_PYTHON = Path(os.environ.get("MATSIM_ML_PYTHON", sys.executable))
ASE_RUNNER = PACKAGE_DIR / "engines" / "ase_runner.py"
# NEP のファインチューニング (SevenNet を先生にした蒸留 + 能動学習、finetune.py)。繰り返しは ML_PYTHON で ft_runner.py が動かす
FT_RUNNER = PACKAGE_DIR / "engines" / "ft_runner.py"
NEP_BIN = Path(os.environ.get("MATSIM_NEP_BIN", GPUMD_DIR / "src" / "nep"))
# nep は学習の計算を GPU に合わせて nvcc でその場でコンパイルする。PATH の /usr/bin/nvcc (CUDA 12.0) は RTX 50 系 (sm_120) に
# 対応しておらず、使えないと遅い汎用の計算になる (約 2 倍遅い) ので、GPUMD のビルドに使った CUDA 13 の bin を先に置く
CUDA_BIN = os.environ.get("MATSIM_CUDA_BIN", "/usr/local/cuda/bin" if Path("/usr/local/cuda/bin/nvcc").is_file() else "")
# NEP89 をファインチューニングするための restart (GPUMD に付いてくる。nep89_20250409.txt と対)
NEP89_RESTART = Path(os.environ.get("MATSIM_NEP89_RESTART",
                                    GPUMD_DIR / "potentials" / "nep" / "nep89_20250409" / "nep89_20250409.restart"))
# 一覧に並べる SevenNet の学習済みモデル: 名前 → "sevennet:<モデル>[:<modal (学習データの計算条件)>]"
SEVENNET_MODELS = {
    "SevenNet-0 (7net-0、速い)": "sevennet:7net-0",
    "SevenNet-MF-ompa (MP と同じ PBE)": "sevennet:7net-mf-ompa:mpa",
    "SevenNet-MF-ompa (OMat24)": "sevennet:7net-mf-ompa:omat24",
    "SevenNet-omat (OMat24)": "sevennet:7net-omat",
    # SevenNet-Nano: 7net-Omni (mpa) から蒸留した軽いモデル。数字はカットオフ [Å]。おすすめは 5.5。
    # PyPI の sevenn 0.13.0 にはまだないので、GitHub の sevenn が要る (初めて使うときにチェックポイントを自動で落とす)
    "SevenNet-Nano 5.5 (軽量、おすすめ)": "sevennet:7net-nano-5.5",
    "SevenNet-Nano 4.5 (軽量、最も速い)": "sevennet:7net-nano-4.5",
    "SevenNet-Nano 5.0 (軽量)": "sevennet:7net-nano-5.0",
    "SevenNet-Nano 6.0 (軽量、長いカットオフ)": "sevennet:7net-nano-6.0",
}

# MD・静的計算・フォノン分散で NEP を動かすもの: auto (GPUMD があれば GPUMD、なければ CPU) / gpumd / cpu。
# CPU は calorine の CPUNEP を ASE で動かす (engines/ase_runner.py、ML_PYTHON に calorine が要る)。GPUMD より桁違いに遅い
MD_ENGINE = os.environ.get("MATSIM_MD_ENGINE", "auto").strip().lower()
if MD_ENGINE not in ("auto", "gpumd", "cpu"):
    raise ValueError("MATSIM_MD_ENGINE は auto / gpumd / cpu のどれかです")


def gpumd_available() -> bool:
    return GPUMD_BIN.is_file() and os.access(GPUMD_BIN, os.X_OK)


def cpu_nep() -> bool:
    """NEP を CPU (calorine) で計算するか。"""
    return MD_ENGINE == "cpu" or (MD_ENGINE == "auto" and not gpumd_available())


# 構造モデリングに使う Atomsk。~/repos/atomsk でビルドしたものを既定にする
ATOMSK_BIN = Path(os.environ.get("MATSIM_ATOMSK_BIN", Path.home() / "repos" / "atomsk" / "src" / "atomsk"))
ATOMSK_TIMEOUT = int(os.environ.get("MATSIM_ATOMSK_TIMEOUT", 300))   # 1 回の呼び出しの上限 [秒]

# 最初から選べるポテンシャル (読むだけ。コピーはしない)
BUILTIN_POTENTIALS = {
    "NEP89 (汎用・89元素)": Path(os.environ.get(
        "MATSIM_NEP89",
        Path.home() / "repos" / "GPUMD-Tutorials" / "benchmark" / "01_CuMoTaVW" / "nep89_20250409.txt")),
}

# 繰り返しを指定しないとき、セルの厚みがこの長さ [Å] 以上になるようスーパーセルにする
MIN_THICKNESS = 12.0
# 原子数がこれを超えたら警告を出す (実行はできる)
WARN_ATOMS = int(os.environ.get("MATSIM_WARN_ATOMS", 10_000))
# サーバーが構造を組み立てられなくなるのを防ぐための、桁違いの入力に対する最後の歯止め
MAX_ATOMS = int(os.environ.get("MATSIM_MAX_ATOMS", 2_000_000))
# 誤って重い計算を投入しないための上限
MAX_TOTAL_STEPS = int(os.environ.get("MATSIM_MAX_TOTAL_STEPS", 10_000_000))
MAX_STAGES = 20
MAX_REPEAT = 50
MAX_TRAJ_ATOM_FRAMES = 20_000_000   # トラジェクトリに書く 原子数×フレーム数 (約 1GB 相当)
MAX_QUEUED_JOBS = 30

MAX_STRUCTURE_BYTES = 5 * 1024 * 1024
MAX_STL_BYTES = 5 * 1024 * 1024
MAX_POTENTIAL_BYTES = 200 * 1024 * 1024
MAX_STRUCTURES = 500

# 所要時間の目安に使う速度 [原子×ステップ/秒]。完了したジョブの実測があればそちらを使う
DEFAULT_SPEED = 4.0e5

# 画面に返すデータの間引き
MAX_THERMO_POINTS = 1500
MAX_VIEW_FRAMES = 60
MAX_VIEW_ATOM_FRAMES = 150_000
MAX_VIEW_ATOMS = 6000               # 3D 表示に送る原子数。超えたら等間隔に間引く
MAX_TRAJ_READ_BYTES = 2 * 1024 * 1024 * 1024
