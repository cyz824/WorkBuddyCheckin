# WorkBuddy 签到助手

> 一键为本机所有 WorkBuddy 账号自动签到，支持开机自启和定时签到。

![GUI 截图](gui_shot.png)

## 功能

- **多账号签到** — 自动扫描本机所有 Windows 用户的 WorkBuddy 凭据，一次操作为全部账号签到
- **开机自启** — 可设置开机 / 登录时自动签到（通过 Windows 计划任务实现）
- **定时签到** — 支持设置每日多个签到时间点（如 `08:00`、`12:00`、`22:00`）
- **Token 自动刷新** — 签到前自动检查并刷新过期的 accessToken
- **网络等待** — 开机时网络未就绪会自动等待（最长 5 分钟）
- **签到日志** — 按日期记录每次签到的详细结果
- **图形界面** — PySide6 (Qt) 深色主题 GUI，操作简单直观
- **命令行模式** — 支持 `--checkin` 无头签到、`--apply` 注册计划任务、`--scan` 导出账号

## 工作原理

程序通过扫描 `C:\Users\<用户名>\AppData\Local\CodeBuddyExtension\Data\Public\auth\` 目录下的 `workbuddy-desktop.info` 文件获取 WorkBuddy 登录凭据，然后调用 `https://copilot.tencent.com/v2` 的签到 API 完成自动签到。

## 使用方式

### 方式一：直接运行 EXE

双击 `WorkBuddyCheckin.exe` 打开图形界面，点击「立即签到」即可。

### 方式二：命令行

```bash
# 无头签到（适合计划任务）
WorkBuddyCheckin.exe --checkin

# 注册/更新计划任务
WorkBuddyCheckin.exe --apply

# 导出账号列表
WorkBuddyCheckin.exe --scan
```

### 方式三：从源码运行

```bash
pip install PySide6 requests
python workbuddy_checkin.py
```

## 打包为 EXE

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed \
  --icon=app.ico \
  --version-file=version.txt \
  --add-data "app.ico;." \
  --name WorkBuddyCheckin \
  workbuddy_checkin.py
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `workbuddy_checkin.py` | 主程序源码（含 GUI 和签到逻辑） |
| `make_icon.py` | 应用图标生成脚本 |
| `app.ico` | 应用图标 |
| `app_icon_256.png` | 256x256 图标 |
| `version.txt` | PyInstaller 版本信息文件 |
| `gui_shot.png` | GUI 截图 |
| `requirements.txt` | Python 依赖 |

## 运行时文件

程序运行时会在 `C:\ProgramData\WorkBuddyCheckin\` 下生成以下文件：

| 文件 | 说明 |
|------|------|
| `config.json` | 用户配置（开机自启、定时时间等） |
| `last-run.json` | 最近一次签到结果 |
| `accounts.json` | 扫描到的账号列表 |
| `setup-result.json` | 计划任务注册结果 |
| `logs/checkin-YYYY-MM-DD.log` | 每日签到日志 |

## 系统要求

- Windows 10 / 11
- 已安装并登录 WorkBuddy 桌面端
- 管理员权限（首次注册计划任务时需要）

## License

MIT
