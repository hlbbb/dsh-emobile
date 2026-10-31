# E-Mobile CLI（emcli）功能梳理与插件化准备

> 分析对象：本机 `emobile-package` 内部分发包（含 `cli-anything-e-mobile 1.0.0`）
> 代码版本：`cli-anything-e-mobile 1.0.0`（Python ≥ 3.10，仅 Windows 桌面端）
> 入口：`emcli` / `cli-anything-e-mobile` / `python -m cli_anything_e_mobile`
> 结论先行：这是一个"复用本机 E-Mobile 客户端登录态"的 agent-native CLI，**读能力成熟稳定、发送能力依赖本机 GUI 且有强安全护栏**。插件化推荐「子进程调用 + `--json`」为主、「Python API 直调」为辅。

---

## 1. 总体架构

```
                  ┌──────────────────────────── 命令层 (click) ───────────────────────────┐
                  │ status compat setup launcher token config login chat policy contact    │
                  │ group ding db repl                                                    │
                  └───────────────────────────────────┬──────────────────────────────────┘
                                                      │ EMSession（门面）
        ┌──────────────────────────┬──────────────────┼─────────────────────┬────────────────────────┐
        │                          │                  │                     │                        │
  ① 登录态读取                ② REST 通道        ③ XMPP 通道          ④ GUI 驱动               ⑤ 本地库
  CDP(9333) → 磁盘 LevelDB   {base}/emp/**      TCP 5222 / WS 7070    CDP 操作官方客户端       sqlite em7.db
  （读类不需要调试端口）      token/IM参数/搜人  会话/历史/必达/收消息  发送文本/文件/图片       （7.1.4 不存在）
                           /群列表/企业信息     SCRAM-SHA-1           Quill DOM(7.2.6 验证)
```

- **登录态来源优先级**：CDP 调试端口 → 磁盘 `%APPDATA%\E-Mobile\Local Storage\leveldb`（纯 Python 解析 LevelDB/Snappy，不依赖端口）。只有"发东西"才必须开调试端口。
- **发送为什么必须走 GUI**：自拼 XMPP 文本消息会被服务端**静默丢弃**（实测群/单聊均失败），只有驱动官方客户端（打开会话 → 输入/拖放 → 点发送）才能以本人身份经官方通道送达。
- **进程/端口治理**：exe 五路发现（环境变量 → 传统路径 → 注册表 Uninstall → 桌面/开始菜单快捷方式 → 运行进程镜像）；端口探测 9333/9334/9335/9222/9223；关闭客户端为两级（优雅 `WM_CLOSE`+`taskkill` 等 15s → 超时强制 `taskkill /F`，仅针对复核过的 `E-Mobile.exe`）；`launcher install/remove` 改快捷方式让其默认带端口启动（改动前备份，回滚走回收站）。
- **自动登入**：凭证文件（中英键名 `账号/登录名/username` + `密码/password`，支持 Windows DPAPI 密文 `enc:v1:`）→ 驱动登录页 CDP → canvas 取当前验证码像素 → ddddocr 识别 → 提交后按服务端判据（`gettoken errcode=0` 才算成功）换图重试。`ensure_login()` 供自动化自愈。
- **版本自适应**：`compat.PROFILES` 定义 7.1 / 7.2 / unknown 三套档案，适配 localStorage 键名（`gn-/en-current-info` vs `nc-current-info`）、REST User-Agent、GUI DOM 候选选择器、登录态字段嵌套形态。

---

## 2. 能力矩阵（一眼看清"能不能用、需不需要开端口"）

| 能力 | 命令 | 数据通道 | 需调试端口 | 版本可用性 | 成熟度 |
|---|---|---|---|---|---|
| 状态/诊断 | `status [--probe]`、`compat [--probe]` | 本地探测 + 可选 REST/XMPP | 否 | 全版本 | 稳定 |
| 会话列表（未读数/最后消息） | `chat list` | XMPP | 否（读 token 走磁盘即可，连 XMPP 需网络） | 7.1.4/7.2.6 实测 | 稳定 |
| 历史消息（分页） | `chat history` | XMPP | 否 | 实测 | 稳定 |
| 实时监听新消息 | `chat watch` | XMPP | 否 | 实测 | 稳定 |
| 搜人（姓名/拼音） | `contact search` | REST | 否 | 实测 | 稳定 |
| 我的群组 | `group list` | REST | 否 | 实测 | 稳定 |
| 必达列表 | `ding list` | XMPP | 否 | 实测 | 稳定 |
| 发文本（官方通道） | `chat send-gui` | GUI/CDP | **是** | 7.2.6 验证，7.1.4 候选化未实测 | 可用 |
| 发文件（拖放） | `chat send-file` | GUI/CDP | **是** | 同上 | 可用 |
| 发图片（图片消息） | `chat send-image` | GUI/CDP | **是** | 同上 | 可用 |
| 发文本（自拼 XMPP） | `chat send` | XMPP | 否 | **服务端丢弃，不可用** | 废弃 |
| 本地 sqlite 只读 | `db overview/urls/users/items/chatlist` | sqlite | 否 | **7.1.4 不生成 em7.db → 不可用** | 7.2.x 可用 |
| 自动登入 | `login` / `login status/init/protect` | CDP 登录页 + OCR | 是 | 7.2.6 实测 | 可用（验证码非 100%） |
| 端口/启动入口治理 | `setup`、`launcher show/install/remove` | 进程/快捷方式 | 视情况 | 实测 | 稳定 |

---

## 3. 命令全清单（含关键参数）

> 全局选项：`--json`（**必须放在子命令前**）、`--cdp-port`、`--quiet`、`--verbose`、`--show-identity`（仅排障面）、`--version`。

```
status [--probe]                       # 配置+连通总览；--probe 实探 REST/XMPP
compat [--probe]                       # 版本/档案/登录态键/端口/凭证/启动入口/DOM 命中数（只读诊断，失败不影响退出码）
setup [--no-launch] [--no-launcher] [--wait-login 90]
                                       # 首次初始化：端口就绪→启动入口→读登录态→取 IM 参数
launcher show | install [--port 9333] | remove [--port]

token refresh [--force]                # 从客户端读登录态并取 IM 参数（CDP→磁盘回退）
token set <access_token> [--base-url]  # 手动写入 token
token clear                            # 清除本机登录态
config show | set <key> <value>        # key 仅 transport(tcp|ws)/proxy/resource

login [--env-file --code --attempts 5 --timeout 25 --ocr-models default --force --no-launch]
login status                           # 只读：是否需重新登录（会先实测 token）
login init [--env-file --account --password --force]   # 生成凭证模板
login protect [--env-file --yes]       # DPAPI 加密明文凭证

chat                                   # 等价 chat list
chat list [-n 50] [--unread] [--name <过滤>]
chat history <会话名|id> [-n 20] [--before <游标>]
chat send <会话> <消息> [--id] [--dry-run] [--yes]      # 自拼 XMPP，服务端丢弃（仅 dry-run 有用）
chat send-file <会话> <文件> [--yes --attempts 6 --interval 10 --first-delay 10 --no-wait --no-open]
chat send-image <会话> <文件> [--yes --wait 30 --no-open]
chat send-gui <会话> [TEXT] [--text-file <文件>] [--yes --no-open]   # 多行文本用 --text-file
chat watch [--seconds 30] [--from <sender>] [--no-receipt]

policy show | add <会话> [--force] | remove <会话> | clear     # 发送白名单（默认空=禁止一切发送）

contact search <关键词> [-n 20] [--simple]     # --simple 走 baseuser/search，不需 corpid
group list [-n 100]
ding list [-n 20]

db overview | urls | users | items [--type N --key <模糊> -n 50 --value] | chatlist

repl                                   # 交互模式
```

> 注：`cli.py` 文件头 docstring 提到 `group members`，实际**未实现**，只有 `group list`。

---

## 4. JSON 输出契约（插件消费的关键，务必注意不统一）

- 全部命令支持 `--json`，但**格式不完全统一**：
  - 大多数命令：`{"ok": true, ...}`；失败 `{"ok": false, "error": "..."}`。
  - `status` / `config show`：直接输出配置摘要字段（**没有 `ok` 字段**）。
  - `compat`：`{"ok": true, "user_agent": ..., "checks": [...]}`。
  - `login status`：`session_status` 原样 + 脱敏用户字段（含 `ok`/`needs_login`/`reason`/`errcode`）。
- `--json` 模式下**错误 JSON 也写 stdout**（不是 stderr）；非 JSON 模式人类可读错误写 stderr。插件解析必须按 `ok` 判断，不能假设"stdout 只有成功"。
- 退出码：`0` 成功；`1` 一般错误；**`2` = 发送被白名单拦下**（`blocked: true`）。
- `--json` 是**全局选项**，必须写在子命令前：`emcli --json chat list` ✅，`emcli chat list --json` ❌。

---

## 5. 程序化接口（Python API，供进程内复用；插件若同进程可直调）

| 模块.对象 | 关键方法 / 说明 |
|---|---|
| `session.EMSession` | `ensure_token(force)`、`refresh_token_from_app()`、`ensure_login(auto, env_file, manual_code, max_attempts)`、`fetch_im_params(force)`、`rest`（REST 客户端）、`xmpp()`（上下文管理器）、`conversations()`、`resolve_conversation(query)`、`resolve_names(ids)`、`profile`/`client_version`、`ts()` |
| `rest.EMRestClient` | `get_im_params()`、`passport_alive()`、`corpid()`、`user_search()`、`baseuser_search()`、`user_infos()`、`group_list()`、`tenant_list()`；通用 `get/post(path, ...)` |
| `im.xmpp.XMPPClient` | `get_conversations()`、`get_conversations_page()`、`get_history()`、`search_history()`、`get_msg_by_ids()`、`ding_list()`、`group_call()`、`send_text()`（服务端不收）、`drain_messages()`、`iq()` |
| `gui.GuiClient` | `open_conversation()`、`send_text()`、`send_file()`、`send_image()`、`assert_conversation()`、`dom_report()` |
| `login` | `load_credentials()`、`resolve_cred_file()`、`init_cred_file()`、`protect_file()`、`perform_login()`、`session_status()`、`OcrEngine`、`LoginPage` |
| `localdb` | `db_path()`、`exists()`、`connect_ro()`、`q_urls/q_users/q_items/q_chatlist/...` |
| `localstore` | `read_current_info()`、`diagnose()`（磁盘 LevelDB 解析） |
| `app` | `ensure_port()`、`launcher_status()`、`configure_launcher()`、`remove_launcher()`、`discover_exes()` |
| `config` | `Config.load()/save()/summary()`、`mask_*` 脱敏函数 |

> ⚠️ `ensure_login()` / `refresh_token_from_app()` 返回体里的 `user` 是**完整姓名(工号)**（session 层不脱敏），脚本要打印/落库/转发必须先过 `config.mask_user_label_text()`。

---

## 6. 配置 / 环境变量 / 关键文件

| 类别 | 内容 |
|---|---|
| 配置文件 | `%LOCALAPPDATA%\emcli\config.json`（token、IM 参数、`send_allowlist`、`login_env_file`；**不含账号密码**） |
| 凭证文件 | 查找顺序：`--env-file` → `$EMCLI_LOGIN_ENV` → 配置记录路径 → 当前目录 → 各级父目录(≤6 层) → `%LOCALAPPDATA%\emcli`。推荐 DPAPI 加密（`enc:v1:`） |
| 主要环境变量 | `EMCLI_EMOBILE_EXE`、`EMCLI_CDP_PORT`、`EMCLI_CLIENT_VERSION`、`EMCLI_PROXY`、`EMCLI_TRANSPORT`(tcp/ws)、`EMCLI_LOGIN_ENV`、`EMCLI_NO_RESTART`、`EMCLI_NO_FORCE`、`EMCLI_EMOBILE_USERDATA`、`EMCLI_LAUNCHER_DIR`/`EMCLI_BACKUP_DIR`/`EMCLI_EMOBILE_LNK`、`EMCLI_MAX_IMAGE_BYTES` |
| 数据目录 | `%APPDATA%\E-Mobile\Local Storage\leveldb`（登录态）、`%APPDATA%\E-Mobile\sqlite3\em7.db`（7.2.x 本地库）、`%LOCALAPPDATA%\emcli\backup\`（快捷方式备份） |
| 依赖 | `click`、`requests`、`websocket-client`、`psutil`/`pywin32`(Windows)、可选 `ddddocr`(验证码) |

---

## 7. 安全护栏与脱敏边界（插件必须尊重，不能绕过）

1. **零信任写**：`chat send*` 默认一律禁止；目标会话必须先在 `policy add` 白名单，否则退出码 2。`--json` 无人交互时**必须显式 `--yes`**。发送后默认轮询会话历史确认真正落地（只认 `sendTime` 新于发送前的那条）。
2. **不让 agent 自行 `policy add` 真实群** —— 授权是人的决定（SKILL.md 明文纪律）。
3. **脱敏三面**：
   - 登录态展示面（`status`/`login`/`setup`/`config show`/`compat`）：姓名只露首字、工号只露前 2 位 + 字数 + 稳定指纹（如 `张**(2字·1d84)`）。
   - 排障面（`--probe`/`--dry-run`/`--verbose`）：JID、`to`、stanza 默认掩码；需完整值要显式 `--show-identity`（仅排障，勿落盘）。
   - 功能数据面（`chat list` 的 id、`chat history` 的 `conversation.id`、发送结果、`policy show`、`db users`、`db items --value`、`contact search`）：**原样输出，不掩码**（会话 id 是操作入口）。
4. **凭证**：账号密码只在进程内存；DPAPI 按机器+Windows 账号绑定，换机解不开，正式环境需各自 `login protect`。

---

## 8. 已知限制与坑（插件设计要预留处理）

- `chat send`（自拼 XMPP 文本）**发不出去**，文本必须 `chat send-gui`。
- 发送需要 E-Mobile **图形界面处于登录状态**，不能纯 headless；发送类需要调试端口（会自动优雅重启客户端兜底，**强杀会丢未发送草稿**）。
- **图片必须 `send-image`**，`send-file` 拖放 image 类型会被客户端拒绝；图片有 8MB 上限（`EMCLI_MAX_IMAGE_BYTES`）。
- 多行文本走命令行会被 shell 吃掉换行，**必须 `--text-file`**。
- 单聊发送服务端配置 `private_chat: 0`，**单聊发送不被接收**，群发送正常（重要！插件应默认只面向群）。
- 会话 id 规则：单聊 `"对方id,我的id"`、群 UUID、应用推送 `"我的id,a_xxx_1"`；应用推送会话**无历史**。
- GUI DOM 仅在 7.2.6 验证；7.1.4 上发送类需先跑 `compat` 看 DOM 命中数。
- 7.1.4 不生成 `em7.db`，`db *` 不可用。
- 自动登入验证码识别非 100%，失败用 `--code` 人工兜底。
- 文件上传耗时与大小相关（21MB≈4 分钟）；确认窗口默认约 1 分钟。

---

## 9. 插件化准备方案

### 9.1 推荐路线：子进程 + `--json`（首选）

理由：CLI 已经是完整稳定的能力封装，自带 `--json`、退出码、安全护栏与脱敏；插件只需做"参数拼装 + 结果解析 + 状态映射"，不必复刻登录/端口/XMPP/GUI 逻辑，也不会把 E-Mobile 依赖塞进插件运行时。

```
插件 Tool → spawn(emcli, ["--json", ...]) → 读取 stdout JSON（按 ok 判断）
                              → 退出码 0/1/2 → 映射为插件结果/错误
```

要点：
- 统一在命令行最前面加 `--json`；读 stdout 全量解析，`ok=false` 或非 0 退出码即失败。
- 发送类 Tool 必须在插件层前置"人工确认/白名单"策略（与 CLI 的 policy 双层护栏一致），并强制传 `--yes`（`--json` 下无人交互）。
- 长任务（发大文件）设置较长超时；`--no-wait` 可立即返回但放弃落地复核。
- 单例约束：发送/GUI 操作同一时刻只允许一个进程操作客户端，插件应串行化发送类调用。

### 9.2 备选路线：Python API 直调（同进程/可控环境）

适合需要进程内复用、避免每次冷启动的场景。用 `EMSession` 门面（`conversations()/resolve_conversation()/xmpp()/rest`、`ensure_login()` 自愈）；发送仍走 `gui.GuiClient`。注意：**返回的 user 是完整值，需自行脱敏**；且需保证插件运行环境已 `pip install -e .` 装好依赖。

### 9.3 建议暴露的插件工具清单（读优先、写受限）

| 建议 Tool | 对应命令 | 风险级别 | 说明 |
|---|---|---|---|
| `emobile_status` | `--json status --probe` | 只读 | 登录态/连通性 |
| `emobile_compat` | `--json compat` | 只读 | 换版本/排障先跑 |
| `emobile_list_conversations` | `--json chat list` | 只读 | 会话+未读 |
| `emobile_history` | `--json chat history` | 只读 | 分页游标 `next_cursor` |
| `emobile_search_contact` | `--json contact search` | 只读 | 手机号已打码 |
| `emobile_list_groups` | `--json group list` | 只读 | |
| `emobile_ding_list` | `--json ding list` | 只读 | |
| `emobile_send_text` | `--json chat send-gui --yes --text-file` | 写（高危） | 仅群；须人工授权 |
| `emobile_send_file` | `--json chat send-file --yes` | 写（高危） | 群；大文件长超时 |
| `emobile_send_image` | `--json chat send-image --yes` | 写（高危） | 图片消息 |
| `emobile_policy_show` | `--json policy show` | 只读 | 看白名单 |

> 不建议直接暴露 `policy add/clear`、`login init/protect`、`launcher install/remove` 给模型自动调用（人的决定 / 需要交互式填凭证或改系统快捷方式）。

### 9.4 落地前待确认（需要你拍板）

1. **插件宿主**：DSH 插件（Host Tool 注册）还是 MCP Server / 其他 agent skill？决定用哪种脚手架与工具 schema 格式。
2. **运行方式**：子进程调 `emcli`（推荐）还是进程内直调 Python？影响依赖打包与冷启动。
3. **写能力开放范围**：是否允许插件发起发送？只开放群发送、且仍保留 CLI policy 白名单为最终闸门？
4. **目标客户端版本**：正式环境 7.1.4 的发送 DOM 未实测，是否需要在 7.1.4 上先跑一次 `emcli compat` 验证再上线发送类工具。

---

## 附：机器可读能力清单

见同目录 [capabilities.json](./capabilities.json)，供插件脚手架直接消费（命令、参数、通道、是否需调试端口、写副作用、退出码、环境变量、限制）。
