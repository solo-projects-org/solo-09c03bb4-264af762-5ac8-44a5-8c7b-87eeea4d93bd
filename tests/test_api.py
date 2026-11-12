"""HTTP 级并发自检（仅标准库）：直接运行 `python3 -I tests/test_api.py`。

在后台线程启动真实 uvicorn，用 urllib 验证：
1. 大批清洗占用名额期间，样例下载 / 小批清洗仍能并发返回；
2. 名额占用时第二个大批（清洗或导出）立即 503，不排队；
3. 客户端断开（模拟关掉发起窗口）后作业继续跑完并最终释放名额；
4. 沿用 10 MB 上传上限、整链唯一判定与导出格式。
"""
import asyncio
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# -I 隔离模式会忽略 PYTHONPATH，允许通过 TEST_EXTRA_PATH 显式注入依赖目录
_extra = os.environ.get("TEST_EXTRA_PATH")
if _extra:
    sys.path[:0] = [p for p in _extra.split(os.pathsep) if p]

# 大批 worker 演示延迟（必须在 app 导入前设置，worker 启动时读取）
os.environ["LARGE_JOB_DELAY_SECONDS"] = "4"

import uvicorn  # noqa: E402

PORT = 18000
BASE = f"http://127.0.0.1:{PORT}"
THRESHOLD = 1024 * 1024


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        raise SystemExit(1)


def http(method, path, body=None, headers=None, raw=False, timeout=30):
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers=headers or {})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        payload = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.headers.items()}, payload
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def multipart(fields, files):
    """fields: dict[str,str]; files: dict[name,(filename,bytes,ctype)]。"""
    boundary = "----test" + uuid.uuid4().hex
    buf = io.BytesIO()
    for name, value in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(value.encode() + b"\r\n")
    for name, (filename, content, ctype) in files.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
        buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.write(content + b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def get_slot():
    status, _, body = http("GET", "/api/slot")
    return json.loads(body)


def start_server():
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=PORT,
                            log_level="warning")
    server = uvicorn.Server(config)

    def run():
        asyncio.run(server.serve())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            status, _, _ = http("GET", "/api/health", timeout=2)
            if status == 200:
                return server
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def main():
    # 没有依赖 fastapi 的测试工具，直接用已安装的 uvicorn 起服务
    start_server()

    small_csv = (ROOT / "samples" / "dst_sample.csv").read_bytes()
    large_csv = (ROOT / "samples" / "dst_sample_large.csv").read_bytes()
    check("大批样例达到 1 MiB 阈值", len(large_csv) >= THRESHOLD)
    check("内置样例属于小批", len(small_csv) < THRESHOLD)

    # 初始空闲
    check("初始名额空闲", get_slot()["busy"] is False)

    # ---- 1) 发起大批清洗（后台线程，保持连接等待）----
    large_body, large_headers = multipart({}, {"file": ("big.csv", large_csv, "text/csv")})
    holder = {}

    def post_large_solve():
        holder["result"] = http("POST", "/api/solve", large_body, large_headers)

    t = threading.Thread(target=post_large_solve)
    t.start()

    deadline = time.time() + 10
    while time.time() < deadline and not get_slot()["busy"]:
        time.sleep(0.1)
    slot_info = get_slot()
    check("大批清洗已占用名额", slot_info["busy"] is True)
    check("名额信息标注为 solve 且字节数正确",
          slot_info["current"]["kind"] == "solve"
          and slot_info["current"]["size_bytes"] == len(large_csv))

    # ---- 2) 占用期间：样例下载不受影响 ----
    t0 = time.time()
    status, _, body = http("GET", "/api/sample")
    check("大批占用时样例仍可下载", status == 200 and body == small_csv)
    check("样例请求未被大批阻塞（< 2s）", time.time() - t0 < 2)

    # ---- 3) 占用期间：小批清洗并发完成 ----
    small_body, small_headers = multipart({}, {"file": ("small.csv", small_csv, "text/csv")})
    t0 = time.time()
    status, _, body = http("POST", "/api/solve", small_body, small_headers)
    data = json.loads(body)
    check("大批占用时小批清洗成功", status == 200 and data["summary"]["total_tails"] == 5)
    check("小批清洗未被大批阻塞（< 2s）", time.time() - t0 < 2)

    # ---- 4) 第二个大批清洗立即 503，不排队 ----
    status, h503, body = http("POST", "/api/solve", large_body, large_headers)
    detail = json.loads(body).get("detail", "")
    check("并发大批清洗返回 503", status == 503)
    check("503 提示稍后重试且不排队", "稍后重试" in detail)
    check("503 带 Retry-After 头", h503.get("retry-after") == "5")
    check("名额仍只被原作业持有（未被抢占）",
          get_slot()["current"]["id"] == slot_info["current"]["id"])

    # ---- 5) 大批 UTC 导出共用名额，同样立即 503 ----
    exp_body, exp_headers = multipart(
        {"tail": "__all__"}, {"file": ("big.csv", large_csv, "text/csv")})
    status, _, body = http("POST", "/api/export", exp_body, exp_headers)
    check("并发大批导出返回 503（共用名额）", status == 503)

    # ---- 6) 小批导出仍可工作，且沿用整链唯一判定与导出格式 ----
    small_exp, small_exp_h = multipart(
        {"tail": "__all__"}, {"file": ("small.csv", small_csv, "text/csv")})
    status, headers, body = http("POST", "/api/export", small_exp, small_exp_h)
    check("小批导出成功", status == 200)
    check("导出表头沿用原格式",
          body.splitlines()[0] == b"tail_number,seq,dep_utc,arr_utc,duration_minutes")
    check("仅导出唯一解机尾（B-7001/B-7005 共 5 段）",
          body.count(b"\n") == 1 + 3 + 2)
    check("导出不包含多解机尾 B-7004", b"B-7004" not in body)

    # 非唯一解导出仍被拒（整链判定不变）
    bad_exp, bad_exp_h = multipart(
        {"tail": "B-7004"}, {"file": ("small.csv", small_csv, "text/csv")})
    status, _, _ = http("POST", "/api/export", bad_exp, bad_exp_h)
    check("多解机尾导出仍为 409", status == 409)

    # ---- 7) 关窗口语义：客户端断开后作业继续，完成后自动释放名额 ----
    # 先等当前大批结束、名额释放，再发起一个随后立即断开的大批请求
    t.join(timeout=30)
    check("最初大批请求正常返回结果",
          holder.get("result", (None,))[0] == 200)
    result_json = json.loads(holder["result"][2])
    check("大批结果完整（469 机尾全部唯一解）",
          result_json["summary"]["total_tails"] == 469
          and result_json["summary"]["unique"] == 469)
    deadline = time.time() + 10
    while time.time() < deadline and get_slot()["busy"]:
        time.sleep(0.2)
    check("作业完成后名额自动释放", get_slot()["busy"] is False)

    # 用裸 socket 发出大批请求后立刻关闭连接（模拟关掉发起窗口/取消请求）
    disc_body, disc_headers = multipart(
        {}, {"file": ("big.csv", large_csv, "text/csv")})
    raw = (b"POST /api/solve HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
           + b"".join(f"{k}: {v}\r\n".encode() for k, v in disc_headers.items())
           + f"Content-Length: {len(disc_body)}\r\n\r\n".encode() + disc_body)
    import socket
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    sock.sendall(raw)
    sock.close()  # 不读响应直接断开

    deadline = time.time() + 10
    while time.time() < deadline and not get_slot()["busy"]:
        time.sleep(0.1)
    check("断开连接后作业仍被受理并占用名额（关窗口不取消作业）",
          get_slot()["busy"] is True)

    # 名额占用期间新大批仍被拒
    status503, h503, body503 = http("POST", "/api/solve", large_body, large_headers)
    check("断开窗口后名额仍有效占用，新大批继续 503", status503 == 503)
    check("503 带 Retry-After 头", h503.get("retry-after") == "5")

    deadline = time.time() + 30
    while time.time() < deadline and get_slot()["busy"]:
        time.sleep(0.2)
    check("断线作业跑完后名额自动释放", get_slot()["busy"] is False)

    # ---- 8) 释放后大批可再次提交 ----
    again = threading.Event()

    def post_once():
        s, _, _ = http("POST", "/api/solve", large_body, large_headers)
        again.status = s
        again.set()

    th = threading.Thread(target=post_once)
    th.start()
    deadline = time.time() + 5
    while time.time() < deadline and not get_slot()["busy"]:
        time.sleep(0.1)
    check("释放后新大批成功占名额", get_slot()["busy"] is True)
    th.join(timeout=30)
    check("释放后的大批请求成功", again.status == 200)
    deadline = time.time() + 10
    while time.time() < deadline and get_slot()["busy"]:
        time.sleep(0.2)
    check("新大批完成后名额再次释放", get_slot()["busy"] is False)

    # ---- 9) 上传上限沿用 10 MB ----
    oversize = b"x" * (10 * 1024 * 1024 + 10)
    ov_body, ov_h = multipart({}, {"file": ("big.csv", oversize, "text/csv")})
    status, _, _ = http("POST", "/api/solve", ov_body, ov_h)
    check("超过 10 MB 仍返回 413", status == 413)

    print("\nHTTP 并发自检全部通过 ✅")


if __name__ == "__main__":
    main()
