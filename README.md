# dsh-emobile

DSH 原生插件：把 **E-Mobile（泛微移动办公）** 的会话/消息/发送能力接成 **DSH Agent Tools**。

**开箱即用、可分发**：插件随包携带 `emcli` 的 Python 源码与最小依赖 wheel（`vendor/`）。
目标机**只要有 Python 3.10+（含 Anaconda / Miniconda）并装有 E-Mobile 客户端**，无需预装 `emcli`、
无需联网、无需管理员权限 —— 首次调用会把依赖离线装配到插件私有目录，然后直接可用。
**不写死任何机器路径**：Python 解释器、DSH 数据目录、依赖目录全部运行时探测或由配置注入。

## 工作原理

```
DSH 插件 (host)
  lib/index.js      apply → 注册 13 个 Tool
  lib/client.js     内置运行时优先；失败回退本机已装 emcli
  lib/runtime.js    找 Python → 私有目录离线装 vendor/wheels → python -m cli_anything_e_mobile
  lib/emcli.js      定位/子进程调用/JSON 解析（拒绝 .cmd，避免 shell 注入）
  vendor/cli/       emcli 的 Python 源码（cli_anything_e_mobile）
  vendor/wheels/    最小依赖 wheel（click/requests/websocket-client/psutil/urllib3/certifi/charset-normalizer/idna/colorama）
```

- 内置依赖安装目录：`<DSH_HOME>/storages/dsh-emobile/python-libs`（可用 `runtimeDir` 覆盖），**不污染用户 site-packages**。
- 只读能力（会话/历史/搜人/群/必达/状态）只需要 Python；发送类仍依赖 E-Mobile 客户端 GUI 与调试端口。
- 若目标机已装 `emcli` 且想优先用它：配置 `emcliPath` 或 `pythonPath`（此时跳过内置运行时）。

## 工具清单（13）

| 工具 | 类型 | 说明 |
|---|---|---|
| `emobile_status` | 只读 | 配置 + 连通状态 |
| `emobile_compat` | 只读 | 客户端版本/能力诊断 |
| `emobile_doctor` | 只读 | 运行时定位诊断（内置 Python/依赖/已装 emcli） |
| `emobile_list_conversations` | 只读 | 会话 + 未读 + 最后消息 |
| `emobile_chat_history` | 只读 | 历史消息（分页 `before`） |
| `emobile_search_contact` | 只读 | 搜人/群（手机号打码） |
| `emobile_list_groups` | 只读 | 我的群组 |
| `emobile_ding_list` | 只读 | 必达列表 |
| `emobile_policy_show` | 只读 | 发送白名单 |
| `emobile_login_status` | 只读 | 是否需要重新登录 |
| `emobile_send_text` | 写（受限） | 发文本（仅群） |
| `emobile_send_file` | 写（受限） | 发文件（仅群） |
| `emobile_send_image` | 写（受限） | 发图片（仅群） |

## 配置（profile `cordis.patch.yml` 的 `- id: dsh-emobile` 下）

| 键 | 默认 | 说明 |
|---|---|---|
| `useBundled` | `true` | 优先用内置 emcli 源码+依赖（开箱即用） |
| `runtimeDir` | `''` | 内置依赖安装目录；留空用 DSH_HOME 下私有目录 |
| `bootstrapTimeoutMs` | `300000` | 首次离线装配依赖超时 |
| `emcliPath` | `''` | 可选：已有 emcli.exe 绝对路径（最高优先级） |
| `pythonPath` | `''` | 可选：已有 Python 解释器绝对路径（Anaconda 亦可） |
| `allowSend` | `false` | 是否允许发送类工具 |
| `allowRestart` | `false` | 是否允许 emcli 自动重启/强杀客户端；默认 false（绝不重启/登出） |
| `requestTimeoutMs` | `60000` | 只读类超时 |
| `sendTimeoutMs` | `600000` | 发送类超时 |
| `probeTimeoutMs` | `15000` | 探测超时 |
| `defaultLimit` | `50` | 列表默认条数 |

## 安全

- 发送默认关闭；即使开启也需调用参数 `confirm: true`；插件**绝不自动** `policy add`。
- 插件**绝不自动重启/登出/登录客户端**：所有调用强制 `EMCLI_NO_RESTART=1`、`EMCLI_NO_FORCE=1`；没有调试端口时发送会明确报错，不会动客户端。
- 读能力（会话/历史/搜人/群/必达/状态）在 OA 正常登录运行时即可用，**不需要调试端口**；发送类（文本/文件/图片）目前仍需客户端已带 `--remote-debugging-port=9333` 启动（用户手动用 CLI 快捷方式启动，插件不代做）。
- 拒绝执行 `.cmd/.bat`（避免 shell 注入，群名/正文含 `&` 也安全）。
- 脱敏沿用 emcli：token/工号掩码；会话 id 作为操作入口原样返回。

## 分发

把**整个插件目录**（必须包含 `vendor/`）压缩分发即可；`package.json` 的 `files` 已包含 `vendor`。
不要只拷贝 `lib/`，否则内置运行时缺失。

## 验证

```powershell
npm test                       # 41 项单测（含真实干净 venv 离线装配 E2E）
node tools\probe-real.mjs      # 用内置运行时真实跑 status / policy show
node tools\install-to-profile.mjs --doctor
```

安装见 [INSTALL.md](./INSTALL.md)。

## 已知限制

- 发送类需 E-Mobile 客户端图形界面登录态 + 调试端口；插件不会自动重启/登录，端口未开时会直接报错。
- 内置 wheel 不含验证码 OCR（ddddocr 体积大）；如需要客户端自动登录，可在目标机 `pip install dddocr` 或配置已有 emcli。
- 内置 wheel 不含 **pywin32**（体积大且按 Python 版本区分）：DPAPI 凭证加密有 **ctypes 兜底**、`.lnk` 读写有 **PowerShell COM 兜底**；若某些 Windows 环境仍报 win32 相关错误，可 `pip install pywin32` 或配置已有 emcli。
- 内置 `psutil` wheel 为 **Windows x64**；Windows arm64 请自备，或让插件回退本机已装 emcli。
- GUI DOM 仅 7.2.6 验证；7.1.4 先跑 `emobile_compat`。
- 插件不热加载：改代码/配置后需完全重启 DSH 桌面端。
