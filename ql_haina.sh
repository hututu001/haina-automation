#!/usr/bin/env bash
# 海纳百川统一青龙任务入口：bash ql_haina.sh <signin|draw|farm|status> [透传参数]
# 先 source 青龙的 env.sh / config.sh，保证环境变量与通知渠道生效，再调 haina.py。
set -u

CMD="${1:-}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$CMD" in
  signin|draw|farm|status) ;;
  *)
    echo "用法: bash ql_haina.sh <signin|draw|farm|status> [参数...]" >&2
    echo "  signin   签到 + 领取福利（每天 00:10）" >&2
    echo "  draw     抽奖 1 次（任务禁用，手动运行）" >&2
    echo "  farm     农场收菜/补种/兑换（每天 6/14/22 点；偷菜加 --steal）" >&2
    echo "  status   只读状态总览（签到 + 农场，排查用）" >&2
    exit 2
    ;;
esac

QL_ENV_FILE="${QL_ENV_FILE:-/app/user-packages/node/lib/node_modules/@whyour/qinglong/shell/preload/env.sh}"
QL_NOTIFY_CONFIG="${QL_NOTIFY_CONFIG:-/app/user-packages/node/lib/node_modules/@whyour/qinglong/data/config/config.sh}"
SCRIPT="${HAINA_SCRIPT:-/app/user-packages/node/lib/node_modules/@whyour/qinglong/data/scripts/haina.py}"

if [[ -f "$QL_ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$QL_ENV_FILE"
fi
if [[ -f "$QL_NOTIFY_CONFIG" ]]; then
  # 青龙通知渠道配置，供 sendNotify.js 读取。
  # shellcheck disable=SC1090
  source "$QL_NOTIFY_CONFIG"
fi

# -u 关闭输出缓冲，青龙日志页实时逐行显示
exec python3 -u "$SCRIPT" "$CMD" "$@"
