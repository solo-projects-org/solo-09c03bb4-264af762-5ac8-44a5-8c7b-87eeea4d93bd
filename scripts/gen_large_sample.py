#!/usr/bin/env python3
"""生成大批样例 CSV（默认 ≥ 1 MiB），用于演示大批名额。

特性
----
- 只用标准库；输出仍是系统既有的 8 列表头，求解规则、导出格式完全不变；
- 每个机尾生成一条航段链，航段时间选在各自时区的夏令时稳定期（7 月），
  当地时间均为唯一 UTC 候选，再按真实时区偏移计算可衔接的起飞/落地时刻，
  并给出包含该时长的闭区间，因此每条链都是**整链唯一解**、可导出；
- 目标大小按字节数逼近（默认 1.5 MiB），可通过命令行参数调整；
- 行尾统一 ``\\n``，UTF-8 无 BOM。

用法
----
    python3 -I scripts/gen_large_sample.py
    python3 -I scripts/gen_large_sample.py --target-mib 2 --out samples/dst_sample_large.csv
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HEADER = ("tail_number,seq,dep_local,dep_tz,arr_local,arr_tz,"
          "duration_min_minutes,duration_max_minutes\n")

# 北半球 7 月为夏令时稳定期，且全部为整点，不存在/重复时刻问题
TZ_PAIRS = [
    ("Asia/Shanghai", "Asia/Singapore"),
    ("Europe/London", "Europe/Paris"),
    ("America/New_York", "America/Chicago"),
    ("America/Los_Angeles", "America/Denver"),
    ("Europe/Berlin", "Europe/Helsinki"),
    ("Asia/Tokyo", "Asia/Seoul"),
    ("Australia/Sydney", "Australia/Perth"),  # 7 月为澳洲标准时间，无歧义
    ("America/Sao_Paulo", "America/Argentina/Buenos_Aires"),
]
SEGMENTS_PER_TAIL = 40
# 每段空中时长（分钟），取在区间正中保证唯一
FLIGHT_MINUTES = 150
# 地面过站时长（分钟），保证下一段起飞不早于上一段落地
TURNAROUND_MINUTES = 70


def _local_parts(dt_utc: datetime, tz_name: str) -> tuple[str, str]:
    local = dt_utc.astimezone(ZoneInfo(tz_name))
    return local.strftime("%Y-%m-%d %H:%M"), tz_name


def build_rows(tail_index: int) -> list[str]:
    """为一个机尾生成 SEGMENTS_PER_TAIL 段、整链唯一解的航段。"""
    dep_tz, arr_tz = TZ_PAIRS[tail_index % len(TZ_PAIRS)]
    # 不同机尾错开起点，全部落在 2026 年 7 月
    start_utc = datetime(2026, 7, 1, 0, 0, tzinfo=ZoneInfo("UTC")) \
        + timedelta(days=tail_index % 28, hours=(tail_index * 3) % 12)
    rows: list[str] = []
    cursor_utc = start_utc
    tail = f"L{tail_index + 1:05d}"
    for seq in range(1, SEGMENTS_PER_TAIL + 1):
        dep_local, dep_tz_name = _local_parts(cursor_utc, dep_tz)
        arr_utc = cursor_utc + timedelta(minutes=FLIGHT_MINUTES)
        arr_local, arr_tz_name = _local_parts(arr_utc, arr_tz)
        rows.append(
            f"{tail},{seq},{dep_local},{dep_tz_name},{arr_local},{arr_tz_name},"
            f"{FLIGHT_MINUTES - 20},{FLIGHT_MINUTES + 20}\n"
        )
        # 下一段：在落地机场过站后从落地时区起飞（仅用于推进游标，仍是合法时刻）
        cursor_utc = arr_utc + timedelta(minutes=TURNAROUND_MINUTES)
    return rows


def generate(target_bytes: int) -> str:
    parts = [HEADER]
    tail_index = 0
    while sum(len(p.encode("utf-8")) for p in parts) < target_bytes:
        parts.extend(build_rows(tail_index))
        tail_index += 1
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成大批航段 CSV 样例")
    parser.add_argument("--target-mib", type=float, default=1.5,
                        help="目标文件大小（MiB，默认 1.5，必须 > 1 才算大批）")
    default_out = Path(__file__).resolve().parents[1] / "samples" / "dst_sample_large.csv"
    parser.add_argument("--out", type=Path, default=default_out,
                        help="输出路径（默认 samples/dst_sample_large.csv）")
    args = parser.parse_args()

    target_bytes = int(args.target_mib * 1024 * 1024)
    if target_bytes <= 1024 * 1024:
        print("目标大小必须大于 1 MiB（大批阈值含 1 MiB 整）", file=sys.stderr)
        return 2

    text = generate(target_bytes)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    data_rows = text.count("\n") - 1
    size = args.out.stat().st_size
    print(f"已生成 {args.out}")
    print(f"  字节数: {size}（{size / 1048576:.2f} MiB，阈值 1.00 MiB）")
    print(f"  数据行: {data_rows}，机尾数: {data_rows // SEGMENTS_PER_TAIL}，"
          f"每机尾 {SEGMENTS_PER_TAIL} 段，全部为整链唯一解")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
