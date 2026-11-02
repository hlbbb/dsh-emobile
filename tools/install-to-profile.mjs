#!/usr/bin/env node
/**
 * dsh-emobile 安装 / 体检脚本（只加不删）。
 *
 *   node tools/install-to-profile.mjs --dry-run
 *   node tools/install-to-profile.mjs --emcli-path "C:/.../Scripts/emcli.exe"
 *   node tools/install-to-profile.mjs --python-path "C:/ProgramData/anaconda3/python.exe"
 *   node tools/install-to-profile.mjs --doctor
 *
 * 做的事（三项，全部幂等）：
 *   1) 在 <profile>/node_modules/dsh-emobile 建指向本仓库的 junction；
 *   2) 写 profile package.json：dependencies 增加 link: 依赖 + dsh.profile.bundles 增加 dsh-emobile；
 *   3) 写 profile cordis.patch.yml：upsert `- id: dsh-emobile` 配置块。
 * 不删除任何文件；不覆盖其它条目。
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const DEFAULT_SOURCE = path.resolve(HERE, '..')
const PACKAGE_NAME = 'dsh-emobile'

function parseArgs(argv) {
  const out = { source: DEFAULT_SOURCE, dshHome: '', profile: 'desktop', emcliPath: '', pythonPath: '', dryRun: false, doctor: false, noOcr: false, help: false }
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i]
    const take = () => argv[++i]
    if (a === '--source') out.source = take()
    else if (a === '--dsh-home') out.dshHome = take()
    else if (a === '--profile') out.profile = take()
    else if (a === '--emcli-path') out.emcliPath = take()
    else if (a === '--python-path') out.pythonPath = take()
    else if (a === '--dry-run') out.dryRun = true
    else if (a === '--no-ocr') out.noOcr = true
    else if (a === '--doctor') out.doctor = true
    else if (a === '--help' || a === '-h') out.help = true
    else throw new Error(`未知参数: ${a}`)
  }
  return out
}

function usage() {
  console.log(`用法: node tools/install-to-profile.mjs [选项]
  --source <插件根目录>     默认: 脚本上级目录（即本仓库根）
  --dsh-home <DSH 数据目录> 默认: $DSH_HOME 或 ~/.dsh
  --profile <名称>          默认: desktop
  --emcli-path <绝对路径>   emcli.exe 路径（写入配置；Anaconda 环境可显式给）
  --python-path <绝对路径>  解释器路径（写入配置，用于 python -m）
  --dry-run                 只报告，不写盘
  --no-ocr                  不预装验证码 OCR（ddddocr）；默认安装时按本机 PATH 的 Python 自动安装
  --doctor                  只读体检
  --help                    显示帮助`)
}

function dshHomeOf(explicit) {
  if (explicit) return path.resolve(explicit)
  const configured = process.env.DSH_HOME
  if (configured && configured.trim()) return path.resolve(configured.trim())
  return path.join(os.homedir(), '.dsh')
}

function normalizeLink(p) {
  return p.replace(/\\/g, '/')
}

function readText(file) {
  try { return fs.readFileSync(file, 'utf8') } catch { return null }
}

function writeText(file, text) {
  fs.writeFileSync(file, text, 'utf8')
}

function ensureJunction(source, profileDir, { dryRun }) {
  const linkPath = path.join(profileDir, 'node_modules', PACKAGE_NAME)
  // 用 lstat（而非 existsSync）判断：悬空链接 existsSync 会返回 false，容易被误判为“不存在”。
  let stat = null
  try { stat = fs.lstatSync(linkPath) } catch { stat = null }
  if (stat) {
    if (stat.isSymbolicLink()) {
      let target = ''
      try { target = fs.realpathSync(linkPath) } catch { target = '' }
      if (target && path.resolve(target).toLowerCase() === path.resolve(source).toLowerCase()) {
        return { ok: true, action: 'already', linkPath, target }
      }
      throw new Error(
        `junction 已存在${target ? `但指向别处：${linkPath} -> ${target}` : '但已失效（悬空链接）'}。\n`
        + '请先把该链接改名（不要删除），再重跑。',
      )
    }
    throw new Error(`目标已存在且不是 junction：${linkPath}\n请先改名（不要删除），再重跑。`)
  }
  if (dryRun) return { ok: true, action: 'would-create', linkPath }
  fs.mkdirSync(path.dirname(linkPath), { recursive: true })
  fs.symlinkSync(source, linkPath, process.platform === 'win32' ? 'junction' : 'dir')
  return { ok: true, action: 'created', linkPath }
}

function updatePackageJson(profileDir, source, { dryRun }) {
  const file = path.join(profileDir, 'package.json')
  const raw = readText(file)
  if (raw == null) throw new Error(`找不到 profile package.json: ${file}`)
  const pkg = JSON.parse(raw)
  pkg.dependencies = pkg.dependencies || {}
  const link = `link:${normalizeLink(source)}`
  let changed = false
  if (pkg.dependencies[PACKAGE_NAME] !== link) {
    pkg.dependencies[PACKAGE_NAME] = link
    changed = true
  }
  pkg.dsh = pkg.dsh || {}
  pkg.dsh.profile = pkg.dsh.profile || {}
  const bundles = Array.isArray(pkg.dsh.profile.bundles) ? pkg.dsh.profile.bundles : []
  if (!bundles.includes(PACKAGE_NAME)) {
    bundles.push(PACKAGE_NAME)
    changed = true
  }
  pkg.dsh.profile.bundles = bundles
  if (changed && !dryRun) writeText(file, `${JSON.stringify(pkg, null, 2)}\n`)
  return { ok: true, changed, file }
}

/** 解析已有 dsh-emobile 块里的 config 扁平条目（保留顺序与未知键）。 */
export function parseConfigEntries(lines, start, end) {
  const entries = []
  let inConfig = false
  for (let i = start; i < end; i += 1) {
    const line = lines[i]
    if (/^\s{2}config:\s*$/.test(line)) { inConfig = true; continue }
    if (inConfig) {
      const m = line.match(/^\s{4}([A-Za-z0-9_]+):\s*(.*)$/)
      if (m) entries.push({ key: m[1], raw: line.replace(/\s+$/, '') })
      else if (/^\s{2}\S/.test(line)) inConfig = false
    }
  }
  return entries
}

/**
 * 生成 patch 块文本（不含前后空行）。
 * `existingEntries` 用于**合并保留**已有配置键（如 allowSend/defaultLimit），
 * 只有显式传入非空的 emcliPath / pythonPath 才覆盖对应项。
 */
export function buildPatchBlock({ emcliPath = '', pythonPath = '' } = {}, existingEntries = []) {
  const map = new Map(existingEntries.map((entry) => [entry.key, entry.raw]))
  const setKey = (key, value) => {
    if (typeof value === 'string' && value !== '') map.set(key, `    ${key}: ${JSON.stringify(value)}`)
  }
  setKey('emcliPath', emcliPath)
  setKey('pythonPath', pythonPath)
  const lines = [`- id: ${PACKAGE_NAME}`, `  name: ${PACKAGE_NAME}`, '  disabled: false']
  if (map.size) {
    lines.push('  config:')
    for (const raw of map.values()) lines.push(raw)
  }
  return lines
}

/** upsert 整个 profile 的 cordis.patch.yml 文本（纯函数，便于测试）。 */
export function upsertPatchText(raw, options = {}) {
  const lines = String(raw || '').split(/\r?\n/)
  let start = -1
  for (let i = 0; i < lines.length; i += 1) {
    if (new RegExp(`^-\\s+id:\\s*${PACKAGE_NAME}\\s*$`).test(lines[i])) { start = i; break }
  }
  if (start >= 0) {
    let end = lines.length
    for (let i = start + 1; i < lines.length; i += 1) {
      if (/^-\s/.test(lines[i])) { end = i; break }
    }
    let contentEnd = end
    while (contentEnd - 1 > start && lines[contentEnd - 1].trim() === '') contentEnd -= 1
    const existing = parseConfigEntries(lines, start, contentEnd)
    const block = buildPatchBlock(options, existing)
    const next = [...lines.slice(0, start), ...block, ...lines.slice(end)]
    return `${next.join('\n').replace(/\n+$/, '')}\n`
  }
  const block = buildPatchBlock(options, [])
  const trimmed = String(raw || '').replace(/\s*$/, '')
  const prefix = trimmed.length ? `${trimmed}\n\n` : ''
  return `${prefix}${block.join('\n')}\n`.replace(/\n+$/, '') + '\n'
}

function upsertPatch(profileDir, options, { dryRun }) {
  const file = path.join(profileDir, 'cordis.patch.yml')
  const raw = readText(file) ?? ''
  const text = upsertPatchText(raw, options)
  const changed = text !== raw
  if (changed && !dryRun) writeText(file, text)
  return { ok: true, changed, file }
}

async function doctor(config, { source, profileDir }) {
  const checks = []
  const push = (name, ok, detail) => checks.push({ name, ok, detail })

  push('插件源码', fs.existsSync(path.join(source, 'lib', 'index.js')), source)
  push('内置 emcli 源码', fs.existsSync(path.join(source, 'vendor', 'cli', 'cli_anything_e_mobile', '__init__.py')),
    path.join(source, 'vendor', 'cli'))
  const wheelsDir = path.join(source, 'vendor', 'wheels')
  let wheelCount = 0
  try { wheelCount = fs.readdirSync(wheelsDir).filter((f) => f.endsWith('.whl')).length } catch { wheelCount = 0 }
  push('内置依赖 wheels', wheelCount > 0, `${wheelsDir}（${wheelCount} 个 wheel）`)
  const linkPath = path.join(profileDir, 'node_modules', PACKAGE_NAME)
  let linkOk = false
  let linkDetail = linkPath
  try {
    if (fs.existsSync(linkPath)) {
      const target = fs.realpathSync(linkPath)
      linkOk = path.resolve(target).toLowerCase() === path.resolve(source).toLowerCase()
      linkDetail = `${linkPath} -> ${target}`
    } else {
      linkDetail = '不存在'
    }
  } catch (error) {
    linkDetail = String(error?.message || error)
  }
  push('node_modules junction', linkOk, linkDetail)

  const pkgText = readText(path.join(profileDir, 'package.json')) || ''
  let pkg = null
  try { pkg = JSON.parse(pkgText) } catch { /* 下面判失败 */ }
  const depOk = Boolean(pkg?.dependencies?.[PACKAGE_NAME])
  const bundleOk = Array.isArray(pkg?.dsh?.profile?.bundles) && pkg.dsh.profile.bundles.includes(PACKAGE_NAME)
  push('package.json 依赖', depOk, pkg?.dependencies?.[PACKAGE_NAME] || '缺失')
  push('package.json bundles', bundleOk, bundleOk ? '已包含' : '缺失')

  const patchText = readText(path.join(profileDir, 'cordis.patch.yml')) || ''
  const patchOk = new RegExp(`^-\\s+id:\\s*${PACKAGE_NAME}\\s*$`, 'm').test(patchText)
  push('cordis.patch.yml 条目', patchOk, patchOk ? '已包含' : '缺失')

  // emcli / 内置运行时（只读；这里不触发离线装配）
  const bundledReady = fs.existsSync(path.join(source, 'vendor', 'cli', 'cli_anything_e_mobile', '__init__.py'))
  try {
    const { resolveEmcli } = await import('../lib/emcli.js')
    const resolved = await resolveEmcli({ config, probeTimeoutMs: config.probeTimeoutMs || 15000 })
    push('emcli 可定位', true, `${resolved.command} ${resolved.args.join(' ')} (${resolved.source}, v${resolved.version})`)
  } catch (error) {
    if (bundledReady) {
      push('emcli 可定位', true, '本机未预装 emcli → 将使用内置运行时（首次调用自动离线装配，只需 Python 3.10+）')
    } else {
      push('emcli 可定位', false, String(error?.message || error).split('\n')[0])
    }
  }

  const allOk = checks.every((c) => c.ok)
  console.log('=== dsh-emobile 体检 ===')
  for (const c of checks) console.log(`${c.ok ? '✅' : '❌'} ${c.name}: ${c.detail}`)
  return allOk
}

/**
 * 安装时按本机 PATH/常见目录找到的 Python 预装 ddddocr（离线包不存在时在线装）。
 * 失败不阻塞安装：首次 emobile_login 还会自动重试。
 */
async function installOcrAtInstallTime({ source, dshHome }) {
  try {
    const rt = await import('../lib/runtime.js')
    const libsDir = rt.runtimeLibsDir({ dshHome, config: {} })
    const candidates = rt.pythonCandidates({ config: {}, env: process.env })
    if (!candidates.length) {
      console.log('- OCR: 跳过（PATH/常见目录未找到 Python，可稍后由 emobile_login 自动安装）')
      return
    }
    const py = candidates[0]
    console.log(`- OCR: 使用 ${py.command}（${py.source}）安装 ddddocr 到 ${libsDir}（首次约 1–3 分钟）…`)
    const res = await rt.ensureOcrInstalled({
      spec: { command: py.command, args: ['-m', 'cli_anything_e_mobile'], source: `installer:${py.source}` },
      pluginRoot: source,
      libsDir,
      env: process.env,
      config: {},
    })
    console.log(`- OCR: ${res.ok ? '完成' : `失败（可稍后由 emobile_login 重试）：${(res.error || '').slice(0, 200)}`}`)
  } catch (error) {
    console.log(`- OCR: 跳过（${String(error?.message || error).slice(0, 200)}）`)
  }
}

async function main() {
  let args
  try {
    args = parseArgs(process.argv.slice(2))
  } catch (error) {
    console.error(error.message)
    usage()
    process.exit(2)
  }
  if (args.help) { usage(); return }
  const source = path.resolve(args.source)
  const dshHome = dshHomeOf(args.dshHome)
  const profileDir = path.join(dshHome, 'profiles', args.profile)
  const config = {
    emcliPath: args.emcliPath || '',
    pythonPath: args.pythonPath || '',
    probeTimeoutMs: 15000,
  }

  if (args.doctor) {
    const ok = await doctor(config, { source, profileDir })
    process.exit(ok ? 0 : 2)
  }

  console.log(`插件源码: ${source}`)
  console.log(`目标 profile: ${profileDir}`)
  console.log(`模式: ${args.dryRun ? 'dry-run（不写盘）' : '安装'}`)

  const junction = ensureJunction(source, profileDir, { dryRun: args.dryRun })
  console.log(`- junction: ${junction.action} ${junction.linkPath}`)
  const pkg = updatePackageJson(profileDir, source, { dryRun: args.dryRun })
  console.log(`- package.json: ${pkg.changed ? (args.dryRun ? '将更新' : '已更新') : '无变化'}`)
  const patch = upsertPatch(profileDir, config, { dryRun: args.dryRun })
  console.log(`- cordis.patch.yml: ${patch.changed ? (args.dryRun ? '将更新' : '已更新') : '无变化'}`)
  if (!args.dryRun && !args.noOcr) {
    await installOcrAtInstallTime({ source, dshHome })
  } else if (args.noOcr) {
    console.log('- OCR: 已跳过（--no-ocr）；首次 emobile_login 时仍会自动安装')
  }
  console.log('')
  console.log('完成。请**完全退出并重开 DSH 桌面端**（不是关窗口）后生效。')
  if (!args.emcliPath && !args.pythonPath) {
    console.log('提示：若 emcli 不在 PATH（Anaconda 环境常见），请带 --emcli-path 或 --python-path 重跑一次。')
  }
}

const invokedDirectly = process.argv[1]
  && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
if (invokedDirectly) {
  main().catch((error) => {
    console.error(`安装失败: ${error?.message || error}`)
    process.exit(1)
  })
}
