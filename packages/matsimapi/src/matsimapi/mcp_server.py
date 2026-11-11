"""LLM から計算を動かすための MCP サーバー。

    matsimapi mcp          標準入出力で待ち受ける (データ置き場は MATSIM_DATA_DIR)

認証がないので標準入出力だけで動かす。計算の流れと確認の順番は INSTRUCTIONS に書いてある。
LLM に渡す量を抑えるため、3D 表示用のデータは落とし、長い列は _compact で切り詰める。
"""
from __future__ import annotations

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, crystals, drafts, pubchem, service, structures
from .client import Client, Job

INSTRUCTIONS = """GPUMD・SevenNet で材料シミュレーションを動かす。GPU は 1 枚でジョブは順番に実行される。
流れ: (1) list_structures か add_structure_file / add_sample_structure / import_mp_structure で構造を用意
(2) list_potentials でポテンシャルを選ぶ (3) list_calculation_kinds と describe_calculation_kind で計算と入力欄を調べる
(4) preview_calculation で原子数・所要時間の目安・run.in を確かめる (5) submit_calculation で投入
(6) wait_for_job か get_job で進み具合を見る (7) get_job_results と get_job_dataset で結果を読む
(8) 必要なら submit_analysis で解析する。
長い計算を投入する前には preview の estimated_seconds を利用者に伝えること。"""

HEAVY_KEYS = {"view", "view_labels", "view_stride", "colors"}
MAX_LIST = 200

server = MCPServer("matsimapi", version=__version__, instructions=INSTRUCTIONS)
READ = ToolAnnotations(readOnlyHint=True)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True)
_client: Client | None = None


def _tool(annotations: ToolAnnotations | None = None):
    """ツールとして登録する。入力の誤りは、LLM が直せるように説明をそのまま返す。"""
    def decorate(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (ValueError, OSError) as exc:
                raise ToolError(str(exc)) from exc
        return server.tool(annotations=annotations)(wrapper)
    return decorate


def client() -> Client:
    global _client
    if _client is None:
        _client = Client()
    return _client


def _compact(value):
    """3D 表示用のデータを落とし、長い列を先頭だけにする。"""
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items() if key not in HEAVY_KEYS}
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_LIST:
            return {"truncated": True, "length": len(value), "head": [_compact(v) for v in value[:20]]}
        return [_compact(item) for item in value]
    return value


def _job(job: Job) -> dict:
    """ジョブの要点。"""
    keys = ("id", "name", "kind", "status", "message", "progress", "steps_done", "total_steps", "unit",
            "elapsed_seconds", "remaining_seconds", "queue_position", "series", "summary")
    return {key: job.data[key] for key in keys if key in job.data}


# ---- 環境・構造 -----------------------------------------------------------------------

@_tool(READ)
def get_system_status() -> dict:
    """計算環境の状態: GPUMD・GPU・Atomsk・PLUMED・SevenNet が使えるか、データ置き場、入力の上限。"""
    return {**client().system(), "limits": client().limits()}


@_tool(READ)
def list_structures() -> list[dict]:
    """登録済みの構造の一覧 (id・名前・組成・原子数・スーパーセルの既定値)。"""
    return [{k: s[k] for k in ("id", "name", "formula", "n_atoms", "source", "default_repeat")} for s in client().structures()]


@_tool(READ)
def get_structure(structure_id: int, repeat: tuple[int, int, int] = (1, 1, 1)) -> dict:
    """構造の詳細。repeat を付けるとスーパーセルにしたときの原子数とセルの大きさも返す。"""
    return client().structure(structure_id, repeat)


@_tool(READ)
def get_structure_view(structure_id: int, repeat: tuple[int, int, int] = (1, 1, 1)) -> dict:
    """3D 表示用のデータ: 元素 (symbols)・座標 (positions, Å)・セル (cell, 3x3 Å)。

    repeat でスーパーセルにする。原子が多いときは等間隔に間引く (n_atoms が全体、shown が返した数)。
    """
    data = client().structure(structure_id, repeat, view=True)
    if "view" not in data:
        raise ValueError(f"原子が多すぎて表示用のデータを作れません ({data['repeat']['n_atoms']} 原子)")
    return {"id": data["id"], "name": data["name"], "formula": data["formula"], "repeat": list(repeat), **data["view"]}


@_tool()
def add_structure_file(path: str, name: str = "") -> dict:
    """このマシンにある cif / POSCAR / xyz のファイルを構造として登録する。"""
    return {"id": client().add_structure(path, name)}


@_tool()
def add_sample_structure(key: str = "Cu") -> dict:
    """動作確認用のテスト構造を登録する。key は Cu か MgO。"""
    return {"id": client().add_sample(key)}


@_tool(READ)
def search_mp_structures(query: str) -> list[dict]:
    """Materials Project を組成式 (SiO2) か mp-id (mp-149) で検索する。安定なものから並ぶ。"""
    return client().mp_search(query)


@_tool()
def import_mp_structure(material_id: str, conventional: bool = True) -> dict:
    """Materials Project の構造 (mp-149 など) を取得して登録する。"""
    return {"id": client().mp_import(material_id, conventional=conventional)}


@_tool(READ)
def search_crystals(query: str) -> list[dict]:
    """Materials Project の結晶構造を OPTIMADE で探す (API キー不要)。query は組成式 (TiO2)・元素の組 (Ti-O)・ID (mp-149)。
    候補の ID・組成式・原子数・空間群・凸包からの距離 (eV/原子) を安定な順に返す。importable が false のものは取り込めない。"""
    return crystals.search(query)


@_tool()
def import_crystal(entry_id: str, conventional: bool = True, name: str = "") -> dict:
    """search_crystals の候補 (entry_id は mp-149 のような Materials Project の ID) を構造として登録する。
    conventional で慣用の単位胞にする (既定。OPTIMADE の MP は基本単位胞で返すため)。"""
    atoms, info = crystals.fetch(entry_id, conventional)
    label = name or f"{info['formula']} {info['spacegroup'] or ''} ({entry_id})".replace("  ", " ")
    structure_id = client()._call(structures.save, atoms, label[:60], "optimade:mp", f"Materials Project (OPTIMADE) {entry_id}")
    return {"id": structure_id, "name": label[:60], "formula": info["formula"], "spacegroup": info["spacegroup"],
            "n_atoms": len(atoms)}


@_tool(READ)
def search_pubchem(query: str) -> list[dict]:
    """PubChem で有機分子を探す。query は名前 (英語、benzene など)・CID (数字)・SMILES。候補の CID・名前・組成式・分子量を返す。"""
    return pubchem.search(query)


@_tool()
def import_pubchem(cid: int, vacuum: float = 10.0, name: str = "") -> dict:
    """PubChem の分子の 3D 配座を取り、全方位に vacuum Å の真空を空けた箱に入れて、構造として登録する。"""
    atoms, info = pubchem.fetch(cid, vacuum)
    structure_id = client()._call(structures.save, atoms, (name or info["name"])[:60], "pubchem",
                                  f"PubChem CID {info['cid']} ({info['formula']}、真空 {vacuum:g} Å)")
    return {"id": structure_id, "name": info["name"], "formula": info["formula"], "n_atoms": len(atoms)}


@_tool(DESTRUCTIVE)
def delete_structures(ids: list[int]) -> dict:
    """構造を削除する。元に戻せない。"""
    return client().delete_structures(*ids)


@_tool(READ)
def list_modeling_operations() -> dict:
    """構造のモデリング (置換・空孔・合金・スーパーセル・転位・粒界・多結晶など) の操作と入力欄の定義。"""
    return client().modeling_ops()


@_tool(READ)
def preview_model(op: str, structure_id: int | None = None, options: dict | None = None) -> dict:
    """モデリングの結果を保存せずに確かめる (組成・原子数・警告)。"""
    return _compact(client().preview_model(op, structure_id, options))


@_tool()
def save_model(op: str, structure_id: int | None = None, options: dict | None = None, name: str = "") -> dict:
    """モデリングの結果を新しい構造として登録する。"""
    return {"id": client().save_model(op, structure_id, options, name)}


# ---- 編集の下書き (重ねて、戻して、最後に 1 回だけ登録する) ------------------------------------------------
# 下書きは一時フォルダに置くだけで構造の一覧は変えないので、登録 (draft_save) 以外は読むだけの印を付ける

@_tool(READ)
def draft_start(structure_id: int) -> dict:
    """構造の編集の下書きを始める。draft (名札) を draft_apply・draft_undo・draft_save に渡す。表示用の座標 (view) も返す。"""
    return client()._call(drafts.start, structure_id)


@_tool(READ)
def draft_apply(draft: str, op: str, options: dict | None = None) -> dict:
    """下書きに編集を 1 つ重ねる (op・options は preview_model と同じ)。まだ登録しない。"""
    return client()._call(drafts.apply, draft, op, options or {})


@_tool(READ)
def draft_undo(draft: str) -> dict:
    """下書きを一つ前に戻す。"""
    return drafts.undo(draft)


@_tool(READ)
def draft_view(draft: str) -> dict:
    """下書きのいまの状態 (重ねた手順・組成・表示用の座標)。"""
    return drafts.view(draft)


@_tool()
def draft_save(draft: str, name: str = "") -> dict:
    """下書きを構造として登録する (重ねた手順は取得元に残す)。"""
    return client()._call(drafts.save, draft, name)


@_tool(READ)
def list_potentials() -> list[dict]:
    """ポテンシャルの一覧。NEP は GPUMD、SevenNet は ASE で計算する。elements は長いので元素の数だけ返す。"""
    return [{**{k: p[k] for k in ("id", "name", "builtin", "available", "engine")}, "n_elements": len(p["elements"]),
             "elements": p["elements"] if len(p["elements"]) <= 20 else None} for p in client().potentials()]


# ---- 計算 ---------------------------------------------------------------------------

@_tool(READ)
def list_calculation_kinds() -> list[dict]:
    """計算の種類の一覧 (key・名前・分類・説明)。key を preview_calculation などに渡す。"""
    return [{k: item.get(k) for k in ("key", "label", "group", "note")} for item in client().kinds()["kinds"]]


@_tool(READ)
def describe_calculation_kind(kind: str) -> dict:
    """計算の種類の入力欄の定義 (options に渡せる key・既定値・範囲・選択肢)。MD は stages で条件を渡す。"""
    for item in client().kinds()["kinds"]:
        if item["key"] == kind:
            if kind == "md":
                return {**item, "stages_example": [{"ensemble": "nvt", "steps": 10000, "temperature": 300},
                                                   {"ensemble": "npt", "steps": 10000, "temperature": 300,
                                                    "temperature_end": 600, "pressure": 0.0}]}
            return item
    raise ValueError(f"計算の種類 {kind} はありません")


@_tool(READ)
def preview_calculation(kind: str, structure_id: int, potential_id: int | None = None, stages: list[dict] | None = None,
                        options: dict | None = None, repeat: tuple[int, int, int] | None = None, time_step: float = 1.0,
                        minimize: bool = False, extra_potential_ids: list[int] | None = None, thermo_interval: int = 100,
                        traj_interval: int = 1000) -> dict:
    """投入せずに確かめる: 原子数・合計ステップ・所要時間の目安 (秒)・生成される run.in・注意書き。

    MD (kind="md") は stages、それ以外は options で条件を渡す。time_step の単位は fs。
    repeat を省くと、セルの厚みが 12 Å 以上になるスーパーセルにする。
    """
    return client().preview(kind, structure_id, potential_id, stages=stages, options=options, repeat=repeat,
                            time_step=time_step, minimize=minimize, extra_potentials=tuple(extra_potential_ids or ()),
                            thermo_interval=thermo_interval, traj_interval=traj_interval)


@_tool()
def submit_calculation(kind: str, structure_id: int, potential_id: int | None = None, stages: list[dict] | None = None,
                       options: dict | None = None, repeat: tuple[int, int, int] | None = None, time_step: float = 1.0,
                       minimize: bool = False, name: str = "", thermo_interval: int = 100, traj_interval: int = 1000,
                       extra_potential_ids: list[int] | None = None) -> dict:
    """計算をキューに入れて、すぐに戻る。GPU を使うので preview_calculation で確かめてから投入する。

    温度スキャンでは温度ごとにジョブができ、jobs に全部が入る (series でまとめて読める)。
    """
    made = client().submit(kind, structure_id, potential_id, stages=stages, options=options, repeat=repeat,
                           time_step=time_step, minimize=minimize, name=name, thermo_interval=thermo_interval,
                           traj_interval=traj_interval, extra_potentials=tuple(extra_potential_ids or ()))
    return {"jobs": [_job(job) for job in (made if isinstance(made, list) else [made])]}


# ---- ジョブ -------------------------------------------------------------------------

@_tool(READ)
def list_jobs(limit: int = 30) -> list[dict]:
    """ジョブの一覧 (新しい順)。"""
    return [{k: j[k] for k in ("id", "name", "kind", "status", "progress", "message", "series")}
            for j in client().jobs()[:limit]]


@_tool(READ)
def get_job(job_id: int, include_log: bool = False) -> dict:
    """ジョブの状態・進み具合・残り時間の目安。include_log でログの末尾とファイルの一覧も返す。"""
    job = client().job(job_id)
    data = _job(job)
    if include_log:
        data.update({key: job.data[key] for key in ("log", "error_log", "files")})
    return data


@_tool(READ)
def wait_for_job(job_id: int, timeout_seconds: float = 60) -> dict:
    """ジョブが終わるか、timeout_seconds (最大 300) がたつまで待って状態を返す。"""
    job = client().job(job_id)
    try:
        job.wait(min(timeout_seconds, 300), poll=1.0, check=False)
    except TimeoutError:
        pass
    return _job(job)


@_tool()
def cancel_job(job_id: int) -> dict:
    """実行中・実行待ちのジョブを中止する。"""
    return {"was": client().job(job_id).cancel()}


@_tool(DESTRUCTIVE)
def delete_jobs(ids: list[int]) -> dict:
    """終わったジョブを結果ごと削除する。元に戻せない。実行中のものは消さずに skipped に返す。"""
    return client().delete_jobs(*ids)


@_tool(READ)
def get_job_thermo_summary(job_id: int) -> list[dict]:
    """ステージごとの温度・圧力・エネルギー・体積の平均と標準偏差 (各ステージの先頭 30% は除く)。"""
    return client().job(job_id).thermo()["summary"]


@_tool(READ)
def get_job_results(job_id: int) -> dict:
    """結果の要点の数値 (highlights)・注意書き (notes)・読める表の一覧 (datasets)。"""
    return client().job(job_id).results()


@_tool(READ)
def get_job_trajectory(job_id: int) -> dict:
    """ジョブの計算過程の構造 (トラジェクトリ、dump.xyz) を、3D で動かして見る用に等間隔に抜き出して返す。
    symbols (原子の元素)・frames (index・cell・positions) と n_frames_total。原子が多いときは間引く。トラジェクトリがなければ frames は空。"""
    return client().job(job_id).trajectory()


@_tool(READ)
def get_job_dataset(job_id: int, key: str, columns: list[str] | None = None, max_rows: int = 100) -> dict:
    """表 1 つ分のデータ。key は get_job_results の datasets にあるもの (thermo など)。

    columns で列を選び、行は max_rows に間引く。
    """
    table = client().job(job_id).dataset(key)
    data = {name: values for name, values in table["data"].items() if not columns or name in columns}
    n_rows = max((len(values) for values in data.values()), default=0)
    step = max(1, -(-n_rows // max(max_rows, 1)))
    return {"key": key, "n_rows": table["n_rows"], "stride": step, "data": {n: v[::step] for n, v in data.items()}}


@_tool(READ)
def read_job_file(job_id: int, name: str) -> dict:
    """ジョブのテキストファイル (run.in・plumed.dat・ログなど) を読む。大きいものは先頭だけ。"""
    return client()._call(service.job_text, job_id, name)


@_tool()
def export_job(job_id: int, directory: str, max_file_mb: float = 200) -> dict:
    """ジョブのファイル一式を directory にコピーする。max_file_mb より大きいファイル (長いトラジェクトリなど) は飛ばす。"""
    return client()._call(service.export_job, job_id, directory, max_file_mb)


@_tool()
def export_structure(structure_id: int, directory: str) -> dict:
    """構造を directory に cif・xyz・POSCAR (.vasp) で書き出す。"""
    return client()._call(service.export_structure, structure_id, directory)


@_tool()
def save_final_structure(job_id: int, name: str = "") -> dict:
    """完了したジョブの最終構造を、次の計算に使える構造として登録する。"""
    return {"id": client().job(job_id).save_final_structure(name)}


@_tool(READ)
def get_series(series: str) -> dict:
    """温度スキャンのジョブのまとめ。融点の計算なら融点の推定も返す。"""
    return _compact(client().series(series))


# ---- 解析 ---------------------------------------------------------------------------

@_tool()
def submit_analysis(kind: str, job_id: int | None = None, structure_id: int | None = None,
                    params: dict | None = None) -> dict:
    """解析をジョブとして投入する。対象はジョブか構造のどちらか。同じ条件の結果があれば reused が true。

    kind: voronoi (ボロノイ多面体) / cna (結晶構造の判定) / coord (配位数) / sro (ウォーレン・カウリーの秩序度) /
    poly (配位多面体)。params: frames (時間変化に使う枚数)、cutoff (coord・poly)、center (voronoi・poly)、ligand (poly)。
    """
    job = client().analyze(kind, job=job_id, structure=structure_id, **(params or {}))
    return {**_job(job), "reused": job.reused}


@_tool(READ)
def get_analysis_result(job_id: int) -> dict | None:
    """解析のジョブの結果。終わっていなければ null。"""
    return _compact(client().job(job_id).result())


@_tool(READ)
def get_atom_distribution(job_id: int | None = None, structure_id: int | None = None, species: list[str] | None = None,
                          axis: str = "z", bin_width: float = 1.0, frames: int = 0) -> dict:
    """選んだ元素の原子を軸 (x / y / z) に射影したヒストグラム。species が空ならすべての元素。"""
    return _compact(client().distribution(job=job_id, structure=structure_id, species=species or [], axis=axis,
                                          bin_width=bin_width, frames=frames))


@_tool(READ)
def fit_arrhenius(job_ids: list[int], excluded: list[int] | None = None, included: list[int] | None = None) -> dict:
    """拡散・イオン伝導のジョブからアレニウスプロットを当てはめ、活性化エネルギーなどを返す。"""
    return _compact(client().arrhenius(job_ids, excluded=excluded, included=included))


def main() -> None:
    client()
    server.run("stdio")
