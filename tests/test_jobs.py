"""大批名额管理器自检（仅标准库）：直接运行 `python3 -I tests/test_jobs.py`。"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.jobs import LARGE_JOB_BYTES, LargeBatchSlot, LargeJobBusy  # noqa: E402


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        raise SystemExit(1)


def main():
    slot = LargeBatchSlot()

    # 1) 阈值：达到 1 MiB（含整）算大批
    check("阈值常量为 1 MiB", LARGE_JOB_BYTES == 1024 * 1024)
    check("1 MiB 整判定为大批", slot.is_large(1024 * 1024) is True)
    check("略小于 1 MiB 判定为小批", slot.is_large(1024 * 1024 - 1) is False)

    # 2) 大批占用期间，第二个大批立即拒入（不排队），完成后名额释放
    started = threading.Event()
    release = threading.Event()

    def slow_job():
        started.set()
        release.wait(timeout=10)
        return "done"

    fut1, job1 = slot.submit("solve", LARGE_JOB_BYTES, slow_job)
    check("大批提交返回作业快照", job1 is not None and job1.kind == "solve")
    check("名额已占用", slot.current() is job1)
    check("worker 已开始执行", started.wait(timeout=5))

    def reject_job():
        return "should-never-run"

    busy = None
    try:
        slot.submit("solve", LARGE_JOB_BYTES + 10, reject_job)
    except LargeJobBusy as exc:
        busy = exc
    check("第二个大批立即抛 LargeJobBusy（不排队）", busy is not None)
    check("拒入时返回占用者信息", busy is not None and busy.current is job1)

    # 3) 大批占用期间，清洗与导出共享名额：导出大批同样被拒
    export_busy = False
    try:
        slot.submit("export", LARGE_JOB_BYTES, reject_job)
    except LargeJobBusy:
        export_busy = True
    check("大批 UTC 导出与清洗共用名额，同样被拒", export_busy)

    # 4) 大批占用期间，小批作业可并发执行（独立线程池，不被大批阻塞）
    small_done = threading.Event()

    def small_job():
        small_done.set()
        return "small-ok"

    fut_small, job_small = slot.submit("solve", 1024, small_job)
    check("小批不占用大批名额（job 为 None）", job_small is None)
    check("大批运行时小批仍能立即完成", small_done.wait(timeout=5))
    check("小批结果正确", fut_small.result(timeout=5) == "small-ok")
    check("大批名额仍被原作业持有", slot.current() is job1)

    # 5) 释放大批，后续大批可再次获取（完成即释放）
    release.set()
    check("第一个大批结果返回", fut1.result(timeout=5) == "done")
    time.sleep(0.05)  # 等 worker 的 finally 执行
    check("作业完成后名额释放", slot.current() is None)

    fut2, job2 = slot.submit("export", LARGE_JOB_BYTES, lambda: "again")
    check("释放后新大批可获取名额", fut2.result(timeout=5) == "again")
    time.sleep(0.05)
    check("第二个大批完成后名额再次释放", slot.current() is None)

    # 6) 作业失败也释放名额
    def boom():
        raise RuntimeError("boom")

    fut3, job3 = slot.submit("solve", LARGE_JOB_BYTES, boom)
    raised = False
    try:
        fut3.result(timeout=5)
    except RuntimeError:
        raised = True
    time.sleep(0.05)
    check("worker 抛错向上传递", raised)
    check("作业失败后名额仍释放", slot.current() is None)

    print("\n名额管理器自检全部通过 ✅")


if __name__ == "__main__":
    main()
