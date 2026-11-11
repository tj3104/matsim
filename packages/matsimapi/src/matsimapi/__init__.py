"""MatSimAPI: GPUMD・SevenNet による材料シミュレーションの実行エンジン。

構造の用意・計算の投入・ジョブの実行・結果の解析を担当する。使う入口は 3 つあり、どれも同じデータを扱う。

- Python: ``from matsimapi import Client``
- コマンド: ``matsimapi``
- LLM (MCP サーバー): ``matsimapi mcp``

画面は別のパッケージ MatSimApp がこのパッケージの上に作っている。

    from matsimapi import Client

    client = Client()
    cu = client.add_sample("Cu")
    job = client.submit("md", cu, repeat=(3, 3, 3), stages=[{"ensemble": "nvt", "steps": 1000, "temperature": 300}])
    job.wait()
    print(job.thermo()["summary"])
"""

__version__ = "0.16.0"
__all__ = ["Client", "Job", "JobFailed", "NotFound", "__version__"]


def __getattr__(name: str):
    # ase や gpumd_toolkit の読み込みは重いので、使うときに初めて読む
    if name in ("Client", "Job", "JobFailed"):
        from . import client
        return getattr(client, name)
    if name == "NotFound":
        from .service import NotFound
        return NotFound
    raise AttributeError(name)
