/**
 * emcli 定位与调用层（纯 Node，无第三方依赖，便于单测）。
 *
 * 设计目标：把「找得到、跑得动 emcli」这件事做扎实，尤其是 **Anaconda / Miniconda /
 * mamba / PyManager / 系统 Python** 混装、`python` 指向 Microsoft Store 存根、
 * `emcli` 不在 PATH 等常见环境下也能定位。
 *
 * 定位策略（按顺序，命中即用，逐个用 `--version` 实测验证）：
 *   1. 配置 `emcliPath`（emcli.exe 绝对路径）
 *   2. 配置 `pythonPath`（解释器绝对路径 → `python -m cli_anything_e_mobile`）
 *   3. 环境变量 `EMCLI_EXE` / `EMCLI_PYTHON`
 *   4. PATH 上的 `emcli`
 *   5. Conda 根目录（CONDA_PREFIX / CONDA_EXE / MAMBA_ROOT_PREFIX / 常见安装目录 /
 *      ~/.conda/environments.txt / <root>/envs/*）下的 `Scripts\emcli.exe` 与 `python.exe`
 *   6. 常见 Python 安装目录（%LOCALAPPDATA%\Programs\Python\Python*\Scripts 等）
 *   7. PATH 上的 `python` / `python3`
 *   8. Windows `py` 启动器（py -3 -m ...）
 *
 * 安全约束：**不执行 `.cmd` / `.bat`**（避免经由 cmd 的 shell 注入与群名/正文里的 `& | < > ^`
 * 被解释）。只接受 `.exe`/二进制或 Python 解释器；脚本类候选直接拒绝并给出改用 pythonPath 的提示。
 */
import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export const EMCLI_PACKAGE = 'cli_anything_e_mobile'

/** emcli 相关错误；`candidates` 携带已尝试过的候选，便于排障。 */
export class EmcliError extends Error {
  constructor(message, { code = 'EMCLI_ERROR', candidates = [] } = {}) {
    super(message)
    this.name = 'EmcliError'
    this.code = code
    this.candidates = candidates
  }
}

function existsFile(p) {
  try {
    return fs.statSync(p).isFile()
  } catch {
    return false
  }
}

function existsDir(p) {
  try {
    return fs.statSync(p).isDirectory()
  } catch {
    return false
  }
}

/** Windows 批处理脚本：安全策略下拒绝执行。 */
export function isShellScript(p) {
  return /\.(cmd|bat)$/i.test(String(p || ''))
}

function dedupe(items, keyOf) {
  const seen = new Set()
  const out = []
  for (const item of items) {
    const key = keyOf(item)
    if (seen.has(key)) continue
    seen.add(key)
    out.push(item)
  }
  return out
}

/** 拆 PATH（Windows 用 `;`，其余用 `:`）。 */
export function pathDirs({ env = process.env, platform = process.platform } = {}) {
  const raw = env.PATH || env.Path || env.path || ''
  return String(raw)
    .split(platform === 'win32' ? ';' : ':')
    .map((s) => s.trim())
    .filter(Boolean)
}

/** 在 PATH 目录里按名字（含 Windows 扩展名）找可执行文件。 */
export function findOnPath(names, { env = process.env, platform = process.platform } = {}) {
  const exts = platform === 'win32' ? ['.exe', '.cmd', '.bat', ''] : ['']
  const hits = []
  for (const dir of pathDirs({ env, platform })) {
    for (const name of names) {
      for (const ext of exts) {
        const p = path.join(dir, name + ext)
        if (existsFile(p)) {
          hits.push(p)
          break
        }
      }
    }
  }
  return dedupe(hits, (p) => p.toLowerCase())
}

/** 收集 Conda 环境根目录（含 base 与 envs/*）。纯只读探测，不执行 conda 命令。 */
export function condaRoots({ env = process.env, home = os.homedir(), platform = process.platform } = {}) {
  const roots = []
  const add = (p) => {
    if (typeof p === 'string' && p.trim()) roots.push(path.normalize(p.trim()))
  }
  add(env.CONDA_PREFIX)
  add(env.MAMBA_ROOT_PREFIX)
  // CONDA_EXE 通常形如 <root>\Scripts\conda.exe；只有绝对路径才可信（裸名会把 cwd 当候选）
  if (env.CONDA_EXE && path.isAbsolute(env.CONDA_EXE)) {
    add(path.dirname(path.dirname(env.CONDA_EXE)))
  }
  const homeNames = platform === 'win32'
    ? ['anaconda3', 'miniconda3', 'Anaconda3', 'Miniconda3', 'mambaforge', 'micromamba']
    : ['.anaconda3', '.miniconda3', 'anaconda3', 'miniconda3']
  for (const name of homeNames) add(path.join(home, name))
  const programData = env.ProgramData || env.PROGRAMDATA || `${env.SystemDrive || 'C:'}\\ProgramData`
  if (platform === 'win32') {
    for (const name of ['anaconda3', 'miniconda3', 'Anaconda3', 'Miniconda3']) {
      add(path.join(programData, name))
    }
  }
  try {
    const txt = fs.readFileSync(path.join(home, '.conda', 'environments.txt'), 'utf8')
    for (const line of txt.split(/\r?\n/)) {
      const t = line.trim()
      if (t && !t.startsWith('#') && path.isAbsolute(t)) add(t)
    }
  } catch {
    /* 正常情况：没有该文件 */
  }
  const expanded = []
  for (const root of roots) {
    if (!existsDir(root)) continue
    expanded.push(root)
    try {
      for (const name of fs.readdirSync(path.join(root, 'envs'))) {
        const p = path.join(root, 'envs', name)
        if (existsDir(p)) expanded.push(p)
      }
    } catch {
      /* 没有 envs 目录 */
    }
  }
  return dedupe(expanded, (p) => p.toLowerCase())
}

/** Conda/普通 Python 根目录里可能的 emcli 可执行文件（不含 .cmd/.bat）。 */
function emcliInRoot(root, platform) {
  const list = platform === 'win32'
    ? [path.join(root, 'Scripts', 'emcli.exe'), path.join(root, 'emcli.exe')]
    : [path.join(root, 'bin', 'emcli')]
  return list.filter(existsFile)
}

export function pythonInRoot(root, platform) {
  const candidates = platform === 'win32'
    ? [path.join(root, 'python.exe'), path.join(root, 'Scripts', 'python.exe')]
    : [path.join(root, 'bin', 'python'), path.join(root, 'bin', 'python3')]
  return candidates.find(existsFile) || null
}

/** 常见 Python 独立安装根（不含 Conda）。 */
export function pythonInstallRoots({ env = process.env, home = os.homedir(), platform = process.platform } = {}) {
  const roots = []
  if (platform !== 'win32') return roots
  const localAppData = env.LOCALAPPDATA || path.join(home, 'AppData', 'Local')
  const programs = path.join(localAppData, 'Programs', 'Python')
  try {
    for (const name of fs.readdirSync(programs)) {
      const p = path.join(programs, name)
      if (existsDir(p)) roots.push(p)
    }
  } catch {
    /* 没有该目录 */
  }
  const systemDriveRoot = `${env.SystemDrive || 'C:'}\\`
  try {
    for (const name of fs.readdirSync(systemDriveRoot)) {
      if (/^Python\d/i.test(name)) {
        const p = path.join(systemDriveRoot, name)
        if (existsDir(p)) roots.push(p)
      }
    }
  } catch {
    /* 无权限或该盘不可读 */
  }
  return dedupe(roots, (p) => p.toLowerCase())
}

/**
 * 生成候选执行描述列表（不做实际验证）。
 * 每项：{ command, args, source, kind: 'exe' | 'python' }
 * 脚本类（.cmd/.bat）一律不进入候选。
 */
export function candidateSpecs({
  config = {},
  env = process.env,
  home = os.homedir(),
  platform = process.platform,
} = {}) {
  const specs = []
  const add = (command, args, source, kind) => {
    if (typeof command !== 'string' || !command.trim()) return
    if (isShellScript(command)) return
    specs.push({ command: command.trim(), args: [...args], source, kind })
  }

  if (config.emcliPath) add(config.emcliPath, [], 'config.emcliPath', 'exe')
  if (config.pythonPath) add(config.pythonPath, ['-m', EMCLI_PACKAGE], 'config.pythonPath', 'python')
  if (env.EMCLI_EXE) add(env.EMCLI_EXE, [], 'env.EMCLI_EXE', 'exe')
  if (env.EMCLI_PYTHON) add(env.EMCLI_PYTHON, ['-m', EMCLI_PACKAGE], 'env.EMCLI_PYTHON', 'python')
  for (const p of findOnPath(['emcli'], { env, platform })) add(p, [], 'PATH:emcli', 'exe')
  for (const root of condaRoots({ env, home, platform })) {
    for (const exe of emcliInRoot(root, platform)) add(exe, [], `conda:${root}`, 'exe')
    const py = pythonInRoot(root, platform)
    if (py) add(py, ['-m', EMCLI_PACKAGE], `conda-python:${root}`, 'python')
  }
  for (const root of pythonInstallRoots({ env, home, platform })) {
    for (const exe of emcliInRoot(root, platform)) add(exe, [], `python-install:${root}`, 'exe')
    const py = pythonInRoot(root, platform)
    if (py) add(py, ['-m', EMCLI_PACKAGE], `python-install:${root}`, 'python')
  }
  for (const p of findOnPath(['python', 'python3'], { env, platform })) {
    add(p, ['-m', EMCLI_PACKAGE], 'PATH:python', 'python')
  }
  if (platform === 'win32') {
    for (const p of findOnPath(['py'], { env, platform })) {
      add(p, ['-3', '-m', EMCLI_PACKAGE], 'py-launcher', 'python')
    }
  }

  return dedupe(specs, (s) => `${s.command.toLowerCase()}|${s.args.join(' ')}`)
}

/** 杀掉进程树（Windows 用 taskkill /T /F；其它用 SIGKILL）。 */
function killProcessTree(child) {
  const pid = child?.pid
  if (!pid) {
    try { child?.kill?.('SIGKILL') } catch { /* 已退出 */ }
    return
  }
  if (process.platform === 'win32') {
    try {
      spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
      return
    } catch {
      /* 退回直接 kill */
    }
  }
  try { child.kill('SIGKILL') } catch { /* 已退出 */ }
}

/**
 * 运行一个子进程并收集输出。
 * 返回 { code, stdout, stderr, timedOut, aborted, spawnError, rejected }。
 * 超时/取消时杀**进程树**并等待 close（或 300ms 兜底）后才 resolve。
 */
export function runProcess(command, args, { timeoutMs = 30000, signal, env, spawnImpl, cwd } = {}) {
  const doSpawn = spawnImpl || spawn
  return new Promise((resolve) => {
    if (isShellScript(command)) {
      resolve({
        code: -1,
        stdout: '',
        stderr: `拒绝执行批处理脚本（安全考虑，避免 shell 注入）：${command}。请改用 .exe，或在插件配置里使用 pythonPath。`,
        spawnError: true,
        rejected: true,
      })
      return
    }
    let child
    try {
      child = doSpawn(command, args, {
        windowsHide: true,
        env: env || process.env,
        cwd: cwd || undefined,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
    } catch (error) {
      resolve({ code: -1, stdout: '', stderr: String(error?.message || error), spawnError: true })
      return
    }

    let stdout = ''
    let stderr = ''
    let settled = false
    let timer = null
    let forceTimer = null
    let timedOut = false
    let aborted = false

    const cleanup = () => {
      if (timer) clearTimeout(timer)
      if (forceTimer) clearTimeout(forceTimer)
      if (signal) {
        try { signal.removeEventListener('abort', onAbort) } catch { /* ignore */ }
      }
    }
    const finish = (code, extra = {}) => {
      if (settled) return
      settled = true
      cleanup()
      resolve({ code, stdout, stderr, ...extra })
    }
    const terminate = (code, extra) => {
      if (settled) return
      killProcessTree(child)
      // 等待 close；最多 300ms 后兜底 resolve
      forceTimer = setTimeout(() => finish(code, extra), 300)
    }
    const onAbort = () => {
      aborted = true
      terminate(130, { aborted: true })
    }

    timer = setTimeout(() => {
      timedOut = true
      terminate(124, { timedOut: true })
    }, Math.max(200, Number(timeoutMs) || 30000))

    if (signal) {
      if (signal.aborted) onAbort()
      else signal.addEventListener('abort', onAbort, { once: true })
    }

    child.stdout?.on('data', (chunk) => { stdout += chunk.toString('utf8') })
    child.stderr?.on('data', (chunk) => { stderr += chunk.toString('utf8') })
    child.on('error', (error) => finish(-1, { spawnError: true, stderr: stderr + String(error?.message || error) }))
    child.on('close', (code) => {
      if (timedOut) return finish(124, { timedOut: true })
      if (aborted) return finish(130, { aborted: true })
      return finish(typeof code === 'number' ? code : -1)
    })
  })
}

function extractVersion(text) {
  const m = String(text).match(/\d+\.\d+\.\d+/)
  return m ? m[0] : String(text).trim().split(/\r?\n/).pop() || ''
}

/** 实测验证一个候选：跑 `--version`，退出码 0 且输出像版本号才算可用。 */
export async function probeSpec(spec, { timeoutMs = 15000, runProcessImpl, signal, env } = {}) {
  const result = await (runProcessImpl || runProcess)(spec.command, [...spec.args, '--version'], { timeoutMs, signal, env })
  const text = `${result.stdout || ''}\n${result.stderr || ''}`.trim()
  if (result.rejected) return { ok: false, error: text }
  if (result.timedOut) return { ok: false, error: `探测超时（${timeoutMs}ms）` }
  if (result.spawnError) return { ok: false, error: text || '无法启动进程' }
  if (result.code === 0 && /cli-anything-e-mobile|cli_anything_e_mobile|\d+\.\d+\.\d+/.test(text)) {
    return { ok: true, version: extractVersion(text) }
  }
  return { ok: false, error: text ? text.slice(0, 400) : `退出码 ${result.code}` }
}

/** 解析出首个可用的 emcli。`cache` 可传入 { value } 做进程内缓存。 */
export async function resolveEmcli({
  config = {},
  env = process.env,
  home = os.homedir(),
  platform = process.platform,
  probeTimeoutMs = 15000,
  cache = null,
  runProcessImpl,
  signal,
} = {}) {
  if (cache && cache.value) return cache.value
  if (config.emcliPath && isShellScript(config.emcliPath)) {
    throw new EmcliError(
      `emcliPath 指向批处理脚本 ${config.emcliPath}，出于安全（避免 shell 注入）插件拒绝执行。`
      + '请改成 emcli.exe 的绝对路径，或改用 pythonPath 指向解释器。',
      { code: 'EMCLI_SHELL_SCRIPT_REJECTED' },
    )
  }
  const specs = candidateSpecs({ config, env, home, platform })
  const tried = []
  for (const spec of specs) {
    if (signal?.aborted) throw new EmcliError('解析 emcli 已被取消', { code: 'ABORTED', candidates: tried })
    const probe = await probeSpec(spec, { timeoutMs: probeTimeoutMs, runProcessImpl, signal, env })
    if (probe.ok) {
      const resolved = { ...spec, version: probe.version }
      if (cache) cache.value = resolved
      return resolved
    }
    tried.push({ ...spec, error: probe.error })
  }
  throw new EmcliError(
    '找不到可用的 emcli（cli_anything_e_mobile）。请在插件配置里设置 emcliPath（emcli.exe 绝对路径）'
    + '或 pythonPath（解释器绝对路径，Anaconda 环境亦可），或把 emcli 加入 PATH。',
    { code: 'EMCLI_NOT_FOUND', candidates: tried },
  )
}

/** 从输出里尽力抠出第一个 JSON 对象（容忍前置日志行）。 */
export function extractJson(stdout) {
  const text = String(stdout || '').trim()
  if (!text) return { ok: false }
  try {
    return { ok: true, value: JSON.parse(text) }
  } catch {
    /* 继续尝试截取 */
  }
  const start = text.indexOf('{')
  const end = text.lastIndexOf('}')
  if (start >= 0 && end > start) {
    const slice = text.slice(start, end + 1)
    try {
      return { ok: true, value: JSON.parse(slice) }
    } catch {
      return { ok: false }
    }
  }
  return { ok: false }
}

/** 以 `--json` 调用 emcli。 */
export async function runEmcliJson(resolved, argv, {
  timeoutMs = 60000,
  signal,
  spawnImpl,
  env,
} = {}) {
  const args = [...(resolved.args || []), '--json', ...argv]
  const result = await runProcess(resolved.command, args, { timeoutMs, signal, spawnImpl, env })
  const parsed = extractJson(result.stdout)
  const base = {
    exitCode: result.code,
    timedOut: Boolean(result.timedOut),
    stdout: result.stdout,
    stderr: result.stderr,
    argv: args,
  }
  if (result.timedOut) {
    return { ...base, ok: false, code: 'TIMEOUT', error: `emcli 执行超时（${timeoutMs}ms）` }
  }
  if (result.aborted) {
    return { ...base, ok: false, code: 'ABORTED', error: 'emcli 执行已取消' }
  }
  if (result.spawnError) {
    return { ...base, ok: false, code: 'SPAWN_FAILED', error: (result.stderr || '无法启动 emcli').slice(0, 800) }
  }
  if (!parsed.ok) {
    return {
      ...base,
      ok: false,
      code: 'BAD_JSON',
      parseError: true,
      raw: String(result.stdout || '').slice(0, 4000),
      error: `emcli 输出不是 JSON（退出码 ${result.code}）：${String(result.stderr || result.stdout || '').slice(0, 500)}`,
    }
  }
  const data = parsed.value
  const blocked = data?.blocked === true || (result.code === 2 && data?.ok === false)
  const ok = result.code === 0 && data?.ok !== false && !blocked
  return { ...base, ok, blocked, data }
}

/** 把 runEmcliJson 的结果压成适合模型阅读的精简对象。 */
export function summarizeRun(result) {
  const summary = { ok: Boolean(result.ok), exitCode: result.exitCode }
  if (result.blocked) {
    summary.blocked = true
    summary.hint = '发送被 emcli 白名单拦下：需要先人工执行 `emcli policy add <会话>` 授权，再重试。'
  }
  if (result.timedOut) summary.timedOut = true
  if (result.code) summary.code = result.code
  if (result.error && !result.ok) summary.error = result.error
  if (result.data !== undefined) summary.data = result.data
  if (result.parseError) {
    summary.parseError = true
    summary.raw = result.raw
  }
  if (result.stderr && !result.ok) summary.stderr = String(result.stderr).slice(0, 3000)
  return summary
}
