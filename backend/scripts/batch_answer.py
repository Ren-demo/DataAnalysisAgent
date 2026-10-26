"""批量答题回填脚本（精简版，独立通用，不依赖本项目内部代码）。

把题目 JSON（``[{"id": ..., "question": ..., "answer": ""}]``）里的问题逐条经后端
``POST /api/chat/stream`` 送入系统处理，把系统返回的答案回填到 ``answer`` 字段。

- 每个问题的 ``id`` 与 ``question`` 合并后一起作为 ``question`` 传入（id 是块 id，供后端按块定位）。
- 只回填「答案」：query 取 ``explanation``，chat 取 ``answer``，不取 ``analysis``（分析说明）。
- 路径 / 数据源 / 开关均在下方「配置区」硬编码，直接 ``python batch_answer.py`` 运行。
- 依赖仅 ``requests``；后端需先启动：``uvicorn app.main:app --port 8000``。
"""
import json
import sys
from pathlib import Path

import requests

# ===================== 配置区（用户指定） =====================
BASE_URL = "http://localhost:8000"                 # 后端地址
# ⚠ doc_id 是**每次导入文档时随机生成**的（`uuid4().hex[:12]`），重新上传同一个文件也会变。
# 写死一个旧的 id 会让 get_engine 抛 ValueError（未知数据源），全量题目**一条都答不出**。
# 跑之前先 `curl http://localhost:8000/api/datasources` 取当前值。
DATA_SOURCE_ID = "doc:a85671194933"                # 数据源 id
QUESTION_FILE = r"E:\ChongqingAI\赛题一数据\非结构化数据\Table_Query_task.json"   # 题目 JSON
OUTPUT_FILE = r"E:\ChongqingAI\赛题一数据\非结构化数据\Table_Query_task_ans.json"  # None = 回填到原文件
OVERWRITE = False   # False = 跳过 answer 非空条目（续跑）；True = 覆盖重跑
LIMIT = 0           # >0 只处理前 N 条
# ==============================================================

_TIMEOUT = (30, 600)   # (连接超时, 读取超时) 秒


def _extract_answer(result: dict) -> str:
    """从 QueryResponse 提取答案：chat 取 answer，query 取 explanation（不取 analysis）。"""
    if result.get("kind") == "chat":
        return result.get("answer") or ""
    return result.get("explanation") or ""


def _ask(question: str) -> tuple[str | None, dict | None]:
    """发一条问题，返回 ``(answer, error)``（成功时 error 为 None，失败时 answer 为 None）。"""
    url = f"{BASE_URL.rstrip('/')}/api/chat/stream"
    resp = requests.post(
        url,
        json={"question": question, "data_source_id": DATA_SOURCE_ID},
        stream=True,
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        resp.close()
        return None, {"message": f"HTTP {resp.status_code}", "error_code": "SYSTEM_ERROR"}

    result: dict | None = None
    error: dict | None = None
    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        try:
            evt = json.loads(payload)
        except ValueError:
            continue
        if evt.get("type") == "result":
            result = evt.get("data") or {}
        elif evt.get("type") == "error":
            error = {"message": evt.get("message", ""), "error_code": evt.get("error_code", "SYSTEM_ERROR")}
    resp.close()

    if result is not None:
        if result.get("error") or result.get("error_code"):
            return None, {"message": result.get("error") or "", "error_code": result.get("error_code") or "SYSTEM_ERROR"}
        return _extract_answer(result), None
    return None, error or {"message": "未收到 result 事件", "error_code": "SYSTEM_ERROR"}


def main() -> int:
    if not QUESTION_FILE or not DATA_SOURCE_ID:
        print("错误：请在配置区填写 QUESTION_FILE 与 DATA_SOURCE_ID")
        return 1

    src = Path(QUESTION_FILE)
    if not src.is_file():
        print(f"错误：题目文件不存在：{src}")
        return 1

    # utf-8-sig：兼容带/不带 UTF-8 BOM 的文件
    items = json.loads(src.read_text(encoding="utf-8-sig"))
    if not isinstance(items, list):
        print("错误：题目 JSON 顶层必须是数组（[{...}, ...]）")
        return 1

    dst = Path(OUTPUT_FILE) if OUTPUT_FILE else src
    total = len(items)
    ok = failed = skipped = 0

    for i, item in enumerate(items):
        if LIMIT and i >= LIMIT:
            print(f"已达 LIMIT {LIMIT}，停止。")
            break
        if not isinstance(item, dict) or not item.get("question"):
            skipped += 1
            continue
        if item.get("answer") and not OVERWRITE:
            skipped += 1
            continue

        question = f"{item.get('id', '')} {item['question']}".strip()
        answer, error = _ask(question)

        if error:
            failed += 1
            item["answer"] = ""
            print(f"[{i + 1}/{total}] 失败 {item.get('id')} -> {error['error_code']}: {error['message'][:60]}")
        else:
            ok += 1
            item["answer"] = answer or ""
            print(f"[{i + 1}/{total}] 成功 {item.get('id')} -> {(answer or '')[:50]}")

        dst.write_text(json.dumps(items, ensure_ascii=False, indent=4), encoding="utf-8")

    print(f"\n完成：共 {total} 条，成功 {ok}，失败 {failed}，跳过 {skipped}")
    print(f"结果已写入：{dst}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
