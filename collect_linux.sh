#!/bin/sh
# 作品 3 · Linux 采集脚本 —— collect_linux.sh
#
# 为什么是 **shell** 而不是 Python：
#   目标机是板子（RK3568）/ 最小化 VM —— 那里常常没有 Python，也没有 pip，
#   但一定有 busybox sh、/proc、/sys。运维工具要落到**目标机真实环境**，不是落到理想环境。
#
# 只用 POSIX sh + busybox 能用的命令（cat/grep/df/head/sort/find），**纯只读**：
#   不写文件、不改配置、不起服务。所有命令 2>/dev/null 容错 —— 权限不足/文件不存在都不许中断。
#
# 用法（在目标机上）：
#   sh collect_linux.sh > snap.txt            # 采集
#   sh collect_linux.sh /var/log > snap.txt   # 顺带列大文件
# 传回分析机（板子上常用 http，Day 29 的经验）：
#   python -m http.server 8001                # 在能 ssh/串口的机器上起服务
#   wget -qO snap.txt http://<pc>:8001/push   # 或直接把输出粘回来
# 分析（在开发机上）：
#   python day42_live.py --from-linux-dump snap.txt
#
# 输出格式：`###段名` 分段 + 原始命令输出。解析在 collect.parse_linux_dump() 里做 ——
# **采集与解析解耦**：脚本只负责"把事实原样搬回来"，判断全在分析侧（可复现、可回归）。

set -u

echo "###HOST"
uname -a 2>/dev/null
hostname 2>/dev/null
echo "###DATE"
date '+%Y-%m-%d %H:%M:%S' 2>/dev/null

echo "###UPTIME"
cat /proc/uptime 2>/dev/null

echo "###LOADAVG"
cat /proc/loadavg 2>/dev/null
echo "ncpu=$(grep -c '^processor' /proc/cpuinfo 2>/dev/null)"

echo "###MEMINFO"
grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|Dirty):' /proc/meminfo 2>/dev/null

echo "###DF"
df -P 2>/dev/null

echo "###DFI"
df -Pi 2>/dev/null

echo "###VMSTAT"
grep -E '^(oom_kill|pgmajfault|pgfault) ' /proc/vmstat 2>/dev/null

echo "###FILENR"
cat /proc/sys/fs/file-nr 2>/dev/null
cat /proc/sys/fs/file-max 2>/dev/null

echo "###TCP"
echo "total=$(wc -l < /proc/net/tcp 2>/dev/null)"
echo "time_wait=$(grep -c ' 0A ' /proc/net/tcp 2>/dev/null)"

echo "###PSI"
for r in cpu memory io; do
    echo "-- $r"
    cat "/proc/pressure/$r" 2>/dev/null
done

echo "###CGROUP"
cat /sys/fs/cgroup/memory.max 2>/dev/null
cat /sys/fs/cgroup/memory.current 2>/dev/null
cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null
cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null

echo "###THERMAL"
for z in /sys/class/thermal/thermal_zone*; do
    [ -r "$z/temp" ] || continue
    printf '%s type=%s temp=%s\n' "$(basename "$z")" "$(cat "$z/type" 2>/dev/null)" "$(cat "$z/temp" 2>/dev/null)"
done

echo "###CPUFREQ"
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq 2>/dev/null

echo "###CONTAINER"
if [ -f /.dockerenv ]; then echo yes; else echo no; fi
grep -m1 -o 'docker\|kubepods\|lxc' /proc/1/cgroup 2>/dev/null

echo "###TOP"
top -bn1 2>/dev/null | head -12

# 大文件：回答"是谁把盘吃满的"。-xdev 不跨文件系统，find 失败也不中断。
if [ $# -gt 0 ]; then
    echo "###BIGFILES"
    for d in "$@"; do
        echo "-- $d"
        find "$d" -xdev -type f -size +100M -printf '%s %p\n' 2>/dev/null | sort -rn | head -5
    done
fi

echo "###END"
