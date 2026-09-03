#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海纳百川控制台 · 本地网页版（不依赖青龙）

启动：python haina_server.py （或双击 启动控制台.bat），浏览器自动打开
      http://127.0.0.1:8787

功能：
  - 网页按钮手动触发 签到 / 抽奖 / 农场(可含偷菜) / 状态总览，实时查看输出与历史
  - 内置定时（可选，默认开）：签到每天 00:10、农场每天 6/14/22 点；
    电脑关机错过的时点，开机后 15 秒内自动补跑当天（签到当天没跑过就补，农场补最近错过的时段）
  - 完全复用 haina.py 的逻辑与会话缓存；配置存 haina_web.json，会话缓存存 data/
  - 前端页面在 web/ 目录（index.html + app.js + style.css）；
    目录缺失时自动回退到本文件内置的旧版页面（PAGE），保证升级不炸

说明：
  - 默认只绑定 127.0.0.1（仅本机访问）。想用手机/其他电脑访问：
      python haina_server.py --host 0.0.0.0
    首次启动会生成访问令牌（打印在控制台），用 http://<本机IP>:8787/?token=xxx 打开。
  - 账号密码保存在本地 haina_web.json（明文），请勿把该目录分享给他人。
  - 定时依赖电脑开机在线；7x24 稳定定时仍建议用青龙部署（haina.py 两边通用）。
"""

import argparse
import importlib
import io
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "haina_web.json")
DATA_DIR = os.path.join(HERE, "data")
LOG_DIR = os.path.join(HERE, "logs")
WEB_DIR = os.path.join(HERE, "web")
STATE_FILE = os.path.join(DATA_DIR, "web_state.json")
CST = timezone(timedelta(hours=8))

# 静态前端文件路由：文件名白名单，杜绝路径穿越
STATIC_ROUTES = {
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "notify": False,                 # 任务完成后走青龙 sendNotify 推送（本机一般没有，默认关）
    "schedule_enabled": True,        # 内置定时
    "signin_time": "00:10",
    "farm_times": ["06:00", "14:00", "22:00"],
    "draw_times": [],                # 抽奖定时（默认空 = 仅手动，防止误抽光次数）
    "farm_crop": "",
    "draw_all": False,               # 抽奖抽光模式：开=抽光全部次数，关=每次只抽1
    "signin_draw": False,            # 签到领完福利后自动抽奖（次数遵循 draw_all）
    "base_url": "",
    "farm_url": "",
    "www_session": "",               # 空 = data/haina_session.json
    "farm_session": "",              # 空 = data/haina_farm_session.json
    "host": "",                      # 监听地址，空 = 127.0.0.1；服务器部署填 0.0.0.0
    "port": 8787,
    "token": "",                     # 非本机访问令牌，首次启动自动生成
    "notify_mode": "off",            # off / webhook / qinglong
    "webhook_url": "",               # Bark/Server酱/TG/钉钉/飞书/企微/通用 JSON
    "webhook_when": "fail",          # fail=仅失败推送 / all=每次都推
    "trust_proxy": False,            # nginx 反代时信任 X-Real-IP 判定本机
}

# 单任务看门狗超时（秒）：任务卡死时强制收尾并解锁控制台，避免永远"正在运行"
RUN_TIMEOUT = int(os.environ.get("HAINA_WEB_RUN_TIMEOUT", "900"))

TASKS = {
    "signin":     ("签到",      ["signin"]),
    "draw":       ("抽奖×1",    ["draw"]),
    "farm":       ("农场",      ["farm"]),
    "farm_steal": ("农场+偷菜", ["farm", "--steal"]),
    "status":     ("状态总览",  ["status"]),
}

core = None          # haina 模块；配置变更后 reload
core_dirty = False
RUN_LOCK = threading.Lock()
FINISH_LOCK = threading.Lock()
CURRENT = None
RUNS = []            # 本次会话的运行（含 buf 引用），新的在后
PERSISTED = []       # 启动时从 logs/ 回载的历史运行（只读，跨重启保留）
LOGIN_CALLS = {}     # ip -> [count, window_start]，登录接口防爆破（10 次/10 分钟）


# ── 配置 / 状态 ───────────────────────────────────────────────────────────
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    if not cfg.get("token"):
        cfg["token"] = secrets.token_hex(16)
        save_config(cfg)
    return cfg


def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def apply_env():
    """把配置映射成 haina.py 读取的环境变量（每次任务运行前调用）。"""
    cfg = load_config()
    e = os.environ
    e["HAINA_USERNAME"] = cfg.get("username", "")
    e["HAINA_PASSWORD"] = cfg.get("password", "")
    e["HAINA_NOTIFY"] = "1" if cfg.get("notify") else "0"
    e["HAINA_SESSION_FILE"] = cfg.get("www_session") or os.path.join(DATA_DIR, "haina_session.json")
    e["HAINA_FARM_SESSION_FILE"] = cfg.get("farm_session") or os.path.join(DATA_DIR, "haina_farm_session.json")
    e["HAINA_SUMMARY_FILE"] = os.path.join(DATA_DIR, "last_summary.json")
    e["HAINA_FARM_CROP"] = cfg.get("farm_crop", "")
    e["HAINA_DRAW_ALL"] = "1" if cfg.get("draw_all") else "0"
    e["HAINA_SIGNIN_DRAW"] = "1" if cfg.get("signin_draw") else "0"
    for key, env in (("base_url", "HAINA_BASE_URL"), ("farm_url", "HAINA_FARM_URL")):
        if cfg.get(key):
            e[env] = cfg[key]
        else:
            e.pop(env, None)
    return cfg


def sync_core():
    """配置有改动时重载 haina 模块（须在 stdout 被重定向之前、且无任务运行时）。"""
    global core, core_dirty
    apply_env()
    if core_dirty and core is not None:
        core = importlib.reload(core)
        core_dirty = False


# ── 通用 Webhook 推送 ─────────────────────────────────────────────────────
def _send_webhook(url, title, content):
    """按 URL 自动识别 Bark/Server酱/TG/钉钉/飞书/企微，其余发通用 JSON。"""
    if not url:
        return False, "未配置 Webhook URL"
    content = (content or "")[:3000]
    u = url.lower()
    try:
        if "bark." in u:
            data = json.dumps({"title": title, "body": content}); ctype = "json"
        elif "sctapi.ftqq.com" in u:
            data = urllib.parse.urlencode({"title": title, "desp": content}); ctype = "form"
        elif "api.telegram.org" in u:
            data = json.dumps({"text": f"{title}\n\n{content}"}); ctype = "json"
        elif "oapi.dingtalk.com" in u:
            data = json.dumps({"msgtype": "text", "text": {"content": f"{title}\n{content}"}}); ctype = "json"
        elif "open.feishu.cn" in u:
            data = json.dumps({"msg_type": "text", "content": {"text": f"{title}\n{content}"}}); ctype = "json"
        elif "qyapi.weixin.qq.com" in u:
            data = json.dumps({"msgtype": "text", "text": {"content": f"{title}\n{content}"}}); ctype = "json"
        else:
            data = json.dumps({"title": title, "content": content}); ctype = "json"
        headers = {"User-Agent": "haina-web/1.0",
                   "Content-Type": "application/json" if ctype == "json"
                   else "application/x-www-form-urlencoded"}
        req = urllib.request.Request(url, data=data.encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read(400).decode("utf-8", "replace")
            return resp.status == 200, f"HTTP {resp.status} {body[:150]}"
    except Exception as exc:
        return False, str(exc)[:200]


# ── 启动时从 logs/ 回载历史运行 ───────────────────────────────────────────
def _load_persisted_runs():
    """解析 logs/*.log 的文件名与首行头部，重建跨重启的运行历史。"""
    out = []
    try:
        names = sorted((f for f in os.listdir(LOG_DIR) if f.endswith(".log")),
                       reverse=True)
    except OSError:
        return out
    for name in names[:50]:
        m = re.match(r"(\d{8})_(\d{6})_(\w+)\.log", name)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        try:
            with open(os.path.join(LOG_DIR, name), "r", encoding="utf-8",
                      errors="replace") as fh:
                content = fh.read(300_000)
        except OSError:
            continue
        head = content.split("\n", 1)[0]
        hm = re.match(r"任务:\s*(\S+)\s+触发:\s*(\S+)\s+exit:\s*(-?\d+)", head)
        task = m.group(3)
        out.append({
            "id": int(dt.timestamp() * 1000),
            "task": task,
            "label": TASKS.get(task, (task,))[0],
            "trigger": hm.group(2) if hm else "手动",
            "start": dt.strftime("%m-%d %H:%M:%S"),
            "end": None,
            "code": int(hm.group(3)) if hm else None,
            "output": content,
            "buf": None,
        })
    return out


# ── 任务执行 ──────────────────────────────────────────────────────────────
class RunBuffer(io.TextIOBase):
    """线程安全地收集一次任务的输出。"""

    def __init__(self):
        self._parts = []
        self._lock = threading.Lock()

    def write(self, text):
        with self._lock:
            self._parts.append(text)
        return len(text)

    def flush(self):
        pass

    def getvalue(self):
        with self._lock:
            return "".join(self._parts)


def dispatch(task):
    ns = SimpleNamespace
    crop = os.environ.get("HAINA_FARM_CROP", "")
    draw_all = os.environ.get("HAINA_DRAW_ALL", "") == "1"
    signin_draw = os.environ.get("HAINA_SIGNIN_DRAW", "") == "1"
    if task == "signin":
        return core.cmd_signin(ns(draw=signin_draw, draw_all=draw_all))
    if task == "draw":
        return core.cmd_draw(ns(status_only=False, all=draw_all))
    if task == "farm":
        return core.cmd_farm(ns(status_only=False, steal=False,
                                no_plant=False, no_redeem=False, crop=crop))
    if task == "farm_steal":
        return core.cmd_farm(ns(status_only=False, steal=True,
                                no_plant=False, no_redeem=False, crop=crop))
    return core.cmd_status(ns())


def normalize_time(s):
    m = re.match(r"^(\d{1,2})[:：](\d{1,2})$", str(s).strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def parse_farm_times(s):
    parts = re.split(r"[,，;；\s]+", str(s).strip())
    return [t for t in (normalize_time(p) for p in parts if p) if t]


def _mark_for(task):
    """手动/定时运行后写入的补跑标记，避免定时器重复跑同一时点。"""
    today = datetime.now(CST).strftime("%Y-%m-%d")
    if task == "signin":
        return "last_signin", today
    if task in ("farm", "farm_steal"):
        cfg = load_config()
        now_hm = datetime.now(CST).strftime("%H:%M")
        passed = [t for t in sorted(cfg.get("farm_times") or []) if now_hm >= t]
        if passed:
            return "last_farm", f"{today} {passed[-1]}"
    if task == "draw":
        cfg = load_config()
        now_hm = datetime.now(CST).strftime("%H:%M")
        passed = [t for t in sorted(cfg.get("draw_times") or []) if now_hm >= t]
        if passed:
            return "last_draw", f"{today} {passed[-1]}"
    return None, None


def start_run(task, trigger="手动"):
    global CURRENT
    if task not in TASKS:
        return None, "未知任务"
    if not RUN_LOCK.acquire(blocking=False):
        return None, "已有任务在运行，请稍候"
    mark_key, mark_val = _mark_for(task)
    # 标签跟随抽奖设置，历史记录里一眼看出当时用的哪种模式
    label = TASKS[task][0]
    cfg = load_config()
    if task == "signin" and cfg.get("signin_draw"):
        label = "签到+抽奖"
    elif task == "draw" and cfg.get("draw_all"):
        label = "抽光抽奖"
    run = {
        "id": int(time.time() * 1000),
        "task": task,
        "label": label,
        "trigger": trigger,
        "start": datetime.now(CST).strftime("%m-%d %H:%M:%S"),
        "t0": time.time(),
        "end": None,
        "code": None,
        "buf": RunBuffer(),
        "mark": (mark_key, mark_val),
        "finished": False,
        "done": threading.Event(),
    }
    CURRENT = run
    threading.Thread(target=_worker, args=(run,), daemon=True).start()
    threading.Thread(target=_watchdog, args=(run,), daemon=True).start()
    return run, None


def _worker(run):
    """任务线程。任何异常（含重定向前）都不能泄漏 RUN_LOCK。"""
    orig_out, orig_err = sys.stdout, sys.stderr
    # 首行直接写入缓冲区：网页日志区立刻可见"已启动"，不依赖控制台输出
    run["buf"].write(f"[控制台] {run['start']} 开始 {run['label']}（{run['trigger']}）\n")
    try:
        try:
            sync_core()
            sys.stdout = sys.stderr = run["buf"]
            try:
                run["code"] = dispatch(run["task"]) or 0
            except Exception as exc:
                print(f"[FAIL] 任务异常: {exc}")
                traceback.print_exc(file=sys.stderr)
                run["code"] = 1
        finally:
            sys.stdout, sys.stderr = orig_out, orig_err
    except Exception as exc:
        run["buf"].write(f"[FAIL] 任务线程异常: {exc}\n")
        try:
            traceback.print_exc(file=run["buf"])
        except Exception:
            pass
        if run["code"] is None:
            run["code"] = 1
    finally:
        run["done"].set()
        _finish_run(run)


def _watchdog(run):
    """看门狗：任务超时未结束则强制收尾解锁，防止控制台永远"正在运行"。"""
    if run["done"].wait(RUN_TIMEOUT):
        return
    run["buf"].write(
        f"\n[FAIL] 任务超过 {RUN_TIMEOUT // 60} 分钟未结束，已强制收尾并解锁控制台\n"
        f"[i]   旧任务线程可能仍在后台；若反复出现此提示请反馈\n")
    if run["code"] is None:
        run["code"] = 1
    _finish_run(run)


def _finish_run(run):
    """任务收尾（幂等）：补跑标记 / 推送 / 落盘 / 历史 / 解锁。"""
    global CURRENT
    with FINISH_LOCK:
        if run.get("finished"):
            return
        run["finished"] = True
        run["end"] = datetime.now(CST).strftime("%m-%d %H:%M:%S")
        # 更新补跑标记（无论成败，避免失败后每隔15秒重试轰炸站点）
        if run["mark"] and run["mark"][0]:
            try:
                st = load_state()
                st[run["mark"][0]] = run["mark"][1]
                save_state(st)
            except Exception:
                pass
        # 可选推送（webhook / 青龙 sendNotify）
        try:
            cfg = load_config()
            mode = cfg.get("notify_mode") or ("qinglong" if cfg.get("notify") else "off")
            if mode == "webhook":
                when = cfg.get("webhook_when") or "fail"
                if when == "all" or run["code"] != 0:
                    status = "成功" if run["code"] == 0 else "失败"
                    ok, detail = _send_webhook(
                        cfg.get("webhook_url", ""),
                        f"海纳百川{run['label']} · {status}",
                        run["buf"].getvalue().strip() or "任务无输出")
                    print(f"[控制台] Webhook 推送{'成功' if ok else '失败'}: {detail}")
            elif mode == "qinglong" and core is not None:
                status = "成功" if run["code"] == 0 else "失败"
                core.send_ql_notify(f"海纳百川{run['label']} · {status}",
                                    run["buf"].getvalue().strip() or "任务无输出")
        except Exception:
            pass
        try:
            _persist_log(run)
        except Exception:
            pass
        RUNS.append(run)
        del RUNS[:-50]
        if CURRENT is run:
            CURRENT = None
        try:
            RUN_LOCK.release()
        except RuntimeError:
            pass  # 锁已被看门狗路径释放（幂等保护）


def _persist_log(run):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        name = datetime.now(CST).strftime("%Y%m%d_%H%M%S") + f"_{run['task']}.log"
        with open(os.path.join(LOG_DIR, name), "w", encoding="utf-8") as fh:
            fh.write(f"任务: {run['label']}  触发: {run['trigger']}  "
                     f"exit: {run['code']}\n\n{run['buf'].getvalue()}\n")
    except OSError:
        pass


# ── 内置定时 ──────────────────────────────────────────────────────────────
def sched_loop():
    while True:
        time.sleep(15)
        try:
            sched_tick()
        except Exception:
            traceback.print_exc()


def sched_tick():
    cfg = load_config()
    if not cfg.get("schedule_enabled"):
        return
    # 从未配置过账号时定时必失败，跳过以免历史里堆满无意义的失败记录
    if not cfg.get("username") and not cfg.get("password"):
        return
    if RUN_LOCK.locked():          # 有任务在跑，本轮跳过
        return
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")
    now_hm = now.strftime("%H:%M")
    st = load_state()

    signin_t = normalize_time(cfg.get("signin_time") or "00:10")
    if signin_t and now_hm >= signin_t and st.get("last_signin") != today:
        start_run("signin", trigger="定时")
        return

    passed = [t for t in sorted(cfg.get("farm_times") or []) if now_hm >= t]
    if passed and st.get("last_farm") != f"{today} {passed[-1]}":
        start_run("farm", trigger="定时")
        return

    draw_passed = [t for t in sorted(cfg.get("draw_times") or []) if now_hm >= t]
    if draw_passed and st.get("last_draw") != f"{today} {draw_passed[-1]}":
        start_run("draw", trigger="定时")


def next_schedule_view(cfg):
    """给网页展示的接下来几个定时点。"""
    if not cfg.get("schedule_enabled"):
        return []
    now = datetime.now(CST)
    entries = [("签到", normalize_time(cfg.get("signin_time") or "00:10"))]
    for t in cfg.get("farm_times") or []:
        entries.append(("农场", normalize_time(t)))
    for t in cfg.get("draw_times") or []:
        entries.append(("抽奖", normalize_time(t)))
    out = []
    for label, tt in entries:
        if not tt:
            continue
        h, m = map(int, tt.split(":"))
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        out.append((cand, label))
    out.sort()
    return [f"{c.strftime('%m-%d %H:%M')} {l}" for c, l in out[:6]]


# ── HTTP ──────────────────────────────────────────────────────────────────
PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>海纳百川控制台</title>
<style>
:root{--bg:#0f1420;--card:#171e2e;--line:#26304a;--tx:#e8ecf6;--mut:#8b96b0;
--acc:#5b9dff;--ok:#3ecf8e;--bad:#ff6b6b}
*{box-sizing:border-box}
body{margin:0 auto;padding:20px;max-width:920px;font:15px/1.6 system-ui,
"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--tx)}
h1{font-size:20px;margin:0}
.sub{color:var(--mut);font-size:13px;margin:2px 0 16px}
.actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
gap:10px;margin-bottom:12px}
button{padding:12px 8px;border:1px solid var(--line);background:var(--card);
color:var(--tx);border-radius:10px;font-size:15px;cursor:pointer}
button:hover:not(:disabled){border-color:var(--acc)}
button:disabled{opacity:.4;cursor:not-allowed}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px 16px;margin-bottom:14px}
.card h2{font-size:14px;margin:0 0 8px;color:var(--mut);font-weight:600}
#log{background:#0b0f19;border:1px solid var(--line);border-radius:12px;
padding:12px;max-height:360px;overflow:auto;white-space:pre-wrap;
word-break:break-all;font:12.5px/1.55 Consolas,monospace;min-height:110px}
#runmeta{font-size:13px;margin:8px 2px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.mut{color:var(--mut)}
.banner{background:#3a2b12;border:1px solid #7a5a1e;color:#ffd479;
border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:14px}
.banner.hidden,.hidden{display:none}
ul{list-style:none;margin:0;padding:0}
li{padding:6px 2px;border-bottom:1px dashed var(--line);cursor:pointer;
font-size:14px;display:flex;gap:10px;justify-content:space-between}
li:hover{color:var(--acc)}
label{display:block;margin:10px 0 3px;font-size:13px;color:var(--mut)}
input[type=text],input[type=password],select{width:100%;padding:8px 10px;border-radius:8px;
border:1px solid var(--line);background:#0b0f19;color:var(--tx);font-size:14px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px;margin-bottom:14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px}
.stat .k{font-size:12px;color:var(--mut)}
.stat .v{font-size:20px;font-weight:600;margin-top:2px}
.stat .s{font-size:11px;color:var(--mut);margin-top:2px}
#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(20px);
background:#222b40;border:1px solid var(--line);color:var(--tx);padding:10px 18px;
border-radius:10px;opacity:0;pointer-events:none;transition:all .25s;z-index:9;font-size:14px;
max-width:86vw}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.bad{border-color:#a33;color:#ffb3b3}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--mut);
border-top-color:var(--acc);border-radius:50%;animation:sp 1s linear infinite;
vertical-align:-2px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
.loghead{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.loghead h2{margin:0}
.mini{padding:5px 12px;font-size:12.5px;border-radius:8px}
.utable{width:100%;border-collapse:collapse;font-size:13px}
.utable th{color:var(--mut);font-weight:500;text-align:left;padding:3px 6px;
border-bottom:1px solid var(--line);white-space:nowrap}
.utable td{padding:4px 6px;border-bottom:1px dashed var(--line);white-space:nowrap}
.utable tr:last-child td{border-bottom:none}
.utable .num{text-align:right;font-variant-numeric:tabular-nums}
@media (max-width:640px){.utable th:nth-child(6),.utable td:nth-child(6){display:none}}
@media (max-width:640px){
  body{padding:12px}
  .actions{grid-template-columns:repeat(2,1fr)}
  .stat .v{font-size:17px}
  h1{font-size:18px}
}
footer{color:var(--mut);font-size:12px;margin:18px 0 8px;text-align:center}
.row{display:flex;gap:10px}.row>div{flex:1}
.chk{display:flex;align-items:center;gap:8px;margin:10px 0;font-size:14px;color:var(--tx)}
.chk input{width:auto}
.save{margin-top:12px;width:100%}
#schedinfo{font-size:13.5px;color:var(--mut)}
.tip{font-size:12px;color:var(--mut);margin-top:8px}
footer{color:var(--mut);font-size:12px;margin:18px 0 8px;text-align:center}
</style></head><body>
<h1>海纳百川控制台</h1>
<div class="sub" id="clock"></div>

<div class="stats" id="stats"></div>

<div id="loginCard" class="card hidden">
  <h2>登录海纳百川</h2>
  <div class="row">
    <div><label>登录邮箱</label><input type="text" id="l_user" autocomplete="off"></div>
    <div><label>密码</label><input type="password" id="l_pass" placeholder="请输入密码" autocomplete="current-password"></div>
  </div>
  <button class="save" id="btnlogin">登录并保存凭证</button>
  <div class="tip">凭证自动保存在本机 haina_web.json；登录成功后会话缓存存入 data/ 并自动续期，之后基本不再用到密码。换号 / 改密可展开底部「设置」。</div>
</div>

<div class="actions">
  <button data-task="signin" id="btnsignin">签到</button>
  <button data-task="draw" id="btndraw">抽奖 ×1</button>
  <button data-task="farm">农场收补兑</button>
  <button data-task="farm_steal" id="btnsteal">农场 + 偷菜</button>
  <button data-task="status">状态总览</button>
</div>

<div class="card">
  <div class="loghead">
    <h2 style="font-size:14px;color:var(--mut);font-weight:600">任务输出</h2>
    <button class="mini" onclick="copyLog()">复制</button>
  </div>
  <div id="runmeta" class="mut">空闲中</div>
  <pre id="log">点上方按钮开始；输出会实时显示在这里（运行中每行日志逐条滚动）。</pre>
</div>

<div class="card">
  <h2>定时（内置，替代青龙 cron）</h2>
  <div id="schedinfo"></div>
  <button style="margin-top:10px" onclick="editSched()">✎ 修改定时规则</button>
</div>

<details class="card">
  <summary style="cursor:pointer;font-size:15px">📖 每个按钮做什么（点开看说明）</summary>
  <div style="font-size:14px;line-height:1.9;margin-top:8px">
    <b>签到</b>：查今日 token 消耗（≥100 万才签）→ 钱包签到 → 自动领取所有可领福利（含签到抽奖机会）。已领过的不会重复领。开了「签到后自动抽奖」会接着把奖抽了（次数遵循抽光设置）。<br>
    <b>抽奖</b>：先显示余额 / 可抽次数再抽。默认只抽 1 次；设置里开了「抽光模式」就一次抽完全部次数（按钮文字会变成「抽奖 · 抽光」）。次数不足时只报状态不抽。<br>
    <b>农场收补兑</b>：状态 → 一键收菜（只收成熟的）→ 给空闲地补种（<b>种子库存优先，不够会自动买</b>，买前校验主站余额，余额不足自动跳过；默认种当前能种里时薪最高的）→ 把待兑换额度兑成主站余额（受本周 40% 消耗上限约束）。<br>
    <b>农场+偷菜</b>：上面全部 + 偷菜（3 体力/次，自动扫描全服可偷目标；成熟作物竞争激烈，常常无可偷）。<br>
    <b>状态总览</b>：只读。抽奖面板 + 农场一次看全，不做任何写操作，随时可点。<br>
    <span class="mut">运行中日志实时滚动；每个任务都有 15 分钟看门狗，超时自动解锁不会卡死。</span>
  </div>
</details>

<div class="card">
  <div class="loghead">
    <h2>最近 API 调用</h2>
    <button class="mini" onclick="loadUsage()">刷新</button>
  </div>
  <div id="usagebody" class="mut" style="font-size:13px">加载中…</div>
</div>

<div class="card">
  <h2>运行历史（点击查看输出，重启不丢失）</h2>
  <ul id="history"></ul>
</div>

<div class="card">
  <h2>日志文件（logs/ 目录全部历史）</h2>
  <ul id="logfiles"><li style="cursor:default" class="mut">加载中…</li></ul>
</div>

<details class="card" id="settingsCard">
  <summary style="cursor:pointer;font-size:15px">⚙ 设置（账号 / 定时 / 推送）</summary>
  <form id="settings" onsubmit="return false">
    <label>登录邮箱</label><input type="text" id="s_user" autocomplete="off">
    <label>密码 <span class="mut">（留空 = 不修改；有会话缓存时可以不存密码）</span></label>
    <input type="password" id="s_pass" placeholder="" autocomplete="new-password">
    <div class="row">
      <div><label>签到时间 <span class="mut">HH:MM</span></label><input type="text" id="s_signin"></div>
      <div><label>农场时间 <span class="mut">逗号分隔多个</span></label><input type="text" id="s_farm"></div>
    </div>
    <div class="row">
      <div><label>抽奖时间 <span class="mut">逗号分隔，留空=仅手动</span></label><input type="text" id="s_draw"></div>
      <div><label>补种作物 cropId <span class="mut">留空=自动选时薪最高</span></label><input type="text" id="s_crop" placeholder="如 crop_corn"></div>
    </div>
    <div class="chk"><input type="checkbox" id="s_sched"><span>启用内置定时（电脑关机错过的时点，开机后自动补跑当天）</span></div>
    <div class="chk"><input type="checkbox" id="s_sdraw"><span>签到后自动抽奖：签到领完福利后接着把奖抽了（次数遵循下一项）</span></div>
    <div class="chk"><input type="checkbox" id="s_drawall"><span>抽奖抽光模式：把可抽次数一次抽完（关 = 每次只抽 1 次）</span></div>
    <label>推送方式</label>
    <select id="s_nmode">
      <option value="off">关闭（不推送）</option>
      <option value="webhook">Webhook（Bark / Server酱 / TG / 钉钉 / 飞书 / 企微）</option>
      <option value="qinglong">青龙 sendNotify（需青龙环境）</option>
    </select>
    <div class="row">
      <div style="flex:2"><label>Webhook URL <span class="mut">（按格式自动识别渠道）</span></label>
        <input type="text" id="s_whurl" placeholder="如 https://api.day.app/xxxx 或 TG 完整链接含 chat_id"></div>
      <div><label>推送时机</label>
        <select id="s_nwhen"><option value="fail">仅失败</option><option value="all">每次任务</option></select></div>
    </div>
    <button type="button" class="mini" style="margin:2px 0 4px" onclick="testNotify()">发送测试推送（先保存设置）</button>
    <details style="margin-top:6px"><summary class="mut" style="cursor:pointer;font-size:13px">高级：站点 / 会话缓存 / 监听地址</summary>
      <div class="row">
        <div><label>主站地址</label><input type="text" id="s_base"></div>
        <div><label>农场地址</label><input type="text" id="s_farmurl"></div>
      </div>
      <label>www 会话缓存路径</label><input type="text" id="s_wwwsess">
      <label>农场会话缓存路径</label><input type="text" id="s_farmsess">
      <label>监听地址 <span class="mut">（空 = 仅本机 127.0.0.1；服务器部署填 0.0.0.0，改后需重启生效）</span></label>
      <input type="text" id="s_host" placeholder="127.0.0.1">
      <div class="chk"><input type="checkbox" id="s_tproxy"><span>信任反向代理 IP（nginx HTTPS 反代到本服务时勾选，令牌校验才不会被绕过）</span></div>
    </details>
    <button class="save" id="btnsave">保存设置</button>
    <div class="tip">保存在本地 haina_web.json（明文）。修改定时/账号后立即生效，无需重启。</div>
  </form>
</details>
<footer>会话缓存自动复用 · 密码仅兜底登录 · 写操作带幂等键可重复执行</footer>
<div id="toast"></div>

<script>
const $=id=>document.getElementById(id);
let state=null, followId=null, lastBusy=false, fileView=false;
async function api(p,o){const r=await fetch(p,o);const t=await r.text();
  let d;try{d=JSON.parse(t)}catch(e){throw new Error(t)}if(!r.ok)throw new Error(d.error||t);return d}

function esc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;")}
function fmtCode(c){if(c===null||c===undefined)return '<span class="mut">运行中…</span>';
  return c===0?'<span class="ok">成功</span>':`<span class="bad">失败(${c})</span>`}
function todayStr(){return new Date().toLocaleDateString("sv-SE")}
function markToday(v){ // 返回 "今日已跑" 或 null
  if(!v)return null;
  const t=todayStr();
  if(v===t)return "今日已跑 ✓";
  if(v.startsWith(t+" "))return `今日 ${v.split(" ")[1]} 已跑 ✓`;
  return null}

let toastTimer=null;
function showToast(msg,bad){const t=$("toast");t.textContent=msg;t.className=bad?"show bad":"show";
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.className="",2800)}

async function copyLog(){try{await navigator.clipboard.writeText($("log").textContent);
  showToast("日志已复制")}catch(e){showToast("复制失败："+e.message,true)}}

async function loadLogs(){try{const d=await api("/api/logs");
  $("logfiles").innerHTML=d.files.map(f=>
    `<li onclick="viewLogFile('${f.name}')"><span>${esc(f.name)}</span>
     <span class="mut">${esc(f.size)} · ${esc(f.mtime)}</span></li>`).join("")
    ||'<li style="cursor:default" class="mut">暂无日志文件</li>'}catch(e){}}

async function viewLogFile(name){followId=null;fileView=true;
  try{const d=await api("/api/logfile?name="+encodeURIComponent(name));
    $("log").textContent=d.content;$("log").scrollTop=1e9;
    $("runmeta").innerHTML=`<span class="mut">文件 ${esc(d.name)} · ${esc(d.size)}（只读）</span>`}
  catch(e){showToast(e.message,true)}}

async function loadUsage(){const el=$("usagebody");
  try{const d=await api("/api/usage");
    if(d.needs_refresh){
      el.innerHTML=`<span class="mut">会话已过期，</span><button class="mini" onclick="run('status')">跑一次状态总览刷新会话</button><span class="mut">成功后再看。</span>`;return}
    if(d.error){el.textContent="加载失败："+d.error;return}
    if(!d.items.length){el.textContent="暂无调用记录";return}
    const rows=d.items.map(it=>{
      const dt=new Date(it.ts*1000),today=new Date().toDateString()===dt.toDateString();
      const hm=dt.toTimeString().slice(0,8);
      const time=today?hm:(dt.getMonth()+1)+"-"+dt.getDate()+" "+hm;
      return `<tr><td>${time}</td><td>${esc(it.model)}</td>
        <td class="num">${it.prompt.toLocaleString()}</td>
        <td class="num">${it.completion.toLocaleString()}</td>
        <td class="num">${it.spend.toFixed(3)}</td><td>${it.use_time}s</td></tr>`}).join("");
    el.innerHTML=`<table class="utable"><tr><th>时间</th><th>模型</th><th>输入</th><th>输出</th><th>消耗额度</th><th>耗时</th></tr>${rows}</table>`}
  catch(e){el.textContent="加载失败："+String(e.message||e)}}

async function testNotify(){
  try{const d=await api("/api/notify_test",{method:"POST",
    headers:{"Content-Type":"application/json"},body:"{}"});
    showToast("测试推送已发出："+d.detail)}
  catch(e){showToast(e.message,true)}}

function statCard(k,v,sub){return `<div class="stat"><div class="k">${k}</div>
  <div class="v">${v}</div>${sub?`<div class="s">${sub}</div>`:""}</div>`}

function renderStats(){
  const sm=state.summary||{},w=sm.www||{},f=sm.farm||{};
  const fmtT=t=>t?new Date(t*1000).toLocaleTimeString("zh-CN",{hour12:false,hour:"2-digit",minute:"2-digit"}):"";
  const bal=f.balance??w.balance;
  $("stats").innerHTML=[
    statCard("主站余额",bal??"—",`更新 ${fmtT(f.updated_at||w.updated_at)||"—"}`),
    statCard("待兑换额度",f.pending??"—",`周消费 Lv.${f.weekly_level??"?"}`),
    statCard("可抽次数",w.draws_remaining??"—",`今日消耗 ${w.today_spend??"?"}`),
    statCard("地块",f.plots??"—",f.locked!=null?`另锁定 ${f.locked} 块`:""),
    statCard("体力",f.stamina??"—","偷菜 3 点/次"),
  ].join("");
  $("stats").style.display=Object.keys(sm).length?"":"none";
}

function render(){
  const busy=state.busy;
  document.querySelectorAll(".actions button").forEach(b=>b.disabled=busy);
  renderStats();
  if(!fileView)$("runmeta").innerHTML=busy
    ?`<span class="spin"></span><span class="mut">正在运行：${esc(state.current.label)}（${esc(state.current.trigger)}，${esc(state.current.start)}）已 ${state.current.elapsed??0} 秒…</span>`
    :"空闲中";
  // 登录卡片：凭证未保存完整时始终显示，保存成功后自动隐藏
  const s=state.settings;
  // 按钮文字随抽奖设置变化
  $("btndraw").textContent=s.draw_all?"抽奖 · 抽光":"抽奖 ×1";
  $("btnsignin").textContent=s.signin_draw?"签到 + 抽奖":"签到";
  $("loginCard").classList.toggle("hidden",!!(s.username&&s.has_password));
  // 定时规则展示：规则 + 今日执行状态 + 接下来
  const sc=state.schedule,lm=state.last_marks||{};
  if(sc.enabled){
    const rows=[
      `<tr><td>签到</td><td>每天 ${esc(sc.signin_time)}</td><td>${markToday(lm.last_signin)||'<span class="mut">今日待跑</span>'}</td></tr>`,
      `<tr><td>农场</td><td>每天 ${esc(sc.farm_times.join(" / ")||"—")}</td><td>${markToday(lm.last_farm)||'<span class="mut">今日待跑</span>'}</td></tr>`,
      `<tr><td>抽奖</td><td>${sc.draw_times.length?`每天 ${esc(sc.draw_times.join(" / "))}`:'仅手动'}</td><td>${sc.draw_times.length?(markToday(lm.last_draw)||'<span class="mut">今日待跑</span>'):'<span class="mut">按需点击</span>'}</td></tr>`];
    $("schedinfo").innerHTML=
      `<table style="width:100%;border-collapse:collapse;font-size:13.5px">${rows.join("")}</table>`
      +`<div style="margin-top:8px">接下来：${state.schedule.next.map(esc).join("　")||"—"}</div>`
      +`<div class="tip">电脑关机错过的时点，开机后自动补跑当天；已跑过的当天不会重复。</div>`;
  }else{
    $("schedinfo").innerHTML="内置定时已关闭（仍可手动运行）";
  }
  // history
  $("history").innerHTML=state.runs.map(r=>
    `<li onclick="view(${r.id})"><span>${esc(r.start)}　${esc(r.label)}
     <span class="mut">(${esc(r.trigger)})</span></span><span>${fmtCode(r.code)}</span></li>`).join("")
    ||'<li style="cursor:default" class="mut">还没有运行记录</li>';
}

function renderRunMeta(d){
  const bits=[`任务 ${esc(d.label)}`];
  if(d.running){bits.push(`<span class="mut">运行中… 已 ${d.elapsed??0} 秒</span>`)}
  else{bits.push(fmtCode(d.code))}
  if(d.start)bits.push(`<span class="mut">${esc(d.start)}${d.end?" → "+esc(d.end):""}</span>`);
  $("runmeta").innerHTML=bits.join("　");
}

async function tick(){
  try{state=await api("/api/state")}catch(e){return}
  render();
  if(fileView){   // 正在浏览日志文件：不抢输出面板
    $("clock").textContent=new Date().toLocaleString("zh-CN",{hour12:false});
    lastBusy=state.busy;
    return;
  }
  if(state.current){if(followId!==state.current.id)followId=state.current.id}
  else if(followId===null&&state.runs.length){followId=state.runs[0].id}
  else if(followId!==null&&!state.runs.some(r=>r.id===followId)){followId=state.runs.length?state.runs[0].id:null}
  if(followId!==null){try{const d=await api("/api/run?id="+followId);
    $("log").textContent=d.output||"(等待输出…)";$("log").scrollTop=1e9;renderRunMeta(d)}catch(e){}}
  $("clock").textContent=new Date().toLocaleString("zh-CN",{hour12:false});
  if(lastBusy&&!state.busy)loadLogs();   // 任务刚结束，刷新日志文件列表
  lastBusy=state.busy;
}
// 自适应轮询：有任务在跑时 800ms 刷新日志，空闲时 1600ms
async function loop(){try{await tick()}catch(e){}setTimeout(loop,state&&state.busy?800:1600)}

async function run(task){
  if(task==="draw"){
    const all=state&&state.settings&&state.settings.draw_all;
    const n=(state&&state.summary&&state.summary.www&&state.summary.www.draws_remaining);
    if(all){if(!confirm(`抽光模式：将把全部 ${n!=null?n:"?"} 次可抽机会一次抽完，确定？`))return}
    else if(!confirm("确定抽奖 1 次？"))return;
  }
  if(task==="farm_steal"&&!confirm("农场流程 + 偷菜（消耗体力），确定？"))return;
  followId=null;fileView=false;
  $("runmeta").innerHTML='<span class="mut">正在启动…</span>';
  $("log").textContent="任务已启动，等待第一行输出…";
  try{const d=await api("/api/run",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({task})});
    followId=d.id}
  catch(e){showToast(e.message,true);$("log").textContent=""}
  tick();
}

function view(id){followId=id;fileView=false;tick()}
function editSched(){const c=$("settingsCard");c.open=true;c.scrollIntoView({behavior:"smooth"})}

async function loadSettings(){
  try{state=await api("/api/state")}catch(e){return}
  const s=state.settings;
  $("l_user").value=s.username||"";
  $("s_user").value=s.username||"";$("s_pass").value="";
  $("s_signin").value=s.signin_time;$("s_farm").value=s.farm_times.join(",");
  $("s_draw").value=(s.draw_times||[]).join(",");
  $("s_crop").value=s.farm_crop||"";$("s_sched").checked=!!s.schedule_enabled;
  $("s_sdraw").checked=!!s.signin_draw;$("s_drawall").checked=!!s.draw_all;
  $("s_nmode").value=s.notify_mode||"off";$("s_whurl").value=s.webhook_url||"";
  $("s_nwhen").value=s.webhook_when||"fail";
  $("s_base").value=s.base_url||"";
  $("s_farmurl").value=s.farm_url||"";$("s_wwwsess").value=s.www_session||"";
  $("s_farmsess").value=s.farm_session||"";
  $("s_host").value=s.host||"";$("s_tproxy").checked=!!s.trust_proxy;
}
async function login(){
  const u=$("l_user").value.trim(),p=$("l_pass").value;
  if(!u||!p){alert("请填写登录邮箱和密码");return}
  const bt=$("btnlogin");bt.disabled=true;bt.textContent="登录中…";
  $("log").textContent="正在登录并验证（约 10~20 秒），输出实时显示…";
  try{const d=await api("/api/login",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({username:u,password:p})});
    followId=d.id}
  catch(e){showToast("登录失败: "+e.message,true)}
  bt.disabled=false;bt.textContent="登录并保存凭证";tick();
}
async function saveSettings(){
  const body={username:$("s_user").value.trim(),farm_crop:$("s_crop").value.trim(),
    signin_time:$("s_signin").value.trim(),farm_times:$("s_farm").value,
    draw_times:$("s_draw").value,
    schedule_enabled:$("s_sched").checked,
    signin_draw:$("s_sdraw").checked,draw_all:$("s_drawall").checked,
    notify_mode:$("s_nmode").value,webhook_url:$("s_whurl").value.trim(),
    webhook_when:$("s_nwhen").value,
    base_url:$("s_base").value.trim(),farm_url:$("s_farmurl").value.trim(),
    www_session:$("s_wwwsess").value.trim(),farm_session:$("s_farmsess").value.trim(),
    host:$("s_host").value.trim(),trust_proxy:$("s_tproxy").checked};
  if($("s_pass").value)body.password=$("s_pass").value;
  try{await api("/api/settings",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    $("s_pass").value="";showToast("已保存，立即生效（监听地址需重启生效）");tick()}
  catch(e){showToast("保存失败: "+e.message,true)}
}

document.querySelectorAll(".actions button").forEach(
  b=>b.addEventListener("click",()=>run(b.dataset.task)));
$("btnsave").addEventListener("click",saveSettings);
$("btnlogin").addEventListener("click",login);
loop();loadSettings();loadLogs();loadUsage();setInterval(loadUsage,60000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "HainaWeb/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志，保持控制台干净
        pass

    # ── 工具 ──
    def _send(self, code, ctype, body, cookie=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # 日志轮询必须拿到实时内容
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False))

    def _client_local(self):
        ip = self.client_address[0]
        if load_config().get("trust_proxy"):
            ip = (self.headers.get("X-Real-IP")
                  or self.headers.get("X-Forwarded-For") or ip
                  ).split(",")[0].strip()
        return ip in ("127.0.0.1", "::1")

    def _token_ok(self):
        if self._client_local():
            return True
        tok = load_config().get("token", "")
        if not tok:
            return True
        m = re.search(r"haina_token=([0-9a-f]+)", self.headers.get("Cookie", ""))
        return bool(m and m.group(1) == tok)

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _page_html(self):
        """优先读 web/index.html（新版前端），缺失时回退到内置旧版页面。"""
        path = os.path.join(WEB_DIR, "index.html")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return PAGE

    def _static_file(self, name, ctype):
        try:
            with open(os.path.join(WEB_DIR, name), "r", encoding="utf-8") as fh:
                self._send(200, ctype, fh.read())
        except OSError:
            self._json({"error": "static file missing"}, 404)

    # ── 路由 ──
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            q = self._query()
            if not self._client_local():
                tok = load_config().get("token", "")
                qt = (q.get("token") or [""])[0]
                m = re.search(r"haina_token=([0-9a-f]+)", self.headers.get("Cookie", ""))
                if tok and qt != tok and not (m and m.group(1) == tok):
                    self._send(401, "text/plain; charset=utf-8",
                               "需要访问令牌：请用控制台打印的 http://<ip>:8787/?token=xxx 打开")
                    return
                cookie = f"haina_token={tok}; Path=/" if qt == tok and tok else None
                self._send(200, "text/html; charset=utf-8", self._page_html(), cookie=cookie)
                return
            self._send(200, "text/html; charset=utf-8", self._page_html())
            return
        static = STATIC_ROUTES.get(u.path)
        if static:
            return self._static_file(*static)
        if not self._token_ok():
            return self._json({"error": "未授权"}, 401)
        if u.path == "/api/state":
            return self.api_state()
        if u.path == "/api/run":
            return self.api_run(self._query())
        if u.path == "/api/logs":
            return self.api_logs()
        if u.path == "/api/logfile":
            return self.api_logfile(self._query())
        if u.path == "/api/usage":
            return self.api_usage()
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._token_ok():
            return self._json({"error": "未授权"}, 401)
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
        except (ValueError, OSError):
            return self._json({"error": "请求体不是合法 JSON"}, 400)
        if self.path == "/api/run":
            return self.api_start(body)
        if self.path == "/api/login":
            return self.api_login(body)
        if self.path == "/api/settings":
            return self.api_settings(body)
        if self.path == "/api/notify_test":
            return self.api_notify_test(body)
        self._json({"error": "not found"}, 404)

    # ── API ──
    def api_state(self):
        cfg = load_config()
        st = load_state()
        merged = list(RUNS) + list(PERSISTED)
        merged.sort(key=lambda r: -r["id"])
        runs = [
            {"id": r["id"], "task": r["task"], "label": r["label"],
             "trigger": r["trigger"], "start": r["start"], "end": r["end"],
             "code": r["code"]}
            for r in merged[:20]
        ]
        summary = {}
        try:
            with open(os.path.join(DATA_DIR, "last_summary.json"), "r",
                      encoding="utf-8") as fh:
                summary = (json.load(fh) or {}).get("sections") or {}
        except (OSError, ValueError, AttributeError):
            pass
        current = None
        if CURRENT is not None:
            current = {"id": CURRENT["id"], "label": CURRENT["label"],
                       "trigger": CURRENT["trigger"], "start": CURRENT["start"],
                       "elapsed": int(time.time() - CURRENT["t0"])}
        self._json({
            "busy": RUN_LOCK.locked(),
            "current": current,
            "runs": runs,
            "summary": summary,
            "schedule": {
                "enabled": bool(cfg.get("schedule_enabled")),
                "signin_time": normalize_time(cfg.get("signin_time") or "00:10"),
                "farm_times": sorted(t for t in (normalize_time(x) for x in cfg.get("farm_times") or []) if t),
                "draw_times": sorted(t for t in (normalize_time(x) for x in cfg.get("draw_times") or []) if t),
                "next": next_schedule_view(cfg),
            },
            "last_marks": {k: st.get(k) for k in ("last_signin", "last_farm", "last_draw")},
            "settings": {
                "username": cfg.get("username", ""),
                "has_password": bool(cfg.get("password")),
                "notify_mode": cfg.get("notify_mode") or ("qinglong" if cfg.get("notify") else "off"),
                "webhook_url": cfg.get("webhook_url", ""),
                "webhook_when": cfg.get("webhook_when") or "fail",
                "schedule_enabled": bool(cfg.get("schedule_enabled")),
                "signin_time": normalize_time(cfg.get("signin_time") or "00:10"),
                "farm_times": sorted(t for t in (normalize_time(x) for x in cfg.get("farm_times") or []) if t),
                "draw_times": sorted(t for t in (normalize_time(x) for x in cfg.get("draw_times") or []) if t),
                "farm_crop": cfg.get("farm_crop", ""),
                "draw_all": bool(cfg.get("draw_all")),
                "signin_draw": bool(cfg.get("signin_draw")),
                "base_url": cfg.get("base_url", ""),
                "farm_url": cfg.get("farm_url", ""),
                "www_session": cfg.get("www_session", ""),
                "farm_session": cfg.get("farm_session", ""),
                "host": cfg.get("host", ""),
                "trust_proxy": bool(cfg.get("trust_proxy")),
            },
        })

    def api_run(self, q):
        try:
            rid = int((q.get("id") or ["0"])[0])
        except ValueError:
            return self._json({"error": "id 不合法"}, 400)
        run = next((r for r in RUNS if r["id"] == rid), None) or \
              (CURRENT if CURRENT and CURRENT["id"] == rid else None) or \
              next((p for p in PERSISTED if p["id"] == rid), None)
        if run is None:
            return self._json({"error": "记录不存在（完整日志在 logs/ 目录）"}, 404)
        self._json({
            "id": run["id"], "task": run["task"], "label": run["label"],
            "start": run["start"], "end": run["end"], "code": run["code"],
            "running": run["code"] is None and run.get("buf") is not None,
            "elapsed": int(time.time() - run["t0"]) if run.get("t0") else None,
            "output": run["buf"].getvalue() if run.get("buf") else run.get("output", ""),
        })

    def api_start(self, body):
        task = body.get("task")
        run, err = start_run(task)
        if run is None:
            return self._json({"error": err}, 409)
        self._json({"ok": True, "id": run["id"]})

    def api_login(self, body):
        """保存凭证并立刻验证登录：跑一次只读 status，成功即建立会话缓存。"""
        global core_dirty
        # 防爆破：每 IP 10 分钟窗口内最多 10 次登录尝试
        ip = self.client_address[0]
        now = time.time()
        rec = LOGIN_CALLS.get(ip) or [0, now]
        if now - rec[1] > 600:
            rec = [0, now]
        if rec[0] >= 10:
            return self._json({"error": "登录尝试过于频繁，请 10 分钟后再试"}, 429)
        rec[0] += 1
        LOGIN_CALLS[ip] = rec

        u = str(body.get("username") or "").strip()
        p = str(body.get("password") or "")
        if not u or not p:
            return self._json({"error": "请填写登录邮箱和密码"}, 400)
        cfg = load_config()
        cfg["username"], cfg["password"] = u, p
        save_config(cfg)
        core_dirty = True
        run, err = start_run("status", trigger="登录")
        if run is None:
            return self._json({"error": err}, 409)
        self._json({"ok": True, "id": run["id"]})

    def api_settings(self, body):
        global core_dirty
        cfg = load_config()
        # 运行中先落盘；下一次任务运行前的 sync_core() 会 reload 生效
        core_dirty = True
        if "username" in body:
            cfg["username"] = str(body["username"]).strip()
        if body.get("password"):
            cfg["password"] = str(body["password"])
        if "notify" in body:
            cfg["notify"] = bool(body["notify"])
        if "schedule_enabled" in body:
            cfg["schedule_enabled"] = bool(body["schedule_enabled"])
        if "signin_time" in body:
            t = normalize_time(body["signin_time"])
            if not t:
                return self._json({"error": "签到时间格式应为 HH:MM"}, 400)
            cfg["signin_time"] = t
        if "farm_times" in body:
            ts = parse_farm_times(body["farm_times"] if isinstance(body["farm_times"], list)
                                  else str(body["farm_times"]))
            if not ts:
                return self._json({"error": "农场时间格式应为 HH:MM,HH:MM,..."}, 400)
            cfg["farm_times"] = sorted(set(ts))
        if "draw_times" in body:
            raw = body["draw_times"] if isinstance(body["draw_times"], list) else str(body["draw_times"])
            ts = parse_farm_times(raw) if str(raw).strip() else []
            cfg["draw_times"] = sorted(set(ts))  # 留空 = 仅手动，允许
        if "farm_crop" in body:
            cfg["farm_crop"] = str(body["farm_crop"]).strip()
        if "draw_all" in body:
            cfg["draw_all"] = bool(body["draw_all"])
        if "signin_draw" in body:
            cfg["signin_draw"] = bool(body["signin_draw"])
        if "notify_mode" in body:
            m = str(body["notify_mode"])
            if m not in ("off", "webhook", "qinglong"):
                return self._json({"error": "推送方式不合法"}, 400)
            cfg["notify_mode"] = m
            cfg["notify"] = (m == "qinglong")  # 兼容旧字段
        if "webhook_url" in body:
            cfg["webhook_url"] = str(body["webhook_url"]).strip()
        if "webhook_when" in body:
            cfg["webhook_when"] = "all" if str(body["webhook_when"]) == "all" else "fail"
        if "trust_proxy" in body:
            cfg["trust_proxy"] = bool(body["trust_proxy"])
        if "host" in body:
            h = str(body["host"]).strip()
            if h and not re.match(r"^[0-9a-zA-Z.:\[\]]+$", h):
                return self._json({"error": "监听地址格式不合法"}, 400)
            cfg["host"] = h
        for key in ("base_url", "farm_url", "www_session", "farm_session"):
            if key in body:
                cfg[key] = str(body[key]).strip()
        save_config(cfg)
        self._json({"ok": True})

    def api_logs(self):
        """列出 logs/ 目录的日志文件（新→旧）。"""
        try:
            names = sorted((f for f in os.listdir(LOG_DIR) if f.endswith(".log")),
                           reverse=True)
        except OSError:
            names = []
        files = []
        for name in names[:100]:
            try:
                st = os.stat(os.path.join(LOG_DIR, name))
                files.append({"name": name,
                              "size": f"{st.st_size / 1024:.1f} KB",
                              "mtime": datetime.fromtimestamp(st.st_mtime, CST).strftime("%m-%d %H:%M")})
            except OSError:
                continue
        self._json({"files": files})

    def api_logfile(self, q):
        """读取单个日志文件内容（basename 校验防路径穿越）。"""
        name = os.path.basename((q.get("name") or [""])[0])
        if not name or not name.endswith(".log"):
            return self._json({"error": "文件名不合法"}, 400)
        path = os.path.abspath(os.path.join(LOG_DIR, name))
        if os.path.dirname(path) != os.path.abspath(LOG_DIR) or not os.path.isfile(path):
            return self._json({"error": "文件不存在"}, 404)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read(300_000)
            size = f"{os.path.getsize(path) / 1024:.1f} KB"
        except OSError as exc:
            return self._json({"error": str(exc)}, 500)
        self._json({"name": name, "content": content, "size": size})

    def api_usage(self):
        """最近 20 条 API 调用记录（站点使用日志，只读，不落盘）。"""
        try:
            items = core.get_recent_usage(limit=20)
        except Exception as exc:
            return self._json({"items": [], "error": str(exc)[:200]})
        if items is None:
            return self._json({"items": [], "needs_refresh": True})
        self._json({"items": items})

    def api_notify_test(self, body):
        """按当前保存的配置发一条测试推送。"""
        cfg = load_config()
        mode = cfg.get("notify_mode") or ("qinglong" if cfg.get("notify") else "off")
        if mode != "webhook":
            return self._json({"error": "请先把推送方式选为「Webhook」并保存设置"}, 400)
        ok, detail = _send_webhook(cfg.get("webhook_url", ""),
                                   "海纳百川控制台 · 测试推送",
                                   "收到这条说明 Webhook 推送配置成功")
        self._json({"ok": ok, "detail": detail}, 200 if ok else 502)


# ── 入口 ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="海纳百川本地网页控制台")
    ap.add_argument("--host", default=None,
                    help="监听地址（默认取配置，再默认 127.0.0.1；服务器部署用 0.0.0.0）")
    ap.add_argument("--port", type=int, default=None, help="端口（默认取配置或 8787）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    cfg = load_config()
    apply_env()
    global core, PERSISTED
    import haina as core
    PERSISTED = _load_persisted_runs()

    host = args.host or cfg.get("host") or "127.0.0.1"
    port = args.port or cfg.get("port") or 8787
    srv = None
    for p in range(port, port + 10):
        try:
            srv = ThreadingHTTPServer((host, p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print(f"[FAIL] 端口 {port}~{port + 9} 均被占用")
        return 1

    print(f"{'─' * 55}")
    print(f"  海纳百川控制台已启动")
    print(f"  本机访问:  http://127.0.0.1:{port}/")
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(f"  局域网/外网访问: http://<本机IP>:{port}/?token={cfg.get('token')}")
        print(f"  [!] 服务已暴露到网络，令牌即密码，请勿泄露；公网建议 nginx HTTPS 反代")
    if PERSISTED:
        print(f"  [i] 已回载 {len(PERSISTED)} 条历史运行记录（来自 logs/）")
    if not (cfg.get("username") and cfg.get("password")):
        print(f"  [i] 尚未配置完整账号，请在网页「设置」里填写（会话缓存有效时可不用密码）")
    print(f"{'─' * 55}")

    threading.Thread(target=sched_loop, daemon=True).start()
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[控制台] 已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
