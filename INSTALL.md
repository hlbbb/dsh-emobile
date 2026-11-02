# dsh-emobile 安装说明（可分发 · 只需 Python）

## 一、前置条件（目标机）

- [ ] **DSH 桌面端**至少完整启动过一次（存在 `~/.dsh/profiles/desktop`）。
- [ ] 已安装 **E-Mobile 客户端**并已登录。
- [ ] **Python 3.10+**（系统 Python / Anaconda / Miniconda 均可）。
- [ ] **不需要**预装 `emcli`，也**不需要**联网：插件自带 emcli 源码与依赖 wheel。

确认 Python（任选一种能找到即可）：

```powershell
python -c "import sys;print(sys.version)"
# 或 Anaconda
conda info --envs
```

## 二、安装插件

在插件目录（含 `vendor/`、`lib/`、`package.json`）下执行：

```powershell
node tools\install-to-profile.mjs --dry-run   # 先空跑
node tools\install-to-profile.mjs             # 正式安装
```

脚本只做三件事（幂等、不删除任何文件）：建 `node_modules` junction、写 profile `package.json`（link 依赖 + bundles）、写 `cordis.patch.yml` 的 `- id: dsh-emobile` 配置块。

安装时还会**按本机 PATH 找到的 Python 自动安装 ddddocr**（验证码 OCR，约 150MB，需联网；用 `--no-ocr` 可跳过，首次登录仍会自动装）。

> **一般无需任何路径参数**：Python 与依赖目录运行时自动探测。
> 仅当目标机有多个 Python、想指定某个 Anaconda 环境时才需要：
> ```powershell
> node tools\install-to-profile.mjs --python-path "C:\ProgramData\anaconda3\python.exe"
> # 或直接用已有的 emcli：
> node tools\install-to-profile.mjs --emcli-path "C:\ProgramData\anaconda3\Scripts\emcli.exe"
> ```

## 三、重启并首次使用（自动装配）

**插件不热加载**：从托盘/菜单**完全退出 DSH 桌面端进程**（任务管理器确认相关进程没了），再打开。

首次调用任一 `emobile_*` 工具时，插件会：
1. 在目标机找一个 Python 3.10+；
2. 把 `vendor/wheels` 里的依赖**离线安装**到 `<DSH_HOME>\storages\dsh-emobile\python-libs`；
3. 以 `python -m cli_anything_e_mobile` 运行。

首次调用可能多花几秒~几十秒（仅一次）；之后有缓存，直接可用。

**自动登录（emobile_login）** 首次会在目标机上按本地 Python 版本在线安装验证码 OCR（`ddddocr`，约 150MB、约 1 分钟，需能访问 PyPI；可用 `pipIndexUrl` 配镜像）。离线环境可把 ddddocr 及依赖 wheel 放进 `vendor/wheels`（存在则优先离线安装），或在登录时用 `code` 手工喂验证码。

> 注意：发送/自动登录要求 E-Mobile 能带 `--remote-debugging-port=9333` 启动。**推荐安装后执行一次** `emobile_launcher_install`（或 `emcli launcher install`）创建 `E-Mobile (CLI).lnk`，以后用该图标启动客户端即可；插件不会自动重启/登出。若客户端已在运行且没开端口、当前权限又无法结束它，需要一次性手动退出（或用 CLI 快捷方式启动）。

验证：

```powershell
node tools\install-to-profile.mjs --doctor
```

期望：`内置 emcli 源码`、`内置依赖 wheels`、`node_modules junction`、profile 三项均 ✅；
`emcli 可定位` 若本机没预装会显示“将使用内置运行时”。然后在会话里让 agent 调 `emobile_doctor`、`emobile_status`。

## 四、发给别人（分发）

- 把**整个插件目录**打包（必须包含 `vendor/`，约 1.5MB）。切勿只发 `lib/`。
- 对方解压后，按第二节在自己的机器上跑一次安装器（他的 profile 路径自动取 `~/.dsh`，无需改配置）。
- 对方的第一调用会自动装内置依赖；无需对方懂 Python 细节。

## 五、发送能力（默认关闭）

默认只读。要在 `cordis.patch.yml` 加 `allowSend: true` 并完全重启；发送前仍需**人工**执行
`emcli policy add "群名"` 授权（插件不会自动加白），调用发送工具时传 `confirm: true`。单聊不可发，仅群。

## 六、排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `emobile_doctor` 报找不到 Python | 目标机未装 Python 或版本 <3.10 | 安装 Python 3.10+，或配置 `pythonPath` 指向 Anaconda python.exe |
| 首次调用很慢/超时 | 正在离线装配依赖 | 属正常，可调大 `bootstrapTimeoutMs` |
| 安装器提示 junction 冲突 | 目标已存在且指向别处 | 把该链接**改名**（勿删）后重跑 |
| 插件不出现 | 桌面端没完全重启 / profile 未初始化 | 完全退出重开 |
| 发送 blocked | 目标群不在白名单 | 人工 `emcli policy add` 后重试 |
| `emobile_login` 报“客户端仍在运行/无法结束” | 客户端无调试端口且权限不足 | 完全退出 E-Mobile（管理员任务管理器）或用 `E-Mobile (CLI).lnk` 启动 |
| 首次登录很慢 | 正在联网安装 ddddocr | 正常（约 1 分钟，仅一次）；可调 `ocrTimeoutMs` 或用镜像 |
| OCR 安装失败 | 无法访问 PyPI | 配 `pipIndexUrl` 镜像，或把 OCR wheel 放进 `vendor/wheels`，或传 `code` 手工验证码 |
| 需要客户端自动登录验证码 | 内置 wheel 未含 ddddocr | 目标机 `pip install dddocr`，或配置已有 emcli |
| 提示 win32/COM 相关错误 | 内置 wheel 未含 pywin32 | DPAPI 有 ctypes 兜底、.lnk 有 PowerShell 兜底；仍异常则 `pip install pywin32` 或配置已有 emcli |
| Windows arm64 装 psutil 失败 | 内置 psutil 仅 x64 | 自备 arm64 psutil，或配置已有 emcli / pythonPath |

## 七、卸载（手动，仅加不删）

先把三处改名为 `*_old`（**不要直接删除**），确认正常后再走回收站：
1. `~/.dsh/profiles/desktop/node_modules/dsh-emobile`
2. `~/.dsh/profiles/desktop/package.json`：移除 `dependencies.dsh-emobile` 与 bundles 中的 `dsh-emobile`
3. `~/.dsh/profiles/desktop/cordis.patch.yml`：移除 `- id: dsh-emobile` 整块

可选：删除内置依赖缓存 `~/.dsh/storages/dsh-emobile/`（走回收站）。改完完全重启桌面端。

> ⚠️ 任何删除必须走回收站，不要用 `Remove-Item -Recurse -Force` / `rmdir /s`，尤其 junction。
