#!/usr/bin/env bash
set -eu

read -r _ user nice system idle iowait irq softirq steal _ < /proc/stat
sleep 0.5
read -r _ user2 nice2 system2 idle2 iowait2 irq2 softirq2 steal2 _ < /proc/stat

prev_idle=$((idle + iowait))
idle_now=$((idle2 + iowait2))
prev_non_idle=$((user + nice + system + irq + softirq + steal))
non_idle_now=$((user2 + nice2 + system2 + irq2 + softirq2 + steal2))

total_prev=$((prev_idle + prev_non_idle))
total_now=$((idle_now + non_idle_now))
total_delta=$((total_now - total_prev))
idle_delta=$((idle_now - prev_idle))

cpu_percent=$(awk -v total="$total_delta" -v idle="$idle_delta" 'BEGIN {
  if (total <= 0) {
    printf "0.0"
  } else {
    printf "%.1f", ((total - idle) * 100) / total
  }
}')

memory_percent=$(awk '
  /MemTotal:/ { total = $2 }
  /MemAvailable:/ { available = $2 }
  END {
    used = total - available
    if (total <= 0) {
      printf "0.0"
    } else {
      printf "%.1f", (used * 100) / total
    }
  }
' /proc/meminfo)

cpu_cores=$(nproc)
memory_total_gb=$(awk '
  /MemTotal:/ {
    printf "%.1f", $2 / 1024 / 1024
  }
' /proc/meminfo)

disk_percent=$(df -P / | awk 'NR == 2 { gsub(/%/, "", $5); printf "%.1f", $5 }')

printf '{"cpu":%s,"memory":%s,"disk":%s,"cpuCores":%s,"totalMemoryGb":%s}\n' \
  "$cpu_percent" \
  "$memory_percent" \
  "$disk_percent" \
  "$cpu_cores" \
  "$memory_total_gb"
