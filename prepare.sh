#!/bin/sh
set -eu
cd "$(dirname "$0")"
umask 077
mkdir -p data
chmod 700 data
if [ ! -e .env ]; then
  cp .env.example .env
fi
printf '%s\n' '目录已准备。请在 .env 中确认 PUID/PGID 与 data 目录所有者一致，然后执行 docker compose up -d --build。' '首次启动后，从飞牛文件管理器打开 data/first-login.txt 获取临时管理员密码。'
