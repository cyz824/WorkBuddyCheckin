# -*- coding: utf-8 -*-
"""
WorkBuddy 签到助手 - 单文件 EXE 版 (v1.1)
功能：全机多账号签到 / 开机自启签到 / 每日多时间点签到 / 签到日志
用法：双击打开控制台（GUI）；--checkin 无头签到；--apply 按 config.json 注册计划任务
"""
import sys, os, json, time, ctypes, argparse, datetime, subprocess, tempfile, re

APP_NAME   = "WorkBuddy-AutoCheckin"
APP_TITLE  = "WorkBuddy 签到助手"
APP_VER    = "1.1.0"
APP_ID     = "WorkBuddy.CheckinAssistant"   # 任务栏图标分组
TOOL_DIR   = r"C:\ProgramData\WorkBuddyCheckin"
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

def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

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
                nick = data["account"].get("nickname") or data["account"].get("phoneNumber") or uid[:8]
                accounts.append({
                    "name": "%s/%s" % (wu, nick), "win_user": wu, "nick": nick,
                    "uid": uid, "backup": fn != "workbuddy-desktop.info",
                    "file": path, "data": data,
                })
            except Exception:
                continue
    return accounts

# ---------------- API ----------------
import requests
requests.packages.urllib3.disable_warnings()

def api_headers(acc):
    return {"Content-Type": "application/json", "Accept": "application/json",
            "Authorization": "Bearer " + acc["data"]["auth"]["accessToken"],
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
    rt = auth.get("refreshToken")
    if not rt:
        log("[%s] 无 refreshToken" % acc["name"], "ERROR")
        return False
    h = {"Content-Type": "application/json", "X-User-Id": acc["uid"],
         "X-Domain": auth.get("domain", "www.workbuddy.cn"),
         "X-Refresh-Token": rt, "X-Auth-Refresh-Source": "desktop"}
    try:
        r = requests.post(API_BASE + EP_REFRESH, headers=h, json={}, timeout=30, verify=False)
        j = r.json()
        if j.get("code") == 0 and j.get("data"):
            auth.update({k: j["data"][k] for k in
                         ("accessToken", "refreshToken", "expiresAt", "refreshExpiresAt") if k in j["data"]})
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
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("写入 %s 失败: %s" % (path, e), "ERROR")

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
    try:
        cur = os.path.abspath(sys.executable)
        if cur.lower() != dst.lower():
            if not os.path.exists(dst) or os.path.getmtime(cur) > os.path.getmtime(dst):
                import shutil
                shutil.copyfile(cur, dst)
        return dst
    except Exception:
        return sys.executable

def apply_tasks():
    """按 config.json 注册/更新/删除计划任务（需管理员）"""
    cfg = load_config()
    trig = build_triggers_xml(cfg)
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
                                   QLabel, QPushButton, QListWidget, QLineEdit, QPlainTextEdit,
                                   QFrame, QScrollArea, QMessageBox, QAbstractButton, QProgressBar,
                                   QGraphicsDropShadowEffect)
    from PySide6.QtCore import Qt, QThread, Signal, QPropertyAnimation, Property, QSize
    from PySide6.QtGui import QFont, QIcon, QPixmap, QPainter, QColor, QPainterPath

    C = dict(BG="#161923", CARD="#212637", BORDER="#2E3550", TXT="#E8EAF2", SUB="#98A0B8",
             ACCENT="#5B8DEF", ACCENT_H="#74A1F2", GREEN="#3FB68B", RED="#E5534B", YELLOW="#D9A13B")

    QSS = """
        QMainWindow, QWidget { background: %(BG)s; color: %(TXT)s;
            font-family: 'Segoe UI', 'Microsoft YaHei'; font-size: 13px; }
        QFrame#card { background: %(CARD)s; border: 1px solid %(BORDER)s; border-radius: 14px; }
        QLabel#appname { font-size: 18px; font-weight: bold; }
        QLabel#ver { color: %(SUB)s; font-size: 11px; }
        QLabel#h { font-size: 14px; font-weight: bold; }
        QLabel#sub { color: %(SUB)s; font-size: 12px; }
        QLabel#badge { font-size: 11px; padding: 4px 12px; border-radius: 10px; }
        QPushButton { background: #333B59; border: none; border-radius: 9px;
                      padding: 9px 18px; color: %(TXT)s; }
        QPushButton:hover { background: #404A73; }
        QPushButton:pressed { background: #2C3350; }
        QPushButton:disabled { color: #6B7390; background: #2A3049; }
        QPushButton#accent { background: %(ACCENT)s; color: #0E1220; font-weight: bold; }
        QPushButton#accent:hover { background: %(ACCENT_H)s; }
        QPushButton#accent:pressed { background: #4A7AD6; }
        QLineEdit { background: #161923; border: 1px solid %(BORDER)s; border-radius: 8px;
                    padding: 7px 10px; color: %(TXT)s; selection-background-color: %(ACCENT)s; }
        QLineEdit:focus { border: 1px solid %(ACCENT)s; }
        QListWidget { background: #161923; border: 1px solid %(BORDER)s; border-radius: 8px; padding: 4px; }
        QListWidget::item { padding: 4px 8px; border-radius: 5px; }
        QListWidget::item:selected { background: %(ACCENT)s; color: #0E1220; }
        QPlainTextEdit { background: #12141D; border: 1px solid %(BORDER)s; border-radius: 8px;
                         font-family: Consolas, 'Microsoft YaHei'; font-size: 11px; color: %(SUB)s; padding: 6px; }
        QProgressBar { background: #2A3049; border: none; border-radius: 3px; max-height: 6px; }
        QProgressBar::chunk { background: %(ACCENT)s; border-radius: 3px; }
        QScrollArea { border: none; }
        QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 2px; }
        QScrollBar::handle:vertical { background: #3A4266; border-radius: 4px; min-height: 30px; }
        QScrollBar::handle:vertical:hover { background: #4A5480; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QToolTip { background: #2A3049; color: %(TXT)s; border: 1px solid %(BORDER)s; padding: 4px 8px; }
    """ % C

    class Toggle(QAbstractButton):
        """iOS 风格滑动开关"""
        def __init__(self, checked=False):
            super().__init__()
            self.setCheckable(True)
            self.setChecked(checked)
            self.setCursor(Qt.PointingHandCursor)
            self.setFixedSize(46, 26)
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
            # 轨道颜色随开关过渡
            off = QColor("#3A4266"); on = QColor(C["ACCENT"])
            t = self._pos
            col = QColor(int(off.red() + (on.red() - off.red()) * t),
                         int(off.green() + (on.green() - off.green()) * t),
                         int(off.blue() + (on.blue() - off.blue()) * t))
            p.fillPath(path, col)
            d = h - 8
            x = 4 + self._pos * (w - 8 - d)
            p.setBrush(QColor("#FFFFFF"))
            p.setPen(Qt.NoPen)
            p.drawEllipse(int(x), 4, d, d)
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

    def badge(text, color):
        lb = QLabel(text)
        lb.setObjectName("badge")
        lb.setStyleSheet("color: %s; background: %s26;" % (color, color))
        return lb

    class Win(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("%s v%s" % (APP_TITLE, APP_VER))
            self.setWindowIcon(QIcon(resource_path("app.ico")))
            self.setMinimumSize(660, 720)
            self.resize(700, 800)
            self.setStyleSheet(QSS)

            scroll = QScrollArea(); scroll.setWidgetResizable(True)
            root = QWidget(); lay = QVBoxLayout(root)
            lay.setContentsMargins(20, 20, 20, 16); lay.setSpacing(14)

            # ---------- 头部 ----------
            head = QHBoxLayout()
            iconLb = QLabel()
            pm = QPixmap(resource_path("app.ico"))
            if not pm.isNull():
                iconLb.setPixmap(pm.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            nameBox = QVBoxLayout(); nameBox.setSpacing(0)
            nm = QLabel(APP_TITLE); nm.setObjectName("appname")
            ver = QLabel("v%s · 多账号自动签到" % APP_VER); ver.setObjectName("ver")
            nameBox.addWidget(nm); nameBox.addWidget(ver)
            self.adminBadge = badge("管理员" if is_admin() else "普通模式", C["GREEN"] if is_admin() else C["YELLOW"])
            head.addWidget(iconLb); head.addSpacing(12); head.addLayout(nameBox)
            head.addStretch(1); head.addWidget(self.adminBadge)
            lay.addLayout(head)

            # ---------- 账号 ----------
            c1, v1 = self.card("本机 WorkBuddy 账号")
            self.accLabel = QLabel("读取中…"); self.accLabel.setWordWrap(True)
            self.accLabel.setTextFormat(Qt.RichText)
            v1.addWidget(self.accLabel)
            lay.addWidget(c1)

            # ---------- 立即签到 ----------
            c2, v2 = self.card("立即签到")
            row = QHBoxLayout()
            self.hint = QLabel("为全部账号签到（会弹出一次管理员授权）")
            self.hint.setObjectName("sub"); self.hint.setWordWrap(True)
            self.btnNow = QPushButton("立即签到"); self.btnNow.setObjectName("accent")
            self.btnNow.setFixedSize(130, 40)
            row.addWidget(self.hint, 1); row.addWidget(self.btnNow)
            v2.addLayout(row)
            lay.addWidget(c2)

            # ---------- 自动签到设置 ----------
            c3, v3 = self.card("自动签到设置")
            r1 = QHBoxLayout()
            r1.addWidget(QLabel("开机 / 登录时自动签到一次")); r1.addStretch(1)
            self.tgBoot = Toggle()
            r1.addWidget(self.tgBoot)
            v3.addLayout(r1)
            r2 = QHBoxLayout()
            lb2 = QLabel("同时签到历史切换过的账号（一般不用开）"); lb2.setObjectName("sub")
            r2.addWidget(lb2); r2.addStretch(1)
            self.tgBackups = Toggle()
            r2.addWidget(self.tgBackups)
            v3.addLayout(r2)
            sub = QLabel("每日定时签到（格式 HH:mm，可添加多个）"); sub.setObjectName("sub")
            v3.addWidget(sub)
            tr = QHBoxLayout()
            self.timeEdit = QLineEdit("12:00"); self.timeEdit.setFixedWidth(90)
            bAdd = QPushButton("＋ 添加"); bDel = QPushButton("－ 删除选中")
            tr.addWidget(self.timeEdit); tr.addWidget(bAdd); tr.addWidget(bDel); tr.addStretch(1)
            v3.addLayout(tr)
            self.timeList = QListWidget(); self.timeList.setFixedHeight(90)
            v3.addWidget(self.timeList)
            sr = QHBoxLayout()
            self.taskLabel = QLabel(""); self.taskLabel.setObjectName("sub")
            self.btnSave = QPushButton("保存设置"); self.btnSave.setObjectName("accent")
            self.btnSave.setFixedSize(130, 38)
            sr.addWidget(self.taskLabel, 1); sr.addWidget(self.btnSave)
            v3.addLayout(sr)
            lay.addWidget(c3)

            # ---------- 上次运行 ----------
            c4, v4 = self.card("上次运行")
            self.lastLabel = QLabel("--"); self.lastLabel.setObjectName("sub")
            self.lastLabel.setWordWrap(True); self.lastLabel.setTextFormat(Qt.RichText)
            v4.addWidget(self.lastLabel)
            lay.addWidget(c4)

            # ---------- 日志 ----------
            c5, v5 = self.card("今日日志")
            lr = QHBoxLayout()
            bRef = QPushButton("刷新"); bOpen = QPushButton("打开日志目录")
            bRef.setFixedWidth(80); bOpen.setFixedWidth(120)
            lr.addStretch(1); lr.addWidget(bRef); lr.addWidget(bOpen)
            v5.addLayout(lr)
            self.logView = QPlainTextEdit(); self.logView.setReadOnly(True); self.logView.setFixedHeight(150)
            v5.addWidget(self.logView)
            lay.addWidget(c5)

            # ---------- 底部进度 ----------
            self.prog = QProgressBar(); self.prog.setRange(0, 1); self.prog.setValue(0)
            self.prog.setTextVisible(False)
            lay.addWidget(self.prog)

            scroll.setWidget(root)
            self.setCentralWidget(scroll)

            bAdd.clicked.connect(self.add_time)
            bDel.clicked.connect(lambda: self.timeList.takeItem(self.timeList.currentRow()))
            self.btnSave.clicked.connect(self.save_settings)
            self.btnNow.clicked.connect(self.checkin_now)
            bRef.clicked.connect(self.refresh_all)
            bOpen.clicked.connect(lambda: os.startfile(LOG_DIR))

            cfg = load_config()
            self.tgBoot.setChecked(cfg["EnableBoot"])
            self.tgBackups.setChecked(cfg["IncludeBackups"])
            for t in cfg["Times"]:
                self.timeList.addItem(t)
            self.refresh_all()

        def card(self, title):
            f = QFrame(); f.setObjectName("card")
            sh = QGraphicsDropShadowEffect()
            sh.setBlurRadius(18); sh.setOffset(0, 3); sh.setColor(QColor(0, 0, 0, 70))
            f.setGraphicsEffect(sh)
            v = QVBoxLayout(f); v.setContentsMargins(18, 14, 18, 16); v.setSpacing(10)
            h = QLabel(title); h.setObjectName("h")
            v.addWidget(h)
            return f, v

        def busy(self, on):
            self.prog.setRange(0, 0 if on else 1)
            if not on:
                self.prog.setValue(0)

        def add_time(self):
            t = self.timeEdit.text().strip()
            if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", t):
                QMessageBox.warning(self, "提示", "时间格式应为 HH:mm（如 08:30、12:00）")
                return
            hh, mm = t.split(":")
            t = "%02d:%s" % (int(hh), mm)
            if self.timeList.findItems(t, Qt.MatchExactly):
                return
            self.timeList.addItem(t)

        def collect_times(self):
            return sorted(self.timeList.item(i).text() for i in range(self.timeList.count()))

        def refresh_all(self):
            try:
                if os.path.exists(ACCOUNTS):
                    with open(ACCOUNTS, encoding="utf-8-sig") as f:
                        lst = json.load(f)
                    if lst:
                        rows = []
                        for a in lst:
                            chip = (' <span style="color:%s; font-size:11px;">[历史]</span>' % C["YELLOW"]) if a.get("backup") else ""
                            rows.append('<span style="color:%s;">●</span> %s%s' % (C["GREEN"], a["name"], chip))
                        self.accLabel.setText("<br>".join(rows))
                    else:
                        self.accLabel.setText("未发现账号")
                else:
                    accs = find_accounts(False)
                    self.accLabel.setText("<br>".join(
                        '<span style="color:%s;">●</span> %s' % (C["GREEN"], a["name"]) for a in accs) or "未发现账号")
            except Exception as e:
                self.accLabel.setText("读取失败: %s" % e)
            try:
                with open(LASTRUN, encoding="utf-8-sig") as f:
                    r = json.load(f)
                lines = ['<span style="color:%s;">%s</span>' % (C["SUB"], r.get("time", ""))]
                for x in r.get("results", []):
                    col = C["GREEN"] if x.get("ok") else C["RED"]
                    mark = "✔" if x.get("ok") else "✖"
                    lines.append('<span style="color:%s;">%s</span> %s: %s' % (col, mark, x.get("user"), x.get("msg")))
                self.lastLabel.setText("<br>".join(lines))
            except Exception:
                self.lastLabel.setText("暂无运行记录")
            ex, msg = query_task_exists()
            col = C["GREEN"] if ex else C["SUB"]
            self.taskLabel.setText('<span style="color:%s;">计划任务：%s</span>' % (col, msg))
            lf = os.path.join(LOG_DIR, "checkin-%s.log" % datetime.date.today().isoformat())
            if os.path.exists(lf):
                with open(lf, encoding="utf-8", errors="replace") as f:
                    self.logView.setPlainText("".join(f.readlines()[-80:]))
            else:
                self.logView.setPlainText("今日暂无日志")

        def save_settings(self):
            cfg = {"EnableBoot": self.tgBoot.isChecked(),
                   "Times": self.collect_times(),
                   "IncludeBackups": self.tgBackups.isChecked()}
            save_config(cfg)
            self.btnSave.setEnabled(False); self.btnSave.setText("应用中…"); self.busy(True)
            def work():
                if is_admin():
                    return apply_tasks() == 0
                if not run_elevated("--apply"):
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

        def checkin_now(self):
            self.btnNow.setEnabled(False); self.btnNow.setText("签到中…"); self.busy(True)
            self.hint.setText("正在以管理员身份为全部账号签到，请稍候…")
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
                    self.hint.setText("签到流程已完成，结果见下方")
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
    # 单实例
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
    a = p.parse_args()
    if a.checkin:
        rc = run_checkin()
        write_setup_result(rc == 0, "签到流程已执行，详见日志")
        sys.exit(rc)
    if a.apply:
        sys.exit(apply_tasks())
    if a.scan:
        cfg = load_config()
        save_accounts(find_accounts(cfg["IncludeBackups"]))
        sys.exit(0)
    run_gui()

if __name__ == "__main__":
    main()
