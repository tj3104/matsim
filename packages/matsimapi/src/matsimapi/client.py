"""Python から MatSimAPI を使うための入口。

    from matsimapi import Client

    client = Client()                      # 既定のデータ置き場 (MatSimApp と同じ場所を指せば内容を共有する)
    cu = client.add_sample("Cu")           # 構造の登録。ファイルのパスや ASE の Atoms も渡せる
    client.preview("md", cu, stages=...)   # 実行前の確認 (所要時間の目安・run.in)
    job = client.submit("md", cu, repeat=(3, 3, 3),
                        stages=[{"ensemble": "nvt", "steps": 1000, "temperature": 300}])
    job.wait()                             # 完了を待つ。失敗したら JobFailed
    job.results()                          # 要点の数値と表の一覧
    job.dataset("thermo")                  # 表 1 つ分のデータ
    cna = job.analyze("cna").wait()        # 解析もジョブとして動かす
    cna.result()

計算の種類は kinds()、入力欄は kinds() の fields で確かめる。MD は stages、それ以外は options で条件を渡す。
ジョブを動かす係 (ワーカー) がいなければ裏で起動するので、スクリプトが終わっても計算は最後まで進む。
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from ase import Atoms

from . import arrhenius, calcjob, jobs, mdjob, modeling, potentials, runtime, service, settings, structures, system
from .db import transaction
from .service import NotFound

__all__ = ["Client", "Job", "JobFailed", "NotFound"]


class JobFailed(RuntimeError):
    """待っていたジョブが失敗した、または中止された。"""


class Job:
    """投入したジョブ 1 つ。状態は data に入っていて、refresh() で読み直す。"""

    def __init__(self, client: Client, job_id: int, *, reused: bool = False) -> None:
        self.client = client
        self.id = job_id
        self.reused = reused   # 解析で、同じ条件の結果を使い回したとき True
        self.data: dict = {}
        self.refresh()

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.kind} {self.status}>"

    def refresh(self) -> Job:
        self.data = self.client._call(service.job_detail, self.id)
        if self.data["status"] == "queued":   # ワーカーが終わっていても進むように
            self.client._ensure_worker()
        return self

    @property
    def status(self) -> str:
        return self.data["status"]

    @property
    def kind(self) -> str:
        return self.data["kind"]

    @property
    def finished(self) -> bool:
        return self.status not in ("queued", "running", "preparing")

    @property
    def workdir(self) -> Path:
        return jobs.workdir(self.id)

    def wait(self, timeout: float | None = None, *, poll: float = 2.0, check: bool = True) -> Job:
        """終わるまで待つ。check=True なら完了以外の結果で JobFailed を出す。時間切れは TimeoutError。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.refresh().finished:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"ジョブ {self.id} は {timeout} 秒たっても終わっていません ({self.status})")
            time.sleep(poll)
        if check and self.status != "done":
            raise JobFailed(f"ジョブ {self.id} は {self.status} で終わりました: {self.data['message']}")
        return self

    def cancel(self) -> str:
        """実行中・実行待ちのジョブを中止する。戻り値は中止前の状態。"""
        with transaction() as db:
            return jobs.cancel(db, self.id)

    def delete(self) -> None:
        with transaction() as db:
            jobs.delete(db, self.id)

    def thermo(self) -> dict:
        """熱力学量の時系列とステージごとの要約。"""
        return self.client._call(service.job_thermo, self.id)

    def results(self) -> dict:
        """要点の数値 (highlights) と、表の一覧 (datasets)。"""
        return self.client._call(service.job_results, self.id)

    def dataset(self, key: str) -> dict:
        """表 1 つ分のデータ。key は results() の datasets にあるもの。行が多いときは間引く。"""
        return self.client._call(service.job_dataset, self.id, key)

    def files(self) -> list[dict]:
        return self.refresh().data["files"]

    def read_text(self, name: str) -> str:
        """ジョブのファイル (run.in など) の中身。"""
        return self.client._call(service.job_text, self.id, name)["text"]

    def trajectory(self) -> dict:
        return self.client._call(service.job_trajectory, self.id)

    def zip(self, target: str | Path) -> Path:
        """結果一式を zip にして target に保存する。"""
        path = self.client._call(service.job_zip, self.id)
        return Path(shutil.move(path, target))

    def save_final_structure(self, name: str = "") -> int:
        """最終構造を構造として登録し、その ID を返す。"""
        return self.client._call(service.save_final_structure, self.id, name)

    def analyze(self, kind: str, **params) -> Job:
        """このジョブの結果を解析する。kind は voronoi / cna / coord / sro / poly。"""
        return self.client.analyze(kind, job=self.id, **params)

    def result(self) -> dict | None:
        """解析のジョブの結果。終わっていなければ None。"""
        return self.client._call(service.analysis_result, self.id, self.kind)["result"]

    def distribution(self, **params) -> dict:
        return self.client.distribution(job=self.id, **params)

    def phonon(self) -> dict:
        return self.client._call(service.phonon_overview, self.id)

    def phonon_mode(self, q: int, band: int) -> dict:
        return self.client._call(service.phonon_mode, self.id, q, band)


class Client:
    """構造・ポテンシャル・計算・解析の操作。

    data_dir を渡すと、そのデータ置き場を使う (環境変数 MATSIM_DATA_DIR と同じ)。1 つのプロセスで
    使えるデータ置き場は 1 つ。start_worker=False なら、キューに入れるだけで計算は始めない。
    """

    def __init__(self, data_dir: str | Path | None = None, *, start_worker: bool = True) -> None:
        if data_dir is not None:
            settings.set_data_dir(data_dir)
        self.start_worker = start_worker
        runtime.prepare()

    def _call(self, function, *args, **kwargs):
        with transaction() as db:
            return function(db, *args, **kwargs)

    def _ensure_worker(self) -> None:
        if self.start_worker:
            runtime.ensure_worker()

    # ---- 環境 ---------------------------------------------------------------------

    def system(self) -> dict:
        """GPUMD・toolkit・GPU・Atomsk・PLUMED・SevenNet の状態。"""
        return {**system.status(), "worker_running": runtime.is_running(), "data_dir": str(settings.DATA_DIR)}

    def limits(self) -> dict:
        return service.limits()

    def set_mp_key(self, key: str) -> bool:
        """Materials Project の API キーを保存する。"""
        return service.set_mp_key(key)

    # ---- 構造 ---------------------------------------------------------------------

    def structures(self) -> list[dict]:
        return self._call(service.list_structures)

    def structure(self, structure_id: int, repeat: tuple[int, int, int] = (1, 1, 1), *, view: bool = False) -> dict:
        """構造の情報。view=True なら 3D 表示用の座標も返す。"""
        data = self._call(service.structure_detail, structure_id, *repeat)
        if not view:
            data.pop("view", None)
        return data

    def atoms(self, structure_id: int) -> Atoms:
        """構造を ASE の Atoms で返す。"""
        self._call(service.structure_detail, structure_id)
        return structures.load(structure_id)

    def add_structure(self, source: str | Path | Atoms, name: str = "") -> int:
        """cif / POSCAR / xyz のファイル、または ASE の Atoms を登録し、ID を返す。"""
        if isinstance(source, Atoms):
            atoms = structures.clean(source)
            return self._call(structures.save, atoms, name or atoms.get_chemical_formula(mode="metal"), "python")
        path = Path(source).expanduser()
        if path.stat().st_size > settings.MAX_STRUCTURE_BYTES:
            raise ValueError(f"ファイルが大きすぎます (最大 {settings.MAX_STRUCTURE_BYTES // 1024 // 1024}MB)")
        return self._call(service.add_structure_bytes, path.name, path.read_bytes(), name)

    def add_sample(self, key: str = "Cu") -> int:
        """動作確認用の構造 (Cu・MgO) を登録する。"""
        return self._call(service.add_sample, key)

    def delete_structures(self, *ids: int) -> dict:
        return self._call(service.delete_structures, list(ids))

    def mp_search(self, query: str) -> list[dict]:
        """Materials Project を組成式か mp-id で検索する。"""
        return structures.mp_search(query)

    def mp_import(self, material_id: str, *, conventional: bool = True) -> int:
        return self._call(service.mp_import, material_id, conventional)

    # ---- 構造のモデリング -----------------------------------------------------------

    def modeling_ops(self) -> dict:
        """モデリングの操作と、それぞれの入力欄の定義。"""
        return modeling.listing()

    def preview_model(self, op: str, structure: int | None = None, options: dict | None = None) -> dict:
        return self._call(service.model_preview, service.ModelIn(op=op, structure_id=structure, options=options or {}))

    def save_model(self, op: str, structure: int | None = None, options: dict | None = None, name: str = "") -> int:
        body = service.ModelIn(op=op, structure_id=structure, options=options or {}, name=name)
        return self._call(service.model_save, body)

    # ---- ポテンシャル ---------------------------------------------------------------

    def potentials(self) -> list[dict]:
        return self._call(service.list_potentials)

    def add_potential(self, path: str | Path, name: str) -> int:
        """nep.txt を登録する。"""
        path = Path(path).expanduser()
        if path.stat().st_size > settings.MAX_POTENTIAL_BYTES:
            raise ValueError(f"ファイルが大きすぎます (最大 {settings.MAX_POTENTIAL_BYTES // 1024 // 1024}MB)")
        with tempfile.TemporaryDirectory(dir=settings.TMP_DIR) as tmp:
            copied = Path(shutil.copyfile(path, Path(tmp) / "upload.nep"))
            return self._call(potentials.add_upload, name, copied)

    def delete_potential(self, potential_id: int) -> None:
        self._call(potentials.delete, potential_id)

    def _potential_id(self, value: int | str | None) -> int:
        """ID・名前からポテンシャルの ID を求める。None なら一覧の先頭 (既定の NEP89)。"""
        if isinstance(value, int):
            return value
        rows = self.potentials()
        if value is None:
            if not rows:
                raise ValueError("ポテンシャルが登録されていません")
            return rows[0]["id"]
        for row in rows:
            if row["name"] == value:
                return row["id"]
        raise NotFound(f"ポテンシャル「{value}」が見つかりません")

    # ---- 計算 ---------------------------------------------------------------------

    def kinds(self) -> dict:
        """計算の種類と入力欄の定義。"""
        return service.list_kinds()

    def _body(self, kind: str, structure: int, potential, *, stages, options, extra_potentials, minimize, **common):
        common = {key: value for key, value in common.items() if value is not None}
        common.update(structure_id=structure, potential_id=self._potential_id(potential))
        common.setdefault("repeat", self._call(service.default_repeat, structure))
        if kind == "md":
            if options:
                raise ValueError("MD の条件は options ではなく stages で渡してください")
            return mdjob.MDJobIn(stages=stages or [], minimize=minimize, **common)
        if stages:
            raise ValueError("stages を使うのは MD だけです。ほかの計算は options で渡してください")
        return calcjob.CalcJobIn(kind=kind, options=options or {}, extra_potential_ids=[
            self._potential_id(p) for p in extra_potentials], **common)

    def preview(self, kind: str, structure: int, potential: int | str | None = None, *, stages: list[dict] | None = None,
                options: dict | None = None, extra_potentials: tuple = (), minimize: bool = False, name: str | None = None,
                repeat: tuple[int, int, int] | None = None, time_step: float | None = None,
                thermo_interval: int | None = None, traj_interval: int | None = None) -> dict:
        """投入せずに確かめる。原子数・所要時間の目安・生成される run.in を返す。"""
        body = self._body(kind, structure, potential, stages=stages, options=options, extra_potentials=extra_potentials,
                          minimize=minimize, name=name, repeat=repeat, time_step=time_step,
                          thermo_interval=thermo_interval, traj_interval=traj_interval)
        return self._call(service.md_preview if kind == "md" else service.calc_preview, body)

    def submit(self, kind: str, structure: int, potential: int | str | None = None, *, stages: list[dict] | None = None,
               options: dict | None = None, extra_potentials: tuple = (), minimize: bool = False, name: str | None = None,
               repeat: tuple[int, int, int] | None = None, time_step: float | None = None,
               thermo_interval: int | None = None, traj_interval: int | None = None) -> Job | list[Job]:
        """計算をキューに入れる。温度スキャンのときは Job のリストを返す。

        kind は kinds() にあるもの。MD は stages (1 ステージは {"ensemble": "nvt", "steps": 1000,
        "temperature": 300}) を、それ以外は options を渡す。potential は ID・名前・None (既定の NEP89)。
        repeat を省くと、セルの厚みが 12 Å 以上になるスーパーセルにする (画面の既定と同じ)。
        """
        body = self._body(kind, structure, potential, stages=stages, options=options, extra_potentials=extra_potentials,
                          minimize=minimize, name=name, repeat=repeat, time_step=time_step,
                          thermo_interval=thermo_interval, traj_interval=traj_interval)
        made = self._call(service.md_submit if kind == "md" else service.calc_submit, body)
        self._ensure_worker()
        ids = made.get("ids", [made["id"]])
        return Job(self, ids[0]) if len(ids) == 1 else [Job(self, job_id) for job_id in ids]

    # ---- ジョブ -------------------------------------------------------------------

    def jobs(self) -> list[dict]:
        """新しいものから 200 件。"""
        return self._call(service.list_jobs)

    def job(self, job_id: int) -> Job:
        return Job(self, job_id)

    def delete_jobs(self, *ids: int) -> dict:
        return self._call(service.delete_jobs, list(ids))

    def series(self, series: str) -> dict:
        """温度スキャンのジョブのまとめ。"""
        return self._call(service.series_summary, series)

    # ---- 解析 ---------------------------------------------------------------------

    def analyze(self, kind: str, *, job: int | None = None, structure: int | None = None, **params) -> Job:
        """解析をジョブとして動かす。kind は voronoi / cna / coord / sro / poly、対象はジョブか構造のどちらか。"""
        if kind not in service.ANALYSES:
            raise ValueError(f"解析の種類は {', '.join(service.ANALYSES)} のどれかです")
        body = service.ANALYSES[kind][0](job_id=job, structure_id=structure, **params)
        made = self._call(service.submit_analysis, kind, body)
        self._ensure_worker()
        return Job(self, made["id"], reused=made["reused"])

    def distribution(self, *, job: int | None = None, structure: int | None = None, **params) -> dict:
        """原子分布 (選んだ元素を軸に射影したヒストグラム)。"""
        return self._call(service.atom_distribution, service.DistributionIn(job_id=job, structure_id=structure, **params))

    def arrhenius(self, job_ids: list[int], *, excluded: list[int] | None = None, included: list[int] | None = None,
                  temperature: str = "measured", sigma_mode: str = "sigma_t") -> dict:
        """拡散・イオン伝導のジョブからアレニウスプロットを当てはめる。"""
        return self._call(arrhenius.analyze, job_ids, excluded=excluded, included=included, temperature=temperature,
                          sigma_mode=sigma_mode)
