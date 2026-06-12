#!/usr/bin/env bash
# 释放 MiniOrangeServer 占用的端口（默认 10104）
set -euo pipefail

PORT="${1:-10104}"

list_pids() {
  lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null | sort -u | tr '\n' ' ' | xargs
}

PIDS="$(list_pids)"
if [ -z "$PIDS" ]; then
  echo "端口 ${PORT} 未被占用"
  exit 0
fi

echo "占用端口 ${PORT} 的进程:"
for pid in $PIDS; do
  ps -p "$pid" -o pid=,command= 2>/dev/null || true
done

kill $PIDS 2>/dev/null || true
sleep 0.8

PIDS="$(list_pids)"
if [ -n "$PIDS" ]; then
  kill -9 $PIDS 2>/dev/null || true
  sleep 0.3
fi

# uvicorn --reload 的父进程/watch 有时不占 LISTEN 但仍相关
pkill -f "[u]vicorn.*main:app" 2>/dev/null || true
pkill -f "[p]ython.*MiniOrangeServer.*main.py" 2>/dev/null || true

if [ -n "$(list_pids)" ]; then
  echo "未能释放端口 ${PORT}。可尝试: lsof -i :${PORT}"
  exit 1
fi

echo "端口 ${PORT} 已释放"
