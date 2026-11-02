/**
 * Tool 规格（纯数据 + 纯函数，无第三方依赖，可单测）。
 *
 * 每个 spec：
 *   name        工具名
 *   title       简短中文标题（用于日志/文档）
 *   description 给模型看的完整说明
 *   kind        'read' | 'send' | 'diagnose'
 *   parameters  dsh-tools 的 ParameterSchemaSpec
 *   build(args, ctx) -> { argv: string[], timeoutKind?: 'request'|'send' }
 *   timeoutMs   可选固定超时
 *
 * 安全语义：
 *   - 所有发送类都要求配置 `allowSend === true`，并要求调用参数 `confirm === true`；
 *   - 插件**绝不**自动执行 `policy add`；白名单授权必须由人完成；
 *   - 单聊发送服务端不接收（private_chat:0），发送类仅面向群聊。
 */

export const SEND_NOT_ALLOWED_MESSAGE =
  '发送能力未启用：本插件默认只读。要发送，请在 DSH 插件配置里设置 allowSend: true 并完全重启桌面端；'
  + '同时需由人先执行 `emcli policy add <会话>` 授权目标群（插件不会自动加白名单）。'

function limitOf(args, ctx) {
  const n = Number(args?.limit)
  if (Number.isFinite(n) && n > 0) return Math.floor(n)
  return Number(ctx?.config?.defaultLimit) > 0 ? Math.floor(Number(ctx.config.defaultLimit)) : 50
}

/** 组装 `chat list` 参数。 */
export function buildChatList(args = {}, ctx = {}) {
  const argv = ['chat', 'list', '-n', String(limitOf(args, ctx))]
  if (args.unread === true) argv.push('--unread')
  if (typeof args.name === 'string' && args.name.trim()) argv.push('--name', args.name.trim())
  return { argv }
}

/** 组装 `chat history` 参数。 */
export function buildChatHistory(args = {}, ctx = {}) {
  const argv = ['chat', 'history', String(args.conversation), '-n', String(limitOf({ limit: args.limit }, ctx))]
  if (typeof args.before === 'string' && args.before.trim()) argv.push('--before', args.before.trim())
  return { argv }
}

/** 组装 `contact search` 参数。 */
export function buildContactSearch(args = {}, ctx = {}) {
  const argv = ['contact', 'search', String(args.keyword), '-n', String(limitOf({ limit: args.limit }, ctx))]
  if (args.simple === true) argv.push('--simple')
  return { argv }
}

/**
 * 正文是否需要走 `--text-file`。
 * emcli 对多行正文推荐 `--text-file`；虽然本插件用子进程（不经过 shell）能保留换行，
 * 但为避免不同客户端/版本对位置参数的处理差异，多行或超长正文统一落临时文件走该参数。
 */
export function shouldUseTextFile(text) {
  const s = String(text ?? '')
  if (s === '') return false
  return /[\r\n]/.test(s) || s.length > 1500 || s.trimStart().startsWith('-')
}

/** 组装 `chat send-gui` 参数（textFile 优先；否则用位置参数）。 */
export function buildSendText(args = {}) {
  const argv = ['chat', 'send-gui', String(args.conversation)]
  if (typeof args.textFile === 'string' && args.textFile.trim()) {
    argv.push('--text-file', args.textFile.trim())
  } else {
    argv.push(String(args.text ?? ''))
  }
  argv.push('--yes')
  return { argv }
}

/** 组装 `chat send-file` 参数。 */
export function buildSendFile(args = {}) {
  const argv = ['chat', 'send-file', String(args.conversation), String(args.filePath), '--yes']
  if (Number.isFinite(Number(args.attempts))) argv.push('--attempts', String(Math.floor(Number(args.attempts))))
  if (Number.isFinite(Number(args.interval))) argv.push('--interval', String(Math.floor(Number(args.interval))))
  if (args.noWait === true) argv.push('--no-wait')
  return { argv }
}

/** 组装 `chat send-image` 参数。 */
export function buildSendImage(args = {}) {
  const argv = ['chat', 'send-image', String(args.conversation), String(args.filePath), '--yes']
  if (Number.isFinite(Number(args.wait))) argv.push('--wait', String(Math.floor(Number(args.wait))))
  return { argv }
}

/** 组装 `policy show` 参数。 */
export function buildPolicyShow() {
  return { argv: ['policy', 'show'] }
}

/**
 * 发送类统一闸门。返回 null 表示允许，否则返回给模型的拒绝说明。
 * @param {object} spec
 * @param {object} args
 * @param {object} config 插件配置
 */
export function gateSpec(spec, args, config = {}) {
  if (spec.kind === 'control' && config.allowControl === false) {
    return '控制类操作已禁用（config.allowControl=false）。'
  }
  if (spec.kind !== 'send') return null
  if (config.allowSend !== true) return SEND_NOT_ALLOWED_MESSAGE
  if (args?.confirm !== true) {
    return '拒绝发送：必须在调用参数里显式传 `confirm: true`（防止误发到真实群）。'
  }
  return null
}

/** 读类工具一律可直出；发送类成功后也应附带发送结果。 */
export function toolTimeoutMs(spec, config = {}) {
  if (Number.isFinite(Number(spec.timeoutMs)) && Number(spec.timeoutMs) > 0) return Number(spec.timeoutMs)
  if (spec.kind === 'send') {
    const n = Number(config.sendTimeoutMs)
    return Number.isFinite(n) && n > 0 ? n : 600000
  }
  const n = Number(config.requestTimeoutMs)
  return Number.isFinite(n) && n > 0 ? n : 60000
}

const LIMIT_PARAM = {
  type: 'number',
  description: '最多返回条数；省略则用插件配置 defaultLimit（默认 50）。',
}
const CONFIRM_PARAM = {
  type: 'boolean',
  description: '防误发闸门：发送类必须显式传 true，否则直接拒绝。',
}

/** 全部工具规格。 */
export const TOOL_SPECS = [
  {
    name: 'emobile_status',
    title: 'E-Mobile 状态',
    kind: 'read',
    description:
      '显示本机 E-Mobile CLI 的配置与连通状态（登录态来源、消息服务器、可选 REST/XMPP 连通探测）。只读。',
    parameters: {
      probe: { type: 'boolean', description: 'true 时实际探测 REST 与 XMPP 连通性（会产生网络请求）。' },
    },
    build: (args) => ({ argv: ['status', ...(args?.probe === true ? ['--probe'] : [])] }),
  },
  {
    name: 'emobile_compat',
    title: 'E-Mobile 版本能力诊断',
    kind: 'read',
    description:
      '只读诊断：报告 E-Mobile 客户端版本、能力档案、登录态键、调试端口、凭证文件、GUI DOM 命中数。换客户端版本后应先跑它。',
    parameters: {
      probe: { type: 'boolean', description: 'true 时追加 REST/XMPP 连通探测。' },
    },
    build: (args) => ({ argv: ['compat', ...(args?.probe === true ? ['--probe'] : [])] }),
  },
  {
    name: 'emobile_doctor',
    title: 'emcli 定位诊断',
    kind: 'diagnose',
    description:
      '诊断本插件如何定位到 emcli：列出配置/环境变量/PATH/Conda/Anaconda/系统 Python 的候选与命中的那一个（含版本）。'
      + '在 Anaconda 环境下找不到命令时先跑它。',
    parameters: {},
    build: () => ({ argv: [] }),
  },
  {
    name: 'emobile_list_conversations',
    title: '会话列表',
    kind: 'read',
    description:
      '列出 E-Mobile 会话（含未读数、最后一条消息、时间、会话 id）。只读。会话 id 是后续查询历史/发送的操作入口。',
    parameters: {
      limit: LIMIT_PARAM,
      unread: { type: 'boolean', description: '只看有未读的会话。' },
      name: { type: 'string', description: '按会话名子串过滤。' },
    },
    build: buildChatList,
  },
  {
    name: 'emobile_chat_history',
    title: '历史消息',
    kind: 'read',
    description:
      '读取某个会话的历史消息（可用会话名或会话 id）。只读。分页时把上页返回的 next_cursor 传回 before。',
    parameters: {
      conversation: { type: 'string', required: true, description: '会话名或会话 id。' },
      limit: LIMIT_PARAM,
      before: { type: 'string', description: '时间游标（上一页返回的 next_cursor）。' },
    },
    build: buildChatHistory,
  },
  {
    name: 'emobile_search_contact',
    title: '搜索联系人',
    kind: 'read',
    description: '按姓名/拼音搜索组织内人员或群。只读。手机号默认打码。',
    parameters: {
      keyword: { type: 'string', required: true, description: '搜索关键词（姓名/拼音）。' },
      limit: LIMIT_PARAM,
      simple: { type: 'boolean', description: 'true 时用 baseuser/search（不需要 corpid，结果更全）。' },
    },
    build: buildContactSearch,
  },
  {
    name: 'emobile_list_groups',
    title: '我的群组',
    kind: 'read',
    description: '列出我加入的群组。只读。',
    parameters: { limit: LIMIT_PARAM },
    build: (args, ctx) => ({ argv: ['group', 'list', '-n', String(limitOf(args, ctx))] }),
  },
  {
    name: 'emobile_ding_list',
    title: '必达列表',
    kind: 'read',
    description: '列出必达（强提醒）消息。只读。',
    parameters: { limit: LIMIT_PARAM },
    build: (args, ctx) => ({ argv: ['ding', 'list', '-n', String(limitOf(args, ctx))] }),
  },
  {
    name: 'emobile_policy_show',
    title: '发送白名单',
    kind: 'read',
    description: '查看当前允许发送的会话白名单。只读。发送类工具能否成功取决于这份白名单（需人工授权）。',
    parameters: {},
    build: buildPolicyShow,
  },
  {
    name: 'emobile_login_status',
    title: '登录状态',
    kind: 'read',
    description: '只读检查 OA 会话是否有效、是否需要重新登录（不会执行登录）。',
    parameters: {},
    build: () => ({ argv: ['login', 'status'] }),
  },
  {
    name: 'emobile_setup',
    title: '初始化/修复运行环境',
    kind: 'control',
    allowRestart: true,
    timeoutMs: 360000,
    description:
      '初始化 E-Mobile 客户端：确保调试端口就绪（必要时自动拉起/重启客户端）、读取登录态、取 IM 参数。'
      + '注意：为了开调试端口可能会短暂重启客户端，未发送的草稿可能丢失。',
    parameters: {
      noLauncher: { type: 'boolean', description: 'true 时跳过“配置启动入口（改快捷方式）”。' },
      noLaunch: { type: 'boolean', description: 'true 时客户端没运行也不自动拉起。' },
      waitLogin: { type: 'number', description: '本次由工具重启客户端时，等待登录态就绪的秒数（默认 90，0 关闭）。' },
    },
    build: (args = {}) => {
      const argv = ['setup']
      if (args.noLauncher === true) argv.push('--no-launcher')
      if (args.noLaunch === true) argv.push('--no-launch')
      if (Number.isFinite(Number(args.waitLogin))) argv.push('--wait-login', String(Math.floor(Number(args.waitLogin))))
      return { argv }
    },
  },
  {
    name: 'emobile_token_refresh',
    title: '刷新登录态',
    kind: 'control',
    timeoutMs: 120000,
    description:
      '从运行中的 E-Mobile 重读 access_token 与 IM 参数（token 二次轮换后的自愈）。只读客户端，不发送消息。',
    parameters: {
      force: { type: 'boolean', description: 'true 时忽略缓存强制刷新。' },
    },
    build: (args = {}) => ({ argv: ['token', 'refresh', ...(args.force === true ? ['--force'] : [])] }),
  },
  {
    name: 'emobile_logout',
    title: '登出/清除本机登录态',
    kind: 'control',
    timeoutMs: 120000,
    description:
      '清除本机保存的 E-Mobile 登录态（token/IM 参数）。只影响本机 emcli 配置，不退出客户端界面。',
    parameters: {},
    build: () => ({ argv: ['token', 'clear'] }),
  },
  {
    name: 'emobile_launcher_show',
    title: '查看启动入口',
    kind: 'read',
    description: '只读查看 E-Mobile 启动快捷方式现状（公共桌面 lnk / 用户桌面 CLI 启动器）。',
    parameters: {},
    build: () => ({ argv: ['launcher', 'show'] }),
  },
  {
    name: 'emobile_launcher_install',
    title: '配置 CLI 启动入口',
    kind: 'control',
    timeoutMs: 120000,
    description:
      '创建/修改启动快捷方式，让 E-Mobile 以后默认带 --remote-debugging-port 启动（只改快捷方式，不启停客户端）。'
      + '推荐分发给别人时执行一次，之后用 “E-Mobile (CLI).lnk” 启动即可发送。',
    parameters: {
      port: { type: 'number', description: '调试端口（默认 9333）。' },
    },
    build: (args = {}) => {
      const argv = ['launcher', 'install']
      if (Number.isFinite(Number(args.port))) argv.push('--port', String(Math.floor(Number(args.port))))
      return { argv }
    },
  },
  {
    name: 'emobile_launcher_remove',
    title: '回滚启动入口',
    kind: 'control',
    timeoutMs: 120000,
    description: '回滚启动入口：去掉追加的调试端口参数；本工具创建的启动器走回收站（不直接删除）。',
    parameters: {
      port: { type: 'number', description: '只回滚指定端口（默认全部）。' },
    },
    build: (args = {}) => {
      const argv = ['launcher', 'remove']
      if (Number.isFinite(Number(args.port))) argv.push('--port', String(Math.floor(Number(args.port))))
      return { argv }
    },
  },
  {
    name: 'emobile_send_text',
    title: '发送文本',
    kind: 'send',
    description:
      '向指定群聊发送文本消息（驱动官方客户端，经官方通道以本人身份发出）。'
      + '注意：仅群聊可用，单聊服务端不接收；目标群必须已在 emcli 白名单内。'
      + '需要配置 allowSend: true，且调用时必须传 confirm: true。',
    parameters: {
      conversation: { type: 'string', required: true, description: '目标群名或会话 id（仅群聊）。' },
      text: { type: 'string', description: '要发送的正文（多行可直接传入；子进程不经过 shell，换行会保留）。' },
      textFile: { type: 'string', description: '可选：正文 UTF-8 文件绝对路径（与 text 二选一，优先此文件）。' },
      confirm: CONFIRM_PARAM,
    },
    build: buildSendText,
  },
  {
    name: 'emobile_send_file',
    title: '发送文件',
    kind: 'send',
    description:
      '向指定群聊发送文件（拖放上传，自动打开会话并轮询确认落地）。'
      + '仅群聊可用；目标群必须已在 emcli 白名单内；需要 allowSend: true 且 confirm: true。',
    parameters: {
      conversation: { type: 'string', required: true, description: '目标群名或会话 id（仅群聊）。' },
      filePath: { type: 'string', required: true, description: '本地文件绝对路径。' },
      attempts: { type: 'number', description: '落地复核次数（默认 6）。' },
      interval: { type: 'number', description: '复核间隔秒数（默认 10）。' },
      noWait: { type: 'boolean', description: 'true 时提交后立即返回，不做落地复核。' },
      confirm: CONFIRM_PARAM,
    },
    build: buildSendFile,
  },
  {
    name: 'emobile_send_image',
    title: '发送图片',
    kind: 'send',
    description:
      '向指定群聊发送图片消息（对方可预览）。仅群聊可用；目标群必须已在 emcli 白名单内；'
      + '需要 allowSend: true 且 confirm: true。图片默认上限 8MB。',
    parameters: {
      conversation: { type: 'string', required: true, description: '目标群名或会话 id（仅群聊）。' },
      filePath: { type: 'string', required: true, description: '图片文件绝对路径。' },
      wait: { type: 'number', description: '发送后等待输入框清空的秒数（默认 30）。' },
      confirm: CONFIRM_PARAM,
    },
    build: buildSendImage,
  },
]

/** 名称 → 规格。 */
export const TOOL_SPEC_BY_NAME = Object.fromEntries(TOOL_SPECS.map((s) => [s.name, s]))
