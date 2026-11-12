"""FastAPI 入口：航段时间清洗（DST 安全入仓）。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from .jobs import LargeBatchSlot, LargeJobBusy
from .solver import CsvFormatError, build_export_csv, solve_csv

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_PATH = BASE_DIR.parent / "samples" / "dst_sample.csv"
LARGE_SAMPLE_PATH = BASE_DIR.parent / "samples" / "dst_sample_large.csv"
INDEX_PATH = BASE_DIR / "static" / "index.html"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB，沿用原上传上限

app = FastAPI(title="航段时间清洗 · DST 安全入仓", version="1.1.0")

# 进程内唯一大批名额（清洗 / UTC 导出共用）
slot = LargeBatchSlot()


class _RequestError(Exception):
    """worker 线程中产生的、需要映射成 HTTP 响应的业务错误。"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def _read_csv_bytes(file: UploadFile) -> bytes:
    # 只在事件循环做 I/O 读取，解码与求解全部下沉到线程，避免阻塞其它窗口
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 10 MB 上限")
    return data


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _RequestError(400, "文件不是合法的 UTF-8 编码 CSV")


def _solve_worker(data: bytes) -> dict:
    try:
        return solve_csv(_decode(data))
    except CsvFormatError as exc:
        raise _RequestError(400, str(exc))


def _export_worker(data: bytes, tail: str) -> tuple[str, str]:
    try:
        result = solve_csv(_decode(data))
    except CsvFormatError as exc:
        raise _RequestError(400, str(exc))

    if tail == "__all__":
        tails = [t["tail"] for t in result["tails"] if t["status"] == "unique"]
        if not tails:
            raise _RequestError(
                409, "没有可导出的机尾：仅整链唯一解的机尾允许导出")
        filename = "utc_export_all.csv"
    else:
        match = next((t for t in result["tails"] if t["tail"] == tail), None)
        if match is None:
            raise _RequestError(404, f"CSV 中不存在机尾 {tail!r}")
        if match["status"] != "unique":
            raise _RequestError(
                409, f"机尾 {tail} 状态为 {match['status']}，非整链唯一解，禁止导出")
        tails = [tail]
        filename = f"utc_export_{tail}.csv"

    # 沿用原导出列与格式：tail_number,seq,dep_utc,arr_utc,duration_minutes
    return build_export_csv(result, tails), filename


async def _dispatch(kind: str, data: bytes, worker, *args):
    """把作业提交到线程池并等待结果。

    等待发生在事件循环上，不占用线程；HTTP 客户端断开（关掉发起窗口）只会取消
    这里的等待，线程池里的 worker 仍会跑完，大批名额在 worker 结束时才释放。
    """
    try:
        future, job = slot.submit(kind, len(data), worker, data, *args)
    except LargeJobBusy as exc:
        current = exc.current
        raise HTTPException(
            status_code=503,
            detail=(
                "大批作业名额已被占用"
                f"（作业 {current.id} · {kind_label(current.kind)} · "
                f"{round(current.size_bytes / 1048576, 2)} MiB），"
                "清洗与 UTC 导出共用一个大批名额，请稍后重试；小批清洗与样例载入不受影响。"
            ),
            headers={"Retry-After": "5"},
        )
    try:
        return await asyncio.wrap_future(future)
    except _RequestError as exc:
        raise HTTPException(exc.status_code, exc.detail)


def kind_label(kind: str) -> str:
    return "UTC 导出" if kind == "export" else "清洗"


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(INDEX_PATH)


@app.get("/api/sample", include_in_schema=False)
async def sample():
    """内置夏令时样例，可直接下载或在前端一键载入。"""
    return FileResponse(SAMPLE_PATH, media_type="text/csv; charset=utf-8",
                        filename="dst_sample.csv")


@app.get("/api/sample/large", include_in_schema=False)
async def large_sample():
    """大批样例（≥ 1 MiB），用于演示大批名额占用。"""
    if not LARGE_SAMPLE_PATH.exists():
        raise HTTPException(404, "大批样例尚未生成，请先运行 scripts/gen_large_sample.py")
    return FileResponse(LARGE_SAMPLE_PATH, media_type="text/csv; charset=utf-8",
                        filename="dst_sample_large.csv")


@app.get("/api/slot", include_in_schema=False)
async def slot_status():
    """查询大批名额当前状态，供前端轮询展示。"""
    current = slot.current()
    return {
        "large_threshold_bytes": 1024 * 1024,
        "busy": current is not None,
        "current": current.view() if current else None,
    }


@app.post("/api/solve")
async def solve(file: UploadFile = File(...)):
    """上传 CSV，返回每个机尾的求解结果（唯一解/无解/多解/错误）。"""
    data = await _read_csv_bytes(file)
    return await _dispatch("solve", data, _solve_worker)


@app.post("/api/export")
async def export(file: UploadFile = File(...), tail: str = Form(...)):
    """导出机尾的 UTC CSV。仅当整链唯一解时允许；tail=__all__ 导出全部可导出机尾。"""
    data = await _read_csv_bytes(file)
    csv_text, filename = await _dispatch("export", data, _export_worker, tail)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/health", include_in_schema=False)
async def health():
    return {"status": "ok"}
