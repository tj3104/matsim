"""フォノンの振動のようす (分枝と q を選んで矢印と動きで示す) と、構造の選びやすさ (計算画面の構造の一覧・
構造の画面からの計算メニュー) の通しテスト。test_transition.py を KEEP_DATA=1 で流して残したデータを使い、
Si の基本単位胞でフォノン分散を GPUMD で流す。

    MATSIM_TEST_DATA=<dir> CHROMIUM_PATH=<chrome> <playwright の入った python> 01_test/test_phonon_ux.py
"""
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import requests
from ase.build import bulk
from ase.io import write
from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MATSIM_TEST_DATA", ROOT / "01_test" / "data_transition"))
SHOTS = ROOT / "01_test" / "screenshots"
PORT = 8041
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Requested-With": "matsimapp"}
SERVER_PYTHON = os.environ.get("MATSIM_PYTHON", str(Path.home() / ".venv/gpumd312/bin/python"))
checks = []


def check(name, condition, detail=""):
    checks.append((name, bool(condition)))
    print(("OK  " if condition else "NG  ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def arrow_pixels(path):
    """矢印の色 (濃い青 #1d3fd1) に近い画素の数。"""
    pixels = Image.open(path).convert("RGB").getdata()
    return sum(1 for r, g, b in pixels if r < 80 and g < 110 and b > 170)


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
        have = {x["name"]: x["id"] for x in s.get(f"{BASE}/api/structures").json()}
        if "Si prim" not in have:
            buf = io.StringIO()
            write(buf, bulk("Si", "diamond", a=5.43), format="vasp", direct=True)
            have["Si prim"] = s.post(f"{BASE}/api/structures/upload", files={"file": ("POSCAR", buf.getvalue())},
                                     data={"name": "Si prim"}).json()["id"]
        si = have["Si prim"]
        if "MgO (岩塩型)" not in have:   # 構造の一覧の検索・構造の画面の確認に使う (サンプルから追加)
            have["MgO (岩塩型)"] = s.post(f"{BASE}/api/structures/sample", json={"key": "MgO"}).json()["id"]
        potential = s.get(f"{BASE}/api/potentials").json()[0]["id"]
        job = s.post(f"{BASE}/api/calc/jobs", json={"kind": "phonon", "structure_id": si, "potential_id": potential,
                     "repeat": [1, 1, 1], "options": {"path": "GXWKGL"}}).json()["id"]
        start = time.time()
        while time.time() - start < 600 and s.get(f"{BASE}/api/jobs/{job}").json()["status"] in ("queued", "running"):
            time.sleep(2)
        check("フォノン分散が完了", s.get(f"{BASE}/api/jobs/{job}").json()["status"] == "done")

        info = s.get(f"{BASE}/api/jobs/{job}/phonon").json()
        check("振動のようす: q 点 501・分枝 6・高対称点", info["n_q"] == 501 and info["n_bands"] == 6
              and [t["label"] for t in info["ticks"]] == ["Γ", "X", "W", "K", "Γ", "L"], info["ticks"])
        disp = s.get(f"{BASE}/api/jobs/{job}/dataset", params={"key": "dispersion"}).json()["data"]
        worst = 0
        for q in (0, 50, 150, 333, 500):
            m = s.get(f"{BASE}/api/jobs/{job}/phonon/mode", params={"q": q, "band": 0}).json()
            gp = sorted(disp[f"nu_{b + 1}"][q] for b in range(6))
            worst = max(worst, max(abs(a - b) for a, b in zip(m["frequencies"], gp)))
        check("振動数が分散曲線 (GPUMD の omega2.out) と一致", worst < 0.01, worst)
        m = s.get(f"{BASE}/api/jobs/{job}/phonon/mode", params={"q": 3, "band": 0}).json()
        u = np.array(m["u_re"]) + 1j * np.array(m["u_im"])
        check("Γ 点の近くの音響モード: 2 つの原子が同じ向きに動く", np.allclose(u[0], u[1], atol=0.05), u.round(2).tolist())
        m = s.get(f"{BASE}/api/jobs/{job}/phonon/mode", params={"q": 0, "band": 5}).json()
        u = np.array(m["u_re"])
        check("Γ 点の光学モード: 2 つの原子が逆向きに動く", np.allclose(u[0], -u[1], atol=0.05) and m["near"] == "Γ", u.round(2).tolist())
        r = s.get(f"{BASE}/api/jobs/{job}/phonon/mode", params={"q": 9999, "band": 0})
        check("範囲外の q はエラー", r.status_code == 400)
        other = next(j["id"] for j in s.get(f"{BASE}/api/jobs").json() if j["kind"] == "transition")
        check("フォノン以外のジョブはエラー", s.get(f"{BASE}/api/jobs/{other}/phonon").status_code == 400)

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
            page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: m.type == "error" and "401" not in m.text and errors.append(m.text))
            page.goto(BASE)
            page.get_by_label("ユーザー名").fill("tester")
            page.get_by_label("パスワード").fill("test-password-123")
            page.get_by_role("button", name="ログイン").click()
            expect(page.get_by_role("heading", name="計算環境")).to_be_visible(timeout=15000)

            # ---- フォノンの振動のようす ---------------------------------------------------------
            page.goto(f"{BASE}/#/jobs/{job}")
            expect(page.get_by_role("heading", name="振動のようす")).to_be_visible(timeout=20000)
            card = page.locator("section.card", has=page.get_by_role("heading", name="振動のようす"))
            expect(card.locator(".phonon-readout")).to_contain_text("THz", timeout=15000)
            check("画面: 既定は光学の分枝", "分枝 4" in card.locator(".phonon-readout").inner_text(),
                  card.locator(".phonon-readout").inner_text())
            card.get_by_role("button", name="X", exact=True).click()
            expect(card.locator(".phonon-readout")).to_contain_text("X 点", timeout=10000)
            card.locator("label.field", has_text="分枝").locator("select").select_option("0")
            expect(card.locator(".phonon-readout")).to_contain_text("分枝 1", timeout=10000)
            check("画面: 高対称点へ飛んで、分枝を選べる", "X 点" in card.locator(".phonon-readout").inner_text())
            card.locator("input[type=range]").first.fill("20")
            expect(card.locator(".phonon-readout")).not_to_contain_text("X 点", timeout=10000)
            check("画面: q のスライダーで q が変わる", "0.100" in card.locator(".phonon-readout").inner_text(),
                  card.locator(".phonon-readout").inner_text())
            canvas = card.locator("canvas.viewer")
            canvas.evaluate("c => c.scrollIntoView({block: 'center'})")   # 下の固定メニューにかからないように
            page.wait_for_timeout(800)
            canvas.screenshot(path=SHOTS / "ph01_phonon_a.png")
            page.wait_for_timeout(500)
            canvas.screenshot(path=SHOTS / "ph02_phonon_b.png")
            check("画面: 振動の向きの矢印 (青) が描かれる", arrow_pixels(SHOTS / "ph01_phonon_a.png") > 300,
                  arrow_pixels(SHOTS / "ph01_phonon_a.png"))
            diff = ImageChops.difference(Image.open(SHOTS / "ph01_phonon_a.png").convert("RGB"),
                                         Image.open(SHOTS / "ph02_phonon_b.png").convert("RGB")).getbbox()
            check("画面: 原子が動いている (アニメーション)", diff is not None)
            same = lambda p1, p2: ImageChops.difference(Image.open(p1).convert("RGB"), Image.open(p2).convert("RGB")).getbbox() is None
            # 原子を止める: 平衡の位置で止まる (時間をおいても同じ画像)
            card.get_by_text("原子を動かす").click()
            page.wait_for_timeout(300)
            canvas.screenshot(path=SHOTS / "ph03a_still1.png")
            page.wait_for_timeout(400)
            canvas.screenshot(path=SHOTS / "ph03a_still2.png")
            check("画面: 原子を動かさないと止まる", same(SHOTS / "ph03a_still1.png", SHOTS / "ph03a_still2.png"))
            # 矢印の長さだけを変える
            card.get_by_label("矢印の長さ").fill("0.6")
            page.wait_for_timeout(300)
            canvas.screenshot(path=SHOTS / "ph03b_arrow_short.png")
            card.get_by_label("矢印の長さ").fill("3.5")
            page.wait_for_timeout(300)
            canvas.screenshot(path=SHOTS / "ph03c_arrow_long.png")
            short, long_ = arrow_pixels(SHOTS / "ph03b_arrow_short.png"), arrow_pixels(SHOTS / "ph03c_arrow_long.png")
            check("画面: 矢印の長さを変えられる (今の値も出る)", long_ > 1.5 * short and "3.5 Å" in card.inner_text(),
                  (short, long_))
            # 動きの大きさを変えても、矢印は変わらない (止めている間は原子も平衡の位置のまま)
            card.get_by_label("動きの大きさ").evaluate("e => { e.disabled = false; }")
            card.get_by_label("動きの大きさ").fill("1")
            page.wait_for_timeout(300)
            canvas.screenshot(path=SHOTS / "ph03d_motion_changed.png")
            check("画面: 動きの大きさと矢印は独立 (動きを変えても矢印は同じ)",
                  same(SHOTS / "ph03c_arrow_long.png", SHOTS / "ph03d_motion_changed.png"))
            # 矢印を消して、原子だけ動かす
            card.get_by_text("矢印を描く").click()
            card.get_by_text("原子を動かす").click()
            page.wait_for_timeout(400)
            canvas.screenshot(path=SHOTS / "ph03e_move1.png")
            page.wait_for_timeout(400)
            canvas.screenshot(path=SHOTS / "ph03e_move2.png")
            check("画面: 矢印を消しても原子は動く", arrow_pixels(SHOTS / "ph03e_move1.png") < 50
                  and not same(SHOTS / "ph03e_move1.png", SHOTS / "ph03e_move2.png"))
            card.get_by_text("矢印を描く").click()
            page.wait_for_timeout(300)
            card.screenshot(path=SHOTS / "ph04_phonon_card.png")

            # ---- 構造を選ぶ部品 (計算画面) --------------------------------------------------------
            page.goto(f"{BASE}/#/calc/static")
            chooser = page.locator(".chooser")
            expect(chooser.locator(".chooser-summary")).to_be_visible(timeout=15000)
            chooser.get_by_role("button", name="変える").click()
            total = len(have)
            check("一覧: すべての構造が出る", chooser.locator(".chooser-item").count() == total,
                  (chooser.locator(".chooser-item").count(), total))
            page.screenshot(path=SHOTS / "ph05_chooser_open.png", full_page=True)
            chooser.locator("input[type=search]").fill("Mg O")
            page.wait_for_timeout(200)
            names = chooser.locator(".chooser-item .list-title").all_inner_texts()
            check("一覧: 元素で探せる (Mg と O を含む)", names and all("MgO" in n or "Mg" in n for n in names), names)
            chooser.locator("input[type=search]").fill("")
            chooser.get_by_role("button", name="テスト構造").click()
            subs = chooser.locator(".chooser-item .list-sub").all_inner_texts()
            check("一覧: 取得元で絞れる", subs and all("テスト構造" in t for t in subs), len(subs))
            chooser.get_by_role("button", name="すべて").click()
            chooser.locator("select").select_option("small")
            atoms = [int(t.split("・")[1].strip().split(" ")[0].replace(",", "")) for t in
                     chooser.locator(".chooser-item .list-sub").all_inner_texts()]
            check("一覧: 原子の少ない順に並べ替え", atoms == sorted(atoms), atoms[:6])
            chooser.locator(".chooser-item", has_text="Si prim").click()
            check("一覧: 選ぶと閉じて、選んだ構造が出る", chooser.locator(".chooser-current strong").inner_text() == "Si prim"
                  and chooser.locator(".chooser-panel").is_hidden())

            # ---- 構造の画面から計算を選ぶ ---------------------------------------------------------
            page.goto(f"{BASE}/#/structures/{have['MgO (岩塩型)']}")
            expect(page.get_by_role("heading", name="この構造で計算する")).to_be_visible(timeout=15000)
            tiles = page.locator(".tiles-compact .tile")
            check("構造の画面: 計算メニューが並ぶ", tiles.count() >= 20, tiles.count())
            page.screenshot(path=SHOTS / "ph06_structure_menu.png", full_page=True)
            page.locator(".tiles-compact .tile", has_text="熱膨張").click()
            expect(page.locator(".chooser-current strong")).to_have_text("MgO (岩塩型)", timeout=15000)
            check("構造の画面から開くと、その構造が選ばれている", page.locator("h1").inner_text() == "熱膨張")
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
