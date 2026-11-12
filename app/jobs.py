"""大批作业名额管理。

背景
----
清洗（``/api/solve``）与 UTC 导出（``/api/export``）都是 CPU 密集的同步求解，
直接跑在事件循环里会阻塞所有窗口的请求。这里把求解下沉到线程池，并引入
**进程内唯一一个大批名额**：

- 源 CSV 字节数达到 ``LARGE_JOB_BYTES``（1 MiB）即判定为大批；
- 大批清洗与大批 UTC 导出**共享同一个名额**，名额被占时新的大批请求立即失败
  （抛 :class:`LargeJobBusy`），**不排队**，由路由层转成 503 提示稍后重试；
- 小批作业走独立线程池，即使大批作业正在运行也能并发完成；
- 名额在 worker 的 ``finally`` 中释放，与发起请求的 HTTP 连接生命周期无关：
  关掉发起窗口后，线程里的作业继续运行并占着名额，完成或失败后自动释放。
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

# 源 CSV 达到 1 MiB（含 1 MiB 整）算大批
LARGE_JOB_BYTES = 1024 * 1024
# 小批独立线程池：保证大批跑满时小批与样例请求仍可被立即调度
SMALL_JOB_WORKERS = 4


class LargeJobBusy(Exception):
    """大批名额已被占用。调用方应提示用户稍后重试，而不是排队等待。"""

    def __init__(self, current: "Job"):
        self.current = current
        super().__init__(f"大批名额已被作业 {current.id}（{current.kind}）占用")


@dataclass
class Job:
    """一个正在占用大批名额的作业快照。"""

    id: str
    kind: str  # "solve" | "export"
    size_bytes: int
    started_at: str

    def view(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "size_mib": round(self.size_bytes / (1024 * 1024), 2),
            "started_at": self.started_at,
        }


class LargeBatchSlot:
    """进程内唯一的大批名额 + 隔离的大/小批线程池。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Job | None = None
        # 单 worker：大批作业经名额互斥后天然串行，队列里不会出现第二个作业
        self._large_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="large-job")
        self._small_pool = ThreadPoolExecutor(
            max_workers=SMALL_JOB_WORKERS, thread_name_prefix="small-job")

    def is_large(self, size_bytes: int) -> bool:
        return size_bytes >= LARGE_JOB_BYTES

    def submit(self, kind: str, size_bytes: int,
               func: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Future, Job | None]:
        """提交一个求解作业。

        返回 ``(future, job)``：大批作业的 ``job`` 为名额快照，小批作业为 ``None``。
        大批名额被占时抛 :class:`LargeJobBusy`，不会进入任何等待队列。
        """
        job: Job | None = None
        if self.is_large(size_bytes):
            job = Job(
                id=uuid.uuid4().hex[:12],
                kind=kind,
                size_bytes=size_bytes,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            with self._lock:
                if self._current is not None:
                    # 不排队：立即拒绝
                    raise LargeJobBusy(self._current)
                # 先占位再提交，保证 submit 返回后名额状态对外已可见
                self._current = job
            try:
                future = self._large_pool.submit(
                    self._run_large, job, func, args, kwargs)
            except BaseException:
                # 提交失败（如线程池已关闭）必须回滚名额
                with self._lock:
                    if self._current is job:
                        self._current = None
                raise
            return future, job

        future = self._small_pool.submit(func, *args, **kwargs)
        return future, None

    def _run_large(self, job: Job, func: Callable[..., Any],
                   args: tuple, kwargs: dict) -> Any:
        """大批 worker：无论成功、抛错还是被解释器关闭，最终都释放名额。"""
        try:
            # 可选演示延迟（秒），默认 0，不改变任何业务逻辑与求解结果。
            # 用环境变量 LARGE_JOB_DELAY_SECONDS 开启，便于在双窗口演示中
            # 观察「名额占用 → 并发拒入 → 完成释放」的完整过程。
            delay = float(os.environ.get("LARGE_JOB_DELAY_SECONDS", "0") or 0)
        except (TypeError, ValueError):
            delay = 0
        try:
            if delay > 0:
                time.sleep(delay)
            return func(*args, **kwargs)
        finally:
            with self._lock:
                if self._current is job:
                    self._current = None

    def current(self) -> Job | None:
        with self._lock:
            return self._current
