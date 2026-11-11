"""NEP のファインチューニング: SevenNet (GNN 系の汎用力場) を先生にして NEP89 などを学び直す (蒸留 + 能動学習)。

0 段目: 元のポテンシャルで MD をして構造を多めに集め、似た構造を除いて SevenNet で計算し、1 回学ぶ
(元のポテンシャルと先生のエネルギーの基準のずれもここでそろう)。
1 段目から: いまの学習データの範囲 (MaxVol の active set、TorchNEP) から外れた構造 (外挿度 γ が大きい) と、
SevenNet との誤差が大きい構造を足して学び直す。どちらもなくなるまで繰り返す (能動学習)。できた nep.txt はポテンシャルの
一覧に登録し、GPUMD の速さのまま、SevenNet に近い精度でほかの計算に使える。

実際の繰り返しは engines/ft_runner.py が受け持つ (SevenNet・gpumd・nep を順に動かす)。ここでは入力の検証、
ft_job.json の書き出し、結果の取り出しと、できたポテンシャルの登録をする。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
from ase import Atoms
from gpumd_toolkit.structure import StructureHandler

from . import potentials, settings
from .kinds import Collected, Context, Dataset, Kind, choice, num, ptext, steps

MAX_TEMPERATURES = 6
MAX_FINETUNE_ATOMS = 1000     # 先生 (SevenNet) と学習の時間がかかりすぎないよう、ファインチューニングに使う構造の原子数の上限
TEACHERS = [{"value": spec, "label": name} for name, spec in settings.SEVENNET_MODELS.items()]
DEFAULT_TEACHER = "sevennet:7net-mf-ompa:mpa"
EOS_SCALES = [0.97, 0.98, 0.99, 1.0, 1.01, 1.02, 1.03]   # 格子の伸縮 (体積で約 ±9%)


def restart_of(potential: dict) -> Path | None:
    """ファインチューニングの出発点に使える restart ファイル (なければ None)。"""
    path = Path(potential["path"])
    if path.name == settings.NEP89_RESTART.name.replace(".restart", ".txt") and settings.NEP89_RESTART.is_file():
        return settings.NEP89_RESTART
    restart = path.with_suffix(".restart")
    return restart if restart.is_file() else None


def hyperparameters(nep_txt: Path) -> list[str]:
    """nep.txt の先頭から、fine_tune で変えられない設定 (nep.in の行) を作る。"""
    lines = nep_txt.read_text(errors="replace").splitlines()[:8]
    head = lines[0].split()
    match = re.match(r"nep(\d)", head[0])
    if not match:
        raise ValueError("NEP ポテンシャルの形式が読めません")
    out = [f"version {match.group(1)}"]
    for line in lines[1:]:
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "zbl":
            out.append(f"zbl {tokens[2]}")
        elif tokens[0] == "cutoff":
            out.append(f"cutoff {tokens[1]} {tokens[2]}")
        elif tokens[0] in ("n_max", "basis_size", "l_max"):
            out.append(line.strip())
        elif tokens[0] == "ANN":
            out.append(f"neuron {tokens[1]}")
    return out


class FinetuneJob:
    """ft_job.json (繰り返しの指示) を書き出す。アプリのジョブとして扱うための部分は GPUMDCalculation と同じ呼び方。"""

    def __init__(self, atoms: Atoms, base: dict, restart: Path, workdir: Path, o: dict, time_step: float):
        self.atoms = atoms
        self.base = base
        self.restart = restart
        self.workdir = Path(workdir)
        self.o = o
        self.time_step = time_step
        self.temperatures = [float(t) for t in o["temperatures"].replace(" ", "").split(",") if t]
        model, modal = o["teacher"].split(":")[1], (o["teacher"].split(":")[2] if o["teacher"].count(":") > 1 else None)
        self.teacher = {"model": model, "modal": modal}
        # NEP89 からは構造の元素で新しく作る。ファインチューニング済みのものからは、その元素の並びのまま続きを学ぶ
        elements = sorted(set(atoms.get_chemical_symbols()))
        self.elements = elements if len(base["elements"]) == 89 else list(base["elements"])
        self.structure_info = StructureHandler.info(atoms)
        self.n_atoms = len(atoms)
        init = 1 if o["init_max"] > 0 else 0
        md = (init + o["max_iterations"] + 1) * len(self.temperatures) * o["md_steps"]
        self.builder = _Builder(md + (init + o["max_iterations"]) * o["generations"], md * time_step / 1000)

    def check_cell(self, strict: bool = False) -> dict:
        return {"ok": True, "message": ""}

    def job(self) -> dict:
        o = self.o
        return {
            "teacher": self.teacher, "base_txt": str(self.base["path"]), "base_restart": str(self.restart),
            "hyper": hyperparameters(Path(self.base["path"])), "elements": self.elements,
            "gpumd": str(settings.GPUMD_BIN), "nep": str(settings.NEP_BIN), "cuda_bin": settings.CUDA_BIN,
            "temperatures": self.temperatures, "ensemble": o["ensemble"], "pressure": o["pressure"],
            "md_steps": o["md_steps"], "sample_interval": o["sample_interval"], "time_step": self.time_step,
            "max_iterations": o["max_iterations"], "threshold": o["threshold"], "max_add": o["max_add"],
            "generations": o["generations"], "population": o["population"], "eos_scales": EOS_SCALES,
            "n_rattle": o["n_rattle"], "init_max": o["init_max"], "init_min": o["init_min"],
            "gamma_select": o["gamma_select"],
            "total_units": self.builder.total_steps,
        }

    def estimate_seconds(self, md_speed: float) -> float:
        """所要時間の目安 [秒]: 探索の MD + 先生の計算 + 学習 (学習データの原子数 × 世代数に比例。RTX 5070 Ti で実測)。"""
        o = self.o
        init = 1 if o["init_max"] > 0 else 0
        rounds = init + o["max_iterations"] + 1
        md = self.n_atoms * rounds * len(self.temperatures) * o["md_steps"] / md_speed
        frames = rounds * len(self.temperatures) * (o["md_steps"] // o["sample_interval"])
        label = frames * (0.03 + self.n_atoms * 1e-4)
        n_train = len(EOS_SCALES) + o["n_rattle"]
        fit = 0.0
        for add in [o["init_max"]] * init + [o["max_add"]] * o["max_iterations"]:
            n_train += add
            fit += 2.5e-6 * n_train * self.n_atoms * o["generations"] * o["population"] / 50 + 5
        return md + label + fit + 10 * rounds + 30   # 10: 段ごとの active set・外挿度の計算

    def preview(self) -> str:
        return ("# ft_job.json (ファインチューニングの指示。engines/ft_runner.py が gpumd・SevenNet・nep を順に動かします)\n"
                + json.dumps(self.job(), ensure_ascii=False, indent=1) + "\n")

    def write_inputs(self) -> dict:
        self.workdir.mkdir(parents=True, exist_ok=True)
        model = StructureHandler.write_model(self.atoms, self.workdir / "model.xyz")
        (self.workdir / "ft_job.json").write_text(json.dumps(self.job(), ensure_ascii=False, indent=1))
        return {"model": model}


class _Builder:
    def __init__(self, total_steps: int, time_ps: float):
        self.total_steps = total_steps
        self._time_ps = time_ps

    def total_time_ps(self) -> float:
        return self._time_ps


def build(c: Context) -> FinetuneJob:
    o = c.o
    base = c.potential
    if potentials.engine_of(base["path"]) != "gpumd":
        raise ValueError("ファインチューニングの出発点には NEP (NEP89 や、ここでファインチューニングしたもの) を選んでください。"
                         "SevenNet は「先生のモデル」で選びます")
    restart = restart_of(base)
    if restart is None:
        raise ValueError(f"「{base['name']}」には restart ファイル (学習の続きの情報) がないので、ファインチューニングできません。"
                         "NEP89 か、このアプリでファインチューニングしたポテンシャルを選んでください")
    atoms = c.atoms.repeat(tuple(c.repeat)) if tuple(c.repeat) != (1, 1, 1) else c.atoms.copy()
    symbols = set(atoms.get_chemical_symbols())
    if len(base["elements"]) != 89 and symbols != set(base["elements"]):
        # GPUMD の fine_tune で元素を選び直せるのは NEP89 からだけ。それ以外は同じ元素の組で続きを学ぶ
        raise ValueError(f"「{base['name']}」({'・'.join(base['elements'])}) から学び直せるのは、同じ元素の組の構造だけです。"
                         "ほかの元素の組には NEP89 から始めてください")
    if len(atoms) > MAX_FINETUNE_ATOMS:
        raise ValueError(f"ファインチューニングに使う構造は {MAX_FINETUNE_ATOMS} 原子までにしてください (今 {len(atoms)} 原子)。"
                         "SevenNet の計算と学習の時間は原子数に比例します")
    missing = potentials.missing_elements(atoms.get_chemical_symbols(), potentials.SEVENNET_ELEMENTS)
    if missing:
        raise ValueError(f"SevenNet は {', '.join(missing)} に対応していません")
    thickness = StructureHandler.cell_thickness(atoms)
    if min(thickness) < 12.0:   # NEP89 のカットオフ 6 Å の 2 倍 (GPUMD の MD に要る)
        raise ValueError(f"セルの厚みが 12 Å 未満です ({min(thickness):.1f} Å)。スーパーセルを大きくしてください")
    temps = [t for t in o["temperatures"].replace(" ", "").split(",") if t]
    if not 1 <= len(temps) <= MAX_TEMPERATURES:
        raise ValueError(f"MD の温度は 1〜{MAX_TEMPERATURES} 個にしてください")
    if o["init_min"] > o["init_max"]:
        raise ValueError("0 段目に学ぶ構造の下限は、上限以下にしてください")
    if o["sample_interval"] > o["md_steps"]:
        raise ValueError("構造を集める間隔は、MD のステップ数以下にしてください")
    return FinetuneJob(atoms, base, restart, c.workdir, o, c.time_step)


def collect(directory: Path) -> None:
    """ft_result.json を結果の画面にし、できたポテンシャルを一覧に登録する。"""
    from . import results
    out = Collected()
    result = json.loads((directory / "ft_result.json").read_text())
    job = json.loads((directory / "ft_job.json").read_text())
    its = result["iterations"]
    first, last = its[0], its[-1]
    out.add("段の数 (0 段目を含む)", len(its), "段")
    out.add("学習データ", result["n_train"], "構造")
    out.add("力の誤差 (元のポテンシャル、95%)", first["force_p95"], "eV/Å")
    out.add("力の誤差 (最後、95%)", last["force_p95"], "eV/Å")
    out.add("力の誤差 (最後、平均)", last["force_mean"], "eV/Å")
    out.add("エネルギーの誤差 (最後)", last["energy_rmse"] * 1000, "meV/atom")
    if "gamma_max" in last:
        out.add("外挿度 γ (最後、最大)", last["gamma_max"], "")
    gamma = result.get("gamma_select", 2.0)
    if result["converged"]:
        out.notes.append(f"学習データの範囲の外 (外挿度 γ > {gamma:g}) の構造がなくなり、まだ学習していない構造の 95% で"
                         f"力の誤差がしきい値 {result['threshold']:g} eV/Å 以下になりました")
    else:
        out.notes.append(f"繰り返しの上限までに、外挿度 γ ≤ {gamma:g} と 力の誤差 ≤ {result['threshold']:g} eV/Å の"
                         "両方を満たしませんでした。最後のポテンシャルを登録しています (繰り返しや世代数を増やすと良くなることがあります)")
    out.notes.append("0 段目の誤差は元のポテンシャル、1 段目からはその段の始めのポテンシャルの値で、その段の MD で新しく集めた "
                     "(まだ学習していない) 構造で測っています。エネルギーの誤差は、先生とのエネルギーの基準のずれも含みます")
    out.notes.append("外挿度 γ (TorchNEP の MaxVol): 学習データの原子環境が張る範囲から、どれだけ外れているか。"
                     "1 以下なら範囲の中、大きいほど学習データにない環境です")

    frame = pd.DataFrame([{k: r.get(k) for k in ("iteration", "force_mean", "force_p95", "force_max",
                                                  "train_force_rmse", "n_train")} for r in its]).astype(float)
    out.datasets.append(Dataset("learning", "段ごとの力の誤差 (まだ学習していない構造)", frame, "iteration",
                                ["force_mean", "force_p95", "train_force_rmse"],
                                {"iteration": "段", "force_mean": "平均 (eV/Å)", "force_p95": "95% (eV/Å)",
                                 "force_max": "最大 (eV/Å)", "train_force_rmse": "学習データでの誤差 (eV/Å)",
                                 "n_train": "学習データ (構造)"}))
    graded = [r for r in its if "gamma_max" in r]
    if graded:
        frame = pd.DataFrame([{"iteration": r["iteration"], "gamma_max": r["gamma_max"], "gamma_p95": r["gamma_p95"],
                               "limit": gamma} for r in graded]).astype(float)
        out.datasets.append(Dataset("gamma", "段ごとの外挿度 γ (学習データの範囲からの外れ具合)", frame, "iteration",
                                    ["gamma_max", "gamma_p95", "limit"],
                                    {"iteration": "段", "gamma_max": "最大", "gamma_p95": "95%",
                                     "limit": "しきい値 (これを超えたら学ぶ)"}))
    table = pd.DataFrame([{"iteration": r["iteration"], "n_candidates": r.get("n_pool", r["n_candidates"]),
                           "n_gamma_over": r.get("n_gamma_over"), "n_error_over": r.get("n_error_over"),
                           "n_added": r["n_added"], "n_train": r["n_train"],
                           "force_p95": round(r["force_p95"] * 1000, 1), "energy_mev": round(r["energy_rmse"] * 1000, 2),
                           "minutes": round(r["seconds"] / 60, 1)} for r in its])
    out.datasets.append(Dataset("iterations", "段ごとの記録 (0 段目: 元のポテンシャルで集めてダブりを除いて学ぶ)", table,
                                "iteration", ["force_p95"],
                                {"iteration": "段", "n_candidates": "集めた構造", "n_gamma_over": "外挿していた構造",
                                 "n_error_over": "誤差が大きかった構造", "n_added": "足した構造",
                                 "n_train": "学習データ", "force_p95": "力の誤差 95% (meV/Å)",
                                 "energy_mev": "エネルギーの誤差 (meV/atom)", "minutes": "かかった時間 (分)"}, table=True))

    eos = result["eos"]
    teacher, base, final = (np.array(eos[k]) for k in ("teacher", "base", "final"))
    frame = pd.DataFrame({"volume_per_atom": eos["volume_per_atom"], "teacher": teacher - teacher.min(),
                          "base": base - base.min(), "final": final - final.min()})
    out.datasets.append(Dataset("eos", "状態方程式 (エネルギーは最小を 0 に。先生に重なるほど良い)", frame, "volume_per_atom",
                                ["teacher", "base", "final"],
                                {"volume_per_atom": "体積 (Å³/atom)", "teacher": "SevenNet (先生) (eV/atom)",
                                 "base": "元のポテンシャル (eV/atom)", "final": "ファインチューニング後 (eV/atom)"}))

    rows = [{"teacher": t, "base": s, "final": np.nan} for t, s in (first.get("parity") or [])]
    rows += [{"teacher": t, "base": np.nan, "final": s} for t, s in (last.get("parity_final") or [])]
    if rows:
        out.datasets.append(Dataset("parity", "力の比較 (原子ごとの力の成分。点が y = x の線に近いほど SevenNet に合う)",
                                    pd.DataFrame(rows), "teacher", ["base", "final"],
                                    {"teacher": "SevenNet の力 (eV/Å)", "base": "元のポテンシャル",
                                     "final": "ファインチューニング後"}, style="parity"))

    if result["trained"]:
        try:
            potential_id, name = register(directory, job)
            out.add("登録したポテンシャル", name)
            out.notes.append(f"できたポテンシャル「{name}」をポテンシャルの一覧に登録しました。MD やほかの計算で選べます。"
                             "さらにファインチューニングする出発点にもできます")
        except Exception as exc:   # 登録に失敗しても結果は残す
            out.notes.append(f"ポテンシャルの登録に失敗しました ({type(exc).__name__}: {exc})")
    elif not result["trained"]:
        out.notes.append("最初から誤差がしきい値以下だったので、学び直していません (元のポテンシャルのままで十分です)")
    results.write_collected(directory, out.highlights, out.notes, out.datasets)


def register(directory: Path, job: dict) -> tuple[int, str]:
    """できた nep.txt (と restart) をポテンシャルの保存場所にコピーして、一覧に登録する。何度呼んでも 1 回だけ登録する。"""
    from . import db as database
    marker = directory / "registered.json"
    if marker.is_file():
        data = json.loads(marker.read_text())
        return data["id"], data["name"]
    teacher = job["teacher"]["model"] + (f"/{job['teacher']['modal']}" if job["teacher"].get("modal") else "")
    job_id = int(directory.name)
    conn = database.connect()
    try:
        row = conn.execute("SELECT name FROM jobs WHERE id = ?", (job_id,)).fetchone()
        base_name = ((row["name"] if row else "") or "FT").strip()[:36]
        name = f"{base_name} (FT #{job_id}, {teacher})"[:60]
        elements = potentials.parse_elements(directory / "nep.txt")
        potential_id = conn.execute("INSERT INTO potentials (name, path, elements) VALUES (?, '', ?)",
                                    (name, json.dumps(elements))).lastrowid
        settings.POTENTIAL_DIR.mkdir(parents=True, exist_ok=True)
        path = settings.POTENTIAL_DIR / f"{potential_id}.txt"
        shutil.copy(directory / "nep.txt", path)
        shutil.copy(directory / "nep.restart", path.with_suffix(".restart"))
        conn.execute("UPDATE potentials SET path = ? WHERE id = ?", (str(path), potential_id))
        conn.commit()
    finally:
        conn.close()
    marker.write_text(json.dumps({"id": potential_id, "name": name}, ensure_ascii=False))
    return potential_id, name


FINETUNE = Kind(
    "finetune", "ファインチューニング (SevenNet → NEP)", "発展",
    "SevenNet を先生にして、NEP89 などを対象の材料向けに学び直します。最初に構造を集めて似たものを除いて学び、"
    "そのあとは学習データの範囲から外れた構造 (外挿度 γ、TorchNEP の MaxVol) と SevenNet との誤差が大きい構造だけを"
    "足して学び直すことを繰り返します (能動学習)。"
    "できたポテンシャルは一覧に登録され、GPUMD の速さで使えます",
    [choice("teacher", "先生のモデル (SevenNet)", DEFAULT_TEACHER, TEACHERS,
            hint="精度は MF-ompa (MP と同じ PBE) が高め。7net-0 や Nano は速いが精度は少し落ちます"),
     ptext("temperatures", "MD の温度 (カンマ区切り)", "300,700,1100", r"\d+(\.\d+)?(,\d+(\.\d+)?){0,5}",
           hint="使いたい温度の範囲を覆うように。例: 300,700,1100 (最大 6 個)"),
     choice("ensemble", "MD の条件", "npt", [{"value": "npt", "label": "NPT (体積も変わる)"},
                                         {"value": "nvt", "label": "NVT (体積一定)"}]),
     num("pressure", "圧力", 0, min=-50, max=500, unit="GPa", show_if={"ensemble": "npt"}),
     steps("md_steps", "各温度の MD のステップ数 (1 回ごと)", 10000),
     num("sample_interval", "構造を集める間隔", 200, min=10, max=100000, integer=True, unit="ステップ",
         hint="近すぎる構造は似ているので、数百ステップおきが目安"),
     num("init_max", "0 段目に学ぶ構造の上限", 50, min=0, max=500, integer=True, unit="構造",
         hint="元のポテンシャルで集めた構造から、似たものを除いてこの数まで選んで最初に学びます。"
              "0 にすると 0 段目を飛ばします (ファインチューニング済みのものから続けるとき)"),
     num("init_min", "0 段目に学ぶ構造の下限", 0, min=0, max=500, integer=True, unit="構造",
         hint="似たものを除いた結果がこれに届かなければ、学習データからより外れている (外挿度の高い) 構造から順に足します。"
              "0 なら似たものを除いた分だけ"),
     num("gamma_select", "外挿度 γ のしきい値", 2.0, min=1.0, max=100, unit="",
         hint="学習データの範囲からの外れ具合。これを超えた構造を学びます (1 以下は範囲の中。2 前後がよく使われます)"),
     num("threshold", "力の誤差のしきい値", 0.05, min=0.005, max=2, unit="eV/Å",
         hint="外挿していなくても、まだ学習していない構造の 95% がこれを超えていれば学びます"),
     num("max_iterations", "学び直しの上限 (1 段目から)", 4, min=1, max=10, integer=True, unit="回"),
     num("max_add", "1 段で足す構造の上限", 30, min=1, max=300, integer=True, unit="構造"),
     num("generations", "学習の世代数 (1 回ごと)", 2000, min=100, max=100000, integer=True,
         hint="多いほど学習データに合いますが、時間は比例して増えます"),
     num("population", "学習の個体数", 50, min=10, max=200, integer=True, advanced=True,
         hint="NEP の学習 (進化戦略) の 1 世代で試すパラメータの数"),
     num("n_rattle", "最初に入れる変位・ひずみの構造", 8, min=0, max=100, integer=True, unit="構造", advanced=True,
         hint="状態方程式の 7 構造に加えて入れます")],
    build, lambda calc, c, out: None)

KINDS = [FINETUNE]
