"""計算環境 (GPUMD・GPUMD_toolkit・GPU) の状態を調べる。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from importlib import metadata

from . import settings


def _run(args: list[str], timeout: float = 5.0) -> str | None:
    """コマンドの標準出力を返す。失敗したら None。"""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def gpumd_status() -> dict:
    """GPUMD の実行ファイルがあるかとバージョン。"""
    available = settings.GPUMD_BIN.is_file() and os.access(settings.GPUMD_BIN, os.X_OK)
    version = _run(["git", "-C", str(settings.GPUMD_DIR), "describe", "--tags"]) if available else None
    return {"available": available, "version": version}


def toolkit_status() -> dict:
    """GPUMD_toolkit が import できるかとバージョン。"""
    try:
        import gpumd_toolkit
    except ImportError:
        return {"available": False, "version": None}
    version = getattr(gpumd_toolkit, "__version__", None)
    if version is None:
        try:
            version = metadata.version("gpumd-toolkit")
        except metadata.PackageNotFoundError:
            version = None
    return {"available": True, "version": version}


def gpu_status() -> dict:
    """nvidia-smi から GPU の名前・メモリ・使用率を読む。"""
    out = _run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits"])
    if not out:
        return {"available": False}
    try:
        name, used, total, util = [x.strip() for x in out.splitlines()[0].split(",")]
        return {"available": True, "name": name, "memory_used_mb": int(used),
                "memory_total_mb": int(total), "utilization_percent": int(util)}
    except ValueError:
        return {"available": False}


def atomsk_status() -> dict:
    """構造モデリングに使う Atomsk の実行ファイルがあるかとバージョン (git のコミット)。"""
    available = settings.ATOMSK_BIN.is_file() and os.access(settings.ATOMSK_BIN, os.X_OK)
    version = None
    if available:
        version = _run(["git", "-C", str(settings.ATOMSK_BIN.parent.parent), "describe", "--tags"])
    return {"available": available, "version": version}


def plumed_status() -> dict:
    """PLUMED 対応の gpumd があるかと、PLUMED のバージョン (~/repos/plumed/plumed2 の git のタグ)。"""
    binary = settings.GPUMD_PLUMED_BIN
    available = binary.is_file() and os.access(binary, os.X_OK)
    version = None
    if available:
        version = _run(["git", "-C", str(binary.parent.parent.parent / "plumed2"), "describe", "--tags"])
    return {"available": available, "version": version}


def sevennet_status() -> dict:
    """SevenNet (GNN 系の汎用力場) を計算する Python があるかと、sevenn の版 (インストールの記録から読む。重い import はしない)。"""
    python = settings.ML_PYTHON
    if not python.is_file():
        return {"available": False, "version": None}
    lib = python.parent.parent / "lib"
    found = sorted(lib.glob("python3*/site-packages/sevenn-*.dist-info"))
    version = found[-1].name[len("sevenn-"):-len(".dist-info")] if found else None
    # GitHub から入れたとき (SevenNet-Nano に要る) は、そのコミットも示す
    try:
        commit = json.loads((found[-1] / "direct_url.json").read_text())["vcs_info"]["commit_id"][:7]
        version = f"{version} (GitHub {commit})"
    except (IndexError, OSError, ValueError, KeyError, TypeError):
        pass
    return {"available": True, "version": version}


def _installed(python: Path, package: str) -> bool:
    """python の環境に package が入っているか (インストールの記録を見るだけ。重い import はしない)。"""
    lib = python.parent.parent / "lib"
    return bool(sorted(lib.glob(f"python3*/site-packages/{package}-*.dist-info")))


def engine_status() -> dict:
    """NEP の MD・静的計算・フォノン分散を何で動かすか (GPUMD か、CPU の calorine)。"""
    cpu = settings.cpu_nep()
    reason = ("MATSIM_MD_ENGINE=cpu" if settings.MD_ENGINE == "cpu" else
              "GPUMD が見つからないので CPU" if cpu else "GPUMD")
    return {"md": "cpu" if cpu else "gpumd", "setting": settings.MD_ENGINE, "reason": reason,
            "calorine": settings.ML_PYTHON.is_file() and _installed(settings.ML_PYTHON, "calorine")}


def status() -> dict:
    return {"gpumd": gpumd_status(), "toolkit": toolkit_status(), "gpu": gpu_status(), "atomsk": atomsk_status(),
            "plumed": plumed_status(), "sevennet": sevennet_status(), "engine": engine_status()}


def doctor() -> int:
    """paths.env のパスと仮想環境で動くかを、1 行ずつ確かめて出す (matsimapi doctor)。✗ があれば 1 を返す。"""
    ok, ng, maybe = "✓", "✗", "△"
    failed = False

    def line(mark: str, label: str, detail: str = "", hint: str = "") -> None:
        nonlocal failed
        failed |= mark == ng
        print(f"  {mark} {label}" + (f": {detail}" if detail else ""))
        if hint and mark != ok:
            print(f"      → {hint}")

    print("MatSim の環境の確認")
    print("\n[パスの設定 (paths.env)]")
    line(ok if settings.PATHS_FILE.is_file() else maybe, "paths.env", str(settings.PATHS_FILE),
         "cp paths.example.env paths.env として、この環境の場所に書き換えてください (なければ既定の場所で動かします)")
    print("\n[Python と GPUMD_toolkit]")
    line(ok, "Python", f"{sys.executable}")
    toolkit = toolkit_status()
    line(ok if toolkit["available"] else ng, "gpumd_toolkit", toolkit["version"] or "入っていません",
         "この venv に GPUMD_toolkit を入れてください (pip install -e <GPUMD_toolkit>)")
    print("\n[計算エンジン]")
    gpumd = gpumd_status()
    engine = engine_status()
    line(ok if gpumd["available"] else maybe, "GPUMD", str(settings.GPUMD_BIN) + ("" if gpumd["available"] else " (ありません)"),
         "GPU で速く計算するなら GPUMD をビルドして MATSIM_GPUMD_BIN に書いてください。なくても CPU の NEP で動きます")
    line(ok, "NEP の MD・静的計算・フォノン", f"{'CPU (calorine の CPUNEP)' if engine['md'] == 'cpu' else 'GPUMD'} ({engine['reason']})")
    if engine["md"] == "cpu":
        line(ok if engine["calorine"] else ng, "calorine (CPU の NEP)", str(settings.ML_PYTHON),
             f"{settings.ML_PYTHON} -m pip install calorine")
        line(maybe, "GPUMD だけの計算", "拡散・熱伝導・融点などは GPUMD がないとできません (CPU は MD・静的計算・フォノン分散)")
    sevennet = sevennet_status()
    line(ok if sevennet["available"] and sevennet["version"] else maybe, "SevenNet (任意)", sevennet["version"] or "入っていません",
         f"使うなら {settings.ML_PYTHON} に sevenn を入れるか、MATSIM_ML_PYTHON に sevenn の入った Python を書いてください")
    plumed = plumed_status()
    line(ok if plumed["available"] else maybe, "PLUMED 版 gpumd (任意)", str(settings.GPUMD_PLUMED_BIN) + ("" if plumed["available"] else " (ありません)"))
    print("\n[構造モデリングとポテンシャル]")
    atomsk = atomsk_status()
    line(ok if atomsk["available"] else maybe, "Atomsk", str(settings.ATOMSK_BIN) + ("" if atomsk["available"] else " (ありません)"),
         "構造モデリングの一部 (Atomsk を使う操作) に要ります。ビルドして MATSIM_ATOMSK_BIN に書いてください")
    for name, path in settings.BUILTIN_POTENTIALS.items():
        line(ok if path.is_file() else ng, name, str(path), "NEP89 のファイル (nep89_*.txt) の場所を MATSIM_NEP89 に書いてください")
    print("\n[データ]")
    target = settings.DATA_DIR
    while not target.exists():
        target = target.parent
    line(ok if os.access(target, os.W_OK) else ng, "MATSIM_DATA_DIR", str(settings.DATA_DIR), "書ける場所を MATSIM_DATA_DIR に書いてください")
    print("\n" + ("直すところがあります (✗)。上の → を見てください" if failed else "動かせます"))
    return 1 if failed else 0
