"""ASE + SevenNet (GNN 系の汎用力場) で計算するスクリプト。アプリのジョブの順番待ち (jobs.Scheduler) が、
SevenNet の入った Python (settings.ML_PYTHON、既定はアプリと同じ環境) でジョブのディレクトリを渡して起動する:

    <ML_PYTHON> matsimapp/engines/ase_runner.py <ジョブのディレクトリ>

アプリの部品は読み込まない (ase・numpy・sevenn・phonopy だけで動き、別の環境の Python でも動かせる)。
入力は ase_job.json と model.xyz。出力は GPUMD と同じ形式にして、アプリの後処理・グラフ・解析をそのまま使う:

    gpumd.out   進み具合 ("Run N steps." / "    N steps completed." / "Speed of this run = ..." / 終わりの印)
    thermo.out  温度・運動/ポテンシャルエネルギー・応力 6 成分 (GPa、圧縮が正)・セル 9 成分
    dump.xyz    トラジェクトリ (extxyz、各ステージで保存間隔ごと)
    restart.xyz 最終構造
    ase_result.json  静的計算の結果 (エネルギー・応力・弾性定数・状態方程式)
    omega2.out / D.out  フォノン分散 (GPUMD の compute_phonon と同じ書き方・単位・位相の決まり)

課題 (task):
    md      ステージ (nve / nvt / npt、温度の直線的な変化つき) を順に。NVT は Bussi の熱浴 (正しいカノニカル分布)、
            NPT は Berendsen の圧力制御 (3 辺を別々に伸び縮み。平衡に近づけるのは速いが、ゆらぎは正しくない)
    static  構造最適化 (FIRE、セルも可)・弾性定数 (±ひずみの応力の差)・状態方程式・エネルギー
    phonon  phonopy で力定数を求め、GPUMD と同じ q 点の並び・位相の決まりで動的行列を作って対角化
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from ase import units
from ase.io import read, write

GPA = units.GPa                 # 1 GPa [eV/Å³]
TIME_UNIT = 1.018051e+1         # GPUMD の時間の単位 [fs] (D.out・omega2.out を GPUMD と同じ単位にする)
FINISHED = "Finished running ASE engine (SevenNet)."


class Log:
    """gpumd.out に、GPUMD と同じ書き方で進み具合を書く (アプリが進捗と速さを読む)。"""

    def __init__(self):
        self.out = sys.stdout

    def line(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()


def calculator(job: dict, log: "Log"):
    """計算機。NEP は calorine の CPUNEP (GPUMD がないとき)。SevenNet は cuEquivariance (テンソル積の高速化、
    7〜11 倍速い) を使い、使えなければ使わずに作る。"""
    if job.get("calculator") == "cpunep":
        from calorine.calculators import CPUNEP
        return CPUNEP(job["nep"])
    from sevenn.calculator import SevenNetCalculator
    kwargs = {"modal": job["modal"]} if job.get("modal") else {}
    try:
        calc = SevenNetCalculator(job["model"], device=job.get("device", "cuda"), enable_cueq=True, **kwargs)
    except Exception as exc:   # 高速化の部品がない・合わないとき
        log.line(f"cuEquivariance を使えないので、使わずに計算します ({type(exc).__name__})")
        return SevenNetCalculator(job["model"], device=job.get("device", "cuda"), **kwargs)
    log.line("cuEquivariance (高速化) を使います")
    return calc


def pressure_tensor(atoms, include_kinetic: bool) -> np.ndarray:
    """GPUMD と同じ向き (圧縮が正) の応力 6 成分 [GPa]: xx yy zz yz xz xy。"""
    stress = atoms.get_stress(voigt=True, include_ideal_gas=include_kinetic)   # ASE は引張が正
    return -stress / GPA


def thermo_line(atoms) -> str:
    ke = atoms.get_kinetic_energy()
    temperature = atoms.get_temperature()
    pe = atoms.get_potential_energy()
    p = pressure_tensor(atoms, include_kinetic=True)
    cell = atoms.cell.array.ravel()
    return " ".join(f"{v:.8g}" for v in [temperature, ke, pe, *p, *cell])


def append_frame(path: Path, atoms) -> None:
    frame = atoms.copy()
    frame.calc = None
    write(path, frame, format="extxyz", append=True, columns=["symbols", "positions"], write_info=False)


# ---- MD -------------------------------------------------------------------------------

def run_md(job: dict, atoms, directory: Path, log: Log) -> None:
    from ase.md.bussi import Bussi
    from ase.md.nptberendsen import Inhomogeneous_NPTBerendsen
    from ase.md.velocitydistribution import MaxwellBoltzmannDistribution, Stationary
    from ase.md.verlet import VelocityVerlet
    from ase.optimize import FIRE

    dt = job["time_step"] * units.fs
    thermo_every = job["thermo_interval"]
    traj_every = job["traj_interval"]
    if job.get("minimize"):
        log.line("Minimize (FIRE, 0.01 eV/Å まで)")
        FIRE(atoms, logfile=None).run(fmax=0.01, steps=1000)
    first = job["stages"][0]
    t0 = first.get("temperature") or 300.0
    rng = np.random.default_rng(job.get("seed", 1))
    MaxwellBoltzmannDistribution(atoms, temperature_K=t0, rng=rng)
    Stationary(atoms)
    thermo = open(directory / "thermo.out", "w")
    for stage in job["stages"]:
        steps = stage["steps"]
        log.line(f"Run {steps} steps.")
        start = time.time()
        done = 0
        while done < steps:
            n = min(thermo_every, steps - done)
            # 温度の直線的な変化: この区切りの真ん中の温度にする
            frac = (done + n / 2) / steps
            t_start = stage.get("temperature") or t0
            t_end = stage.get("temperature_end") or t_start
            temperature = t_start + (t_end - t_start) * frac
            if stage["ensemble"] == "nve":
                dyn = VelocityVerlet(atoms, timestep=dt)
            elif stage["ensemble"] == "nvt":
                dyn = Bussi(atoms, timestep=dt, temperature_K=temperature, taut=100 * dt, rng=rng)
            else:
                dyn = Inhomogeneous_NPTBerendsen(atoms, timestep=dt, temperature_K=temperature,
                                                 pressure_au=stage.get("pressure", 0.0) * GPA,
                                                 taut=100 * dt, taup=1000 * dt, compressibility_au=1 / (100 * GPA))
            # トラジェクトリはステージの中で数えて保存間隔ごと (GPUMD と同じ: run ごとに数え直す)
            for _ in range(n):
                dyn.run(1)
                done += 1
                if traj_every and done % traj_every == 0:
                    append_frame(directory / "dump.xyz", atoms)
            thermo.write(thermo_line(atoms) + "\n")
            thermo.flush()
            log.line(f"    {done} steps completed.")
        elapsed = max(time.time() - start, 1e-9)
        log.line(f"Speed of this run = {len(atoms) * steps / elapsed:.6g} atom*step/second.")
    thermo.close()
    final = atoms.copy()
    final.calc = None
    write(directory / "restart.xyz", final, format="extxyz")


# ---- 静的計算 ------------------------------------------------------------------------

def relax(atoms, relax_cell: bool, steps: int, fmax: float = 0.005):
    from ase.filters import FrechetCellFilter
    from ase.optimize import FIRE
    target = FrechetCellFilter(atoms) if relax_cell else atoms
    opt = FIRE(target, logfile=None)
    opt.run(fmax=fmax, steps=steps)
    return opt.nsteps


def elastic_matrix(atoms, strain: float) -> np.ndarray:
    """原子を動かさない (clamped-ion) ±ひずみの応力の差から C_ij [GPa]。"""
    base = atoms.cell.array.copy()
    c = np.zeros((6, 6))
    voigt = [(0, 0), (1, 1), (2, 2), (1, 2), (0, 2), (0, 1)]
    for j, (a, b) in enumerate(voigt):
        stresses = []
        for sign in (1, -1):
            eps = np.zeros((3, 3))
            if a == b:
                eps[a, a] = sign * strain
            else:
                eps[a, b] = eps[b, a] = sign * strain / 2
            trial = atoms.copy()
            trial.calc = atoms.calc
            trial.set_cell(base @ (np.eye(3) + eps), scale_atoms=True)
            stresses.append(trial.get_stress(voigt=True) / GPA)    # 引張が正 (弾性定数の向き)
        c[:, j] = (stresses[0] - stresses[1]) / (2 * strain)
    return (c + c.T) / 2


def moduli(c: np.ndarray) -> dict:
    """Voigt-Reuss-Hill の平均の体積弾性率・剛性率と、ヤング率・ポアソン比 [GPa]。"""
    bv = (c[0, 0] + c[1, 1] + c[2, 2] + 2 * (c[0, 1] + c[1, 2] + c[0, 2])) / 9
    gv = (c[0, 0] + c[1, 1] + c[2, 2] - c[0, 1] - c[1, 2] - c[0, 2] + 3 * (c[3, 3] + c[4, 4] + c[5, 5])) / 15
    try:
        s = np.linalg.inv(c)
        br = 1 / (s[0, 0] + s[1, 1] + s[2, 2] + 2 * (s[0, 1] + s[1, 2] + s[0, 2]))
        gr = 15 / (4 * (s[0, 0] + s[1, 1] + s[2, 2]) - 4 * (s[0, 1] + s[1, 2] + s[0, 2]) + 3 * (s[3, 3] + s[4, 4] + s[5, 5]))
    except np.linalg.LinAlgError:
        br, gr = bv, gv
    b, g = (bv + br) / 2, (gv + gr) / 2
    e = 9 * b * g / (3 * b + g)
    nu = (3 * b - 2 * g) / (2 * (3 * b + g))
    return {"bulk": b, "shear": g, "young": e, "poisson": nu}


def run_static(job: dict, atoms, directory: Path, log: Log) -> None:
    o = job["options"]
    result: dict = {}
    total = 1 + (1 if o.get("relax") else 0) + (12 if o.get("elastic") else 0) + (21 if o.get("cohesive") else 0)
    log.line(f"Run {total} steps.")
    done = 0
    if o.get("relax"):
        result["relax_steps"] = relax(atoms, o.get("relax_cell", False), o.get("relax_steps", 1000))
        done += 1
        log.line(f"    {done} steps completed.")
    n = len(atoms)
    result["energy_per_atom"] = float(atoms.get_potential_energy() / n)
    result["stress"] = pressure_tensor(atoms, include_kinetic=False).tolist()
    result["max_force"] = float(np.abs(atoms.get_forces()).max())
    done += 1
    log.line(f"    {done} steps completed.")
    if o.get("elastic"):
        c = elastic_matrix(atoms, o.get("strain", 0.01))
        result["elastic"] = c.tolist()
        result["moduli"] = moduli(c)
        done += 12
        log.line(f"    {done} steps completed.")
    if o.get("cohesive"):
        base = atoms.cell.array.copy()
        frac = atoms.get_scaled_positions()
        rows = []
        for scale in np.linspace(o.get("e1", 0.9), o.get("e2", 1.1), 21):
            trial = atoms.copy()
            trial.calc = atoms.calc
            trial.set_cell(base * scale, scale_atoms=False)
            trial.set_scaled_positions(frac)
            rows.append({"scale": float(scale), "volume": float(trial.get_volume()),
                         "energy": float(trial.get_potential_energy()),
                         "energy_per_atom": float(trial.get_potential_energy() / n)})
        result["cohesive"] = rows
        done += 21
        log.line(f"    {done} steps completed.")
    log.line("Speed of this run = 0 atom*step/second.")
    (directory / "ase_result.json").write_text(json.dumps(result, indent=1))
    final = atoms.copy()
    final.calc = None
    write(directory / "restart.xyz", final, format="extxyz")


# ---- フォノン --------------------------------------------------------------------------

def kpath(directory: Path, cell: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """GPUMD と同じ q 点 (高対称点のあいだを逆格子の直交座標で 100 等分)、経路の長さ、高対称点の名前。"""
    frac, labels = [], []
    for line in (directory / "kpoints.in").read_text().splitlines():
        tokens = line.split()
        if len(tokens) >= 4:
            frac.append([float(v) for v in tokens[:3]])
            labels.append(tokens[3])
    recip = 2 * math.pi * np.linalg.inv(cell).T
    hsp = np.array(frac) @ recip
    points, dist = [hsp[0]], [0.0]
    for a in range(1, len(hsp)):
        for j in range(1, 101):
            k = hsp[a - 1] + (hsp[a] - hsp[a - 1]) * j * 0.01
            dist.append(dist[-1] + float(np.linalg.norm(k - points[-1])))
            points.append(k)
    return np.array(points), np.array(dist), labels


def run_phonon(job: dict, atoms, directory: Path, log: Log) -> None:
    from phonopy import Phonopy
    from phonopy.structure.atoms import PhonopyAtoms
    o = job["options"]
    if o.get("relax"):
        relax(atoms, False, 1000, fmax=0.001)
    unit = PhonopyAtoms(symbols=atoms.get_chemical_symbols(), cell=atoms.cell.array,
                        scaled_positions=atoms.get_scaled_positions())
    phonon = Phonopy(unit, supercell_matrix=np.diag(job["replicate"]), primitive_matrix=None)
    phonon.generate_displacements(distance=o.get("displacement", 0.01))
    supercells = phonon.supercells_with_displacements
    log.line(f"Run {len(supercells)} steps.")
    start = time.time()
    forces = []
    from ase import Atoms
    for k, sc in enumerate(supercells, 1):
        trial = Atoms(sc.symbols, cell=sc.cell, scaled_positions=sc.scaled_positions, pbc=True)
        trial.calc = atoms.calc
        f = trial.get_forces()
        forces.append(f - f.mean(axis=0))     # 並進のずれ (数値誤差) を除く
        log.line(f"    {k} steps completed.")
    phonon.forces = np.array(forces)
    phonon.produce_force_constants()
    kpts, dist, labels = kpath(directory, atoms.cell.array)
    # 動的行列は phonopy に作らせる (超格子の境目で等距離になる像を、すべて平均して扱う)。
    # phonopy の動的行列も、GPUMD と同じく原子の位置の差で位相を付け、単位は eV/Å²/amu。q は逆格子の分数座標で渡す
    cell = atoms.cell.array
    dm = phonon.dynamical_matrix
    dmat = []
    for k in kpts:
        dm.run(k @ cell.T / (2 * math.pi))
        dmat.append(np.array(dm.dynamical_matrix))
    dmat = np.array(dmat)
    dmat = (dmat + np.conj(np.transpose(dmat, (0, 2, 1)))) / 2
    to_omega2 = 1.0e6 / TIME_UNIT ** 2
    with open(directory / "D.out", "w") as out:
        for d in dmat:
            for row in d:
                out.write(" ".join(f"{v:.8g}" for v in row.real) + " " + " ".join(f"{v:.8g}" for v in row.imag) + "\n")
    with open(directory / "omega2.out", "w") as out:
        ticks = [dist[i * 100] for i in range(len(labels))]
        out.write("# " + " ".join(f"{v:.6f}" for v in ticks) + " " + " ".join(labels) + "\n")
        for k, d in enumerate(dmat):
            w2 = np.linalg.eigvalsh(d) * to_omega2
            out.write(f"{dist[k]:.6f} " + " ".join(f"{v:.8g}" for v in w2) + "\n")
    elapsed = max(time.time() - start, 1e-9)
    log.line(f"Speed of this run = {len(phonon.supercell) * len(supercells) / elapsed:.6g} atom*step/second.")


def main(directory: Path) -> None:
    log = Log()
    job = json.loads((directory / "ase_job.json").read_text())
    if job.get("calculator") == "cpunep":
        log.line(f"MatSimApp ASE エンジン: NEP ({Path(job['nep']).name}) を CPU (calorine) で計算します")
    else:
        log.line(f"MatSimApp ASE エンジン: SevenNet ({job['model']}{' / ' + job['modal'] if job.get('modal') else ''}) で計算します")
    atoms = read(directory / "model.xyz", format="extxyz")
    atoms.pbc = True
    atoms.calc = calculator(job, log)
    {"md": run_md, "static": run_static, "phonon": run_phonon}[job["task"]](job, atoms, directory, log)
    log.line(FINISHED)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
