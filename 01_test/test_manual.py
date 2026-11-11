"""マニュアル (目次・文字検索) と、入力ファイルの表示 (説明つき) の通しテスト。
test_transition.py を KEEP_DATA=1 で流して残したデータ (PLUMED・構造相転移のジョブ) を使う。

    MATSIM_TEST_DATA=<dir> CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_manual.py
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_transition"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8037
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def main():
    if not (DATA / "app.db").is_file():
        sys.exit("データがありません。先に test_transition.py を KEEP_DATA=1 で流してください")
    server = subprocess.Popen([SERVER_PYTHON, "-m", "uvicorn", "matsimapp.main:app", "--port", str(PORT)], cwd=ROOT,
                              env={**os.environ, "MATSIM_DATA_DIR": str(DATA)}, stdout=subprocess.DEVNULL,
                              stderr=subprocess.STDOUT, start_new_session=True)
    errors = []
    try:
        for _ in range(60):
            try:
                requests.get(f"{BASE}/api/status", timeout=1)
                break
            except requests.ConnectionError:
                time.sleep(0.5)
        s = requests.Session()
        s.headers.update(H)
        s.post(f"{BASE}/api/auth/login", json={"username": "tester", "password": "test-password-123"})
        jobs = s.get(f"{BASE}/api/jobs").json()
        plumed = next(j["id"] for j in jobs if j["kind"] == "plumed" and j["status"] == "done")
        transition = next(j["id"] for j in jobs if j["kind"] == "transition" and j["status"] == "done")

        # ---- ファイルの中身の API ---------------------------------------------------------------
        r = s.get(f"{BASE}/api/jobs/{plumed}/text", params={"name": "plumed.dat"}).json()
        check("API: plumed.dat の中身", "METAD" in r["text"] or "PRINT" in r["text"], r["text"][:80])
        r = s.get(f"{BASE}/api/jobs/{transition}/text", params={"name": "dump.xyz"}).json()
        check("API: 大きいファイルは先頭だけ", r["truncated"] and len(r["text"]) <= 200_000, (r["size"], len(r["text"])))
        r = s.get(f"{BASE}/api/jobs/{plumed}/text", params={"name": "../app.db"})
        check("API: 一覧にない名前は読めない", r.status_code == 400, r.status_code)
        r = requests.get(f"{BASE}/api/jobs/{plumed}/text", params={"name": "run.in"}, headers=H)
        check("API: ログインしないと読めない", r.status_code == 401, r.status_code)

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
            # ---- マニュアル (スマホ幅) ----------------------------------------------------------
            page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: m.type == "error" and "401" not in m.text and errors.append(m.text))
            page.goto(f"{BASE}/manual/manual.html")
            links = page.locator("#toc a")
            check("目次: 部と章から作られる", links.count() >= 25, links.count())
            check("目次: スマホでは閉じている", not page.locator("#toc").is_visible())
            check("スマホ: 検索欄が十分な幅", page.locator("#search").bounding_box()["width"] > 200,
                  page.locator("#search").bounding_box()["width"])
            page.get_by_role("button", name="目次").click()
            expect(page.locator("#toc")).to_be_visible()
            page.screenshot(path=SHOTS / "m01_manual_toc.png")
            page.locator("#toc a", has_text="10. 自由エネルギー面").click()
            page.wait_for_timeout(600)
            top = page.locator("#ch-plumed").bounding_box()["y"]
            check("目次: クリックで章に移動し、目次が閉じる", 0 <= top < 240 and not page.locator("#toc").is_visible(), top)
            figs = page.locator("figure.fig svg")
            check("図: 概念図がすべて描かれる", figs.count() >= 19
                  and all(b and b["height"] > 50 for b in [figs.nth(i).bounding_box() for i in range(figs.count())]), figs.count())
            page.fill("#search", "障壁")
            page.wait_for_timeout(500)
            check("図: 検索しても図の中の文字は消えない (色付けは本文だけ)",
                  page.locator("svg mark").count() == 0 and page.locator("mark.hit").count() > 0
                  and page.locator("svg text", has_text="障壁").count() > 0)
            page.fill("#search", "CV")
            page.wait_for_timeout(500)
            count = page.locator("mark.hit").count()
            check("検索: 一致に色が付く", count > 10, count)
            check("検索: 件数が出る", f"/ {count} 件" in page.locator("#search-count").inner_text(),
                  page.locator("#search-count").inner_text())
            check("検索: 章ごとの一覧", page.locator("#search-results li").count() >= 3)
            page.screenshot(path=SHOTS / "m02_manual_search.png")
            page.locator("#search-next").click()
            page.wait_for_timeout(300)
            check("検索: 次へ移動", page.locator("#search-count").inner_text().startswith("2 /"),
                  page.locator("#search-count").inner_text())
            page.fill("#search", "ENVIRONMENTSIMILARITY")
            page.wait_for_timeout(500)
            page.locator("#search-results button").first.click()
            page.wait_for_timeout(500)
            check("検索: 閉じた折りたたみの中の一致は開く", page.locator("details.box").first.evaluate("d => d.open"))
            page.fill("#search", "存在しない語ぴよ")
            page.wait_for_timeout(500)
            check("検索: 見つからない", page.locator("#search-count").inner_text() == "見つかりません")
            page.goto(f"{BASE}/manual/manual.html?q=ポテンシャル")
            page.wait_for_timeout(500)
            check("検索: ?q= で開くと検索する", page.locator("mark.hit").count() > 3)
            # 広い画面: 目次は左に常に出る
            wide = browser.new_page(viewport={"width": 1280, "height": 900})
            wide.on("pageerror", lambda e: errors.append(str(e)))
            wide.goto(f"{BASE}/manual/manual.html#ch-goal")
            wide.wait_for_timeout(500)
            box = wide.locator("#toc").bounding_box()
            check("広い画面: 目次が画面の中に貼り付いている", 0 <= box["y"] < 120, box)
            check("広い画面: 目次が左に出て、読んでいる章が強調される", wide.locator("#toc").is_visible()
                  and "4." in wide.locator("#toc a.current").inner_text(), wide.locator("#toc a.current").inner_text())
            wide.screenshot(path=SHOTS / "m03_manual_wide.png")

            # ---- 入力ファイル (ジョブの画面) ------------------------------------------------------
            page.goto(BASE)
            page.get_by_label("ユーザー名").fill("tester")
            page.get_by_label("パスワード").fill("test-password-123")
            page.get_by_role("button", name="ログイン").click()
            expect(page.get_by_role("heading", name="計算環境")).to_be_visible(timeout=15000)
            page.goto(f"{BASE}/#/jobs/{plumed}")
            page.get_by_text("入力ファイル (run.in・plumed.dat など)").click()
            expect(page.locator(".annot-note").first).to_be_visible(timeout=10000)
            notes = page.locator(".annot-note").all_inner_texts()
            check("入力ファイル: run.in に説明が付く", any("PLUMED を 1 ステップごと" in n for n in notes)
                  and any("時間刻み" in n for n in notes), notes[:4])
            page.get_by_role("button", name="plumed.dat", exact=True).click()
            notes = page.locator(".annot-note").all_inner_texts()
            check("入力ファイル: plumed.dat に説明が付く", any("メタダイナミクス" in n for n in notes) or any("書く" in n for n in notes),
                  notes[:4])
            page.locator("details.card", has_text="入力ファイル").screenshot(path=SHOTS / "m04_input_annotated.png")
            page.get_by_text("行ごとの説明を付ける").click()
            check("入力ファイル: 説明なし (そのまま) にできる", page.locator("details.card pre.code").first.is_visible()
                  and page.locator(".annot-note").count() == 0)
            page.get_by_role("button", name="COLVAR の中身を見る").click()
            expect(page.locator("h3", has_text="COLVAR")).to_be_visible(timeout=10000)
            check("ファイル一覧: 「見る」で中身が出る", "#! FIELDS" in page.locator("section.card pre.code").last.inner_text())

            # ---- 結晶構造の判定の画面 (1 元素の構造) に「null」が出ない ------------------------------------
            cna_task = next(j["id"] for j in jobs if j["kind"] == "cna" and j["status"] == "done")
            page.goto(f"{BASE}/#/analysis/cna?task={cna_task}")
            expect(page.get_by_role("heading", name="結晶構造の比率")).to_be_visible(timeout=15000)
            page.wait_for_timeout(500)
            check("結晶構造の判定: 画面に null が出ない", "null" not in page.locator("main, #view, body").first.inner_text())

            # ---- 入力ファイル (実行前の確認画面) --------------------------------------------------
            page.goto(f"{BASE}/#/calc/plumed")
            expect(page.get_by_role("button", name="内容を確認する")).to_be_visible(timeout=15000)
            check("計算画面: マニュアルの章へのリンク", page.get_by_role("link", name="初めての人へ: CV とパラメータの決め方 (マニュアル)")
                  .get_attribute("href").endswith("#ch-plumed"))
            page.get_by_role("button", name="内容を確認する").click()
            page.get_by_text("生成される入力ファイル (run.in など)").click()
            expect(page.get_by_role("button", name="plumed.dat", exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name="plumed.dat", exact=True).click()
            check("確認画面: run.in と plumed.dat を分けて説明つきで見られる",
                  any("メタダイナミクス" in n for n in page.locator(".annot-note").all_inner_texts()))
            page.screenshot(path=SHOTS / "m05_preview_inputs.png", full_page=True)
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
    check("ブラウザのエラーなし", not errors, errors)
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} OK" + (f"  失敗: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
