# -*- coding: utf-8 -*-
"""
WorkBuddy 签到助手 - 单文件 EXE 版 (v1.1)
功能：全机多账号签到 / 开机自启签到 / 每日多时间点签到 / 签到日志
用法：双击打开控制台（GUI）；--checkin 无头签到；--apply 按 config.json 注册计划任务
"""
import sys, os, json, time, ctypes, argparse, datetime, subprocess, tempfile, re, base64, hashlib, shutil

APP_NAME   = "WorkBuddy-AutoCheckin"
MIGRATION_TASKS = ["WorkBuddy-AutoCheckin-User1", "WorkBuddy-AutoCheckin-User2"]   # 修复期临时任务，注册主任务后自动清理
APP_TITLE  = "WorkBuddy 签到助手"
APP_VER    = "1.4.0"
APP_ID     = "WorkBuddy.CheckinAssistant"   # 任务栏图标分组
TOOL_DIR   = r"C:\ProgramData\WorkBuddyCheckin"
USER_TOOL_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "WorkBuddyCheckin")
LOG_DIR    = os.path.join(TOOL_DIR, "logs")
CONFIG     = os.path.join(TOOL_DIR, "config.json")
LASTRUN    = os.path.join(TOOL_DIR, "last-run.json")
ACCOUNTS   = os.path.join(TOOL_DIR, "accounts.json")
SETUP_RES  = os.path.join(TOOL_DIR, "setup-result.json")
API_BASE   = "https://copilot.tencent.com/v2"
EP_STATUS  = "/billing/meter/checkin-activity-status"   # 新版状态接口（旧 checkin-status 已废弃）
EP_CHECKIN = "/billing/meter/daily-checkin"
EP_REFRESH = "/auth/token/refresh"
SKIP_USERS = {"Public", "Default", "Default User", "All Users"}

os.makedirs(LOG_DIR, exist_ok=True)

def ensure_tool_dir_writable():
    """ProgramData 下的工具目录常由 SYSTEM/管理员先创建，普通用户会写失败。
    可写性探测失败时尝试用 icacls 给 Users 组补修改权限（需管理员/SYSTEM，失败静默）。"""
    def probe():
        try:
            t = os.path.join(LOG_DIR, ".wtest")
            with open(t, "w") as f:
                f.write("ok")
            os.remove(t)
            return True
        except Exception:
            return False
    if probe():
        return
    try:
        subprocess.run(["icacls", TOOL_DIR, "/grant", "*S-1-5-32-545:(OI)(CI)M", "/T", "/C", "/Q"],
                       capture_output=True, timeout=60)
    except Exception:
        pass

def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

# ---------------- WorkBuddy 凭据字段解密（$wbEncrypted） ----------------
# 新版 WorkBuddy 把 auth 文件中的 nickname/phoneNumber/accessToken/refreshToken
# 加密为 {"$wbEncrypted":1,"envelope":<base64 JSON>}，算法为 AES-256-GCM：
#   key    = sha256(atRestSecretKey字符串utf8)          （32B）
#   keyId  = sha256(key).hex[:16]
#   AAD    = b"WB-AAD\0" + 0x01 + lp("WBEV1") + lp("sym-v1") + u32be(1) + lp(keyId) + 0x02 + 0x00 + 0x00
# atRestSecretKey 由 WorkBuddy（定制 Electron）内置 binding 提供，
# 这里用 ELECTRON_RUN_AS_NODE 启动 WorkBuddy.exe 调 loggerGet() 获取，不落盘。
_WB_AAD_PREFIX = b"WB-AAD\0"
_WB_KEY_CACHE = {"key": None, "key_id": None, "tried": False}
_WB_JS = "process.stdout.write(process._linkedBinding('electron_browser_workbuddy_storage').loggerGet())"

def find_workbuddy_exe():
    cands = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WorkBuddy", "WorkBuddy.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "WorkBuddy", "WorkBuddy.exe"),
        r"D:\workbuddy\workbuddy\WorkBuddy.exe",
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None

def get_wb_key():
    """返回 (aes_key_bytes, keyId)；拿不到返回 (None, None)。"""
    if _WB_KEY_CACHE["tried"]:
        return _WB_KEY_CACHE["key"], _WB_KEY_CACHE["key_id"]
    _WB_KEY_CACHE["tried"] = True
    exe = find_workbuddy_exe()
    if not exe:
        log("未找到 WorkBuddy.exe，无法解密新版加密凭据", "WARN")
        return None, None
    try:
        env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
        r = subprocess.run([exe, "-e", _WB_JS], capture_output=True, timeout=60, env=env)
        payload = json.loads((r.stdout or b"").decode("utf-8", "replace"))
        secret = payload.get("atRestSecretKey")
        if not secret:
            raise ValueError("no atRestSecretKey")
        key = hashlib.sha256(secret.encode("utf-8")).digest()
        key_id = hashlib.sha256(key).hexdigest()[:16]
        _WB_KEY_CACHE["key"], _WB_KEY_CACHE["key_id"] = key, key_id
        return key, key_id
    except Exception as e:
        log("获取 WorkBuddy 解密密钥失败: %s" % e, "WARN")
        return None, None

def _wb_b64d(s):
    return base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))

def _wb_field_aad(key_id):
    def lp(s):
        b = s.encode("utf-8")
        return len(b).to_bytes(4, "big") + b
    return (_WB_AAD_PREFIX + b"\x01" + lp("WBEV1") + lp("sym-v1")
            + (1).to_bytes(4, "big") + lp(key_id) + b"\x02\x00\x00")

def wb_unwrap(value):
    """把可能被 $wbEncrypted 加密的字段还原为明文字符串；明文原样返回；失败返回 None。"""
    if isinstance(value, str):
        return value
    if not (isinstance(value, dict) and value.get("$wbEncrypted") == 1):
        return None
    key, _ = get_wb_key()
    if not key:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        e = json.loads(_wb_b64d(value["envelope"]))
        pt = AESGCM(key).decrypt(_wb_b64d(e["nonce"]),
                                 _wb_b64d(e["ciphertext"]) + _wb_b64d(e["authTag"]),
                                 _wb_field_aad(e["keyId"]))
        return pt.decode("utf-8")
    except Exception:
        return None

def wb_wrap(new_value, old_field):
    """回写 token 时保持原字段形态：原来是 $wbEncrypted 就重新加密，否则明文。"""
    if not (isinstance(old_field, dict) and old_field.get("$wbEncrypted") == 1):
        return new_value
    _, key_id = get_wb_key()
    key = _WB_KEY_CACHE["key"]
    if not key:
        return new_value
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        a = AESGCM(key)
        ct = a.encrypt(nonce, new_value.encode("utf-8"), _wb_field_aad(key_id))
        env = {"suite": 1, "keyId": key_id,
               "nonce": base64.b64encode(nonce).decode(),
               "authTag": base64.b64encode(ct[-16:]).decode(),
               "ciphertext": base64.b64encode(ct[:-16]).decode()}
        return {"$wbEncrypted": 1,
                "envelope": base64.b64encode(json.dumps(env, separators=(",", ":")).encode("utf-8")).decode()}
    except Exception:
        return new_value

# ---------------- 日志 ----------------
def log(msg, lvl="INFO"):
    line = "[%s] [%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), lvl, msg)
    print(line, flush=True)
    try:
        lf = os.path.join(LOG_DIR, "checkin-%s.log" % datetime.date.today().isoformat())
        with open(lf, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------- 配置 ----------------
def load_config():
    try:
        with open(CONFIG, encoding="utf-8-sig") as f:
            c = json.load(f)
        return {"EnableBoot": bool(c.get("EnableBoot", True)),
                "Times": [str(t) for t in c.get("Times", ["12:00"])],
                "IncludeBackups": bool(c.get("IncludeBackups", False))}
    except Exception:
        return {"EnableBoot": True, "Times": ["12:00"], "IncludeBackups": False}

def save_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

# ---------------- 账号扫描 ----------------
def find_accounts(include_backups=False):
    """扫描所有 Windows 用户的 WorkBuddy 凭据，默认只取每个用户的当前账号"""
    accounts, seen = [], set()
    users_root = r"C:\Users"
    try:
        win_users = [d for d in os.listdir(users_root) if os.path.isdir(os.path.join(users_root, d))]
    except Exception:
        return accounts
    for wu in win_users:
        if wu in SKIP_USERS:
            continue
        auth_dir = os.path.join(users_root, wu, r"AppData\Local\CodeBuddyExtension\Data\Public\auth")
        if not os.path.isdir(auth_dir):
            continue
        try:
            files = [f for f in os.listdir(auth_dir)
                     if f.startswith("workbuddy-desktop") and f.endswith(".info")]
        except Exception:
            continue
        files.sort(key=lambda f: -os.path.getmtime(os.path.join(auth_dir, f)))
        for fn in files:
            if not include_backups and fn != "workbuddy-desktop.info":
                continue
            path = os.path.join(auth_dir, fn)
            try:
                with open(path, encoding="utf-8-sig") as f:
                    data = json.load(f)
                uid = data.get("account", {}).get("uid")
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                nick = (wb_unwrap(data["account"].get("nickname"))
                        or wb_unwrap(data["account"].get("phoneNumber")) or uid[:8])
                auth = data.get("auth", {})
                tok = wb_unwrap(auth.get("accessToken"))
                ref = wb_unwrap(auth.get("refreshToken"))
                accounts.append({
                    "name": "%s/%s" % (wu, nick), "win_user": wu, "nick": nick,
                    "uid": uid, "backup": fn != "workbuddy-desktop.info",
                    "file": path, "data": data, "tok": tok, "ref": ref,
                })
            except Exception:
                continue
    return accounts

# ---------------- API ----------------
import requests
requests.packages.urllib3.disable_warnings()

def api_headers(acc):
    return {"Content-Type": "application/json", "Accept": "application/json",
            "Authorization": "Bearer " + (acc.get("tok") or ""),
            "X-User-Id": acc["uid"], "X-Domain": acc["data"]["auth"].get("domain", "www.workbuddy.cn")}

def wait_network(max_minutes=5):
    import socket
    deadline = time.time() + max_minutes * 60
    n = 0
    while time.time() < deadline:
        try:
            s = socket.create_connection(("copilot.tencent.com", 443), timeout=5)
            s.close()
            return True
        except Exception:
            if n == 0:
                log("等待网络就绪 (最长 %d 分钟)..." % max_minutes)
            n += 1
            time.sleep(10)
    log("网络等待超时，继续执行", "WARN")
    return False

def refresh_token(acc):
    auth = acc["data"].get("auth", {})
    rt = acc.get("ref") or wb_unwrap(auth.get("refreshToken"))
    if not rt:
        log("[%s] 无 refreshToken（凭据可能已加密且无法解密）" % acc["name"], "ERROR")
        return False
    h = {"Content-Type": "application/json", "X-User-Id": acc["uid"],
         "X-Domain": auth.get("domain", "www.workbuddy.cn"),
         "X-Refresh-Token": rt, "X-Auth-Refresh-Source": "desktop"}
    try:
        r = requests.post(API_BASE + EP_REFRESH, headers=h, json={}, timeout=30, verify=False)
        j = r.json()
        if j.get("code") == 0 and j.get("data"):
            d = j["data"]
            for k in ("accessToken", "refreshToken"):
                if k in d:
                    auth[k] = wb_wrap(d[k], auth.get(k))
                    acc["tok" if k == "accessToken" else "ref"] = d[k]
            for k in ("expiresAt", "refreshExpiresAt"):
                if k in d:
                    auth[k] = d[k]
            auth["lastRefreshTime"] = int(time.time() * 1000)
            with open(acc["file"], "w", encoding="utf-8") as f:
                json.dump(acc["data"], f, ensure_ascii=False)
            log("[%s] Token 已刷新" % acc["name"])
            return True
        log("[%s] Token 刷新被拒绝: code=%s" % (acc["name"], j.get("code")), "ERROR")
    except Exception as e:
        log("[%s] Token 刷新失败: %s" % (acc["name"], e), "ERROR")
    return False

def ensure_token(acc):
    exp = acc["data"].get("auth", {}).get("expiresAt", 0)
    if time.time() * 1000 < exp - 300000:
        return True
    return refresh_token(acc)

def get_status(acc):
    try:
        r = requests.post(API_BASE + EP_STATUS, headers=api_headers(acc), json={}, timeout=30, verify=False)
        j = r.json()
        if j.get("code") == 0:
            return j.get("data")
    except Exception as e:
        log("[%s] 状态查询失败: %s" % (acc["name"], e), "ERROR")
    return None

def claim_checkin(acc):
    try:
        r = requests.post(API_BASE + EP_CHECKIN, headers=api_headers(acc), json={}, timeout=30, verify=False)
        j = r.json()
        if j.get("code") == 0 and j.get("data"):
            d = j["data"]
            return {"ok": True, "msg": "签到成功 +%s积分 连续%s天" % (d.get("credit"), d.get("streak_days"))}
        return {"ok": False, "msg": j.get("msg") or "未知错误", "code": j.get("code")}
    except Exception as e:
        body = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                body = e.response.json().get("msg", "")
            except Exception:
                pass
        return {"ok": False, "msg": body or str(e)}

def process_account(acc, retry=3, delay=30):
    name = acc["name"]
    log("===== %s =====" % name)
    if not acc.get("tok") and not acc.get("ref"):
        log("[%s] 凭据已加密且无法解密（请确认 WorkBuddy 安装完整）" % name, "ERROR")
        return {"user": name, "ok": False, "msg": "凭据加密无法解密"}
    if not ensure_token(acc):
        return {"user": name, "ok": False, "msg": "Token 刷新失败"}
    st = get_status(acc)
    if st is not None:
        log("[%s] active=%s today_checked=%s streak=%sd" %
            (name, st.get("active"), st.get("today_checked_in"), st.get("streak_days")))
        if not st.get("active"):
            return {"user": name, "ok": False, "msg": "签到活动未开启"}
        if st.get("today_checked_in"):
            return {"user": name, "ok": True, "msg": "今日已签 连续%s天" % st.get("streak_days")}
    for i in range(1, retry + 1):
        log("[%s] 第 %d/%d 次签到..." % (name, i, retry))
        if i > 1:
            time.sleep(delay)
            if not ensure_token(acc):
                continue
        r = claim_checkin(acc)
        if r["ok"]:
            log("[%s] %s" % (name, r["msg"]), "SUCCESS")
            return {"user": name, "ok": True, "msg": r["msg"]}
        if r.get("code") == 409001 or re.search(r"already|已签", r.get("msg") or "", re.I):
            return {"user": name, "ok": True, "msg": "今日已签到"}
    st2 = get_status(acc)
    if st2 and st2.get("today_checked_in"):
        return {"user": name, "ok": True, "msg": "今日已签 连续%s天" % st2.get("streak_days")}
    return {"user": name, "ok": False, "msg": "签到失败（已重试）"}

# ---------------- 运行入口 ----------------
def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def save_json(path, obj):
    def _write(p):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    ok = False
    try:
        _write(path)
        ok = True
    except Exception as e:
        log("写入 %s 失败: %s" % (path, e), "ERROR")
    if not ok:   # ProgramData 不可写（普通用户）时镜像到用户目录，读取侧兜底
        try:
            os.makedirs(USER_TOOL_DIR, exist_ok=True)
            _write(os.path.join(USER_TOOL_DIR, os.path.basename(path)))
        except Exception:
            pass

def read_state(fname):
    """读取 ProgramData 与用户镜像中较新的状态文件（accounts.json / last-run.json 等）"""
    best, best_m = None, -1.0
    for p in (os.path.join(TOOL_DIR, fname), os.path.join(USER_TOOL_DIR, fname)):
        try:
            m = os.path.getmtime(p)
            if m > best_m:
                with open(p, encoding="utf-8-sig") as f:
                    best = json.load(f)
                best_m = m
        except Exception:
            continue
    return best

def save_accounts(accs):
    save_json(ACCOUNTS, [{"name": a["name"], "win_user": a["win_user"], "nick": a["nick"],
                          "uid": a["uid"][:8] + "...", "backup": a["backup"]} for a in accs])

def write_setup_result(ok, msg):
    save_json(SETUP_RES, {"ok": ok, "msg": msg, "time": now_str()})

def run_checkin(wait_net_minutes=5):
    log("---------- WorkBuddy auto-checkin started ----------")
    wait_network(wait_net_minutes)
    cfg = load_config()
    accs = find_accounts(cfg["IncludeBackups"])
    save_accounts(accs)
    if not accs:
        log("未发现任何 WorkBuddy 账号", "ERROR")
        save_json(LASTRUN, {"time": now_str(), "action": "checkin", "results": []})
        return 1
    log("发现 %d 个账号: %s" % (len(accs), ", ".join(a["name"] for a in accs)))
    results = [process_account(a) for a in accs]
    save_json(LASTRUN, {"time": now_str(), "action": "checkin", "results": results})
    ok = sum(1 for r in results if r["ok"])
    log("完成: %d/%d 个账号已签到" % (ok, len(results)))
    return 0

# ---------------- 计划任务 ----------------
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
 <RegistrationInfo><Description>WorkBuddy 自动签到</Description></RegistrationInfo>
 <Triggers>
{triggers}
 </Triggers>
 <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
 <Settings>
  <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
  <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
  <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
  <AllowStartOnDemand>true</AllowStartOnDemand>
  <StartWhenAvailable>true</StartWhenAvailable>
  <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
  <Enabled>true</Enabled>
 </Settings>
 <Actions Context="Author"><Exec><Command>"{exe}"</Command><Arguments>--checkin</Arguments></Exec></Actions>
</Task>"""

def build_triggers_xml(cfg):
    parts = []
    if cfg["EnableBoot"]:
        parts.append('  <BootTrigger><Enabled>true</Enabled></BootTrigger>')
        parts.append('  <LogonTrigger><Enabled>true</Enabled></LogonTrigger>')
    for t in cfg["Times"]:
        if re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", t.strip()):
            hh, mm = t.strip().split(":")
            parts.append(
                '  <CalendarTrigger><StartBoundary>2026-01-01T%02d:%s:00</StartBoundary><Enabled>true</Enabled>'
                '<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>' % (int(hh), mm))
    return "\n".join(parts)

def task_exe_path():
    """计划任务使用固定路径的 EXE：必要时把自身复制到 ProgramData（防止源 EXE 被移动后任务失效）"""
    dst = os.path.join(TOOL_DIR, "WorkBuddyCheckin.exe")
    if not getattr(sys, "frozen", False):
        return os.path.abspath(__file__)   # 源码运行时计划任务直接指向本脚本的解释器路径无效，返回自身供报错提示
    try:
        cur = os.path.abspath(sys.executable)
        if cur.lower() != dst.lower():
            if not os.path.exists(dst) or os.path.getmtime(cur) > os.path.getmtime(dst):
                shutil.copyfile(cur, dst)
        return dst
    except Exception:
        return sys.executable

def apply_tasks(cfg=None):
    """按 config.json 注册/更新/删除计划任务（需管理员）；cfg 传入时先落盘（供普通用户提权保存设置）"""
    if cfg is not None:
        try:
            save_config(cfg)
        except Exception as e:
            write_setup_result(False, "配置写入失败: %s" % e)
            return 1
    cfg = cfg or load_config()
    trig = build_triggers_xml(cfg)
    for tn in MIGRATION_TASKS:   # 清理修复期临时任务（存在则删，不报错）
        subprocess.run(["schtasks", "/delete", "/tn", tn, "/f"], capture_output=True)
    try:
        if not trig:
            subprocess.run(["schtasks", "/delete", "/tn", APP_NAME, "/f"], capture_output=True)
            write_setup_result(True, "已关闭所有自动签到（计划任务已删除）")
            return 0
        exe = task_exe_path()
        xml = TASK_XML.format(triggers=trig, exe=exe)
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
            f.write(xml)
            tmp = f.name
        r = subprocess.run(["schtasks", "/create", "/tn", APP_NAME, "/xml", tmp, "/f"],
                           capture_output=True, text=True, encoding="gbk", errors="replace")
        os.unlink(tmp)
        if r.returncode != 0:
            write_setup_result(False, "计划任务注册失败: " + (r.stdout + r.stderr).strip())
            return 1
        nxt = ""
        q = subprocess.run(["schtasks", "/query", "/tn", APP_NAME, "/v", "/fo", "list"],
                           capture_output=True, text=True, encoding="gbk", errors="replace")
        m = re.search(r"Next Run Time:\s*(.+)", q.stdout) or re.search(r"下次运行时间:\s*(.+)", q.stdout)
        if m:
            nxt = "，下次运行: " + m.group(1).strip()
        write_setup_result(True, "计划任务已更新" + nxt)
        return 0
    except Exception as e:
        write_setup_result(False, "计划任务配置异常: %s" % e)
        return 1

def query_task_exists():
    """标准用户查 SYSTEM 任务：'拒绝访问' 也代表存在"""
    r = subprocess.run(["schtasks", "/query", "/tn", APP_NAME],
                       capture_output=True, text=True, encoding="gbk", errors="replace")
    out = r.stdout + r.stderr
    if r.returncode == 0:
        return True, "已启用"
    if re.search(r"拒绝访问|denied", out, re.I):
        return True, "已启用（SYSTEM 任务）"
    return False, "未启用"

# ---------------- 提权 ----------------
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def run_elevated(args):
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 0)
    return rc > 32

def wait_result(path, timeout=180):
    old = os.path.getmtime(path) if os.path.exists(path) else 0
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(path) and os.path.getmtime(path) > old:
            try:
                with open(path, encoding="utf-8-sig") as f:
                    return json.load(f)
            except Exception:
                pass
        time.sleep(1)
    return None

# ============================================================
# GUI - 设计系统
# ============================================================
def run_gui():
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                                   QLabel, QPushButton, QGridLayout, QLineEdit, QPlainTextEdit,
                                   QFrame, QMessageBox, QAbstractButton, QProgressBar,
                                   QGraphicsDropShadowEffect)
    from PySide6.QtCore import Qt, QThread, Signal, QPropertyAnimation, Property
    from PySide6.QtGui import QFont, QIcon, QPainter, QColor, QPainterPath

    C = dict(BG="#F5F6F8", CARD="#FFFFFF", ROW="#F7F8FA", LINE="#E5E6EB",
             TXT="#1F2329", SUB="#646A73", FAINT="#8F959E",
             ACCENT="#3370FF", ACCENT_H="#2860E1", ACCENT_SOFT="#EEF3FF",
             GREEN="#2BA245", RED="#E5484D", ORANGE="#D97706", OFF="#D8DBE0")

    QSS = """
        QMainWindow#root { background: %(BG)s; }
        QWidget { color: %(TXT)s; font-family: 'Segoe UI', 'Microsoft YaHei'; font-size: 13px; }
        QLabel#h2 { font-size: 14px; font-weight: 700; }
        QLabel#sub { color: %(SUB)s; font-size: 11.5px; }
        QLabel#faint { color: %(FAINT)s; font-size: 11px; }
        QLabel#badge { font-size: 11px; padding: 3px 10px; border-radius: 9px; }
        QFrame#card { background: %(CARD)s; border: none; border-radius: 14px; }
        QFrame#accRow { background: %(ROW)s; border: none; border-radius: 10px; }
        QPushButton { background: %(CARD)s; border: 1px solid #DEE0E3; border-radius: 9px;
                      padding: 7px 16px; color: %(TXT)s; font-size: 12.5px; }
        QPushButton:hover { background: #F0F1F5; border-color: #C9CDD4; }
        QPushButton:pressed { background: #E8EAEF; }
        QPushButton:disabled { color: #B5BAC3; background: %(BG)s; border-color: %(LINE)s; }
        QPushButton#cta { background: %(ACCENT)s; color: #FFFFFF; border: none;
                          border-radius: 11px; font-size: 15px; font-weight: 700; padding: 12px; }
        QPushButton#cta:hover { background: #4B82FF; }
        QPushButton#cta:pressed { background: %(ACCENT_H)s; }
        QPushButton#cta:disabled { background: #BCCCFF; color: #FFFFFF; }
        QPushButton#save { background: %(CARD)s; border: 1px solid %(ACCENT)s; border-radius: 9px;
                           color: %(ACCENT)s; font-weight: 600; }
        QPushButton#save:hover { background: %(ACCENT_SOFT)s; border-color: %(ACCENT)s; }
        QPushButton#save:pressed { background: #E0EAFF; }
        QPushButton#tiny { background: transparent; border: none; border-radius: 8px;
                           padding: 5px 10px; font-size: 11.5px; color: %(SUB)s; }
        QPushButton#tiny:hover { background: #EDEEF2; color: %(TXT)s; }
        QPushButton#chip { background: %(ACCENT_SOFT)s; border: none; border-radius: 13px;
                           padding: 6px 14px; color: %(ACCENT)s; font-size: 12px; }
        QPushButton#chip:hover { background: #E0EAFF; }
        QLineEdit { background: %(CARD)s; border: 1px solid #DEE0E3; border-radius: 9px;
                    padding: 7px 10px; color: %(TXT)s; selection-background-color: #D6E4FF; }
        QLineEdit:focus { border: 1px solid %(ACCENT)s; }
        QPlainTextEdit { background: %(ROW)s; border: none; border-radius: 10px;
                         font-family: Consolas, 'Microsoft YaHei'; font-size: 11px;
                         color: %(SUB)s; padding: 10px; selection-background-color: #D6E4FF; }
        QProgressBar { background: #E5E6EB; border: none; max-height: 3px; }
        QProgressBar::chunk { background: %(ACCENT)s; }
        QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 2px; }
        QScrollBar::handle:vertical { background: #C9CDD4; border-radius: 5px; min-height: 30px; }
        QScrollBar::handle:vertical:hover { background: #A8ABB2; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QToolTip { background: %(CARD)s; color: %(TXT)s; border: 1px solid %(LINE)s;
                   padding: 5px 9px; border-radius: 6px; }
    """ % C

    class Toggle(QAbstractButton):
        """iOS 风格滑动开关"""
        def __init__(self, checked=False):
            super().__init__()
            self.setCheckable(True)
            self.setChecked(checked)
            self.setCursor(Qt.PointingHandCursor)
            self.setFixedSize(44, 25)
            self._pos = 1.0 if checked else 0.0
            self._anim = QPropertyAnimation(self, b"knob", self)
            self._anim.setDuration(140)
            self.toggled.connect(self._animate)

        def _animate(self, on):
            self._anim.stop()
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(1.0 if on else 0.0)
            self._anim.start()

        def getKnob(self): return self._pos
        def setKnob(self, v): self._pos = v; self.update()
        knob = Property(float, getKnob, setKnob)

        def paintEvent(self, e):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            path = QPainterPath()
            path.addRoundedRect(1, 1, w - 2, h - 2, (h - 2) / 2, (h - 2) / 2)
            off = QColor(C["OFF"]); on = QColor(C["ACCENT"])
            t = self._pos
            col = QColor(int(off.red() + (on.red() - off.red()) * t),
                         int(off.green() + (on.green() - off.green()) * t),
                         int(off.blue() + (on.blue() - off.blue()) * t))
            p.fillPath(path, col)
            d = h - 6
            x = 3 + self._pos * (w - 6 - d)
            p.setBrush(QColor("#FFFFFF"))
            p.setPen(Qt.NoPen)
            p.drawEllipse(int(x), 3, d, d)
            p.end()

    class Worker(QThread):
        done = Signal(object)
        def __init__(self, fn):
            super().__init__()
            self.fn = fn
        def run(self):
            try:
                self.done.emit(self.fn())
            except Exception as e:
                self.done.emit(e)

    def rgba(color, alpha):
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        return "rgba(%d,%d,%d,%s)" % (r, g, b, alpha)

    def badge(text, color):
        lb = QLabel(text)
        lb.setObjectName("badge")
        lb.setStyleSheet("color: %s; background: %s;" % (color, rgba(color, "0.10")))
        return lb

    def make_card(title=None):
        f = QFrame()
        f.setObjectName("card")
        sh = QGraphicsDropShadowEffect()
        sh.setBlurRadius(28)
        sh.setOffset(0, 4)
        sh.setColor(QColor(31, 35, 41, 18))
        f.setGraphicsEffect(sh)
        lay = QVBoxLayout(f)
        lay.setContentsMargins(22, 18, 22, 20)
        lay.setSpacing(12)
        if title:
            lb = QLabel(title)
            lb.setObjectName("h2")
            lay.addWidget(lb)
        return f, lay

    def toggle_row(text, toggle):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        lb = QLabel(text)
        h.addWidget(lb)
        h.addStretch(1)
        h.addWidget(toggle)
        return w

    class Win(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setObjectName("root")
            self.setWindowTitle("%s v%s" % (APP_TITLE, APP_VER))
            self.setWindowIcon(QIcon(resource_path("app.ico")))
            self.setMinimumSize(880, 600)
            self.resize(940, 680)
            self.setStyleSheet(QSS)

            root = QWidget()
            self.setCentralWidget(root)
            vbox = QVBoxLayout(root)
            vbox.setContentsMargins(0, 0, 0, 0)
            vbox.setSpacing(0)

            # ---------- 进度条 ----------
            self.prog = QProgressBar()
            self.prog.setRange(0, 1)
            self.prog.setValue(0)
            self.prog.setTextVisible(False)
            self.prog.setFixedHeight(3)
            vbox.addWidget(self.prog)

            # ---------- 主体双列 ----------
            body = QWidget()
            bh = QHBoxLayout(body)
            bh.setContentsMargins(28, 22, 28, 18)
            bh.setSpacing(20)
            left = QVBoxLayout()
            left.setSpacing(18)
            right = QVBoxLayout()
            right.setSpacing(18)

            # == 左列：签到中心 ==
            cardSign, laySign = make_card()
            topSign = QHBoxLayout()
            hSign = QLabel("签到中心")
            hSign.setObjectName("h2")
            topSign.addWidget(hSign)
            self.lastMeta = QLabel("")
            self.lastMeta.setObjectName("faint")
            topSign.addStretch(1)
            topSign.addWidget(self.lastMeta)
            laySign.addLayout(topSign)
            self.accBox = QVBoxLayout()
            self.accBox.setSpacing(10)
            accWrap = QWidget()
            accWrap.setLayout(self.accBox)
            laySign.addWidget(accWrap, 1)
            self.btnNow = QPushButton("立即签到")
            self.btnNow.setObjectName("cta")
            self.btnNow.setFixedHeight(52)
            self.btnNow.setCursor(Qt.PointingHandCursor)
            laySign.addWidget(self.btnNow)
            self.hint = QLabel("为全部账号签到（会请求一次管理员授权）")
            self.hint.setObjectName("faint")
            self.hint.setAlignment(Qt.AlignCenter)
            laySign.addWidget(self.hint)
            left.addWidget(cardSign, 1)

            # == 右列：自动签到设置 ==
            cardCfg, layCfg = make_card("自动签到")
            layCfg.setSpacing(11)
            self.tgBoot = Toggle()
            self.tgBackups = Toggle()
            layCfg.addWidget(toggle_row("开机 / 登录时自动签到一次", self.tgBoot))
            layCfg.addWidget(toggle_row("同时签到历史切换过的账号", self.tgBackups))
            subT = QLabel("每日签到时间")
            subT.setObjectName("sub")
            layCfg.addWidget(subT)
            self.chipGrid = QGridLayout()
            self.chipGrid.setSpacing(8)
            self.chipGrid.setColumnStretch(4, 1)
            layCfg.addLayout(self.chipGrid)
            self.times = []
            addRow = QHBoxLayout()
            addRow.setSpacing(8)
            self.timeEdit = QLineEdit("12:00")
            self.timeEdit.setFixedWidth(96)
            self.timeEdit.setPlaceholderText("HH:mm")
            bAdd = QPushButton("＋ 添加")
            bAdd.setCursor(Qt.PointingHandCursor)
            addRow.addWidget(self.timeEdit)
            addRow.addWidget(bAdd)
            addRow.addStretch(1)
            layCfg.addLayout(addRow)
            saveRow = QHBoxLayout()
            self.btnSave = QPushButton("保存设置")
            self.btnSave.setObjectName("save")
            self.btnSave.setFixedSize(128, 40)
            self.btnSave.setCursor(Qt.PointingHandCursor)
            saveRow.addStretch(1)
            saveRow.addWidget(self.btnSave)
            layCfg.addLayout(saveRow)
            right.addWidget(cardCfg)

            # == 右列：今日日志 ==
            cardLog, layLog = make_card()
            topLog = QHBoxLayout()
            topLog.setSpacing(6)
            hLog = QLabel("今日日志")
            hLog.setObjectName("h2")
            topLog.addWidget(hLog)
            topLog.addStretch(1)
            bRef = QPushButton("刷新")
            bRef.setObjectName("tiny")
            bOpen = QPushButton("打开目录")
            bOpen.setObjectName("tiny")
            for b in (bRef, bOpen):
                b.setCursor(Qt.PointingHandCursor)
            topLog.addWidget(bRef)
            topLog.addWidget(bOpen)
            layLog.addLayout(topLog)
            self.logView = QPlainTextEdit()
            self.logView.setReadOnly(True)
            layLog.addWidget(self.logView, 1)
            right.addWidget(cardLog, 1)

            bh.addLayout(left, 11)
            bh.addLayout(right, 9)
            vbox.addWidget(body, 1)

            # ---------- 事件 ----------
            bAdd.clicked.connect(self.add_time)
            self.btnSave.clicked.connect(self.save_settings)
            self.btnNow.clicked.connect(self.checkin_now)
            bRef.clicked.connect(self.refresh_all)
            bOpen.clicked.connect(lambda: os.startfile(LOG_DIR))

            cfg = load_config()
            self.tgBoot.setChecked(cfg["EnableBoot"])
            self.tgBackups.setChecked(cfg["IncludeBackups"])
            self.times = list(cfg["Times"])
            self.rebuild_chips()
            self.refresh_all()

        # ---------- 时间点 chips ----------
        def rebuild_chips(self):
            while self.chipGrid.count():
                it = self.chipGrid.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            items = sorted(self.times)
            if not items:
                empty = QLabel("未设置定时时间")
                empty.setObjectName("faint")
                self.chipGrid.addWidget(empty, 0, 0, Qt.AlignLeft)
                return
            for i, t in enumerate(items):
                b = QPushButton("%s    ✕" % t)
                b.setObjectName("chip")
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, tt=t: self.remove_time(tt))
                self.chipGrid.addWidget(b, i // 4, i % 4, Qt.AlignLeft)

        def remove_time(self, t):
            if t in self.times:
                self.times.remove(t)
            self.rebuild_chips()

        def add_time(self):
            t = self.timeEdit.text().strip()
            if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", t):
                QMessageBox.warning(self, "提示", "时间格式应为 HH:mm（如 08:30、12:00）")
                return
            hh, mm = t.split(":")
            t = "%02d:%s" % (int(hh), mm)
            if t not in self.times:
                self.times.append(t)
                self.rebuild_chips()

        def collect_times(self):
            return sorted(self.times)

        # ---------- 账号行 ----------
        def set_accounts(self, rows):
            while self.accBox.count():
                it = self.accBox.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            if not rows:
                empty = QLabel("未发现本机 WorkBuddy 账号")
                empty.setObjectName("faint")
                empty.setAlignment(Qt.AlignCenter)
                self.accBox.addWidget(empty)
                return
            for name, backup, msg, ok in rows:
                w = QFrame()
                w.setObjectName("accRow")
                w.setFixedHeight(52)
                h = QHBoxLayout(w)
                h.setContentsMargins(16, 0, 16, 0)
                h.setSpacing(10)
                dot = QLabel()
                col = C["GREEN"] if ok else (C["FAINT"] if ok is None else C["RED"])
                dot.setStyleSheet("background:%s; border-radius:5px; min-width:10px; max-width:10px;"
                                  "min-height:10px; max-height:10px;" % col)
                h.addWidget(dot)
                nm = QLabel(name)
                nm.setStyleSheet("font-size:13.5px;")
                h.addWidget(nm)
                if backup:
                    h.addWidget(badge("历史", C["FAINT"]))
                h.addStretch(1)
                ml = QLabel(msg or "—")
                msg_col = C["GREEN"] if ok else (C["RED"] if ok is False else C["FAINT"])
                ml.setStyleSheet("color:%s; font-size:11.5px;" % msg_col)
                if msg:
                    ml.setToolTip(msg)
                h.addWidget(ml)
                self.accBox.addWidget(w)

        def refresh_all(self):
            try:
                lst = read_state("accounts.json")
                names = [a.get("name", "") for a in (lst or [])]
                stale = any(("wbEncrypted" in n) or ("envelope" in n) or (len(n) > 48) for n in names)
                if not lst or stale:
                    cfg = load_config()
                    lst = find_accounts(cfg["IncludeBackups"])
                    save_accounts(lst)
            except Exception:
                lst = []
            results = {}
            rtime = ""
            try:
                r = read_state("last-run.json") or {}
                rtime = r.get("time", "")
                results = {x.get("user"): x for x in r.get("results", [])}
            except Exception:
                pass
            rows = []
            for a in lst or []:
                r = results.get(a.get("name"))
                rows.append((a.get("name", "?"), bool(a.get("backup")),
                             (r or {}).get("msg"), (r or {}).get("ok") if r else None))
            self.set_accounts(rows)
            self.lastMeta.setText(("上次签到  " + rtime) if rtime else "")
            lf = os.path.join(LOG_DIR, "checkin-%s.log" % datetime.date.today().isoformat())
            if os.path.exists(lf):
                with open(lf, encoding="utf-8", errors="replace") as f:
                    self.logView.setPlainText("".join(f.readlines()[-80:]))
            else:
                self.logView.setPlainText("今日暂无日志")

        def busy(self, on):
            self.prog.setRange(0, 0 if on else 1)
            if not on:
                self.prog.setValue(0)

        # ---------- 保存设置 ----------
        def save_settings(self):
            cfg = {"EnableBoot": self.tgBoot.isChecked(),
                   "Times": self.collect_times(),
                   "IncludeBackups": self.tgBackups.isChecked()}
            try:
                save_config(cfg)   # 普通用户可能无权写 ProgramData，失败由提权流程兜底
            except Exception:
                pass
            cfg_b64 = base64.b64encode(json.dumps(cfg, ensure_ascii=False).encode("utf-8")).decode()
            self.btnSave.setEnabled(False); self.btnSave.setText("应用中…"); self.busy(True)
            def work():
                if is_admin():
                    return apply_tasks(cfg) == 0
                if not run_elevated("--apply --config-json " + cfg_b64):
                    return "cancel"
                return wait_result(SETUP_RES) or False
            self._w1 = Worker(work)
            def finish(res):
                self.btnSave.setEnabled(True); self.btnSave.setText("保存设置"); self.busy(False)
                if res is True or (isinstance(res, dict) and res.get("ok")):
                    QMessageBox.information(self, "保存成功",
                        res.get("msg") if isinstance(res, dict) else "计划任务已更新")
                elif res == "cancel":
                    QMessageBox.warning(self, "已取消", "已取消管理员授权，设置未生效")
                else:
                    QMessageBox.warning(self, "保存失败",
                        res.get("msg") if isinstance(res, dict) else str(res))
                self.refresh_all()
            self._w1.done.connect(finish)
            self._w1.start()

        # ---------- 立即签到 ----------
        def checkin_now(self):
            self.btnNow.setEnabled(False); self.btnNow.setText("签到中…"); self.busy(True)
            self.hint.setText("正在为全部账号签到，请稍候…")
            def work():
                if is_admin():
                    return run_checkin(1) == 0
                if not run_elevated("--checkin"):
                    return "cancel"
                return wait_result(LASTRUN) or False
            self._w2 = Worker(work)
            def finish(res):
                self.btnNow.setEnabled(True); self.btnNow.setText("立即签到"); self.busy(False)
                if res == "cancel":
                    self.hint.setText("已取消管理员授权")
                elif res is False:
                    self.hint.setText("签到执行超时或失败，请查看日志")
                else:
                    self.hint.setText("签到流程已完成，结果见上方账号列表")
                self.refresh_all()
            self._w2.done.connect(finish)
            self._w2.start()

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    app.setWindowIcon(QIcon(resource_path("app.ico")))
    from PySide6.QtCore import QSharedMemory
    shm = QSharedMemory("WorkBuddyCheckinGUI")
    if not shm.create(1):
        QMessageBox.warning(None, APP_TITLE, "签到助手已在运行中")
        return
    w = Win()
    w.show()
    app.exec()
    shm.detach()

# ============================================================
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--checkin", action="store_true", help="无头模式：签到全部账号")
    p.add_argument("--apply", action="store_true", help="无头模式：注册计划任务")
    p.add_argument("--scan", action="store_true", help="无头模式：导出账号列表")
    p.add_argument("--config-json", default="", help="（内部）base64 编码的新配置，与 --apply 同用")
    a = p.parse_args()
    ensure_tool_dir_writable()
    cfg_arg = None
    if a.config_json:
        try:
            cfg_arg = json.loads(base64.b64decode(a.config_json).decode("utf-8"))
        except Exception:
            cfg_arg = None
    if a.checkin:
        sys.exit(run_checkin())
    if a.apply:
        sys.exit(apply_tasks(cfg_arg))
    if a.scan:
        cfg = load_config()
        save_accounts(find_accounts(cfg["IncludeBackups"]))
        sys.exit(0)
    run_gui()

if __name__ == "__main__":
    main()
