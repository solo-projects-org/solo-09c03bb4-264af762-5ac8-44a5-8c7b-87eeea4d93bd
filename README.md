# 航段时间清洗 · DST 安全入仓

面向航司数据工程师的网页工具：把地服导出的**当地时间**航段链解析为唯一 **UTC** 时间链，
避免夏令时（DST）切换时被错误入仓。

## 快速启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000> ，点击「📦 载入内置夏令时样例」→「开始清洗」即可看到完整演示。

> Compose 默认设置了 `LARGE_JOB_DELAY_SECONDS=12`：大批作业开始前会等待 12 秒，
> 方便在两个浏览器窗口里观察名额占用/拒入/释放过程。生产环境把该变量删掉或设为 `0` 即可，
> 不影响任何求解结果。

本地开发（不用 Docker）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

自检（仅标准库；HTTP 并发自检需要能 import uvicorn）：

```bash
python3 -I tests/test_solver.py   # 求解规则
python3 -I tests/test_jobs.py     # 大批名额管理器
python3 -I tests/test_api.py      # HTTP 级并发/拒入/断线存活
```

## 大批名额与并发模型

清洗与 UTC 导出都是 CPU 密集求解，原先直接跑在事件循环里，一个大文件会阻塞所有窗口。
现在的模型：

- **阈值**：源 CSV 字节数 **达到 1 MiB（含 1 MiB 整）** 即为大批，判定只看上传字节数，与内容无关；
- **单名额**：进程内只有一个大批名额，大批清洗与大批 UTC 导出**共用**。名额被占时新的大批请求
  **立即返回 `503 + Retry-After` 提示稍后重试，不排队**；
- **互不阻塞**：求解在线程池执行（事件循环只做 I/O）。大批走单 worker 池，小批（< 1 MiB）走独立的
  4 worker 池。因此大批运行时，另一窗口仍可载入样例并完成小批清洗；
- **与窗口生命周期解耦**：名额在 worker 的 `finally` 中释放。关掉发起窗口（HTTP 断开）不会取消
  线程里的作业，它继续占用名额，**完成或失败后自动释放**；
- **沿用不变**：10 MB 上传上限（超出 413）、整链唯一解判定、导出列与 UTC 格式完全保持原样。

### 两个浏览器窗口并行演示

1. `docker compose up --build` 后，开两个浏览器窗口都访问 <http://localhost:8000>；
2. 窗口 A 点「📦 载入大批样例（≥1 MiB）」→「开始清洗」。顶部出现琥珀色名额占用条，
   作业约 12 秒后返回；
3. 名额占用期间在窗口 B：
   - 点「📦 载入内置夏令时样例」→「开始清洗」：**小批立即出结果**，不被 A 阻塞；
   - 也可在窗口 B 载入大批样例再清洗：**立即收到「稍后重试」503 提示**（不排队）；
   - 窗口 B 对大批文件点导出 UTC CSV 同样被 503 拒绝（清洗/导出共用名额）；
4. 窗口 A 的作业结束后名额条消失，窗口 B 再提交大批即可成功；
5. 验证关窗口语义：窗口 A 提交大批后立刻关闭该窗口，在窗口 B 观察名额条仍在，
   到点后自动消失（作业跑完释放名额）。

### 大批样例与生成脚本

- 现成样例：`samples/dst_sample_large.csv`（1.5 MiB，469 个机尾、每机尾 40 段，全部整链唯一解，可直接导出）；
- 重新生成（仅标准库）：

```bash
python3 -I scripts/gen_large_sample.py                 # 默认 1.5 MiB
python3 -I scripts/gen_large_sample.py --target-mib 4  # 自定义大小
```

## 输入 CSV 格式

UTF-8 编码，必需表头：

```csv
tail_number,seq,dep_local,dep_tz,arr_local,arr_tz,duration_min_minutes,duration_max_minutes
B-7001,1,2026-10-25 00:30,Europe/London,2026-10-25 02:15,Europe/Berlin,90,130
```

| 列 | 含义 |
| --- | --- |
| `tail_number` | 机尾号（同一机尾构成一条航段链） |
| `seq` | 航段顺序，整数，同一机尾内唯一，按升序链接 |
| `dep_local` / `arr_local` | 起飞/落地当地时间，`YYYY-MM-DD HH:MM`（也接受 `T` 分隔与秒） |
| `dep_tz` / `arr_tz` | 两端 IANA 时区名，如 `Europe/Berlin`、`America/New_York` |
| `duration_min_minutes` / `duration_max_minutes` | 飞行时长下/上限（分钟，**闭区间**） |

## 求解规则

1. 当地时间 → UTC 候选：
   - 普通时刻 → 1 个候选；
   - **重复时刻**（秋季拨回重叠，如柏林 2026-10-25 02:30）→ 保留 **2 个候选**参与整链求解；
   - **不存在的时刻**（春季拨快空洞，如柏林 2026-03-29 02:30）→ 该行报错，整架机尾禁止导出。
2. 每个航段的 (起飞, 落地) 候选组合必须满足：飞行时长 ∈ `[下限, 上限]`（闭区间）。
3. 按 `seq` 链接同一机尾：下一段起飞 **不得早于** 上一段落地（相等允许）。
4. 判定：
   - **整链唯一解** → 允许导出该机尾的 UTC CSV；
   - **无解** → 展示冲突航段（前向扫描定位第一个无法衔接的航段及其候选），禁止导出；
   - **多解** → 展示全部可行候选链（最多展示 20 条），禁止导出；
   - **行级错误**（不存在时刻 / 非法时区 / 格式错误 / seq 重复等）→ 禁止导出。

## 导出

- 网页上按机尾导出，或「导出全部可导出机尾」；
- 导出列：`tail_number,seq,dep_utc,arr_utc,duration_minutes`（UTC 为 ISO 8601 `Z` 格式）；
- 服务端在导出时会**重新求解并复核唯一性**，非唯一解返回 409，杜绝误导出。

## 内置样例（samples/dst_sample.csv）

| 机尾 | 场景 | 期望结果 |
| --- | --- | --- |
| B-7001 | 欧洲拨回日三连段，重复时刻由时长区间+链约束消歧 | 唯一解，可导出 |
| B-7002 | 柏林 2026-03-29 02:30 不存在（拨快空洞） | 数据错误 |
| B-7003 | 第二段起飞早于第一段落地 | 无解，展示冲突航段 |
| B-7004 | 起降均为重复时刻，区间宽松 | 多解（2 条候选链） |
| B-7005 | 美国拨回日（纽约/洛杉矶/丹佛） | 唯一解，可导出 |

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 网页入口 |
| `GET` | `/api/sample` | 下载内置样例 CSV |
| `GET` | `/api/sample/large` | 下载大批样例 CSV（≥ 1 MiB） |
| `GET` | `/api/slot` | 大批名额状态：`{busy, current, large_threshold_bytes}` |
| `POST` | `/api/solve` | 上传 CSV（multipart 字段 `file`），返回逐机尾求解结果 JSON；大批且名额被占时返回 503 |
| `POST` | `/api/export` | 字段 `file` + `tail`（`__all__` 表示全部可导出机尾），返回 UTC CSV；非唯一解 409，大批名额被占 503 |
| `GET` | `/docs` | FastAPI 自动文档 |

## 目录结构

```
app/
  main.py            # FastAPI 路由（求解下沉线程池，大批名额 503 拒入）
  jobs.py            # 大批名额 + 大/小批隔离线程池（纯标准库）
  solver.py          # 候选生成 + 整链求解 + 冲突定位（纯标准库）
  static/index.html  # 单页前端（名额状态条、503 提示、大批样例一键载入）
samples/dst_sample.csv        # 小批 DST 样例
samples/dst_sample_large.csv  # 大批样例（1.5 MiB，脚本生成）
scripts/gen_large_sample.py   # 大批样例生成脚本（仅标准库）
tests/test_solver.py          # 求解规则自检
tests/test_jobs.py            # 名额管理器自检
tests/test_api.py             # HTTP 级并发/拒入/断线存活自检
Dockerfile / docker-compose.yml / requirements.txt
```
