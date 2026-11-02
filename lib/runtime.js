/**
 * 内置运行时装配：让插件做到「目标机只要有一个 Python 就能开箱即用」。
 *
 * 插件随包携带 `vendor/cli`（emcli 的 Python 源码）与 `vendor/wheels`（最小依赖 wheel）。
 * 运行时不写死任何路径：
 *   1. 在目标机自动找一个可用 Python（配置 → 环境变量 → Conda/Anaconda → 常见安装 → PATH）；
 *   2. 把内置依赖离线安装到**插件私有目录** `<DSH_HOME>/storages/dsh-emobile/python-libs`；
 *   3. 以 `python -m cli_anything_e_mobile` 运行，`PYTHONPATH` 指向内置源码 + 私有依赖目录。
 * 不修改用户全局 site-packages，不依赖 emcli 预装。
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  condaRoots,
  EMCLI_PACKAGE,
  EmcliError,
  findOnPath,
  isShellScript,
  pythonInRoot,
  pythonInstallRoots,
  runProcess,
} from './emcli.js'

export const VENDOR_DIR = 'vendor'

/** 插件根目录（本文件在 <root>/lib/ 下）。 */
export function pluginRootFromModuleUrl(moduleUrl) {
  try {
    return path.resolve(path.dirname(fileURLToPath(moduleUrl)), '..')
  } catch {
    return path.resolve(process.cwd())
  }
}

export function vendorPaths(pluginRoot) {
  const base = path.join(pluginRoot, VENDOR_DIR)
  return {
    base,
    cli: path.join(base, 'cli'),
    wheels: path.join(base, 'wheels'),
    requirements: path.join(base, 'requirements.txt'),
    requirementsOcr: path.join(base, 'requirements-ocr.txt'),
    packageInit: path.join(base, 'cli', 'cli_anything_e_mobile', '__init__.py'),
  }
}

export function hasBundledCli(pluginRoot) {
  return fs.existsSync(vendorPaths(pluginRoot).packageInit)
}

export function runtimeLibsDir({ dshHome, config = {} } = {}) {
  if (typeof config.runtimeDir === 'string' && config.runtimeDir.trim()) return path.resolve(config.runtimeDir.trim())
  const home = dshHome || process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
  return path.join(home, 'storages', 'dsh-emobile', 'python-libs')
}

/** 候选 Python（按优先级；脚本类 .cmd/.bat 拒绝）。 */
export function pythonCandidates({ config = {}, env = process.env, home = os.homedir(), platform = process.platform } = {}) {
  const out = []
  const add = (command, args, source) => {
    if (typeof command !== 'string' || !command.trim() || isShellScript(command)) return
    out.push({ command: command.trim(), args: [...(args || [])], source })
  }
  if (config.pythonPath) add(config.pythonPath, [], 'config.pythonPath')
  if (env.EMCLI_PYTHON) add(env.EMCLI_PYTHON, [], 'env.EMCLI_PYTHON')
  for (const root of condaRoots({ env, home, platform })) {
    const py = pythonInRoot(root, platform)
    if (py) add(py, [], `conda:${root}`)
  }
  for (const root of pythonInstallRoots({ env, home, platform })) {
    const py = pythonInRoot(root, platform)
    if (py) add(py, [], `python-install:${root}`)
  }
  for (const p of findOnPath(['python', 'python3'], { env, platform })) add(p, [], 'PATH:python')
  if (platform === 'win32') {
    for (const p of findOnPath(['py'], { env, platform })) add(p, ['-3'], 'py-launcher')
  }
  const seen = new Set()
  return out.filter((item) => {
    const key = `${item.command.toLowerCase()}|${item.args.join(' ')}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function composePythonPath(vendorCli, libsDir, env) {
  const parts = [vendorCli, libsDir]
  if (env.PYTHONPATH) parts.push(env.PYTHONPATH)
  return parts.join(path.delimiter)
}

function runtimeEnv(baseEnv, vendorCli, libsDir) {
  const env = baseEnv || process.env
  return {
    ...env,
    PYTHONPATH: composePythonPath(vendorCli, libsDir, env),
    // 不在插件目录里生成 __pycache__（插件目录可能只读，也会污染发布包）
    PYTHONDONTWRITEBYTECODE: '1',
    PYTHONPYCACHEPREFIX: path.join(libsDir, 'pycache'),
  }
}

async function runPython(cand, extraArgs, { runProcessImpl, env, timeoutMs = 15000, signal, cwd } = {}) {
  return (runProcessImpl || runProcess)(cand.command, [...cand.args, ...extraArgs], { env, timeoutMs, signal, cwd })
}

/** 探测 Python 版本是否 >= 3.10。 */
export async function probePythonVersion(cand, { runProcessImpl, env, signal } = {}) {
  const res = await runPython(cand, ['-c', "import sys;print('%d.%d.%d'%sys.version_info[:3])"], {
    runProcessImpl, env, timeoutMs: 10000, signal,
  })
  if (res.spawnError || res.timedOut || res.code !== 0) {
    return { ok: false, error: (res.stderr || res.stdout || '无法运行 python').slice(0, 300) }
  }
  const m = String(res.stdout).match(/(\d+)\.(\d+)\.(\d+)/)
  if (!m) return { ok: false, error: '无法识别 Python 版本' }
  const [major, minor] = [Number(m[1]), Number(m[2])]
  if (major < 3 || (major === 3 && minor < 10)) {
    return { ok: false, error: `Python ${m[0]} 低于 3.10` }
  }
  return { ok: true, version: m[0] }
}

/**
 * 真实依赖自检：必须导入**真实入口 cli_anything_e_mobile.cli 与全部必需第三方依赖**。
 * 注意：cli_anything_e_mobile/__init__.py 只有版本号，只导入包本身永远成功 —— 不能作为自检。
 */
export function requiredModules(platform = process.platform) {
  const base = [EMCLI_PACKAGE + '.cli', 'click', 'requests', 'websocket', 'urllib3', 'certifi', 'idna', 'charset_normalizer']
  if (platform === 'win32') base.push('psutil', 'colorama')
  return base
}

export async function checkImport(cand, { vendorCli, libsDir, baseEnv, runProcessImpl, signal, platform = process.platform } = {}) {
  const env = runtimeEnv(baseEnv, vendorCli, libsDir)
  const modules = requiredModules(platform).join(', ')
  const code = `import ${modules}; import ${EMCLI_PACKAGE} as m; print(getattr(m,'__version__','?'))`
  const res = await runPython(cand, ['-c', code], { runProcessImpl, env, timeoutMs: 20000, signal })
  if (res.code === 0) return { ok: true, version: String(res.stdout).trim() }
  return { ok: false, error: (res.stderr || res.stdout || '导入失败').slice(0, 500) }
}

/** 用内置 wheels 离线安装到私有依赖目录（--target，不污染用户环境）。 */
export async function installBundledDeps(cand, { wheels, requirements, libsDir, baseEnv, runProcessImpl, signal, timeoutMs = 300000, cwd } = {}) {
  fs.mkdirSync(libsDir, { recursive: true })
  // pip install 只是把 wheel 解到 --target，不需要 PYTHONPATH；用干净的基础环境即可。
  const env = { ...(baseEnv || process.env), PYTHONDONTWRITEBYTECODE: '1' }
  const args = [
    '-m', 'pip', 'install',
    '--no-index',
    '--find-links', wheels,
    '--target', libsDir,
    '-r', requirements,
  ]
  let res = await runPython(cand, args, { runProcessImpl, env, timeoutMs, signal, cwd: cwd || os.tmpdir() })
  if (res.code !== 0 && /No module named pip/i.test(`${res.stderr}${res.stdout}`)) {
    // embeddable Python 可能没有 pip：先 ensurepip 再重试一次。
    await runPython(cand, ['-m', 'ensurepip', '--upgrade'], { runProcessImpl, env, timeoutMs: 120000, signal, cwd: cwd || os.tmpdir() })
    res = await runPython(cand, args, { runProcessImpl, env, timeoutMs, signal, cwd: cwd || os.tmpdir() })
  }
  if (res.code === 0) return { ok: true }
  return { ok: false, error: (res.stderr || res.stdout || 'pip 安装失败').slice(0, 800) }
}

function bundledSpec(cand, version, vendorCli, libsDir, baseEnv) {
  return {
    command: cand.command,
    args: [...cand.args, '-m', EMCLI_PACKAGE],
    env: runtimeEnv(baseEnv, vendorCli, libsDir),
    source: `bundled:${cand.source}`,
    kind: 'python',
    pythonVersion: version,
  }
}

/** 进程内并发锁：同一目标目录只允许一个装配链（避免并发 pip 损坏 libs）。 */
const pendingInstalls = new Map()
/** OCR 安装并发锁（与运行时安装分开，但同目录）。 */
const pendingOcr = new Map()

/**
 * 按需安装验证码 OCR（ddddocr 及其依赖）到同一个私有 libsDir。
 * 仅在需要自动登录时调用，避免每次启动都装 300MB。
 */
export function ensureOcrInstalled({ spec, pluginRoot, libsDir, env, runProcessImpl, signal, log, config = {} } = {}) {
  const key = `${libsDir || ''}|ocr`
  if (pendingOcr.has(key)) return pendingOcr.get(key)
  const promise = ensureOcrInstalledUnlocked({ spec, pluginRoot, libsDir, env, runProcessImpl, signal, log, config })
    .finally(() => pendingOcr.delete(key))
  pendingOcr.set(key, promise)
  return promise
}

async function ensureOcrInstalledUnlocked({ spec, pluginRoot, libsDir, env, runProcessImpl, signal, log, config = {} }) {
  const vendor = vendorPaths(pluginRoot)
  const pyArgs = Array.isArray(spec?.args) ? spec.args.slice(0, -2) : []
  const cand = { command: spec.command, args: pyArgs, source: `ocr:${spec.source || ''}` }
  const envFor = spec.env || runtimeEnv(env, vendor.cli, libsDir)
  const check = async () => runPython(cand, ['-c', 'import ddddocr;print("ok")'], {
    runProcessImpl, env: envFor, timeoutMs: 180000, signal,
  })
  const first = await check()
  if (first.code === 0) return { ok: true, already: true }

  fs.mkdirSync(libsDir, { recursive: true })
  // 有本地 ddddocr wheel 时优先离线；否则按本地 Python 版本在线安装（默认走 PyPI，可配镜像）。
  const hasLocalOcrWheel = fs.existsSync(vendor.wheels)
    && fs.readdirSync(vendor.wheels).some((f) => /^ddddocr-/i.test(f))
  let installed
  if (hasLocalOcrWheel && fs.existsSync(vendor.requirementsOcr)) {
    log?.('dsh-emobile：安装验证码 OCR（离线 wheel，首次可能 1–3 分钟）…')
    installed = await installBundledDeps(cand, {
      wheels: vendor.wheels,
      requirements: vendor.requirementsOcr,
      libsDir,
      baseEnv: envFor,
      runProcessImpl,
      signal,
      timeoutMs: config.ocrTimeoutMs || 900000,
    })
  } else {
    log?.('dsh-emobile：按当前 Python 版本在线安装 ddddocr（首次约 1–3 分钟）…')
    const args = ['-m', 'pip', 'install', '--target', libsDir, '--upgrade', 'ddddocr>=1.4']
    if (config.pipIndexUrl) args.push('--index-url', String(config.pipIndexUrl))
    if (config.pipExtraIndexUrl) args.push('--extra-index-url', String(config.pipExtraIndexUrl))
    const timeoutMs = config.ocrTimeoutMs || 900000
    let res = await runPython(cand, args, { runProcessImpl, env: envFor, timeoutMs, signal, cwd: os.tmpdir() })
    if (res.code !== 0 && /No module named pip/i.test(`${res.stderr}${res.stdout}`)) {
      await runPython(cand, ['-m', 'ensurepip', '--upgrade'], { runProcessImpl, env: envFor, timeoutMs: 120000, signal, cwd: os.tmpdir() })
      res = await runPython(cand, args, { runProcessImpl, env: envFor, timeoutMs, signal, cwd: os.tmpdir() })
    }
    installed = res.code === 0
      ? { ok: true }
      : { ok: false, error: (res.stderr || res.stdout || 'pip install ddddocr 失败').slice(0, 800) }
  }
  if (!installed.ok) return { ok: false, error: `OCR 依赖安装失败：${installed.error}` }
  const again = await check()
  if (again.code === 0) return { ok: true }
  return { ok: false, error: `OCR 安装后仍无法导入：${(again.stderr || again.stdout || '').slice(0, 400)}` }
}

/**
 * 确保内置运行时可用，返回可直接 `--json` 调用的 spec。
 * 失败抛 EmcliError（含每个 Python 候选的失败原因）。
 * 并发调用共享同一个装配 Promise。
 */
export function ensureBundledRuntime(options = {}) {
  const cfg = options.config || {}
  const key = `${runtimeLibsDir({ dshHome: options.dshHome, config: cfg })}|${options.pluginRoot || ''}|${cfg.pythonPath || ''}`
  if (pendingInstalls.has(key)) return pendingInstalls.get(key)
  const promise = ensureBundledRuntimeUnlocked(options).finally(() => pendingInstalls.delete(key))
  pendingInstalls.set(key, promise)
  return promise
}

async function ensureBundledRuntimeUnlocked({
  config = {},
  env = process.env,
  home = os.homedir(),
  platform = process.platform,
  dshHome,
  pluginRoot,
  signal,
  runProcessImpl,
  log,
} = {}) {
  const vendor = vendorPaths(pluginRoot)
  if (!hasBundledCli(pluginRoot)) {
    throw new EmcliError('插件缺少内置 emcli 源码（vendor/cli）。请使用完整发布包。', { code: 'BUNDLED_VENDOR_MISSING' })
  }
  const libsDir = runtimeLibsDir({ dshHome, config })
  const candidates = pythonCandidates({ config, env, home, platform })
  const errors = []
  for (const cand of candidates) {
    if (signal?.aborted) throw new EmcliError('内置运行时装配已取消', { code: 'ABORTED' })
    const ver = await probePythonVersion(cand, { runProcessImpl, env, signal })
    if (!ver.ok) { errors.push({ ...cand, error: ver.error }); continue }
    const imported = await checkImport(cand, { vendorCli: vendor.cli, libsDir, baseEnv: env, runProcessImpl, signal, platform })
    if (imported.ok) {
      return bundledSpec(cand, ver.version, vendor.cli, libsDir, env)
    }
    log?.(`dsh-emobile：${cand.command} 缺少依赖，离线安装内置 wheels 到 ${libsDir}`)
    const installed = await installBundledDeps(cand, {
      wheels: vendor.wheels,
      requirements: vendor.requirements,
      libsDir,
      baseEnv: env,
      runProcessImpl,
      signal,
      timeoutMs: config.bootstrapTimeoutMs || 300000,
    })
    if (!installed.ok) { errors.push({ ...cand, error: `依赖安装失败：${installed.error}` }); continue }
    const recheck = await checkImport(cand, { vendorCli: vendor.cli, libsDir, baseEnv: env, runProcessImpl, signal, platform })
    if (recheck.ok) return bundledSpec(cand, ver.version, vendor.cli, libsDir, env)
    errors.push({ ...cand, error: `安装后仍无法导入：${recheck.error}` })
  }
  throw new EmcliError(
    '未找到可用的 Python 运行时来装配内置 emcli。请确认目标机已安装 Python 3.10+（Anaconda 亦可），'
    + '或在插件配置里设置 pythonPath。',
    { code: 'BUNDLED_RUNTIME_FAILED', candidates: errors },
  )
}
