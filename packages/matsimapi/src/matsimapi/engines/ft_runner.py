"""NEP のファインチューニング (SevenNet を先生にした蒸留 + 能動学習) を順に進めるスクリプト。
アプリのジョブの順番待ち (jobs.Scheduler) が、SevenNet の入った Python (settings.ML_PYTHON) で起動する:

    <ML_PYTHON> matsimapp/engines/ft_runner.py <ジョブのディレクトリ>

アプリの部品は読み込まない (ase・numpy・sevenn・torchnep と、GPUMD の gpumd・nep だけで動く)。入力は ft_job.json と model.xyz。

0 段目 (iter_00/): 元のポテンシャル (NEP89 など) で MD をして構造を多めに集め、ダブりを除いて (TorchNEP の D 最適な選び方)
    SevenNet で計算し、1 回学ぶ。元のポテンシャルと先生のエネルギーの基準のずれもここでそろう。
1 段目から (iter_XX/):
    active/      いまの学習データの原子環境から MaxVol の active set を作る (TorchNEP)
    explore_*/   いまの NEP で MD をして構造を集める
    predict/     集めた構造を SevenNet で計算し、いまの NEP の誤差を出す。外挿度 γ も計算する (TorchNEP)
    train/       γ がしきい値を超えた構造 (似たものを除く) と、誤差の大きい構造を足して学び直す
    外挿している構造がなく、誤差の 95% がしきい値以下になったら終える。

最初の学習データには、元の構造の伸び縮み (状態方程式) とランダムな変位の構造も入れる。

出力:
    gpumd.out        進み具合の文章と終わりの印 (アプリがジョブの完了を判定する)
    progress.json    進み具合 {"done", "total", "phase"}
    ft_result.json   段ごとの誤差・外挿度・学習の誤差・状態方程式の比較・力の比較 (アプリが結果の画面にする)
    nep.txt / nep.restart  できたポテンシャル (アプリがポテンシャルの一覧に登録する。restart はさらに学び直すときに使う)
    train.xyz        最後に使った学習データ (SevenNet のラベル付き)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from ase import Atoms
from ase.io import read

FINISHED = "Finished running fine-tuning (NEP <- SevenNet)."
MAX_TEACHER_FORCE = 50.0   # これより大きい力の構造は壊れた構造とみなして使わない [eV/Å]


class Run:
    def __init__(self, directory: Path, job: dict):
        self.dir = directory
        self.job = job
        self.done = 0
        self.total = job["total_units"]
        self.phase = ""

    def log(self, text: str) -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def progress(self, extra: int = 0, phase: str | None = None) -> None:
        if phase is not None:
            self.phase = phase
        data = {"done": min(self.done + extra, self.total), "total": self.total, "phase": self.phase}
        tmp = self.dir / "progress.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(self.dir / "progress.json")

    def run(self, command: list[str], cwd: Path, *, watch: Path | None = None, unit_per_line: int = 1,
            limit: int = 0) -> int:
        """外部のプログラム (gpumd・nep) を動かし、watch のファイルの行数で進み具合を更新する。"""
        env = dict(os.environ)
        if self.job.get("cuda_bin"):   # nep は学習の計算をその場で nvcc でコンパイルする (GPU に合う版の nvcc を先に)
            env["PATH"] = self.job["cuda_bin"] + os.pathsep + env.get("PATH", "")
        with open(cwd / "out.txt", "w") as out, open(cwd / "err.txt", "w") as err:
            proc = subprocess.Popen(command, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env)
            while proc.poll() is None:
                time.sleep(2)
                if watch is not None:
                    self.progress(min(limit, count_lines(watch) * unit_per_line))
        return proc.returncode


def count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as handle:
            return handle.read().count(b"\n")
    except OSError:
        return 0


# ---- 先生 (SevenNet) ---------------------------------------------------------------------

def teacher(job: dict, run: Run):
    from sevenn.calculator import SevenNetCalculator
    model, modal = job["teacher"]["model"], job["teacher"].get("modal")
    kwargs = {"modal": modal} if modal else {}
    try:
        calc = SevenNetCalculator(model, device="cuda", enable_cueq=True, **kwargs)
    except Exception as exc:   # 高速化の部品がない・合わないとき
        run.log(f"cuEquivariance を使えないので、使わずに計算します ({type(exc).__name__})")
        return SevenNetCalculator(model, device="cuda", **kwargs)
    return calc


def label(frames: list[Atoms], calc) -> list[Atoms]:
    """SevenNet でエネルギー・力・ビリアルを付ける。壊れた構造 (力が大きすぎる) は除く。"""
    labelled = []
    for atoms in frames:
        atoms = Atoms(atoms.get_chemical_symbols(), positions=atoms.positions, cell=atoms.cell, pbc=True)
        atoms.calc = calc
        energy = float(atoms.get_potential_energy())
        forces = np.array(atoms.get_forces())
        virial = -np.array(atoms.get_stress(voigt=False)) * atoms.get_volume()   # NEP の train.xyz の書き方
        if not np.isfinite(energy) or not np.all(np.isfinite(forces)) or np.abs(forces).max() > MAX_TEACHER_FORCE:
            continue
        atoms.calc = None
        atoms.info = {"energy": energy, "virial": virial}
        atoms.arrays["forces"] = forces
        labelled.append(atoms)
    return labelled


def write_train(path: Path, frames: list[Atoms]) -> None:
    """nep の train.xyz (extxyz。energy・virial・forces)。"""
    with open(path, "w") as out:
        for atoms in frames:
            lattice = " ".join(f"{v:.10f}" for v in atoms.cell.array.ravel())
            virial = " ".join(f"{v:.10f}" for v in np.asarray(atoms.info["virial"]).ravel())
            out.write(f"{len(atoms)}\nenergy={atoms.info['energy']:.10f} virial=\"{virial}\" Lattice=\"{lattice}\" "
                      "pbc=\"T T T\" Properties=species:S:1:pos:R:3:forces:R:3\n")
            for symbol, p, f in zip(atoms.get_chemical_symbols(), atoms.positions, atoms.arrays["forces"]):
                out.write(f"{symbol} {p[0]:.10f} {p[1]:.10f} {p[2]:.10f} {f[0]:.10f} {f[1]:.10f} {f[2]:.10f}\n")


# ---- NEP (生徒) ---------------------------------------------------------------------------

def model_types(nep_txt: Path) -> list[str]:
    tokens = nep_txt.read_text(errors="replace").split("\n", 1)[0].split()
    return tokens[2:2 + int(tokens[1])]


def nep_in(job: dict, types: list[str], extra: list[str]) -> str:
    return "\n".join([f"type {len(types)} {' '.join(types)}", *job["hyper"], *extra]) + "\n"


def predict(job: dict, run: Run, model: Path, frames: list[Atoms], directory: Path) -> dict:
    """nep の prediction で、構造ごとのエネルギー・力を計算して先生と比べる。"""
    directory.mkdir(parents=True, exist_ok=True)
    write_train(directory / "train.xyz", frames)
    link = directory / "nep.txt"
    link.unlink(missing_ok=True)
    link.symlink_to(model.resolve())
    (directory / "nep.in").write_text(nep_in(job, model_types(model), ["prediction 1"]))
    if run.run([job["nep"]], directory) != 0:
        raise RuntimeError(f"nep の予測が失敗しました ({tail(directory / 'err.txt')})")
    force = np.loadtxt(directory / "force_train.out", ndmin=2)        # 予測 3 列 + 先生 3 列 (原子ごと)
    energy = np.loadtxt(directory / "energy_train.out", ndmin=2)      # 予測・先生 [eV/atom] (構造ごと)
    errors, start = [], 0
    for atoms in frames:
        d = force[start:start + len(atoms), :3] - force[start:start + len(atoms), 3:]
        errors.append(float(np.sqrt(np.mean(d ** 2))))
        start += len(atoms)
    return {"force_rmse": np.array(errors), "force": force, "energy": energy}


def tail(path: Path) -> str:
    try:
        lines = [x for x in path.read_text(errors="replace").splitlines() if x.strip()]
    except OSError:
        return ""
    return lines[-1][:200] if lines else ""


def train(job: dict, run: Run, base: tuple[Path, Path], frames: list[Atoms], directory: Path) -> dict:
    """学び直す。base は (nep.txt, nep.restart)。

    GPUMD の fine_tune の出発点は 89 元素の NEP89 だけ。それ以外 (前の回やこのアプリでファインチューニングしたもの) は、
    nep.txt・nep.restart を作業フォルダに置いて学習の続きとして進める (import_q_scaler で記述子の規格化も引き継ぐ)。
    """
    directory.mkdir(parents=True, exist_ok=True)
    write_train(directory / "train.xyz", frames)
    extra = ["lambda_1 0", "lambda_e 1", "lambda_f 1", "lambda_v 1", f"batch {max(len(frames), 1)}",
             f"population {job['population']}", f"generation {job['generations']}"]
    types = model_types(base[0])
    if len(types) == 89:
        (directory / "nep.in").write_text(
            f"fine_tune {base[0].resolve()} {base[1].resolve()}\n" + nep_in(job, job["elements"], extra))
    else:
        shutil.copy(base[0], directory / "nep.txt")
        shutil.copy(base[1], directory / "nep.restart")
        (directory / "nep.in").write_text(nep_in(job, types, extra + ["import_q_scaler 1"]))
    # loss.out は 100 世代ごとに 1 行
    code = run.run([job["nep"]], directory, watch=directory / "loss.out", unit_per_line=100, limit=job["generations"])
    if code != 0 or not (directory / "loss.out").is_file():
        raise RuntimeError(f"nep のファインチューニングが失敗しました ({tail(directory / 'err.txt')})")
    loss = np.loadtxt(directory / "loss.out", ndmin=2)
    # 列: 世代・損失 (全体・L1・L2)・学習データの RMSE (エネルギー eV/atom・力 eV/Å・ビリアル eV/atom)
    return {"energy_rmse": float(loss[-1, 4]), "force_rmse": float(loss[-1, 5]), "virial_rmse": float(loss[-1, 6]),
            "curve": loss[:, [0, 4, 5, 6]].tolist()}


# ---- 探索の MD (GPUMD) -------------------------------------------------------------------

def explore(job: dict, run: Run, model: Path, atoms: Atoms, temperature: float, directory: Path) -> list[Atoms]:
    """いまの NEP で MD をして、一定の間隔で構造を集める。

    外挿度 γ は、集めた構造で TorchNEP が計算する (grades)。GPUMD の compute_extrapolation は MD 中に見張れるが、
    managed メモリを CPU と GPU から同時に触るので、それができない環境 (WSL2 など、concurrentManagedAccess = 0) では
    落ちる。そのため使わない。
    """
    from ase.io import write
    directory.mkdir(parents=True, exist_ok=True)
    write(directory / "model.xyz", atoms, format="extxyz")
    steps, interval, tau = job["md_steps"], job["sample_interval"], 100
    if job["ensemble"] == "npt":   # 等方的な圧力制御 (どんなセルの形でも使える)。弾性率は目安の値でよい
        ensemble = f"ensemble npt_scr {temperature:g} {temperature:g} {tau} {job['pressure']:g} 100 1000"
    else:
        ensemble = f"ensemble nvt_bdp {temperature:g} {temperature:g} {tau}"
    (directory / "run.in").write_text("\n".join([
        f"potential {model.resolve()}", f"velocity {temperature:g}", f"time_step {job['time_step']:g}", ensemble,
        f"dump_thermo {interval}", f"dump_xyz {interval} dump.xyz", f"run {steps}"]) + "\n")
    code = run.run([job["gpumd"]], directory, watch=directory / "thermo.out", unit_per_line=interval, limit=steps)
    run.done += steps
    try:
        frames = list(read(directory / "dump.xyz", index=":", format="extxyz"))
    except Exception:
        frames = []
    if code != 0:
        run.log(f"  {temperature:g} K の MD が途中で止まりました ({tail(directory / 'err.txt')})。集めた {len(frames)} 構造を使います")
    return frames


def initial_frames(atoms: Atoms, job: dict) -> list[Atoms]:
    """元の構造の伸び縮み (状態方程式) と、ランダムな変位・ひずみの構造。"""
    rng = np.random.default_rng(job.get("seed", 1))
    frames = []
    for scale in job["eos_scales"]:
        a = atoms.copy()
        a.set_cell(atoms.cell.array * scale, scale_atoms=True)
        frames.append(a)
    for _ in range(job["n_rattle"]):
        a = atoms.copy()
        strain = np.eye(3) + rng.normal(0, 0.01, (3, 3))
        a.set_cell(atoms.cell.array @ ((strain + strain.T) / 2), scale_atoms=True)
        a.positions += rng.normal(0, 0.05, a.positions.shape)
        frames.append(a)
    return frames


# ---- 外挿度 γ (TorchNEP の MaxVol) -------------------------------------------------------

def write_positions(path: Path, frames: list[Atoms]) -> None:
    """ラベルのない構造 (座標とセルだけ) の extxyz。"""
    with open(path, "w") as out:
        for atoms in frames:
            lattice = " ".join(f"{v:.10f}" for v in atoms.cell.array.ravel())
            out.write(f"{len(atoms)}\nLattice=\"{lattice}\" pbc=\"T T T\" Properties=species:S:1:pos:R:3\n")
            for symbol, p in zip(atoms.get_chemical_symbols(), atoms.positions):
                out.write(f"{symbol} {p[0]:.10f} {p[1]:.10f} {p[2]:.10f}\n")


def active_set(model: Path, train_set: list[Atoms], directory: Path):
    """学習データの原子環境から、MaxVol の active set を作る。"""
    from torchnep.extrapolation import build_active_set
    directory.mkdir(parents=True, exist_ok=True)
    write_train(directory / "train_now.xyz", train_set)
    return build_active_set(str(model), str(directory / "train_now.xyz"), str(directory / "active_set.pt"),
                            verbose=False)


def grades(model: Path, aset, frames: list[Atoms], directory: Path) -> np.ndarray:
    """構造ごとの外挿度 (原子の中で最大の max(γ, γ_res))。1 以下なら学習データの範囲の中。"""
    from torchnep.extrapolation import compute_gamma
    write_positions(directory / "graded.xyz", frames)
    return np.asarray(compute_gamma(str(model), aset, str(directory / "graded.xyz"), verbose=False)["grade"])


def pick_diverse(model: Path, aset, frames: list[Atoms], directory: Path, max_frames: int,
                 grade_min: float | None = None, min_frames: int = 0) -> tuple[list[int], int]:
    """外挿度の高い順に、すでに選んだものと似ていない構造だけを選ぶ (TorchNEP の D 最適な選び方。ダブりを除く)。

    min_frames に届かなければ、残りの構造から外挿度 (最初の学習データに対して) の高い順に足して埋める。
    戻り値は (選んだ構造の番号, そのうち似たものを除いて選んだ数)。
    """
    from torchnep.extrapolation import select_structures
    if not frames or max_frames <= 0:
        return [], 0
    write_positions(directory / "pool.xyz", frames)
    chosen = select_structures(str(model), aset, str(directory / "pool.xyz"), max_frames=max_frames,
                               grade_min=grade_min, verbose=False)
    picked = [int(i) for i in chosen["index"]]
    n_diverse = len(picked)
    if len(picked) < min(min_frames, len(frames)):
        rest = [i for i in np.argsort(-np.asarray(chosen["grade"])) if int(i) not in set(picked)]
        picked += [int(i) for i in rest[:min(min_frames, max_frames) - len(picked)]]
    return picked, n_diverse


def select(errors: np.ndarray, threshold: float, max_add: int, skip: set[int] = frozenset()) -> list[int]:
    """誤差がしきい値を超えた構造から、MD の時間の並びで偏らないよう等間隔に max_add 個まで選ぶ。"""
    over = [i for i, e in enumerate(errors) if e > threshold and i not in skip]
    if len(over) <= max_add:
        return over
    picks = np.linspace(0, len(over) - 1, max_add).round().astype(int)
    return [over[i] for i in sorted(set(picks))]


def summary(errors: np.ndarray) -> dict:
    return {"mean": float(np.mean(errors)), "p95": float(np.percentile(errors, 95)), "max": float(np.max(errors))}


def parity(result: dict, n: int = 3000) -> list[list[float]]:
    """力の比較 (先生・生徒) の点を、多すぎないよう間引く。"""
    force = result["force"]
    teacher_f, student_f = force[:, 3:].ravel(), force[:, :3].ravel()
    step = max(1, len(teacher_f) // n)
    return [[round(float(t), 5), round(float(s), 5)] for t, s in zip(teacher_f[::step], student_f[::step])]


def error_row(it: int, candidates: list[Atoms], result: dict) -> dict:
    stats = summary(result["force_rmse"])
    energy_rmse = float(np.sqrt(np.mean((result["energy"][:, 0] - result["energy"][:, 1]) ** 2)))
    return {"iteration": it, "n_candidates": len(candidates), "force_mean": stats["mean"], "force_p95": stats["p95"],
            "force_max": stats["max"], "energy_rmse": energy_rmse}


def main(directory: Path) -> None:
    job = json.loads((directory / "ft_job.json").read_text())
    run = Run(directory, job)
    base = (Path(job["base_txt"]), Path(job["base_restart"]))
    run.log(f"MatSimApp ファインチューニング: {base[0].name} を、SevenNet ({job['teacher']['model']}"
            f"{' / ' + job['teacher']['modal'] if job['teacher'].get('modal') else ''}) を先生にして学び直します")
    atoms = read(directory / "model.xyz", format="extxyz")
    atoms.pbc = True
    run.progress(phase="SevenNet を読み込んでいます")
    calc = teacher(job, run)

    run.progress(phase="伸び縮み・変位の構造を SevenNet で計算しています")
    seed = label(initial_frames(atoms, job), calc)
    eos = seed[:len(job["eos_scales"])]
    train_set = list(seed)
    model, restart = base
    base_eos = predict(job, run, base[0], eos, directory / "eos_base")
    iterations = []
    first_parity = None

    # ---- 0 段目: 元のポテンシャルで構造を多めに集め、ダブりを除いて学ぶ (エネルギーの基準のずれもここでそろう) ----
    if job["init_max"] > 0:
        start = time.time()
        folder = directory / "iter_00"
        pool = []
        for temperature in job["temperatures"]:
            run.progress(phase=f"0 段目: {temperature:g} K の MD (元のポテンシャル) で構造を集めています")
            run.log(f"[0 段目] {temperature:g} K の MD (元のポテンシャル)")
            pool += explore(job, run, model, atoms, temperature, folder / f"explore_{temperature:g}K")
        run.progress(phase=f"0 段目: 集めた {len(pool)} 構造からダブりを除いています")
        aset = active_set(model, seed, folder / "select")
        picked, n_diverse = pick_diverse(model, aset, pool, folder / "select", job["init_max"],
                                         min_frames=job.get("init_min", 0))
        chosen = label([pool[i] for i in picked], calc)
        run.log(f"  集めた {len(pool)} 構造から、似ていない {n_diverse} 構造を選びました"
                + (f"。下限 {job['init_min']} まで外挿度の高い順に {len(picked) - n_diverse} 構造を足します"
                   if len(picked) > n_diverse else ""))
        if not chosen:
            raise RuntimeError("0 段目で構造を集められませんでした (MD がすぐに止まった可能性があります)")
        result = predict(job, run, model, chosen, folder / "predict")
        row = error_row(0, chosen, result)
        first_parity = parity(result)
        run.log(f"  元のポテンシャルの力の誤差: 平均 {row['force_mean']:.4f}、95% {row['force_p95']:.4f} eV/Å"
                f" (エネルギー {row['energy_rmse'] * 1000:.1f} meV/atom)")
        train_set += chosen
        run.progress(phase=f"0 段目: {len(train_set)} 構造でファインチューニングしています")
        trained = train(job, run, (model, restart), train_set, folder / "train")
        run.done += job["generations"]
        model, restart = folder / "train" / "nep.txt", folder / "train" / "nep.restart"
        row.update({"n_pool": len(pool), "n_added": len(chosen), "n_train": len(train_set),
                    "train_energy_rmse": trained["energy_rmse"], "train_force_rmse": trained["force_rmse"],
                    "train_virial_rmse": trained["virial_rmse"], "seconds": time.time() - start})
        iterations.append(row)
        run.log(f"  学習データでの誤差: 力 {trained['force_rmse']:.4f} eV/Å、エネルギー {trained['energy_rmse'] * 1000:.2f} meV/atom")

    # ---- 1 段目から: 外挿度 γ (MaxVol) と、先生との誤差の両方で、学ぶ必要があるかを決める ----
    converged = False
    for it in range(1, job["max_iterations"] + 2):
        start = time.time()
        folder = directory / f"iter_{it:02d}"
        run.progress(phase=f"{it} 段目: 学習データの範囲 (active set) を作っています")
        aset = active_set(model, train_set, folder / "active")
        frames = []
        for temperature in job["temperatures"]:
            run.progress(phase=f"{it} 段目: {temperature:g} K の MD で構造を集めています")
            run.log(f"[{it} 段目] {temperature:g} K の MD (いまの NEP)")
            frames += explore(job, run, model, atoms, temperature, folder / f"explore_{temperature:g}K")
        run.progress(phase=f"{it} 段目: 集めた {len(frames)} 構造を SevenNet で計算しています")
        candidates = label(frames, calc)
        if not candidates:
            raise RuntimeError("MD で構造を集められませんでした (MD がすぐに止まった可能性があります)")
        run.progress(phase=f"{it} 段目: いまの NEP の誤差と外挿度を調べています")
        result = predict(job, run, model, candidates, folder / "predict")
        errors = result["force_rmse"]
        gamma = grades(model, aset, candidates, folder / "active")
        row = error_row(it, candidates, result)
        over_gamma = [i for i, g in enumerate(gamma) if g > job["gamma_select"]]
        row.update({"gamma_max": float(gamma.max()), "gamma_p95": float(np.percentile(gamma, 95)),
                    "n_gamma_over": len(over_gamma), "n_error_over": int((errors > job["threshold"]).sum())})
        if first_parity is None:
            first_parity = parity(result)
        run.log(f"  力の誤差: 平均 {row['force_mean']:.4f}、95% {row['force_p95']:.4f} eV/Å "
                f"(エネルギー {row['energy_rmse'] * 1000:.1f} meV/atom)。外挿度 γ: 最大 {row['gamma_max']:.2f}、"
                f"{job['gamma_select']:g} を超えた構造 {len(over_gamma)}")
        done = not over_gamma and row["force_p95"] <= job["threshold"]
        if done or it == job["max_iterations"] + 1:
            converged = done
            row.update({"n_added": 0, "n_train": len(train_set), "seconds": time.time() - start,
                        "parity_final": parity(result)})
            iterations.append(row)
            run.log("  学習データの範囲の外の構造がなく、誤差もしきい値以下になりました。ここで終えます" if done
                    else "  繰り返しの上限に達しました。最後のポテンシャルを結果にします")
            break
        # 外挿している構造は似たものを除いて選び、残りの枠で、範囲の中だが誤差の大きい構造を足す
        diverse, _ = pick_diverse(model, aset, [candidates[i] for i in over_gamma], folder / "active", job["max_add"],
                                  job["gamma_select"])
        picked = [over_gamma[i] for i in diverse]
        picked += select(errors, job["threshold"], job["max_add"] - len(picked), skip=set(picked))
        train_set += [candidates[i] for i in picked]
        run.log(f"  学ぶ必要のある {len(picked)} 構造を足します (外挿 {len(over_gamma)}・誤差 {row['n_error_over']} から。"
                f"合計 {len(train_set)} 構造)")
        run.progress(phase=f"{it} 段目: {len(train_set)} 構造で学び直しています")
        trained = train(job, run, (model, restart), train_set, folder / "train")
        run.done += job["generations"]
        model, restart = folder / "train" / "nep.txt", folder / "train" / "nep.restart"
        row.update({"n_added": len(picked), "n_train": len(train_set), "train_energy_rmse": trained["energy_rmse"],
                    "train_force_rmse": trained["force_rmse"], "train_virial_rmse": trained["virial_rmse"],
                    "seconds": time.time() - start})
        iterations.append(row)
        run.log(f"  学習データでの誤差: 力 {trained['force_rmse']:.4f} eV/Å、エネルギー {trained['energy_rmse'] * 1000:.2f} meV/atom")

    iterations[0]["parity"] = first_parity
    trained_any = model != base[0]
    final_eos = predict(job, run, model, eos, directory / "eos_final") if trained_any else base_eos
    if trained_any:
        shutil.copy(model, directory / "nep.txt")
        shutil.copy(restart, directory / "nep.restart")
    write_train(directory / "train.xyz", train_set)
    n = len(atoms)
    result = {
        "converged": converged, "trained": trained_any, "threshold": job["threshold"],
        "gamma_select": job["gamma_select"], "iterations": iterations,
        "eos": {"scale": job["eos_scales"], "volume_per_atom": [a.get_volume() / n for a in eos],
                "teacher": base_eos["energy"][:, 1].tolist(), "base": base_eos["energy"][:, 0].tolist(),
                "final": final_eos["energy"][:, 0].tolist()},
        "n_train": len(train_set), "n_atoms": n,
    }
    (directory / "ft_result.json").write_text(json.dumps(result, ensure_ascii=False))
    run.done = run.total
    run.progress(phase="終わりました")
    run.log(FINISHED)


if __name__ == "__main__":
    os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    main(Path(sys.argv[1]).resolve())
