"""MatSimApp の API サーバー。画面とログインだけを持ち、計算の操作はすべて matsimapi を呼ぶ。"""
from __future__ import annotations

import os

os.environ.setdefault("MPLBACKEND", "Agg")  # グラフ画像はファイルに書くだけなので画面は要らない

import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from matsimapi import arrhenius, calcjob, jobs, mdjob, modeling, potentials, service, structures, system
from matsimapi import settings as core
from matsimapi.db import get_db
from matsimapi.runtime import WorkerKeeper, prepare
from matsimapi.service import CnaIn, CoordIn, DistributionIn, ModelIn, NotFound, PolyIn, SroIn, VoronoiIn
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import __version__, auth, settings
from .auth import CurrentUser, current_user

USERNAME_PATTERN = r"^[A-Za-z0-9_.\-]{3,32}$"
Db = Depends(get_db, scope="function")
keeper = WorkerKeeper()


@asynccontextmanager
async def lifespan(app: FastAPI):
    prepare()
    auth.init_sessions()
    keeper.start()   # このサーバーはジョブを動かさない。動かすのは独立したワーカーで、サーバーを止めても続く
    yield
    keeper.stop()


app = FastAPI(title="MatSimApp", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # style-src の 'unsafe-inline' は plotly.js が要素の style 属性でグラフを描くために必要
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    return response


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(NotFound)
async def not_found_handler(request: Request, exc: NotFound):
    return JSONResponse({"detail": str(exc)}, status_code=404)


def read_upload(file: UploadFile, limit: int) -> bytes:
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"ファイルが大きすぎます (最大 {limit // 1024 // 1024}MB)")
    return data


# ---- 認証 -------------------------------------------------------------------------

class AccountIn(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=10, max_length=200)


class LoginIn(BaseModel):
    username: str = Field(max_length=32)
    password: str = Field(max_length=200)


@app.get("/api/status")
def status(db: sqlite3.Connection = Db):
    """ログイン前に見える情報。アカウントが 1 つもなければ最初の登録を受け付ける。"""
    has_users = db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    return {"app_version": __version__, "needs_setup": not has_users}


@app.post("/api/auth/setup")
def setup(body: AccountIn, request: Request, response: Response, db: sqlite3.Connection = Db):
    """最初のアカウントを作る。すでにアカウントがあるときは使えない。"""
    auth.check_csrf(request)
    if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        raise HTTPException(403, "アカウントは作成済みです。ログインしてください")
    user_id = db.execute("INSERT INTO users (username, pw_hash) VALUES (?, ?)",
                         (body.username, auth.hash_password(body.password))).lastrowid
    auth.create_session(db, user_id, request, response)
    return {"username": body.username}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: sqlite3.Connection = Db):
    auth.check_csrf(request)
    auth.check_login_allowed(request, body.username)
    row = db.execute("SELECT id, username, pw_hash FROM users WHERE username = ?", (body.username,)).fetchone()
    ok = auth.verify_password(body.password, row["pw_hash"] if row else auth.DUMMY_HASH) and row is not None
    auth.record_login_result(request, body.username, ok)
    if not ok:
        raise HTTPException(401, "ユーザー名またはパスワードが違います")
    auth.create_session(db, row["id"], request, response)
    return {"username": row["username"]}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, db: sqlite3.Connection = Db):
    auth.check_csrf(request)
    auth.destroy_session(db, request, response)
    return {"ok": True}


@app.get("/api/me")
def me(user: CurrentUser = Depends(current_user)):
    return {"username": user.username, "mp_key_set": bool(structures.mp_api_key()), "limits": service.limits()}


@app.get("/api/system")
def get_system(user: CurrentUser = Depends(current_user)):
    """ホーム画面に出す計算環境の状態。"""
    return system.status()


class MPKeyIn(BaseModel):
    key: str = Field(max_length=100, pattern=r"^[A-Za-z0-9]*$")


@app.put("/api/settings/mp-key")
def set_mp_key(body: MPKeyIn, user: CurrentUser = Depends(current_user)):
    """Materials Project の API キーを保存する。空文字で削除。値は画面にもログにも返さない。"""
    return {"mp_key_set": service.set_mp_key(body.key)}


# ---- 構造 -------------------------------------------------------------------------

@app.get("/api/structures")
def list_structures(user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    return service.list_structures(db)


@app.post("/api/structures/upload")
def upload_structure(file: UploadFile = File(...), name: str = Form("", max_length=60),
                     user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    data = read_upload(file, core.MAX_STRUCTURE_BYTES)
    return {"id": service.add_structure_bytes(db, file.filename or "", data, name)}


@app.get("/api/structures/samples")
def list_samples(user: CurrentUser = Depends(current_user)):
    return [{"key": key, "label": item["label"]} for key, item in structures.SAMPLES.items()]


class SampleIn(BaseModel):
    key: str = Field(max_length=20)


@app.post("/api/structures/sample")
def add_sample(body: SampleIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """動作確認用のテスト構造 (Cu・MgO) を登録する。"""
    return {"id": service.add_sample(db, body.key)}


@app.get("/api/structures/{structure_id}")
def get_structure(structure_id: int, na: int = Query(1), nb: int = Query(1), nc: int = Query(1),
                  user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """構造の情報と 3D 表示用のデータ。na, nb, nc を付けるとスーパーセルで返す。"""
    return service.structure_detail(db, structure_id, na, nb, nc)


@app.delete("/api/structures/{structure_id}")
def delete_structure(structure_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    service.delete_structure(db, structure_id)
    return {"ok": True}


class IdsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)


@app.post("/api/structures/delete")
def delete_structures(body: IdsIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """構造をまとめて削除する。見つからなかったものは missing に返す。"""
    return service.delete_structures(db, body.ids)


@app.get("/api/mp/search")
def mp_search(q: str = Query(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9().\-\s]+$"),
              user: CurrentUser = Depends(current_user)):
    return structures.mp_search(q)


class MPImportIn(BaseModel):
    material_id: str = Field(pattern=r"^(mp|mvc)-\d{1,9}$")
    conventional: bool = True


@app.post("/api/mp/import")
def mp_import(body: MPImportIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    return {"id": service.mp_import(db, body.material_id, body.conventional)}


# ---- 構造のモデリング -----------------------------------------------------------------

@app.get("/api/modeling")
def modeling_listing(user: CurrentUser = Depends(current_user)):
    return modeling.listing()


@app.post("/api/modeling/preview")
def modeling_preview(body: ModelIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """モデリングの結果を保存せずに見る。"""
    return service.model_preview(db, body)


@app.post("/api/modeling/save")
def modeling_save(body: ModelIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """モデリングの結果を構造として登録する。"""
    return {"id": service.model_save(db, body)}


# ---- ポテンシャル -------------------------------------------------------------------

@app.get("/api/potentials")
def list_potentials(user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    return service.list_potentials(db)


@app.post("/api/potentials/upload")
def upload_potential(file: UploadFile = File(...), name: str = Form(..., min_length=1, max_length=60),
                     user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    fd, tmp_name = tempfile.mkstemp(dir=core.TMP_DIR, suffix=".nep")
    tmp_path = Path(tmp_name)
    try:
        written = 0
        with os.fdopen(fd, "wb") as handle:
            while chunk := file.file.read(1 << 20):
                written += len(chunk)
                if written > core.MAX_POTENTIAL_BYTES:
                    raise HTTPException(413, f"ファイルが大きすぎます (最大 {core.MAX_POTENTIAL_BYTES // 1024 // 1024}MB)")
                handle.write(chunk)
        return {"id": potentials.add_upload(db, name, tmp_path)}
    finally:
        tmp_path.unlink(missing_ok=True)


@app.delete("/api/potentials/{potential_id}")
def delete_potential(potential_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    potentials.delete(db, potential_id)
    return {"ok": True}


# ---- MD --------------------------------------------------------------------------

@app.post("/api/md/preview")
def md_preview(body: mdjob.MDJobIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """実行前の確認。原子数・所要時間の目安・生成される run.in を返す (ファイルは残さない)。"""
    return service.md_preview(db, body)


@app.post("/api/md/jobs")
def md_submit(body: mdjob.MDJobIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """MD ジョブをキューに入れる。"""
    return service.md_submit(db, body, user.id)


# ---- MD 以外の計算 -----------------------------------------------------------------

@app.get("/api/kinds")
def list_kinds(user: CurrentUser = Depends(current_user)):
    """計算の種類と、それぞれの入力欄の定義。画面はこれを見てメニューとフォームを作る。"""
    return service.list_kinds()


@app.post("/api/calc/preview")
def calc_preview(body: calcjob.CalcJobIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """実行前の確認 (ファイルは残さない)。温度スキャンなら 1 点目の内容と、全体の合計を返す。"""
    return service.calc_preview(db, body)


@app.post("/api/calc/jobs")
def calc_submit(body: calcjob.CalcJobIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """MD 以外の計算をキューに入れる。温度スキャンなら温度ごとにジョブを作り、同じシリーズにまとめる。"""
    return service.calc_submit(db, body, user.id)


# ---- 計算結果の解析 --------------------------------------------------------------------

@app.post("/api/analysis/voronoi")
def voronoi_analysis(body: VoronoiIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """ボロノイ多面体の解析をジョブとして投入する (GPU の計算とは別に、サーバーの中で順に動かす)。

    同じ条件で完了した解析があれば、新しく作らずにそのジョブを返す。
    """
    return service.submit_analysis(db, "voronoi", body, user.id)


@app.get("/api/jobs/{job_id}/voronoi")
def voronoi_result(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """ボロノイ解析のジョブの結果と、投入したときの条件。"""
    return service.analysis_result(db, job_id, "voronoi")


@app.post("/api/analysis/cna")
def cna_analysis(body: CnaIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """結晶構造の判定 (fcc / hcp / bcc / 正二十面体 / その他) をジョブとして投入する。"""
    return service.submit_analysis(db, "cna", body, user.id)


@app.get("/api/jobs/{job_id}/cna")
def cna_result(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """結晶構造の判定のジョブの結果と、投入したときの条件。"""
    return service.analysis_result(db, job_id, "cna")


@app.post("/api/analysis/coord")
def coord_analysis(body: CoordIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """配位数 (距離で数える) と動径分布関数の解析をジョブとして投入する。"""
    return service.submit_analysis(db, "coord", body, user.id)


@app.get("/api/jobs/{job_id}/coord")
def coord_result(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """配位数の解析のジョブの結果と、投入したときの条件。"""
    return service.analysis_result(db, job_id, "coord")


@app.post("/api/analysis/sro")
def sro_analysis(body: SroIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """ウォーレン・カウリーの秩序度 (化学的短範囲秩序) の解析をジョブとして投入する。"""
    return service.submit_analysis(db, "sro", body, user.id)


@app.post("/api/analysis/poly")
def poly_analysis(body: PolyIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """配位多面体の検出・形の判定・つながり方の解析をジョブとして投入する。"""
    return service.submit_analysis(db, "poly", body, user.id)


@app.get("/api/jobs/{job_id}/sro")
def sro_result(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    return service.analysis_result(db, job_id, "sro")


@app.get("/api/jobs/{job_id}/poly")
def poly_result(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    return service.analysis_result(db, job_id, "poly")


@app.post("/api/analysis/distribution")
def distribution_analysis(body: DistributionIn, user: CurrentUser = Depends(current_user),
                          db: sqlite3.Connection = Db):
    """選んだ元素の原子を軸に射影し、分割幅ごとの原子数を返す。"""
    return service.atom_distribution(db, body)


# ---- シリーズ (温度スキャンでまとめて投入したジョブ) -----------------------------------------

@app.get("/api/series/{series}")
def series_summary(series: str, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """シリーズのジョブと、それぞれの結果の要点。融点 (固液共存法) なら融点の推定も返す。"""
    return service.series_summary(db, series)


# ---- アレニウス解析 -------------------------------------------------------------------

def _arrhenius_args(jobs_q: str, excluded_q: str, included_q: str, temperature: str, sigma_mode: str):
    def ids(text: str) -> list[int]:
        return [int(v) for v in text.split(",") if v.strip()] if text else []
    if temperature not in ("measured", "set") or sigma_mode not in ("sigma_t", "sigma"):
        raise ValueError("解析の条件が正しくありません")
    return ids(jobs_q), ids(excluded_q), ids(included_q)


@app.get("/api/arrhenius")
def arrhenius_analyze(jobs_q: str = Query("", alias="jobs", max_length=600, pattern=r"^[0-9,]*$"),
                      excluded: str = Query("", max_length=600, pattern=r"^[0-9,]*$"),
                      included: str = Query("", max_length=600, pattern=r"^[0-9,]*$"),
                      temperature: str = Query("measured"), sigma_mode: str = Query("sigma_t"),
                      user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """拡散・イオン伝導のジョブ (jobs=1,2,3) からアレニウスプロットを当てはめる。"""
    job_ids, skip, keep = _arrhenius_args(jobs_q, excluded, included, temperature, sigma_mode)
    return arrhenius.analyze(db, job_ids, excluded=skip, included=keep, temperature=temperature,
                             sigma_mode=sigma_mode)


@app.get("/api/arrhenius/csv")
def arrhenius_csv(jobs_q: str = Query("", alias="jobs", max_length=600, pattern=r"^[0-9,]*$"),
                  excluded: str = Query("", max_length=600, pattern=r"^[0-9,]*$"),
                  included: str = Query("", max_length=600, pattern=r"^[0-9,]*$"),
                  temperature: str = Query("measured"), sigma_mode: str = Query("sigma_t"),
                  user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    job_ids, skip, keep = _arrhenius_args(jobs_q, excluded, included, temperature, sigma_mode)
    result = arrhenius.analyze(db, job_ids, excluded=skip, included=keep, temperature=temperature,
                               sigma_mode=sigma_mode)
    return Response(arrhenius.to_csv(result), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="arrhenius.csv"'})


# ---- ジョブ -----------------------------------------------------------------------

@app.get("/api/jobs")
def list_jobs(user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    return service.list_jobs(db)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    return service.job_detail(db, job_id)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    return {"was": jobs.cancel(db, job_id)}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    jobs.delete(db, job_id)
    return {"ok": True}


@app.post("/api/jobs/delete")
def delete_jobs(body: IdsIn, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """ジョブをまとめて削除する。実行中・実行待ちのものは消さずに skipped に返す。"""
    return service.delete_jobs(db, body.ids)


@app.get("/api/jobs/{job_id}/thermo")
def job_thermo(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """熱力学量の時系列とステージごとの要約。実行中でもその時点までを返す。"""
    return service.job_thermo(db, job_id)


@app.get("/api/jobs/{job_id}/results")
def job_results(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """要点の数値と、グラフにできる表の一覧 (列の名前つき)。"""
    return service.job_results(db, job_id)


@app.get("/api/jobs/{job_id}/dataset")
def job_dataset(job_id: int, key: str = Query(max_length=40, pattern=r"^[A-Za-z0-9_]+$"),
                user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """表 1 つ分のデータ。画面で横軸・縦軸を選んでグラフにする。"""
    return service.job_dataset(db, job_id, key)


@app.get("/api/jobs/{job_id}/trajectory")
def job_trajectory(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    return service.job_trajectory(db, job_id)


@app.get("/api/jobs/{job_id}/file")
def job_file(job_id: int, name: str = Query(max_length=200), user: CurrentUser = Depends(current_user),
             db: sqlite3.Connection = Db):
    path = service.job_file(db, job_id, name)
    return FileResponse(path, filename=f"job{job_id}_{path.name}", media_type="application/octet-stream")


@app.get("/api/jobs/{job_id}/text")
def job_text(job_id: int, name: str = Query(max_length=200), user: CurrentUser = Depends(current_user),
             db: sqlite3.Connection = Db):
    """ジョブのファイル (run.in・plumed.dat など) の中身を、画面で見るためにテキストで返す。大きいものは先頭だけ。"""
    return service.job_text(db, job_id, name)


@app.get("/api/jobs/{job_id}/phonon")
def phonon_overview(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """フォノンの振動のようすを見るための情報: q 点の数・分枝の数・高対称点・基本単位胞。"""
    return service.phonon_overview(db, job_id)


@app.get("/api/jobs/{job_id}/phonon/mode")
def phonon_mode(job_id: int, q: int = Query(ge=0, le=100_000), band: int = Query(ge=0, le=3_000),
                user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    """q 点 q・分枝 band (0 始まり、振動数の低い順) の振動数と、原子ごとの変位の振幅。"""
    return service.phonon_mode(db, job_id, q, band)


@app.get("/api/jobs/{job_id}/zip")
def job_zip(job_id: int, user: CurrentUser = Depends(current_user), db: sqlite3.Connection = Db):
    path = service.job_zip(db, job_id)
    return FileResponse(path, filename=f"matsim_job{job_id}.zip", media_type="application/zip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


class SaveFinalIn(BaseModel):
    name: str = Field("", max_length=60)


@app.post("/api/jobs/{job_id}/save-final")
def save_final_structure(job_id: int, body: SaveFinalIn, user: CurrentUser = Depends(current_user),
                         db: sqlite3.Connection = Db):
    """完了したジョブの最終構造を、次の計算に使える構造として登録する。"""
    return {"id": service.save_final_structure(db, job_id, body.name)}


if settings.DOCS_DIR.is_dir():
    app.mount("/manual", StaticFiles(directory=settings.DOCS_DIR, html=True), name="manual")
app.mount("/", StaticFiles(directory=settings.FRONTEND_DIR, html=True), name="frontend")
