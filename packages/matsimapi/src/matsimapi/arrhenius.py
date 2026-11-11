"""拡散・イオン伝導のジョブを複数まとめて、アレニウスプロットから活性化エネルギーを求める。

    ln D       = ln D0        - Ea / (kB T)     (元素ごとの拡散係数)
    ln (σ T)   = ln (σ0)      - Ea / (kB T)     (イオン伝導度。ネルンスト–アインシュタインの式に基づく形)

横軸は 1000/T [1/K]、縦軸は自然対数で直線に当てはめ、傾きから Ea [eV] を求める。
温度は既定で本計算 (最後のステージ) の実測の平均温度を使う (設定値も選べる)。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path

import numpy as np
from scipy import stats

from . import jobs, results

KB = 8.617333262e-5          # ボルツマン定数 [eV/K]
ROOM = 300.0                 # 外挿する温度 [K]
MIN_MSD = 1.0                # 本計算の終わりの MSD の目安 (6 D t) がこれ [Å²] 未満なら「拡散していない」とみなす
MAX_JOBS = 50

D_LABEL = re.compile(r"^拡散係数 D \((.+)\)$")
SIGMA_LABEL = re.compile(r"^イオン伝導度 \((.+)\)$")


def _values(directory: Path) -> tuple[dict[str, float], dict[str, float]]:
    """results.json から元素ごとの D [cm²/s] とイオン伝導度 [mS/cm] を読む (key がない古い結果は表示名から)。"""
    try:
        highlights = json.loads((directory / "analysis" / "results.json").read_text())["highlights"]
    except (OSError, ValueError, KeyError):
        return {}, {}
    d, sigma = {}, {}
    for item in highlights:
        key, label, value = item.get("key", ""), item.get("label", ""), item.get("value")
        if not isinstance(value, (int, float)):
            continue
        if key.startswith("D:") or D_LABEL.match(label):
            d[key[2:] if key else D_LABEL.match(label).group(1)] = float(value)
        elif key.startswith("sigma:") or SIGMA_LABEL.match(label):
            sigma[key[6:] if key else SIGMA_LABEL.match(label).group(1)] = float(value)
    return d, sigma


def gather(db: sqlite3.Connection, job_ids: list[int]) -> tuple[list[dict], list[str]]:
    """ジョブごとの温度と D・σ を集める。使えないジョブは理由を notes に書いて飛ばす。"""
    points, notes = [], []
    for job_id in dict.fromkeys(job_ids):
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            notes.append(f"ジョブ #{job_id} が見つかりません")
            continue
        if row["kind"] != "diffusion":
            notes.append(f"#{job_id} は拡散・イオン伝導のジョブではありません")
            continue
        if row["status"] != "done":
            notes.append(f"#{job_id} ({row['name']}) はまだ完了していません")
            continue
        directory = jobs.workdir(job_id)
        d, sigma = _values(directory)
        if not d and not sigma:
            notes.append(f"#{job_id} ({row['name']}) の結果に拡散係数がありません")
            continue
        params = json.loads(row["params"])
        stages = results.stage_summary(directory, row["n_atoms"])
        measured = stages[-1].get("temperature_mean") if stages else None
        summary = json.loads(row["summary"])
        options = params.get("options", {})
        production_ps = float(options.get("steps") or 0) * float(params.get("time_step") or 1.0) / 1000
        points.append({"job_id": job_id, "name": row["name"], "structure": summary.get("structure", ""),
                       "series": row["series"], "t_set": float(options.get("temperature") or 0),
                       "t_measured": float(measured) if measured else None, "production_ps": production_ps,
                       "D": d, "sigma": sigma})
    structures = {p["structure"] for p in points}
    if len(structures) > 1:
        notes.append("異なる構造のジョブが混ざっています: " + "、".join(sorted(structures)))
    return points, notes


def fit_line(x: np.ndarray, y: np.ndarray) -> dict:
    """y = a + b x の最小二乗。点が 3 つ以上なら誤差も出す。"""
    if len(x) < 2 or np.ptp(x) == 0:
        return {}
    if len(x) == 2:
        b = (y[1] - y[0]) / (x[1] - x[0])
        return {"slope": float(b), "intercept": float(y[0] - b * x[0]), "r2": 1.0, "slope_err": None,
                "intercept_err": None}
    res = stats.linregress(x, y)
    return {"slope": float(res.slope), "intercept": float(res.intercept), "r2": float(res.rvalue ** 2),
            "slope_err": float(res.stderr), "intercept_err": float(res.intercept_stderr)}


def stuck(point: dict, symbol: str) -> bool:
    """その元素がほとんど拡散していない (本計算の終わりの MSD の目安 6Dt が MIN_MSD 未満)。"""
    d = point["D"].get(symbol)
    if d is None or not point["production_ps"]:
        return False
    return 6 * d * 1e4 * point["production_ps"] < MIN_MSD      # D [cm²/s] → [Å²/ps] は ×1e4


def _target(name: str, label: str, unit: str, pts: list[tuple[dict, float, float, bool]], excluded: set[int],
            included: set[int], sigma_t: bool) -> dict:
    """1 つの量 (ある元素の D、または σ) の当てはめ。pts は (点, 温度, 値, 拡散していないか)。

    拡散していない点は既定で外す (included に入っていれば使う)。excluded の点は外す。
    """
    rows, notes = [], []
    for point, temperature, value, slow in sorted(pts, key=lambda p: p[1]):
        usable = value > 0 and temperature > 0
        y = math.log(value * temperature) if (usable and sigma_t) else (math.log(value) if usable else None)
        job = point["job_id"]
        used = usable and job not in excluded and (not slow or job in included)
        rows.append({"job_id": job, "name": point["name"], "temperature": temperature,
                     "inv_t": 1000 / temperature if temperature > 0 else None, "value": value, "ln": y,
                     "used": used, "slow": slow})
        if not usable:
            notes.append(f"#{job} は値が 0 以下なので使えません")
    if any(r["slow"] and not r["used"] and r["job_id"] not in excluded for r in rows):
        notes.append(f"ほとんど拡散していない点 (本計算の MSD が {MIN_MSD:g} Å² 未満の見込み。融けていない・"
                     "本計算が短いなど) を当てはめから外しました。表のチェックで戻せます")
    if any(r["slow"] and r["used"] for r in rows):
        notes.append("ほとんど拡散していない点を当てはめに使っています")
    used = [r for r in rows if r["used"]]
    result = {"name": name, "label": label, "unit": unit, "points": rows, "notes": notes, "fit": None}
    if len(used) < 2:
        notes.append("当てはめには使う点が 2 つ以上要ります")
        return result
    fit = fit_line(np.array([r["inv_t"] for r in used]), np.array([r["ln"] for r in used]))
    if not fit:
        notes.append("温度が同じ点しかないので当てはめられません")
        return result
    ea = -fit["slope"] * 1000 * KB
    prefactor = math.exp(fit["intercept"])
    at_room = math.exp(fit["intercept"] + fit["slope"] * 1000 / ROOM) / (ROOM if sigma_t else 1)
    result["fit"] = {
        "ea_ev": ea, "ea_err": fit["slope_err"] * 1000 * KB if fit["slope_err"] is not None else None,
        "prefactor": prefactor, "r2": fit["r2"], "slope": fit["slope"], "intercept": fit["intercept"],
        "room": at_room, "n_points": len(used), "t_range": [min(r["temperature"] for r in used),
                                                           max(r["temperature"] for r in used)]}
    if len(used) < 3:
        notes.append("点が 2 つなので、誤差は出せません (3 点以上を推奨)")
    elif fit["r2"] < 0.95:
        notes.append(f"直線からのずれが大きい (R² = {fit['r2']:.3f})。相転移・融解した温度や、統計の足りない点がないか確認してください")
    if ea <= 0:
        notes.append("活性化エネルギーが 0 以下になりました。温度が上がるほど値が下がっています")
    if ROOM < result["fit"]["t_range"][0]:
        notes.append(f"{ROOM:g} K の値は、計算した温度域 ({result['fit']['t_range'][0]:.0f} K〜) からの外挿です")
    return result


def analyze(db: sqlite3.Connection, job_ids: list[int], *, excluded: list[int] | None = None,
            included: list[int] | None = None, temperature: str = "measured", sigma_mode: str = "sigma_t") -> dict:
    """ジョブを集めて、元素ごとの D とイオン伝導度のアレニウスプロットを当てはめる。

    temperature  "measured" (本計算の平均温度。読めなければ設定値) / "set" (設定値)
    sigma_mode   "sigma_t" (ln σT で当てはめ) / "sigma" (ln σ で当てはめ)
    excluded     当てはめから外すジョブ。included は「拡散していない」と判定されても使うジョブ
    """
    if not job_ids:
        raise ValueError("ジョブを選んでください")
    if len(job_ids) > MAX_JOBS:
        raise ValueError(f"一度に選べるジョブは {MAX_JOBS} 件までです")
    points, notes = gather(db, job_ids)
    skip, keep = set(excluded or []), set(included or [])

    def temp_of(point: dict) -> float:
        if temperature == "measured" and point["t_measured"]:
            return point["t_measured"]
        return point["t_set"]

    targets = []
    # よく動く元素 (拡散係数の大きいもの) から並べる
    species = sorted({s for p in points for s in p["D"]}, key=lambda s: -max(p["D"].get(s, 0) for p in points))
    for symbol in species:
        pts = [(p, temp_of(p), p["D"][symbol], stuck(p, symbol)) for p in points if symbol in p["D"]]
        targets.append(_target(f"D:{symbol}", f"拡散係数 D ({symbol})", "cm²/s", pts, skip, keep, False))
    for symbol in sorted({s for p in points for s in p["sigma"]}):
        pts = [(p, temp_of(p), p["sigma"][symbol], stuck(p, symbol)) for p in points if symbol in p["sigma"]]
        label = f"イオン伝導度 σ ({symbol})"
        targets.insert(0, _target(f"sigma:{symbol}", label, "mS/cm", pts, skip, keep, sigma_mode == "sigma_t"))
    if temperature == "measured" and any(not p["t_measured"] for p in points):
        notes.append("実測の平均温度を読めなかったジョブは、設定した温度を使いました")
    return {"jobs": [{k: p[k] for k in ("job_id", "name", "structure", "series", "t_set", "t_measured")} for p in points],
            "targets": targets, "notes": notes, "temperature": temperature, "sigma_mode": sigma_mode,
            "kb": KB, "room": ROOM}


def to_csv(result: dict) -> str:
    """当てはめに使った点と結果を csv にする。"""
    lines = ["quantity,job_id,name,temperature_K,1000_over_T,value,unit,ln_y,used"]
    for target in result["targets"]:
        for r in target["points"]:
            lines.append(",".join(str(v) for v in (
                target["name"], r["job_id"], json.dumps(r["name"], ensure_ascii=False), f"{r['temperature']:.3f}",
                f"{r['inv_t']:.6f}" if r["inv_t"] else "", f"{r['value']:.6g}", target["unit"],
                f"{r['ln']:.6f}" if r["ln"] is not None else "", int(r["used"]))))
    lines.append("")
    lines.append("quantity,Ea_eV,Ea_err_eV,prefactor,R2,value_at_300K,n_points")
    for target in result["targets"]:
        fit = target["fit"]
        if fit:
            lines.append(",".join(str(v) for v in (
                target["name"], f"{fit['ea_ev']:.6f}", f"{fit['ea_err']:.6f}" if fit["ea_err"] is not None else "",
                f"{fit['prefactor']:.6g}", f"{fit['r2']:.6f}", f"{fit['room']:.6g}", fit["n_points"])))
    return "\n".join(lines) + "\n"
