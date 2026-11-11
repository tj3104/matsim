"""画面の通しテスト (スマホ幅)。テスト用のサーバーを立てて、
アカウント作成 → 構造追加 → MD 設定 → 確認 → 実行 → 結果表示 までをブラウザで操作し、スクリーンショットを残す。

    CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_ui.py
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "01_test" / "data_ui"
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8022
BASE = f"http://127.0.0.1:{PORT}"
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
POSCAR = """Cu fcc
1.0
3.61 0 0
0 3.61 0
0 0 3.61
Cu
4
Direct
0 0 0
0.5 0.5 0
0.5 0 0.5
0 0.5 0.5
"""



def choose_structure(page, text):
    """計算の設定画面で構造を選ぶ (「変える」で一覧を開き、名前で選ぶ)。"""
    chooser = page.locator(".chooser")
    chooser.get_by_role("button", name="変える").click()
    chooser.locator(".chooser-item", has_text=text).first.click()

def main():
    shutil.rmtree(DATA, ignore_errors=True)
    SHOTS.mkdir(exist_ok=True)
    (ROOT / "01_test" / "POSCAR_Cu").write_text(POSCAR)
    server = subprocess.Popen([SERVER_PYTHON, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)],
                              cwd=ROOT, env={**os.environ, "MATSIM_DATA_DIR": str(DATA)},
                              stdout=open(ROOT / "01_test" / "test_server.log", "w"), stderr=subprocess.STDOUT)
    errors = []
    try:
        time.sleep(4)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
            context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                          is_mobile=True, has_touch=True, color_scheme="light")
            page = context.new_page()
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "401" not in m.text and "400 (Bad Request)" not in m.text else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())
            page.set_default_timeout(15_000)

            def shot(name):
                overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                if overflow:
                    errors.append(f"横スクロールが出ています: {name}")
                page.screenshot(path=SHOTS / f"{name}.png", full_page=True)
                print("shot", name, flush=True)

            # アカウント作成
            page.goto(BASE)
            page.get_by_role("heading", name="最初のアカウントを作成").wait_for()
            shot("01_setup")
            page.get_by_label("ユーザー名").fill("tester")
            page.get_by_label("パスワード").fill("test-password-123")
            page.get_by_role("button", name="アカウントを作成").click()
            page.get_by_role("heading", name="計算メニュー").wait_for()
            shot("02_home_empty")

            # テスト構造 (MgO) をワンタップで追加
            page.get_by_role("link", name="構造", exact=True).click()
            page.get_by_role("heading", name="テスト構造を追加").wait_for()
            page.get_by_role("button", name="MgO (岩塩型)").click()
            page.get_by_role("link", name="この構造で MD").wait_for()
            expect(page.get_by_text("Mg × 4")).to_be_visible()
            page.wait_for_timeout(400)
            shot("03a_sample_mgo")

            # 構造を追加
            page.get_by_role("link", name="構造", exact=True).click()
            page.get_by_role("heading", name="ファイルから追加").wait_for()
            page.get_by_label("構造ファイル").set_input_files(ROOT / "01_test" / "POSCAR_Cu")
            page.get_by_label("名前").fill("Cu fcc")
            page.get_by_role("button", name="追加").click()
            page.get_by_role("link", name="この構造で MD").wait_for()
            page.wait_for_timeout(400)
            shot("03_structure")

            # MD の設定
            page.get_by_role("link", name="この構造で MD").click()
            page.wait_for_timeout(1500)
            print("view text:", page.locator("#view").inner_text()[:300].replace("\n", " | "))
            page.get_by_role("heading", name="温度・圧力の流れ").wait_for()
            expect(page.get_by_text("256 原子")).to_be_visible()      # 12 Å 以上になるよう 4×4×4 が既定
            expect(page.get_by_text("に対応しています")).to_be_visible()
            page.get_by_role("button", name="昇温").click()
            steps = page.get_by_label("ステップ数")
            steps.nth(0).fill("1000")
            steps.nth(0).dispatch_event("change")
            steps.nth(1).fill("3000")
            steps.nth(1).dispatch_event("change")
            expect(page.get_by_text("合計 4,000 ステップ")).to_be_visible()
            page.get_by_text("詳細設定").click()
            page.get_by_label("トラジェクトリの保存間隔").fill("200")
            page.get_by_label("トラジェクトリの保存間隔").dispatch_event("change")
            shot("04_md_form")
            page.get_by_role("button", name="内容を確認する").click()
            page.get_by_role("heading", name="実行前の確認").wait_for()
            page.get_by_text("生成される入力ファイル (run.in など)").click()
            expect(page.locator(".confirm .annotated")).to_contain_text("ensemble npt_scr 300 1000")
            page.wait_for_timeout(600)
            shot("05_md_confirm")

            # 実行して結果を見る
            page.get_by_role("button", name="この内容で実行する").click()
            page.wait_for_url("**/#/jobs/1")
            page.locator(".status-ok").wait_for(timeout=120_000)
            page.locator(".chart .main-svg").first.wait_for(timeout=20_000)
            page.get_by_role("heading", name="ステージごとの平均").wait_for()
            page.get_by_role("button", name="再生").wait_for()
            expect(page.locator(".chart-block .chart.js-plotly-plot")).to_have_count(4)
            expect(page.get_by_text("フレーム 1 / 20")).to_be_visible()
            expect(page.get_by_role("link", name="analysis/thermo.csv")).to_be_visible()
            page.wait_for_timeout(800)
            shot("06_job_done")
            # グラフに触れると値が出る
            chart = page.locator(".chart").first
            box = chart.bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.5)
            page.locator(".hoverlayer .legend, .hoverlayer .hovertext").first.wait_for(timeout=5000)
            # スライダーでフレームを動かす
            page.get_by_label("フレーム").fill("19")
            expect(page.get_by_text("フレーム 20 / 20")).to_be_visible()

            # 軸を選んでグラフにする: 横軸を温度、縦軸を体積にする
            page.get_by_role("heading", name="軸を選んでグラフにする").wait_for()
            page.get_by_label("横軸").select_option(label="温度 (K)")
            page.get_by_role("button", name="体積 (Å³)", exact=True).click()
            expect(page.get_by_role("button", name="体積 (Å³)", exact=True)).to_have_attribute("aria-pressed", "true")
            explorer = page.locator("section", has=page.get_by_role("heading", name="軸を選んでグラフにする"))
            expect(explorer.locator(".chart .xtitle")).to_have_text("温度 (K)")
            expect(explorer.locator(".chart .legendtext")).to_have_count(0)     # 系列が体積 1 本なので凡例は出ない (温度は横軸に回って外れる)
            expect(explorer.locator(".chart .ytitle")).to_have_text("体積 (Å³)")
            page.get_by_role("button", name="圧力 (GPa)", exact=True).click()
            expect(explorer.locator(".chart .legendtext")).to_have_count(2)
            page.get_by_role("button", name="圧力 (GPa)", exact=True).click()
            page.get_by_label("点で表示する").check()
            page.wait_for_timeout(500)
            explorer.scroll_into_view_if_needed()
            page.screenshot(path=SHOTS / "06b_axis_explorer.png")

            # 作り直し・一覧・ホーム・設定
            page.get_by_role("link", name="同じ条件で作り直す").click()
            page.get_by_role("heading", name="温度・圧力の流れ").wait_for()
            expect(page.get_by_label("ステップ数").nth(1)).to_have_value("3000")
            page.get_by_role("link", name="ジョブ", exact=True).click()
            page.get_by_role("heading", name="ジョブ", exact=True).wait_for()
            page.locator(".job-card").first.wait_for()
            shot("07_jobs")
            page.get_by_role("link", name="ホーム", exact=True).click()
            page.get_by_role("heading", name="最近のジョブ").wait_for()
            shot("08_home")
            page.get_by_role("link", name="設定", exact=True).click()
            page.get_by_role("heading", name="Materials Project").wait_for()
            shot("09_settings")

            # 静的計算: メニュー → 条件 → 確認 → 実行 → 結果の要点
            page.get_by_role("link", name="ホーム", exact=True).click()
            expect(page.locator(".tile-ready").first).to_be_visible()
            page.get_by_role("link", name="静的計算").click()
            page.get_by_role("heading", name="計算の条件").wait_for()
            expect(page.get_by_text("に対応しています")).to_be_visible()
            choose_structure(page, "MgO")
            expect(page.get_by_text("216 原子")).to_be_visible()
            for axis in "abc":
                box = page.get_by_label(f"{axis} 方向の繰り返し")
                box.fill("2")
                box.dispatch_event("change")
            expect(page.get_by_text("64 原子")).to_be_visible()
            expect(page.get_by_label("セルも最適化する")).to_be_visible()
            page.get_by_label("構造最適化をする").uncheck()
            expect(page.get_by_label("セルも最適化する")).to_have_count(0)        # 条件付きの欄が消える
            page.get_by_label("構造最適化をする").check()
            shot("12_static_form")
            page.get_by_role("button", name="内容を確認する").click()
            page.get_by_role("heading", name="実行前の確認").wait_for()
            page.get_by_role("button", name="この内容で実行する").click()
            page.locator(".status-ok").wait_for(timeout=120_000)
            page.get_by_role("heading", name="結果の要点").wait_for()
            expect(page.get_by_text("体積弾性率 B")).to_be_visible()
            page.get_by_role("heading", name="状態方程式 (凝集エネルギー曲線)").wait_for()
            elastic = page.locator("section", has=page.get_by_role("heading", name="弾性定数 C_ij (GPa)"))
            expect(elastic.locator("tbody tr")).to_have_count(6)                # 6×6 の行列は表で出す
            page.locator(".stack .chart .main-svg").first.wait_for(timeout=20_000)
            page.wait_for_timeout(800)
            shot("13_static_done")
            page.get_by_role("link", name="同じ条件で作り直す").click()
            page.get_by_role("heading", name="計算の条件").wait_for()
            expect(page.get_by_text("64 原子")).to_be_visible()

            # 熱輸送: 手法を切り替えると入力欄が入れ替わる
            page.goto(f"{BASE}/#/calc/thermal")
            page.get_by_role("heading", name="計算の条件").wait_for()
            expect(page.get_by_label("熱流の方向")).to_be_visible()
            page.locator("label.field", has_text="手法").locator("select").select_option("gk")
            expect(page.get_by_label("熱流の方向")).to_have_count(0)
            page.get_by_text("詳細設定").click()
            expect(page.get_by_label("相関の長さ (サンプル)")).to_be_visible()
            shot("14_thermal_form")

            # 細長すぎるセル: 理由と直し方が画面に残る (トーストで消えない)
            choose_structure(page, "MgO")
            for axis, value in (("a", "3"), ("b", "3"), ("c", "20")):
                box = page.get_by_label(f"{axis} 方向の繰り返し")
                box.fill(value)
                box.dispatch_event("change")
            expect(page.get_by_text("1,440 原子")).to_be_visible()
            page.get_by_role("button", name="内容を確認する").click()
            problem = page.get_by_role("alert")
            expect(problem).to_contain_text("a を 5 以上、b を 5 以上")
            expect(problem).to_contain_text("c を 14 以下")
            page.wait_for_timeout(600)
            page.screenshot(path=SHOTS / "14b_cell_problem.png")
            box = page.get_by_label("c 方向の繰り返し")
            box.fill("14")
            box.dispatch_event("change")
            expect(problem).to_have_count(0)                                      # 入力を変えたら消える
            page.get_by_role("button", name="内容を確認する").click()
            page.get_by_role("heading", name="実行前の確認").wait_for()

            # 拡散: 短い条件で実行し、MSD のグラフと元素別の拡散係数を確認
            page.goto(f"{BASE}/#/calc/diffusion")
            page.get_by_role("heading", name="計算の条件").wait_for()
            choose_structure(page, "MgO")
            expect(page.get_by_text("216 原子")).to_be_visible()
            for label, value in (("温度 (K)", "2000"), ("平衡化 (ステップ)", "500"), ("本計算 (ステップ)", "2000")):
                box = page.get_by_label(label, exact=True)
                box.fill(value)
                box.dispatch_event("change")
            page.get_by_role("button", name="内容を確認する").click()
            page.get_by_role("heading", name="実行前の確認").wait_for()
            expect(page.get_by_text("2,500 (2.5 ps)")).to_be_visible()
            page.get_by_role("button", name="この内容で実行する").click()
            page.locator(".status-ok").wait_for(timeout=120_000)
            expect(page.get_by_text("拡散係数 D (Mg)")).to_be_visible()
            page.get_by_role("heading", name="平均二乗変位 (MSD)").wait_for()
            page.locator(".stack .chart .main-svg").first.wait_for(timeout=20_000)
            expect(page.get_by_text("データ", exact=True)).to_be_visible()                  # 表が複数あるので選べる
            page.wait_for_timeout(800)
            shot("15_diffusion_done")

            # ダークモード
            dark = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                                       color_scheme="dark", storage_state=context.storage_state())
            page = dark.new_page()
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "401" not in m.text and "400 (Bad Request)" not in m.text else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{BASE}/#/jobs/1")
            page.locator(".chart .main-svg").first.wait_for(timeout=20_000)
            page.get_by_role("button", name="再生").wait_for()
            page.wait_for_timeout(800)
            shot("10_job_dark")

            # ログアウト
            page.goto(f"{BASE}/#/settings")
            page.get_by_role("button", name="ログアウト").click()
            page.get_by_role("heading", name="ログイン").wait_for()
            shot("11_login")
            browser.close()
    except Exception:
        print("errors so far:", errors)
        raise
    finally:
        server.terminate()
        server.wait(timeout=10)
    print("errors:", errors)
    if errors:
        sys.exit(1)
    shutil.rmtree(DATA, ignore_errors=True)
    print("UI test OK")


if __name__ == "__main__":
    main()
