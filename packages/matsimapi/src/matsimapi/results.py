"""ジョブの結果を画面用に読み出す (熱力学量の時系列・要約・トラジェクトリ・ファイル)。"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from gpumd_toolkit.analysis import ThermoData
from gpumd_toolkit.md import GPUMDResult

from . import settings

SERIES = ("time_ps", "temperature", "target_temperature", "pressure", "potential_energy_per_atom",
          "total_energy_per_atom", "volume", "a", "b", "c", "stage")
LATTICE_RE = re.compile(r'[Ll]attice="([^"]+)"')
# 画面からダウンロードさせないもの (ポテンシャルの絶対パスなどが入るだけで結果ではない)
HIDDEN_FILES = {"metadata.json"}


def _clean(values) -> list:
    """NaN / inf を null にして JSON にできるようにする。"""
    return [None if (isinstance(v, float) and not math.isfinite(v)) else v for v in values]


def _thermo(directory: Path, n_atoms: int) -> ThermoData | None:
    if not (directory / "thermo.out").is_file() or (directory / "thermo.out").stat().st_size == 0:
        return None
    try:
        return ThermoData.from_directory(directory, n_atoms=n_atoms)
    except (ValueError, IndexError):
        # 実行中は最後の行が書きかけのことがある。次の更新で読み直す
        return None


def thermo_series(directory: Path, n_atoms: int) -> dict:
    """熱力学量の時系列。点が多いときは等間隔に間引く。"""
    data = _thermo(directory, n_atoms)
    if data is None or len(data) == 0:
        return {"n_rows": 0, "series": {}, "stages": []}
    frame = data.frame
    stride = max(1, math.ceil(len(frame) / settings.MAX_THERMO_POINTS))
    picked = frame.iloc[::stride]
    series = {}
    for column in SERIES:
        values = picked[column].to_numpy()
        digits = 0 if column == "stage" else 6
        series[column] = _clean(np.round(values.astype(float), digits).tolist())
    stages = [{"stage": int(stage), "label": str(group["label"].iloc[0]), "ensemble": str(group["ensemble"].iloc[0]),
               "start_ps": float(group["time_ps"].iloc[0]), "end_ps": float(group["time_ps"].iloc[-1])}
              for stage, group in frame.groupby("stage", sort=True)]
    return {"n_rows": len(frame), "series": series, "stages": stages}


def stage_summary(directory: Path, n_atoms: int) -> list[dict]:
    """ステージごとの平均と標準偏差 (各ステージの先頭 30% は平衡化として除く)。"""
    data = _thermo(directory, n_atoms)
    if data is None or len(data) == 0:
        return []
    rows = data.summary().to_dict(orient="records")
    return [{key: (None if isinstance(value, float) and not math.isfinite(value) else value)
             for key, value in row.items()} for row in rows]


def postprocess(directory: Path, n_atoms: int, kind: str = "md", params: dict | None = None) -> None:
    """完了したジョブに、最終構造 (CONTCAR)・csv・グラフ画像と、種類ごとの結果を追加する。"""
    if (directory / "ft_job.json").is_file():   # ファインチューニング: 結果の表と、できたポテンシャルの登録
        (directory / "analysis").mkdir(exist_ok=True)
        from . import finetune
        finetune.collect(directory)
        return
    result = GPUMDResult(workdir=directory, command=None, n_atoms=n_atoms, run_in=directory / "run.in",
                         name=directory.name)
    result.write_final_structure()
    data = _thermo(directory, n_atoms)
    if data is not None and len(data):
        data.to_csv(directory / "analysis" / "thermo.csv")
        result.plot()
    if kind != "md":
        from . import calcjob
        calcjob.collect(directory, kind, params or {})


# ---- データセット (軸を自由に選べる表) ------------------------------------------------

THERMO_LABELS = {
    "time_ps": "時間 (ps)", "step": "ステップ", "temperature": "温度 (K)", "target_temperature": "目標温度 (K)",
    "pressure": "圧力 (GPa)", "Pxx": "応力 Pxx (GPa)", "Pyy": "応力 Pyy (GPa)", "Pzz": "応力 Pzz (GPa)",
    "Pyz": "応力 Pyz (GPa)", "Pxz": "応力 Pxz (GPa)", "Pxy": "応力 Pxy (GPa)",
    "potential_energy_per_atom": "ポテンシャルエネルギー (eV/atom)", "kinetic_energy_per_atom": "運動エネルギー (eV/atom)",
    "total_energy_per_atom": "全エネルギー (eV/atom)", "potential_energy": "ポテンシャルエネルギー (eV)",
    "kinetic_energy": "運動エネルギー (eV)", "total_energy": "全エネルギー (eV)",
    "volume": "体積 (Å³)", "volume_per_atom": "体積 (Å³/atom)", "density": "密度 (g/cm³)",
    "a": "セル長 a (Å)", "b": "セル長 b (Å)", "c": "セル長 c (Å)", "stage": "ステージ番号",
}


def _thermo_frame(directory: Path, n_atoms: int):
    data = _thermo(directory, n_atoms)
    if data is None or len(data) == 0:
        return None
    frame = data.frame
    return frame[[column for column in THERMO_LABELS if column in frame]]


def _read_index(directory: Path) -> dict:
    try:
        return json.loads((directory / "analysis" / "results.json").read_text())
    except (OSError, ValueError):
        return {"highlights": [], "notes": [], "datasets": []}


def write_collected(directory: Path, highlights: list[dict], notes: list[str], datasets: list) -> None:
    """種類ごとの結果を analysis/ に保存する (表は csv、一覧と要点は results.json)。"""
    analysis = directory / "analysis"
    analysis.mkdir(exist_ok=True)
    index = []
    for dataset in datasets:
        frame = dataset.frame.replace([np.inf, -np.inf], np.nan)
        frame.to_csv(analysis / f"{dataset.key}.csv", index=False)
        columns = [{"key": str(c), "label": dataset.labels.get(str(c), str(c))} for c in frame.columns]
        index.append({"key": dataset.key, "title": dataset.title, "columns": columns, "x": dataset.x,
                      "y": dataset.y, "n_rows": len(frame), "table": dataset.table,
                      "style": getattr(dataset, "style", "lines"), "ticks": getattr(dataset, "ticks", []),
                      "color": getattr(dataset, "color", None),
                      "equal_axes": getattr(dataset, "equal_axes", True)})
    (analysis / "results.json").write_text(json.dumps(
        {"highlights": highlights, "notes": notes, "datasets": index}, ensure_ascii=False, indent=1))


def results_overview(directory: Path, n_atoms: int) -> dict:
    """要点の数値と、グラフにできる表の一覧。熱力学量は実行中でも含める。"""
    overview = _read_index(directory)
    frame = _thermo_frame(directory, n_atoms)
    if frame is not None:
        columns = [{"key": c, "label": THERMO_LABELS[c]} for c in frame.columns]
        overview["datasets"] = [*overview["datasets"],
                                {"key": "thermo", "title": "熱力学量", "columns": columns, "x": "time_ps",
                                 "y": ["temperature"], "n_rows": len(frame), "table": False}]
    return overview


def dataset(directory: Path, n_atoms: int, key: str) -> dict:
    """表 1 つ分のデータ。行が多いときは等間隔に間引く。"""
    if key == "thermo":
        frame = _thermo_frame(directory, n_atoms)
        if frame is None:
            raise ValueError("まだデータがありません")
    else:
        if not any(item["key"] == key for item in _read_index(directory)["datasets"]):
            raise ValueError("データが見つかりません")
        frame = pd.read_csv(directory / "analysis" / f"{key}.csv")
    stride = max(1, math.ceil(len(frame) / settings.MAX_THERMO_POINTS))
    picked = frame.iloc[::stride]
    data = {str(column): _clean([float(f"{v:.7g}") if math.isfinite(v) else float("nan")
                                 for v in picked[column].astype(float).tolist()]) for column in picked.columns}
    return {"key": key, "n_rows": len(frame), "data": data}


# ---- トラジェクトリ -----------------------------------------------------------------

def _count_lines(path: Path) -> int:
    count = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            count += chunk.count(b"\n")
    return count


def trajectory_frames(directory: Path, filename: str = "dump.xyz") -> dict:
    """3D アニメーション用に、トラジェクトリから等間隔にフレームを抜き出す。

    大きなファイルを丸ごとメモリに載せないよう、必要なフレームの行だけを読む。
    """
    path = directory / filename
    if not path.is_file() or path.stat().st_size == 0:
        return {"n_frames_total": 0, "frames": []}
    if path.stat().st_size > settings.MAX_TRAJ_READ_BYTES:
        raise ValueError("トラジェクトリが大きすぎて画面では表示できません。ファイルをダウンロードしてください")
    with open(path, "r") as handle:
        n_atoms = int(handle.readline().split()[0])
    block = n_atoms + 2
    total = _count_lines(path) // block   # 書きかけの最終フレームは数えない
    if total == 0:
        return {"n_frames_total": 0, "frames": []}
    shown_atoms = min(n_atoms, settings.MAX_VIEW_ATOMS)
    wanted = max(1, min(settings.MAX_VIEW_FRAMES, settings.MAX_VIEW_ATOM_FRAMES // max(shown_atoms, 1), total))
    indices = sorted({round(i * (total - 1) / max(wanted - 1, 1)) for i in range(wanted)})
    chosen = set(indices)

    # 原子が多いときは等間隔に間引く (全原子を送ると画面が重くなる)
    stride = max(1, -(-n_atoms // settings.MAX_VIEW_ATOMS))
    symbols: list[str] = []
    frames = []
    with open(path, "r") as handle:
        for index in range(total):
            if index not in chosen:
                for _ in range(block):
                    handle.readline()
                continue
            handle.readline()
            match = LATTICE_RE.search(handle.readline())
            cell = np.array(match.group(1).split(), dtype=float).reshape(3, 3) if match else np.zeros((3, 3))
            rows = [handle.readline().split() for _ in range(n_atoms)][::stride]
            if not symbols:
                symbols = [row[0] for row in rows]
            positions = np.array([row[1:4] for row in rows], dtype=float)
            frames.append({"index": index, "cell": np.round(cell, 4).tolist(),
                           "positions": np.round(positions, 2).tolist()})
    return {"n_frames_total": total, "n_atoms": n_atoms, "symbols": symbols, "frames": frames}


# ---- ファイル -----------------------------------------------------------------------

def list_files(directory: Path) -> list[dict]:
    """ジョブのディレクトリにあるファイル (analysis/ の下も含む)。"""
    files = []
    if not directory.is_dir():
        return files
    # ファインチューニングは繰り返しごとの途中のファイル (iter_XX/) が多いので、直下と analysis/ だけを見せる
    finetune = (directory / "ft_job.json").is_file()
    for path in sorted(directory.rglob("*")):
        if finetune and len(path.relative_to(directory).parts) > 1 and path.relative_to(directory).parts[0] != "analysis":
            continue
        if path.is_file() and not path.is_symlink() and path.name not in HIDDEN_FILES:
            files.append({"name": path.relative_to(directory).as_posix(), "size": path.stat().st_size})
    return files


TEXT_LIMIT = 200_000   # 画面で中身を見せる上限 [バイト]。超えた分は先頭だけ


def read_text(directory: Path, name: str, limit: int = TEXT_LIMIT) -> dict:
    """ジョブのファイルの中身 (テキストだけ)。大きいファイルは先頭 limit バイトまで。"""
    path = resolve_file(directory, name)
    size = path.stat().st_size
    with open(path, "rb") as handle:
        head = handle.read(limit)
    if b"\0" in head[:8192]:
        raise ValueError("テキストではないファイルなので、ダウンロードして開いてください")
    text = head.decode("utf-8", errors="replace")
    if size > limit:
        text = text.rsplit("\n", 1)[0]   # 途中で切れた行は出さない
    return {"name": name, "size": size, "text": text, "truncated": size > limit}


def resolve_file(directory: Path, name: str) -> Path:
    """一覧にある名前だけをファイルに解決する (ディレクトリの外は指せない)。"""
    for item in list_files(directory):
        if item["name"] == name:
            return directory / name
    raise ValueError("ファイルが見つかりません")


def make_zip(directory: Path, job_id: int) -> Path:
    """結果一式の zip を作業用ディレクトリに作る。呼び出し側で送信後に消す。"""
    fd, tmp_name = tempfile.mkstemp(prefix=f"job{job_id}_", suffix=".zip", dir=settings.TMP_DIR)
    os.close(fd)
    target = Path(tmp_name)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        for item in list_files(directory):
            archive.write(directory / item["name"], f"job{job_id}/{item['name']}")
    return target
