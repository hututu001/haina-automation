#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海纳百川自动化 · 统一脚本（签到 / 抽奖 / 农场三合一）

子命令:
  signin   签到: 查今日 token 消耗(≥100万门槛) → 钱包签到 → 领取所有可领福利
  draw     抽奖: 显示账户/奖池状态, 默认只抽 1 次 (--status-only 仅查看)
  farm     农场: 状态 → 一键收菜 → 自动补种(时薪最优) → 兑换 (--steal 偷菜)
  status   只读总览: 抽奖面板 + 农场状态, 不做任何写操作

青龙任务命令(配合 ql_haina.sh 入口):
  bash ql_haina.sh signin    # 每天 00:10
  bash ql_haina.sh farm      # 每天 6/14/22 点; 需要偷菜加 --steal
  bash ql_haina.sh draw      # 任务保持禁用, 想抽时手动运行

会话缓存与旧版三脚本同路径同格式(HAINA_SESSION_FILE / HAINA_FARM_SESSION_FILE),
从旧版迁移无需重新登录。账号密码只通过环境变量传入, 密码登录仅是缓存全失效时的兜底。
"""

import argparse
import http.cookiejar as cookiejar_mod
import io
import json
import os
import secrets
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta

try:
    import fcntl
except ImportError:  # Windows 本地运行时不提供 fcntl
    fcntl = None

# ── 配置 ──────────────────────────────────────────────────────────────────
BASE = os.environ.get("HAINA_BASE_URL", "https://www.0809.one").rstrip("/")
FARM = os.environ.get("HAINA_FARM_URL", "https://farm.0809.one").rstrip("/")
USERNAME = os.environ.get("HAINA_USERNAME", "").strip()
PASSWORD = os.environ.get("HAINA_PASSWORD", "")
API = "/lottery/api"
TOKEN_THRESHOLD = 1_000_000
CST = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0.0.0 Safari/537.36"
NOTIFY_ENABLED = os.environ.get("HAINA_NOTIFY", "1").strip().lower() not in {
    "0", "false", "no", "off"
}
NOTIFY_JS = os.environ.get("QL_SEND_NOTIFY", "/ql/data/deps/sendNotify.js")
# www 会话缓存: 签到/抽奖/农场三方共享
WWW_SESSION_FILE = os.environ.get(
    "HAINA_SESSION_FILE", "/ql/data/config/haina_session.json"
)
# 农场会话(farm cookie)单独缓存
FARM_SESSION_FILE = os.environ.get(
    "HAINA_FARM_SESSION_FILE", "/ql/data/config/haina_farm_session.json"
)
# 任务产出摘要文件（网页控制台仪表盘用；为空则不写，不影响命令行/青龙）
SUMMARY_FILE = os.environ.get("HAINA_SUMMARY_FILE", "")

# 全局会话状态
_www_access = None
_www_refresh = None
_lottery_token = None
_uid = None
_session_id = None
_access_expires_at = None

# cookiejar 全流程共用(www 登录 cookie / 农场 farm_session), 与浏览器行为一致;
# 手动 Cookie 头优先级高于 jar, refresh 轮换行为不变
_cj = cookiejar_mod.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))

# ── Windows 终端 UTF-8 ──────────────────────────────────────────────────
# 注意：必须用 reconfigure 原地修改，不能新建 TextIOWrapper 盖在 sys.stdout 上。
# 本模块会被网页控制台 importlib.reload：新建的包装器被回收时会 close 底层
# 缓冲区，令后续任何 print 抛 "ValueError: I/O operation on closed file"。
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # 已被重定向到非标准流（如控制台缓冲）时静默跳过


# ── HTTP ──────────────────────────────────────────────────────────────────
def http(url, method="GET", headers=None, body=None):
    """返回 (status, parsed_body_or_None, set_cookie_list)。"""
    h = {"Accept": "application/json", "User-Agent": UA}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    if body is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with _opener.open(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None), (
                resp.headers.get_all("Set-Cookie") or [])
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw), (e.headers.get_all("Set-Cookie") or [])
        except Exception:
            return e.code, {"error": raw}, []
    except Exception as e:
        return 0, {"error": str(e)}, []


def err_text(body):
    """提取服务端错误信息：{"error":{code,message}} 或 {"message":...}。"""
    if not isinstance(body, dict):
        return str(body)
    e = body.get("error")
    if isinstance(e, dict):
        msg = e.get("message") or e.get("code") or "?"
        code = e.get("code")
        return f"{msg} ({code})" if code and code != msg else msg
    return body.get("message") or body.get("error") or json.dumps(body, ensure_ascii=False)[:200]


# ── 会话缓存读写 ──────────────────────────────────────────────────────────
def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return None


def write_json_atomic(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_summary(section, data):
    """把任务产出的关键状态合并进摘要文件（网页控制台仪表盘用，env 开启）。"""
    if not SUMMARY_FILE:
        return
    try:
        cur = load_json(SUMMARY_FILE) or {}
        sections = cur.setdefault("sections", {})
        sections[section] = dict(data, updated_at=int(time.time()))
        write_json_atomic(SUMMARY_FILE, cur)
    except Exception:
        pass  # 摘要失败不影响任务本身


def save_www_session():
    """与旧版脚本同一缓存格式；优先保留本进程新鲜的 lottery_token，否则沿用磁盘上的。"""
    data = {
        "version": 1,
        "base_url": BASE,
        "access_token": _www_access,
        "refresh_cookie": _www_refresh,
        "lottery_token": _lottery_token
            or (load_json(WWW_SESSION_FILE) or {}).get("lottery_token"),
        "uid": _uid,
        "session_id": _session_id,
        "access_expires_at": _access_expires_at,
        "updated_at": int(time.time()),
    }
    write_json_atomic(WWW_SESSION_FILE, data)


def save_farm_session():
    """保存 farm_session cookie（其他 cookie 不需要）。"""
    cookies = {
        c.name: c.value for c in _cj if c.name == "farm_session" and c.value
    }
    if not cookies:
        return
    write_json_atomic(FARM_SESSION_FILE, {
        "version": 1,
        "farm_url": FARM,
        "cookies": cookies,
        "updated_at": int(time.time()),
    })


def inject_farm_cookies(cookies):
    """把缓存的 farm cookie 注入 cookiejar。"""
    for name, value in (cookies or {}).items():
        domain = FARM.split("//", 1)[1]
        _cj.set_cookie(cookiejar_mod.Cookie(
            version=0, name=name, value=value,
            port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=False,
            path="/", path_specified=True,
            secure=True, expires=None, discard=False,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        ))


def _clear_farm_cookies():
    for c in list(_cj):
        if c.name == "farm_session":
            _cj.clear(domain=c.domain, path=c.path, name=c.name)


def _fill_www_from_cache(cached):
    global _www_access, _www_refresh, _lottery_token, _uid, _session_id, _access_expires_at
    _www_access = cached.get("access_token")
    _www_refresh = cached.get("refresh_cookie")
    _lottery_token = cached.get("lottery_token")
    _uid = str(cached.get("uid") or "") or None
    _session_id = cached.get("session_id")
    _access_expires_at = cached.get("access_expires_at")


def _www_access_valid():
    return bool(
        _www_access and _access_expires_at
        and int(_access_expires_at) > int(time.time()) + 60
    )


# ── www 认证原语 ──────────────────────────────────────────────────────────
def validate_lottery_token():
    """用只读 Dashboard 请求验证缓存的 Lottery Token。"""
    if not _lottery_token:
        return False
    status, body, _ = http(
        f"{BASE}{API}/dashboard",
        headers={"Authorization": f"Bearer {_lottery_token}"},
    )
    return status == 200 and body and "error" not in body


def www_refresh_access():
    """使用 HttpOnly Refresh Cookie 轮换 Access Token，不创建新登录会话。"""
    global _www_access, _www_refresh, _uid, _session_id, _access_expires_at
    if not _www_refresh:
        return False
    headers = {
        "Origin": BASE,
        "Referer": f"{BASE}/",
        "Cookie": f"new_api_refresh={_www_refresh}",
    }
    if _session_id:
        headers["X-Auth-Session"] = _session_id
    status, body, cookies = http(
        f"{BASE}/api/user/auth/refresh", method="POST", headers=headers, body={})
    data = body.get("data", {}) if isinstance(body, dict) else {}
    if status != 200 or not data.get("access_token"):
        return False
    _www_access = data["access_token"]
    _access_expires_at = data.get("access_expires_at")
    user = data.get("user") or {}
    session = data.get("session") or {}
    if user.get("id") is not None:
        _uid = str(user["id"])
    if session.get("sid"):
        _session_id = session["sid"]
    for cookie in cookies:
        if "new_api_refresh=" in cookie:
            _www_refresh = cookie.split("new_api_refresh=", 1)[1].split(";", 1)[0]
            break
    return True


def bridge_session():
    """使用已有站点会话换取短期 Lottery Token。"""
    global _lottery_token
    headers = {
        "Origin": BASE,
        "Referer": f"{BASE}/lottery/",
        "Authorization": f"Bearer {_www_access}",
    }
    if _www_refresh:
        headers["Cookie"] = f"new_api_refresh={_www_refresh}"
    status, body, _ = http(
        f"{BASE}{API}/bridge/session",
        method="POST",
        headers=headers,
        body={"uid": _uid},
    )
    if status == 200 and body and body.get("accessToken"):
        _lottery_token = body["accessToken"]
        return True
    return False


def login_with_password():
    """仅在会话缓存无效时使用账号密码创建新会话。"""
    global _www_access, _www_refresh, _uid, _session_id, _access_expires_at
    status, body, cookies = http(
        f"{BASE}/api/user/login", method="POST",
        headers={"Origin": BASE, "Referer": f"{BASE}/sign-in"},
        body={"username": USERNAME, "password": PASSWORD})
    if status != 200 or not body or not body.get("success"):
        code = body.get("code", "") if isinstance(body, dict) else ""
        msg = body.get("message", "?") if isinstance(body, dict) else "?"
        if code == "AUTH_SESSION_LIMIT":
            msg = "登录会话已达上限，请在主站「登录会话」页面撤销旧会话"
        elif code == "AUTH_SESSION_ISSUANCE_LIMIT":
            msg = "近期创建登录会话过多，请等待限制窗口结束后重试"
        print(f"[FAIL] www 登录失败: {msg}" + (f" ({code})" if code else ""))
        return False
    data = body["data"]
    _www_access = data["access_token"]
    _access_expires_at = data.get("access_expires_at")
    _uid = str(data["user"]["id"])
    session = data.get("session") or {}
    _session_id = session.get("sid")
    _www_refresh = None
    for cookie in cookies:
        if "new_api_refresh=" in cookie:
            _www_refresh = cookie.split("new_api_refresh=", 1)[1].split(";", 1)[0]
            break
    return True


# ── 总认证入口 ────────────────────────────────────────────────────────────
def www_auth():
    """签到/抽奖认证：缓存 lottery token → refresh 轮换 → bridge → 密码兜底。"""
    lock_path = f"{WWW_SESSION_FILE}.lock"
    os.makedirs(os.path.dirname(lock_path) or ".", mode=0o700, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        cached = load_json(WWW_SESSION_FILE)
        if (cached and cached.get("base_url") == BASE
                and cached.get("access_token") and cached.get("uid") is not None):
            _fill_www_from_cache(cached)
            lottery_valid = validate_lottery_token()
            access_valid = _www_access_valid()
            if lottery_valid and access_valid:
                print(f"[OK] 已复用缓存 Token uid={_uid}，未重新登录")
                return True
            if not access_valid and www_refresh_access():
                access_valid = True
                print(f"[OK] 已使用 Refresh Cookie 刷新 Access Token uid={_uid}")
            if access_valid and (lottery_valid or bridge_session()):
                save_www_session()
                print(f"[OK] 已复用缓存站点会话 uid={_uid}，未重新登录")
                if not lottery_valid:
                    print("[OK] Lottery token 已刷新")
                return True
            print("[i]  缓存 Token 与站点会话均已失效，改用账号密码重新登录")

        if not USERNAME or not PASSWORD:
            print("[FAIL] 缓存失效且未配置 HAINA_USERNAME / HAINA_PASSWORD")
            return False
        if not login_with_password():
            return False
        if not bridge_session():
            print("[FAIL] 登录成功，但 Bridge 获取 Lottery Token 失败")
            return False

        save_www_session()
        print(f"[OK] 登录成功并更新会话缓存 uid={_uid}")
        print("[OK] Lottery token 获取成功")
        return True


def farm_auth():
    """农场认证三级降级：farm cookie → www 会话 SSO → 密码登录 SSO。"""
    lock_path = f"{FARM_SESSION_FILE}.lock"
    os.makedirs(os.path.dirname(lock_path) or ".", mode=0o700, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        # ── 1. farm cookie 缓存
        farm_cache = load_json(FARM_SESSION_FILE)
        if farm_cache and farm_cache.get("farm_url") == FARM and farm_cache.get("cookies"):
            inject_farm_cookies(farm_cache["cookies"])
            if farm_session_valid():
                print("[OK] 已复用农场会话缓存，未登录")
                return True
            _clear_farm_cookies()

        # ── 2. www 会话 → SSO（status 子命令下优先复用本进程已认证的 token）
        if not _www_access_valid():
            cached = load_json(WWW_SESSION_FILE)
            if (cached and cached.get("base_url") == BASE
                    and cached.get("access_token") and cached.get("uid") is not None):
                _fill_www_from_cache(cached)
                if not _www_access_valid() and www_refresh_access():
                    print("[OK] 已用 Refresh Cookie 轮换 www Access Token")
                    save_www_session()
        if _www_access_valid():
            ok, err = farm_sso(_www_access)
            if ok:
                save_farm_session()
                print("[OK] 已用 www 会话 SSO 桥接农场，未使用密码")
                return True
            print(f"[i]  www 会话 SSO 桥接失败: {err}")
            _clear_farm_cookies()

        # ── 3. 密码登录兜底
        if not USERNAME or not PASSWORD:
            print("[FAIL] 所有会话缓存失效，且未配置 HAINA_USERNAME / HAINA_PASSWORD")
            return False
        print("[i]  缓存均失效，改用账号密码登录 www")
        if not login_with_password():
            return False
        save_www_session()
        ok, err = farm_sso(_www_access)
        if not ok:
            print(f"[FAIL] 登录成功但农场 SSO 失败: {err}")
            return False
        save_farm_session()
        print("[OK] 密码登录 + SSO 完成，农场会话已建立")
        return True


# ── 农场 SSO 桥接 ─────────────────────────────────────────────────────────
def farm_sso(access_token):
    """用 www access token 走 SSO：prepare → callback，拿 farm_session cookie。"""
    state = secrets.token_hex(24)
    status, body, _ = http(
        f"{FARM}/api/sso/prepare", method="POST",
        headers={
            "Origin": BASE,
            "Referer": f"{BASE}/farm-launch",
            "Authorization": f"Bearer {access_token}",
        },
        body={"state": state})
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("code"), str):
        return False, err_text(body)
    code = body["code"]
    # callback（302 → 农场首页 HTML，cookiejar 自动收 farm_session，不解析 JSON）
    req = urllib.request.Request(
        f"{FARM}/api/sso/callback?code={code}&state={state}&redirect=%2F",
        headers={
            "Accept": "text/html,application/json",
            "User-Agent": UA,
            "Referer": f"{BASE}/farm-launch",
        })
    try:
        with _opener.open(req, timeout=20) as resp:
            if resp.status != 200:
                return False, f"callback HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"callback HTTP {e.code}"
    except Exception as e:
        return False, f"callback 网络错误: {e}"
    if not any(c.name == "farm_session" for c in _cj):
        return False, "callback 未返回 farm_session cookie"
    return True, None


def farm_session_valid():
    """验证当前农场会话是否有效。"""
    status, body, _ = http(f"{FARM}/api/session")
    return status == 200 and isinstance(body, dict) and body.get("authenticated") is True


# ── API 封装 ──────────────────────────────────────────────────────────────
def lot(path, method="GET", body=None, extra=None):
    """Lottery API 调用"""
    h = {"Authorization": f"Bearer {_lottery_token}"}
    if extra:
        h.update(extra)
    return http(f"{BASE}{API}{path}", method=method, headers=h, body=body)


def farm_api(path, method="GET", body=None, idempotent=False):
    headers = {"Origin": FARM, "Referer": f"{FARM}/"}
    if idempotent:
        headers["Idempotency-Key"] = str(uuid.uuid4())
    return http(f"{FARM}{path}", method=method, headers=headers, body=body)


def get_dashboard():
    status, body, _ = lot("/dashboard")
    return body if status == 200 and body and "error" not in body else None


def get_bootstrap():
    status, body, _ = farm_api("/api/bootstrap")
    if status == 200 and isinstance(body, dict):
        return body
    return None


def get_token_usage():
    """查询今日 API token 消耗"""
    now_cst = datetime.now(CST)
    midnight = now_cst.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = int(midnight.timestamp())
    total_p, total_c, page = 0, 0, 1
    while True:
        status, body, _ = http(
            f"{BASE}/api/log/self?p={page}&page_size=100&start_timestamp={start_ts}",
            headers={"Authorization": f"Bearer {_www_access}"},
        )
        if status != 200 or not body:
            break
        items = body.get("data", {}).get("items", [])
        if not items:
            break
        for it in items:
            total_p += it.get("prompt_tokens", 0)
            total_c += it.get("completion_tokens", 0)
        if len(items) < 100:
            break
        page += 1
    return total_p, total_c


# ═══════════════════════════════ signin 签到 ═══════════════════════════════
def wallet_checkin():
    """钱包签到"""
    status, body, _ = http(f"{BASE}/api/user/checkin", method="POST", body={}, headers={
        "Authorization": f"Bearer {_www_access}",
        "Origin": BASE, "Referer": f"{BASE}/profile",
    })
    msg = body.get("message", "") if body else ""
    ok = body.get("success", False) if body else False
    return ok, msg


def claim_all():
    """领取所有可领的活动福利，通过比较 remaining 变化来判断是否真的领到了"""
    count = 0
    dash = get_dashboard()
    if not dash:
        return 0
    current_remaining = dash.get("eligibility", {}).get("remaining", 0)

    for _ in range(6):  # 签到 + 3时段 + 消耗奖励 + 余量
        status, body, _ = lot("/check-ins/claim", method="POST")
        if status != 200 or not body or "error" in body:
            break
        new_remaining = 0
        if isinstance(body, dict):
            elig = body.get("eligibility", body.get("eligibilities", {}))
            new_remaining = elig.get("remaining", current_remaining)
        if new_remaining > current_remaining:
            count += 1
            current_remaining = new_remaining
            time.sleep(0.3)
        else:
            break  # 没有新的领取
    return count


STATE_CN = {"claimable": "可领", "used": "已用", "upcoming": "未到",
            "expired": "过期", "missed": "错过", "available": "可用"}


# ── 任务摘要（网页控制台仪表盘） ───────────────────────────────────────────
def _www_summary(dash, prompt_tokens=0, completion_tokens=0):
    acct = dash.get("account", {}) or {}
    elig = dash.get("eligibility", {}) or {}
    metrics = dash.get("metrics", {}) or {}
    return {
        "balance": round(acct.get("balance", 0) or 0, 2),
        "draws_remaining": elig.get("remaining", 0) or 0,
        "today_spend": round(metrics.get("todaySpend", 0) or 0, 2),
        "tokens": int(prompt_tokens + completion_tokens),
    }


def _farm_summary(boot):
    profile = boot.get("profile") or {}
    wallet = boot.get("wallet") or {}
    weekly = boot.get("weekly") or {}
    stamina = boot.get("stamina") or {}
    newapi = boot.get("newApi") or {}
    per_unit = boot.get("quotaPerUnit") or 500000
    states = {}
    for p in boot.get("plots") or []:
        s = plot_state(p)
        states[s] = states.get(s, 0) + 1
    return {
        "farm_name": profile.get("farmName", ""),
        "balance": round((newapi.get("balance") or 0) / per_unit, 2),
        "pending": round(wallet.get("currentWeekPendingQuotaDisplay", 0) or 0, 2),
        "stamina": f"{stamina.get('current', '?')}/{stamina.get('max', '?')}",
        "plots": (f"{states.get('GROWING', 0)}生 {states.get('RIPE', 0)}熟 "
                  f"{states.get('EMPTY', 0)}空"),
        "locked": states.get("LOCKED", 0),
        "weekly_level": weekly.get("level") or 1,
    }


def cmd_signin(args):
    now = datetime.now(CST)
    print(f"{'─'*50}")
    print(f"  海纳百川签到  {now.strftime('%Y-%m-%d %H:%M:%S')} CST")
    print(f"{'─'*50}")

    print("[*] 正在认证（优先复用缓存会话）…")
    if not www_auth():
        return 1

    # 1. 检查 token 消耗
    print("[*] 正在查询今日 token 消耗…")
    prompt, completion = get_token_usage()
    total = prompt + completion
    print(f"     今日 tokens: {total:,} (prompt {prompt:,} + completion {completion:,})")

    # 2. 钱包签到 (达标才签)
    if total >= TOKEN_THRESHOLD:
        ok, msg = wallet_checkin()
        if ok:
            print(f"[OK] 钱包签到成功: {msg}")
        else:
            print(f"[i]  钱包签到: {msg}")
    else:
        deficit = TOKEN_THRESHOLD - total
        print(f"[!]  钱包签到跳过 (差 {deficit:,} tokens)")

    # 3. 领取活动福利
    print("[*] 正在获取抽奖面板…")
    dash = get_dashboard()
    if not dash:
        print("[FAIL] 获取 dashboard 失败", file=sys.stderr)
        return 1
    save_summary("www", _www_summary(dash, prompt, completion))

    elig = dash.get("eligibility", {})
    remaining_before = elig.get("remaining", 0)
    checkin = elig.get("checkIn", {})
    print(f"     签到福利: {checkin.get('state', '?')}")
    for w in elig.get("schedule", []):
        s = STATE_CN.get(w.get("state", ""), w.get("state", "?"))
        print(f"     {w.get('label', '')} ({w.get('timeLabel', '')}): {s}")

    claimed = claim_all()

    # 4. 刷新状态
    dash2 = get_dashboard()
    if dash2:
        elig2 = dash2.get("eligibility", {})
        remaining_after = elig2.get("remaining", 0)
        balance = dash2.get("account", {}).get("balance", 0)
        today_spend = dash2.get("metrics", {}).get("todaySpend", 0) or 0
        pool = next((t for t in dash2.get("tiers", []) if t.get("current")), {})

        print(f"\n{'='*50}")
        print(f"  余额: {balance:.2f}  |  今日消耗: {today_spend:.2f}")
        print(f"  可抽: {remaining_after} 次 (本次 +{remaining_after - remaining_before})")
        print(f"  奖池: {pool.get('name', '?')} (Tier {pool.get('tier', '?')})")
        print(f"{'='*50}")
        save_summary("www", _www_summary(dash2, prompt, completion))

    if claimed > 0:
        print(f"[OK] 本次领取了 {claimed} 项福利")
    else:
        print(f"[-]  当前无可领取的福利")

    # 5. 签到后自动抽奖（--draw / HAINA_SIGNIN_DRAW=1 开启；遵循抽光设置）
    if getattr(args, "draw", False):
        print(f"\n{'─'*50}\n  [签到后抽奖]")
        dashd = dash2 or get_dashboard()
        if not dashd:
            print("[FAIL] 获取抽奖面板失败，跳过自动抽奖", file=sys.stderr)
            return 0
        rem = dashd.get("eligibility", {}).get("remaining", 0)
        if rem > 0:
            draw_all = bool(getattr(args, "draw_all", False))
            results = _draw_pass(rem, draw_all=draw_all)
            if results:
                dash3 = get_dashboard()
                if dash3:
                    b = dash3.get("account", {}).get("balance", 0) or 0
                    r = dash3.get("eligibility", {}).get("remaining", 0)
                    print(f"\n  当前余额: {b:.2f}  |  剩余可抽: {r}")
                    save_summary("www", _www_summary(dash3, prompt, completion))
        else:
            print("[-] 签到后无可抽次数，跳过抽奖")

    return 0


# ═══════════════════════════════ draw 抽奖 ═══════════════════════════════
# 默认每次只抽 1 个（保守）；--all / HAINA_DRAW_ALL=1 时抽光全部可抽次数
DEFAULT_DRAW = 1
DRAW_MAX = 100   # 抽光模式的防御上限（正常次数远小于此）


def show_info(dash, prompt_tokens=0, completion_tokens=0):
    acct = dash.get("account", {})
    elig = dash.get("eligibility", {})
    metrics = dash.get("metrics", {})
    grants = dash.get("grants", [])

    balance = acct.get("balance", 0) or 0
    remaining = elig.get("remaining", 0)
    daily_used = elig.get("dailyUsed", 0)
    pool_tier = elig.get("poolTier", 0)
    today_spend = metrics.get("todaySpend", elig.get("todaySpend", 0)) or 0
    total_tokens = prompt_tokens + completion_tokens

    print(f"\n{'='*55}")
    print(f"  账户余额: {balance:.2f}")
    print(f"  今日消耗: {today_spend:.2f} 额度")
    if total_tokens > 0:
        print(f"  今日 tokens: {total_tokens:,} (prompt {prompt_tokens:,} + completion {completion_tokens:,})")
        print(f"  签到门槛: {TOKEN_THRESHOLD:,} tokens  "
              + ("[OK] 已达标" if total_tokens >= TOKEN_THRESHOLD
                 else f"[X] 未达标 (差 {TOKEN_THRESHOLD - total_tokens:,})"))
    print(f"  可抽次数: {remaining}    今日已抽: {daily_used}")
    print(f"  当前奖池: Tier {pool_tier}")
    print(f"{'='*55}")

    ci = elig.get("checkIn", {})
    if ci:
        print(f"  签到: {STATE_CN.get(ci.get('state', ''), ci.get('state', '?'))}")
    for w in elig.get("schedule", []):
        s = STATE_CN.get(w.get("state", ""), w.get("state", "?"))
        print(f"  {w.get('label', '')} ({w.get('timeLabel', '')}): {s}")

    tiers = dash.get("tiers", [])
    reached = [t for t in tiers if t.get("reached")]
    if reached:
        names = ", ".join(f"Tier{t['tier']}" for t in reached)
        print(f"  已解锁: {names}")

    active = [g for g in grants if g.get("status") == "active"]
    if active:
        print(f"\n  活跃临时额度 ({len(active)} 个):")
        for g in active:
            label = g.get("shortLabel", g.get("label", "?"))
            expires = g.get("expiresAt", "")
            if expires:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                exp_str = exp_dt.astimezone(CST).strftime("%m/%d %H:%M")
            else:
                exp_str = "?"
            amt = g.get("quotaAmount", 0) or 0
            print(f"    {label}: +{amt} (到期 {exp_str})")
    print()


def show_prize(result, idx):
    prize = result.get("prize", {})
    effect = result.get("effect", {})
    label = prize.get("label", "?")
    grade = prize.get("grade", "")
    grade_str = f" [{grade}]" if grade else ""
    amount = prize.get("quotaAmount")
    hours = prize.get("validityHours")
    summary = effect.get("summary", "")
    status = result.get("fulfillmentStatus", result.get("status", ""))

    print(f"\n  +-- 第 {idx} 抽 -------------------------")
    print(f"  | {label}{grade_str}")
    if amount:
        print(f"  | 额度: +{amount}")
    if hours:
        print(f"  | 有效期: {hours} 小时")
    if summary:
        print(f"  | 效果: {summary}")
    if status:
        print(f"  | 状态: {status}")
    print(f"  +----------------------------------")


def _draw_pass(remaining, draw_all=False):
    """执行抽奖并打印明细+汇总，返回 results 列表。

    draw_all=False：只抽 DEFAULT_DRAW(1) 次；True：抽光全部可抽次数。
    签到后自动抽奖与本命令共用此函数，行为完全一致。
    """
    try:
        remaining = int(remaining or 0)
    except (TypeError, ValueError):
        remaining = 0
    n = remaining if draw_all else min(DEFAULT_DRAW, remaining)
    n = max(0, min(n, DRAW_MAX))
    if n <= 0:
        print("[-] 没有可抽次数")
        return []

    mode = "抽光模式" if draw_all else "保守模式"
    print(f"[*] 开始抽奖 ({n} 次, {mode})...")
    results = []
    for i in range(n):
        status, result, _ = lot("/draw", method="POST",
                                extra={"Idempotency-Key": str(uuid.uuid4())})
        if status == 200 and result and "error" not in result:
            show_prize(result, i + 1)
            results.append(result)
        else:
            err = result.get("error", result) if result else "?"
            if isinstance(err, dict):
                err = err.get("message", str(err))
            print(f"\n  [FAIL] 第 {i+1} 抽失败: {err}")
            break
        time.sleep(0.5)

    if results:
        print(f"\n{'─'*50}")
        print(f"  抽了 {len(results)} 次")
        prize_counts = {}
        total_amount = 0
        for r in results:
            p = r.get("prize", {})
            label = p.get("shortLabel", p.get("label", "?"))
            prize_counts[label] = prize_counts.get(label, 0) + 1
            amt = p.get("quotaAmount", 0) or 0
            if amt:
                total_amount += amt
        print("  汇总:")
        for label, cnt in sorted(prize_counts.items(), key=lambda x: -x[1]):
            print(f"    {label}: x{cnt}")
        if total_amount:
            print(f"  总额度: +{total_amount}")
    return results


def cmd_draw(args):
    now = datetime.now(CST)
    print(f"\n  海纳百川抽奖  {now.strftime('%Y-%m-%d %H:%M:%S')} CST")

    print("[*] 正在认证（优先复用缓存会话）…")
    if not www_auth():
        return 1

    print("[*] 正在获取账户与奖池状态…")
    prompt_t, completion_t = get_token_usage()
    dash = get_dashboard()
    if not dash:
        print("[FAIL] 获取 dashboard 失败", file=sys.stderr)
        return 1

    show_info(dash, prompt_t, completion_t)
    save_summary("www", _www_summary(dash, prompt_t, completion_t))

    if not args.status_only:
        remaining = dash.get("eligibility", {}).get("remaining", 0)
        _draw_pass(remaining, draw_all=bool(getattr(args, "all", False)))

        dash2 = get_dashboard()
        if dash2:
            b = dash2.get("account", {}).get("balance", 0) or 0
            r = dash2.get("eligibility", {}).get("remaining", 0)
            print(f"\n  当前余额: {b:.2f}  |  剩余可抽: {r}")
            save_summary("www", _www_summary(dash2, prompt_t, completion_t))
        print()

    return 0


# ═══════════════════════════════ farm 农场 ═══════════════════════════════
def plot_state(plot):
    """与前端 getPlotState 相同的判定。"""
    if not plot or plot.get("available") is False:
        return "LOCKED"
    status = plot.get("state") or plot.get("status") or "EMPTY"
    if status == "GROWING":
        raw = plot.get("matureAt") or plot.get("growsUntil")
        try:
            until = int(raw)
            if until > 0:
                if until < 1e12:
                    until *= 1000
                if until <= time.time() * 1000:
                    return "RIPE"
        except (TypeError, ValueError):
            pass
    return status


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h > 0:
        return f"{h}h{m:02d}m"
    if m > 0:
        return f"{m}m"
    return f"{seconds}s"


def crop_rate(crop):
    """时薪（显示额度/小时）= (产量 - 种子成本) / 生长时长。"""
    grow_h = (crop.get("growSeconds") or 1) / 3600
    return (crop.get("yieldQuota", 0) - crop.get("seedCost", 0)) / grow_h / 500000


def pick_best_crop(boot, override=None):
    """当前周等级能种的作物里，时薪最高；并列时偏好长周期（减少空转）。"""
    crops = boot.get("crops") or []
    weekly_level = (boot.get("weekly") or {}).get("level") or \
        (boot.get("profile") or {}).get("weeklyLevel") or 1
    if override:
        for c in crops:
            if c.get("id") == override:
                if c.get("minLevel", 1) > weekly_level:
                    print(f"[FAIL] {override} 需要周消费 Lv.{c.get('minLevel')}，"
                          f"当前 Lv.{weekly_level}")
                    return None
                return c
        print(f"[FAIL] 未知作物: {override}")
        return None
    plantable = [c for c in crops if c.get("minLevel", 1) <= weekly_level]
    if not plantable:
        return None
    return max(plantable, key=lambda c: (crop_rate(c), c.get("growSeconds") or 0))


def show_farm_status(boot):
    profile = boot.get("profile") or {}
    wallet = boot.get("wallet") or {}
    weekly = boot.get("weekly") or {}
    stamina = boot.get("stamina") or {}
    newapi = boot.get("newApi") or {}
    plots = boot.get("plots") or []
    seeds_items = ((boot.get("seeds") or {}).get("items")) or []
    per_unit = boot.get("quotaPerUnit") or 500000
    weekly_level = weekly.get("level") or 1

    print(f"\n{'='*55}")
    print(f"  农场: {profile.get('farmName', '?')}  "
          f"经验 Lv.{profile.get('experienceLevel')}  "
          f"周消费 Lv.{weekly_level} (解锁作物档位)")
    print(f"  体力: {stamina.get('current', '?')}/{stamina.get('max', '?')}  "
          f"(偷菜 3 点/次)")
    print(f"  主站余额: {(newapi.get('balance') or 0) / per_unit:.2f} 额度")
    print(f"  露水: {wallet.get('dew', 0)}  灵肥: {wallet.get('fertilizer', 0)}")

    # 周兑换
    red = weekly.get("redemption") or {}
    pending = wallet.get("currentWeekPendingQuotaDisplay") or 0
    cap = red.get("capDisplay")
    if cap is not None:
        print(f"  待兑换: {pending:.2f}  本周上限: {cap:.2f}  已兑: {red.get('redeemedDisplay', 0) or 0:.2f}")
        if pending > (cap or 0):
            print(f"  [!]  待兑换超过上限 {(pending - (cap or 0)):.2f}，"
                  f"需增加本周主站消耗才能换出（不跨周结转）")
    else:
        print(f"  待兑换: {pending:.2f}")
    next_lvl = weekly.get("nextUnlockLevel")
    if next_lvl:
        print(f"  下一档: Lv.{next_lvl} 需周消耗 {weekly.get('nextThresholdDisplay')} "
              f"(还差 {weekly.get('remainingDisplay', '?')})")

    # 地块
    states = {}
    for p in plots:
        s = plot_state(p)
        states[s] = states.get(s, 0) + 1
    print(f"\n  地块 {len(plots)} 块: "
          f"生长中 {states.get('GROWING', 0)}  可收 {states.get('RIPE', 0)}  "
          f"空闲 {states.get('EMPTY', 0)}  锁定 {states.get('LOCKED', 0)}")
    for p in plots:
        s = plot_state(p)
        if s == "GROWING":
            raw = p.get("matureAt") or p.get("growsUntil")
            try:
                until = int(raw)
                if until < 1e12:
                    until *= 1000
                left = (until - time.time() * 1000) / 1000
                eta = f"还有 {fmt_duration(left)}"
            except (TypeError, ValueError):
                eta = "?"
            print(f"    #{p.get('slot')}: {p.get('cropIcon', '')} "
                  f"{p.get('cropName')} ({eta})")
        elif s == "RIPE":
            print(f"    #{p.get('slot')}: {p.get('cropIcon', '')} {p.get('cropName')} [已成熟可收]")
        elif s == "EMPTY":
            print(f"    #{p.get('slot')}: 空闲")

    # 种子库存
    stock_items = [s for s in seeds_items if s.get("quantity")]
    if stock_items:
        print("\n  种子库存: " + "  ".join(
            f"{s.get('icon', '')}{s.get('name')}x{s.get('quantity')}"
            for s in stock_items))

    # 推荐作物
    best = pick_best_crop(boot)
    if best:
        print(f"\n  推荐作物: {best.get('icon')} {best.get('name')} "
              f"(Lv.{best.get('minLevel')}可种, {best.get('growSeconds', 0)//3600}h, "
              f"成本 {best.get('seedCost', 0)/per_unit:.3f} → "
              f"产 {best.get('yieldQuota', 0)/per_unit:g}, "
              f"时薪 {crop_rate(best):.3f}/h)")
    print(f"{'='*55}")


def harvest_all(boot):
    """一键收菜（批量接口），返回收获摘要文本。"""
    plots = boot.get("plots") or []
    ripe = [p for p in plots if plot_state(p) == "RIPE"]
    if not ripe:
        print("[-]  没有可收获的作物")
        return None
    status, body, _ = farm_api("/api/plots/batch/harvest", method="POST", idempotent=True)
    if status != 200 or (isinstance(body, dict) and "error" in body):
        print(f"[FAIL] 一键收菜失败: {err_text(body)}")
        return None
    count = body.get("harvestedCount") or body.get("successCount") or body.get("count") or len(ripe)
    gained = body.get("harvestedQuota") or body.get("quotaGained") or body.get("gainedQuota")
    detail = body.get("harvestedPlots") or body.get("plots") or []
    print(f"[OK] 收获 {count} 块地")
    per_unit = boot.get("quotaPerUnit") or 500000
    for d in detail:
        if isinstance(d, dict):
            amt = d.get("harvestedQuota") or d.get("quota") or 0
            print(f"    #{d.get('slot')}: {d.get('cropName', '?')} "
                  f"+{amt / per_unit:g}")
    if gained:
        print(f"    本次共 +{gained / per_unit:g} 额度（进待兑换）")
    return body


def replant_empty(boot, override_crop=None):
    """对每块空闲地补种：库存优先直接播种，无库存则买 1 粒原子播种。"""
    plots = boot.get("plots") or []
    empty = [p.get("slot") for p in plots if plot_state(p) == "EMPTY"]
    if not empty:
        print("[-]  没有空闲地块")
        return

    crop = pick_best_crop(boot, override=override_crop)
    if not crop:
        print("[FAIL] 没有可种作物")
        return
    crop_id = crop.get("id")
    per_unit = boot.get("quotaPerUnit") or 500000
    stock = 0
    for s in ((boot.get("seeds") or {}).get("items")) or []:
        if s.get("cropId") == crop_id:
            stock = s.get("quantity") or 0
    need_buy = max(0, len(empty) - stock)
    balance = (boot.get("newApi") or {}).get("balance") or 0
    cost = need_buy * crop.get("seedCost", 0)
    if need_buy > 0:
        print(f"[*]  补种 {crop.get('icon')} {crop.get('name')} x{len(empty)} "
              f"(库存 {stock}，需买 {need_buy} 粒 = {cost / per_unit:.3f} 额度)")
        if balance < cost:
            print(f"[FAIL] 主站余额不足（{balance / per_unit:.2f} < {cost / per_unit:.3f}），跳过补种")
            return
    else:
        print(f"[*]  补种 {crop.get('icon')} {crop.get('name')} x{len(empty)} (库存 {stock}，无需购买)")

    planted = 0
    for slot in empty:
        if stock > 0:
            status, body, _ = farm_api(
                f"/api/plots/{slot}/plant", method="POST",
                body={"cropId": crop_id}, idempotent=True)
            if status == 200 and not (isinstance(body, dict) and "error" in body):
                stock -= 1
                planted += 1
                print(f"[OK] #{slot} 已播种 {crop.get('name')}")
                continue
            # 播种失败时回退尝试买+播（可能库存数据过期）
            print(f"[i]  #{slot} 直接播种失败: {err_text(body)}，尝试购买补种")
        status, body, _ = farm_api(
            "/api/seeds/purchase", method="POST",
            body={"cropId": crop_id, "quantity": 1, "plantSlot": slot},
            idempotent=True)
        if status == 200 and not (isinstance(body, dict) and "error" in body):
            planted += 1
            print(f"[OK] #{slot} 购买并播种 {crop.get('name')}")
        else:
            print(f"[FAIL] #{slot} 补种失败: {err_text(body)}")
        time.sleep(0.4)
    print(f"[*]  补种完成 {planted}/{len(empty)}")


def redeem(boot):
    """把当周待兑换额度兑成主站余额（受 40% 周消耗上限约束）。"""
    wallet = boot.get("wallet") or {}
    weekly = boot.get("weekly") or {}
    pending = wallet.get("currentWeekPendingQuota")
    if not pending or pending <= 0:
        print("[-]  当前没有可兑换额度")
        return None
    if weekly.get("stale") is True or weekly.get("authoritative") is not True:
        print("[i]  周消费同步异常，兑换暂停（稍后重试）")
        return None
    per_unit = boot.get("quotaPerUnit") or 500000
    cap = (weekly.get("redemption") or {}).get("capDisplay")
    print(f"[*]  兑换待兑换额度 {wallet.get('currentWeekPendingQuotaDisplay'):.2f}"
          + (f"（上限 {cap:.2f}）" if cap is not None else ""))
    status, body, _ = farm_api("/api/rewards/redeem", method="POST", idempotent=True)
    if status != 200 or (isinstance(body, dict) and "error" in body):
        msg = err_text(body)
        if "用完" in msg or "conflict" in msg or "没有" in msg:
            print(f"[-]  {msg}")  # 本周额度已用完属正常状态
        else:
            print(f"[FAIL] 兑换失败: {msg}")
        return None
    redeemed = body.get("redeemedQuota") or body.get("quota") or body.get("amount")
    if redeemed:
        print(f"[OK] 已兑换 {redeemed / per_unit:g} 额度到主站余额")
    else:
        print(f"[OK] 兑换成功: {json.dumps(body, ensure_ascii=False)[:200]}")
    return body


def fetch_players(max_pages=8):
    """翻页拉玩家目录（服务端固定每页 40，用 limit/offset），返回可偷目标。"""
    targets = []
    offset = 0
    page = 0
    while page < max_pages:
        page += 1
        status, body, _ = farm_api(f"/api/players/?limit=40&offset={offset}")
        items = body.get("items") if isinstance(body, dict) else None
        if status != 200 or not items:
            if page == 1:
                print("[FAIL] 玩家目录获取失败，跳过偷菜")
            break
        page_targets = [p for p in items
                        if not p.get("isSelf") and (p.get("stealableCount") or 0) > 0]
        targets.extend(page_targets)
        print(f"[*]  玩家目录第 {page} 页: {len(items)} 人, 可偷 {len(page_targets)} 个目标")
        if not body.get("hasMore"):
            break
        offset = body.get("nextOffset") or (offset + 40)
    return targets


def steal_pass(boot, max_steals=20):
    """扫描玩家目录找可偷目标（成熟且未被偷满的地块），体力允许范围内偷。"""
    stamina = boot.get("stamina") or {}
    current = stamina.get("current") or 0
    cost = stamina.get("stealCost") or 3
    per_unit = boot.get("quotaPerUnit") or 500000
    if current < cost:
        print(f"[i]  体力不足（{current} < {cost}），跳过偷菜")
        return 0

    targets = fetch_players()
    if not targets:
        print("[-]  当前全服没有可偷目标（成熟作物竞争激烈，窗口极短）")
        return 0
    targets.sort(key=lambda p: -(p.get("stealableCount") or 0))

    print(f"[*]  偷菜模式：{len(targets)} 个目标，体力 {current}（约 {current // cost} 次）")
    total = 0
    for t in targets:
        if current < cost or total >= max_steals:
            break
        uid = t.get("uid") or t.get("publicId")
        s, farm, _ = farm_api(f"/api/players/{uid}/farm")
        if s != 200 or not isinstance(farm, dict):
            continue
        for p in (farm.get("plots") or []):
            if current < cost or total >= max_steals:
                break
            if plot_state(p) != "RIPE":
                continue
            if (p.get("stolenCount") or 0) >= 3:
                continue
            slot = p.get("slot")
            s2, body2, _ = farm_api(
                f"/api/players/{uid}/plots/{slot}/steal",
                method="POST", idempotent=True)
            if s2 == 200 and isinstance(body2, dict) and "error" not in body2:
                got = body2.get("stolenQuota") or body2.get("quota") or 0
                current -= cost
                total += 1
                print(f"[OK] 偷 {t.get('displayName', '?')} #{slot} "
                      f"{p.get('cropName', '?')} +{got / per_unit:g} (体力剩 {current})")
            elif s2 in (409, 400):
                break  # 该地块/玩家不可偷，换下一个
            else:
                print(f"[i]  偷取失败: {err_text(body2)[:100]}")
            time.sleep(0.5)
    if total:
        print(f"[*]  偷菜完成，共 {total} 次")
    else:
        print("[-]  本轮无可偷（可能都已被偷满）")
    return total


def cmd_farm(args):
    now = datetime.now(CST)
    print(f"\n{'─'*55}")
    print(f"  百川农场  {now.strftime('%Y-%m-%d %H:%M:%S')} CST")
    print(f"{'─'*55}")

    print("[*] 正在认证农场（优先复用缓存会话）…")
    if not farm_auth():
        return 1

    print("[*] 正在获取农场状态…")
    boot = get_bootstrap()
    if not boot:
        print("[FAIL] 获取农场状态失败", file=sys.stderr)
        return 1

    show_farm_status(boot)
    save_summary("farm", _farm_summary(boot))

    if not args.status_only:
        # 1. 收菜
        print(f"\n{'─'*55}\n  [收菜]")
        harvested = harvest_all(boot)
        if harvested:
            print("[*] 正在刷新农场状态…")
            boot = get_bootstrap() or boot
            show_farm_status(boot)

        # 2. 补种
        if not args.no_plant:
            print(f"\n{'─'*55}\n  [补种]")
            replant_empty(boot, override_crop=args.crop or None)

        # 3. 兑换
        if not args.no_redeem:
            print(f"\n{'─'*55}\n  [兑换]")
            print("[*] 正在刷新农场状态…")
            boot = get_bootstrap() or boot
            redeem(boot)

        # 4. 偷菜（显式开启才执行）
        if args.steal:
            print(f"\n{'─'*55}\n  [偷菜]")
            print("[*] 正在刷新农场状态…")
            boot = get_bootstrap() or boot
            steal_pass(boot)

        # 收尾再刷一次状态
        print("[*] 正在刷新农场状态…")
        boot = get_bootstrap() or boot
        save_summary("farm", _farm_summary(boot))
        wallet = boot.get("wallet") or {}
        red = (boot.get("weekly") or {}).get("redemption") or {}
        balance = ((boot.get("newApi") or {}).get("balance") or 0) / (boot.get("quotaPerUnit") or 500000)
        print(f"\n{'─'*55}")
        print(f"  收尾: 主站余额 {balance:.2f}  "
              f"待兑换 {wallet.get('currentWeekPendingQuotaDisplay', 0) or 0:.2f}  "
              f"本周还可兑 {red.get('remainingDisplay', 0) or 0:.2f}")

    return 0


# ═══════════════════════════════ status 只读总览 ═══════════════════════════════
def cmd_status(args):
    """只读总览：抽奖面板 + 农场状态。两部分互不影响，任一失败返回非 0。"""
    now = datetime.now(CST)
    exit_code = 0

    print(f"{'─'*55}")
    print(f"  海纳百川状态总览  {now.strftime('%Y-%m-%d %H:%M:%S')} CST")
    print(f"{'─'*55}")

    print("\n  [抽奖面板]")
    print("[*] 正在认证（优先复用缓存会话）…")
    if www_auth():
        print("[*] 正在获取抽奖面板…")
        prompt_t, completion_t = get_token_usage()
        dash = get_dashboard()
        if dash:
            show_info(dash, prompt_t, completion_t)
            save_summary("www", _www_summary(dash, prompt_t, completion_t))
        else:
            print("[FAIL] 获取抽奖 dashboard 失败", file=sys.stderr)
            exit_code = 1
    else:
        print("[WARN] 抽奖面板认证失败，跳过")
        exit_code = 1

    print(f"\n{'─'*55}\n  [农场]")
    print("[*] 正在认证农场（优先复用缓存会话）…")
    if farm_auth():
        print("[*] 正在获取农场状态…")
        boot = get_bootstrap()
        if boot:
            show_farm_status(boot)
            save_summary("farm", _farm_summary(boot))
        else:
            print("[FAIL] 获取农场状态失败", file=sys.stderr)
            exit_code = 1
    else:
        print("[WARN] 农场认证失败，跳过")
        exit_code = 1

    return exit_code


# ── 青龙通知 ──────────────────────────────────────────────────────────────
class Tee(io.TextIOBase):
    """同时输出到青龙日志并收集用于通知的文本。"""

    def __init__(self, stream):
        self.stream, self.parts = stream, []

    def write(self, text):
        self.parts.append(text)
        n = self.stream.write(text)
        if "\n" in text:
            try:
                self.stream.flush()  # 青龙等管道环境下让日志逐行实时可见
            except Exception:
                pass
        return n

    def flush(self):
        return self.stream.flush()

    def getvalue(self):
        return "".join(self.parts)


def send_ql_notify(title, content):
    """调用青龙 sendNotify.js，复用面板中已经配置的推送渠道。"""
    if not NOTIFY_ENABLED:
        print("[i] 青龙通知已通过 HAINA_NOTIFY 关闭")
        return True
    if not os.path.isfile(NOTIFY_JS):
        print(f"[WARN] 未找到青龙通知脚本: {NOTIFY_JS}", file=sys.stderr)
        return False

    runner = (
        "const {sendNotify}=require(process.argv[1]);"
        "sendNotify(process.argv[2],process.argv[3])"
        ".then(()=>process.exit(0))"
        ".catch(e=>{console.error(e&&e.message?e.message:String(e));process.exit(1)})"
    )
    env = os.environ.copy()
    ql_modules = "/app/user-packages/node/lib/node_modules/@whyour/qinglong/node_modules"
    env["NODE_PATH"] = os.pathsep.join(
        x for x in (ql_modules, env.get("NODE_PATH", "")) if x)
    try:
        result = subprocess.run(
            ["node", "-e", runner, NOTIFY_JS, title, content],
            text=True, capture_output=True, timeout=90, check=False, env=env)
    except Exception as exc:
        print(f"[WARN] 青龙通知调用失败: {exc}", file=sys.stderr)
        return False
    if result.returncode:
        detail = (result.stderr or result.stdout or "未知错误").strip()
        print(f"[WARN] 青龙通知发送失败: {detail[:500]}", file=sys.stderr)
        return False
    detail = "\n".join(
        line for line in (result.stdout or "").splitlines() if line.strip()
    ).strip()
    if detail:
        print(f"[i] 青龙通知明细: {detail[:800]}")
    else:
        print("[WARN] sendNotify.js 未报告任何渠道结果；请检查青龙通知配置", file=sys.stderr)
        return False
    failure_words = ("失败", "异常", "错误", "未配置", "不能为空")
    success_words = ("成功", "完成")
    if any(word in detail for word in failure_words) and not any(
            word in detail for word in success_words):
        print("[WARN] 青龙通知渠道未报告发送成功", file=sys.stderr)
        return False
    print("[OK] 青龙通知调用完成")
    return True


# ── 入口 ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="haina",
        description="海纳百川自动化（签到 / 抽奖 / 农场三合一）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "signin", help="签到：查消耗 → 钱包签到 → 领取所有可领福利（可 --draw 顺手抽奖）")
    p.add_argument("--draw", action="store_true",
                   default=os.environ.get("HAINA_SIGNIN_DRAW", "") == "1",
                   help="签到领完福利后自动抽奖（次数遵循 --draw-all 设置）")
    p.add_argument("--draw-all", action="store_true",
                   default=os.environ.get("HAINA_DRAW_ALL", "") == "1",
                   help="签到后自动抽奖时抽光全部次数（默认只抽 1 次）")

    p = sub.add_parser("draw", help="抽奖：默认只抽 1 次，--all 抽光全部")
    p.add_argument("--status-only", action="store_true", help="仅查看状态，不抽奖")
    p.add_argument("--all", action="store_true",
                   default=os.environ.get("HAINA_DRAW_ALL", "") == "1",
                   help="抽光全部可抽次数（默认只抽 1 次）")

    p = sub.add_parser("farm", help="农场：收菜 → 补种 → 兑换")
    p.add_argument("--status-only", action="store_true", help="仅查看状态，不做任何写操作")
    p.add_argument("--steal", action="store_true", help="额外执行偷菜（体力允许范围内）")
    p.add_argument("--no-plant", action="store_true", help="收菜后不自动补种")
    p.add_argument("--no-redeem", action="store_true", help="不自动兑换待兑换额度")
    p.add_argument("--crop", default=os.environ.get("HAINA_FARM_CROP", ""),
                   help="指定补种作物 cropId（默认自动选时薪最高的）")

    sub.add_parser("status", help="只读总览：抽奖面板 + 农场状态，不做任何写操作")

    args = parser.parse_args()
    if args.command == "signin":
        return cmd_signin(args)
    if args.command == "draw":
        return cmd_draw(args)
    if args.command == "farm":
        return cmd_farm(args)
    return cmd_status(args)


if __name__ == "__main__":
    NOTIFY_TITLES = {
        "signin": "海纳百川签到",
        "draw": "海纳百川抽奖",
        "farm": "百川农场",
        "status": "海纳百川状态",
    }
    command = "signin"
    if len(sys.argv) > 1 and sys.argv[1] in NOTIFY_TITLES:
        command = sys.argv[1]

    original_stdout, original_stderr = sys.stdout, sys.stderr
    captured_stdout, captured_stderr = Tee(original_stdout), Tee(original_stderr)
    sys.stdout, sys.stderr = captured_stdout, captured_stderr
    exit_code = 1
    try:
        exit_code = main()
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        print(f"[FAIL] 脚本发生未处理异常: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        exit_code = 1
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr

    content = captured_stdout.getvalue().strip()
    errors = captured_stderr.getvalue().strip()
    if errors:
        content = f"{content}\n\n错误信息:\n{errors}".strip()
    send_ql_notify(
        f"{NOTIFY_TITLES[command]} · {'成功' if exit_code == 0 else '失败'}",
        content or "任务无输出")
    raise SystemExit(exit_code)
