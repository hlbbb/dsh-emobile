import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  ensureBundledRuntime,
  hasBundledCli,
  pythonCandidates,
  runtimeLibsDir,
  vendorPaths,
} from '../lib/runtime.js'

const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

test('随包内置 emcli 源码与 wheels 存在', () => {
  assert.equal(hasBundledCli(PLUGIN_ROOT), true)
  const v = vendorPaths(PLUGIN_ROOT)
  assert.equal(fs.existsSync(v.packageInit), true)
  const wheels = fs.readdirSync(v.wheels).filter((f) => f.endsWith('.whl'))
  assert.ok(wheels.length >= 8)
  for (const need of ['click', 'requests', 'websocket', 'psutil', 'certifi', 'urllib3']) {
    assert.ok(wheels.some((w) => w.toLowerCase().includes(need)), `缺少 wheel: ${need}`)
  }
  assert.equal(wheels.some((w) => /^ddddocr-/i.test(w)), false)
})

test('runtimeLibsDir 默认落在 DSH_HOME 下，可被配置覆盖', () => {
  assert.equal(runtimeLibsDir({ dshHome: 'C:/x/.dsh', config: {} }), path.join('C:/x/.dsh', 'storages', 'dsh-emobile', 'python-libs'))
  assert.equal(runtimeLibsDir({ dshHome: 'C:/x/.dsh', config: { runtimeDir: 'D:/rt' } }), path.resolve('D:/rt'))
})

test('pythonCandidates：config.pythonPath 最优先，且过滤 .cmd', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-emobile-rt-'))
  const conda = path.join(home, 'miniconda3')
  fs.mkdirSync(conda, { recursive: true })
  fs.writeFileSync(path.join(conda, 'python.exe'), 'stub')
  const list = pythonCandidates({
    config: { pythonPath: 'C:/explicit/python.exe' },
    env: { PATH: '', CONDA_PREFIX: conda },
    home,
    platform: 'win32',
  })
  assert.equal(list[0].command, 'C:/explicit/python.exe')
  assert.ok(list.every((c) => !/\.(cmd|bat)$/i.test(c.command)))
  assert.ok(list.some((c) => c.source.startsWith('conda:')))
})

test('ensureBundledRuntime：先导入失败 → 离线装 wheels → 再导入成功', async () => {
  const dshHome = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-emobile-runtime-'))
  const calls = []
  const runProcessImpl = async (command, args, opts = {}) => {
    calls.push({ command, args: [...args], env: opts.env })
    const joined = args.join(' ')
    if (args.includes('pip')) return { code: 0, stdout: 'Successfully installed', stderr: '' }
    if (joined.includes('version_info')) return { code: 0, stdout: '3.11.9', stderr: '' }
    if (joined.includes('import cli_anything_e_mobile')) {
      const pipDone = calls.some((c) => c.args.includes('pip'))
      return pipDone
        ? { code: 0, stdout: '1.0.0', stderr: '' }
        : { code: 1, stdout: '', stderr: "ModuleNotFoundError: No module named 'click'" }
    }
    return { code: 1, stdout: '', stderr: 'nope' }
  }
  const spec = await ensureBundledRuntime({
    config: { pythonPath: 'C:/fake/python.exe' },
    env: { PATH: '' },
    home: '/nonexistent',
    platform: 'win32',
    dshHome,
    pluginRoot: PLUGIN_ROOT,
    runProcessImpl,
  })
  assert.ok(spec.source.startsWith('bundled:'))
  assert.equal(spec.command, 'C:/fake/python.exe')
  assert.deepEqual(spec.args.slice(-2), ['-m', 'cli_anything_e_mobile'])
  assert.ok(spec.env.PYTHONPATH.includes(path.join(PLUGIN_ROOT, 'vendor', 'cli')))
  assert.ok(spec.env.PYTHONPATH.includes(path.join(dshHome, 'storages', 'dsh-emobile', 'python-libs')))
  assert.equal(calls.filter((c) => c.args.includes('pip')).length, 1)
  // 自检必须导入真实入口与依赖，而不是只 import 包本身
  const importCall = calls.find((c) => c.args.join(' ').includes('import cli_anything_e_mobile'))
  assert.ok(importCall, '缺少 import 自检调用')
  const code = importCall.args.join(' ')
  assert.ok(code.includes('cli_anything_e_mobile.cli'), '自检应导入真实入口')
  assert.ok(code.includes('requests') && code.includes('click'), '自检应导入真实依赖')
})

test('ensureBundledRuntime：依赖已在（导入即成功）则不跑 pip', async () => {
  const dshHome = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-emobile-runtime2-'))
  const calls = []
  const runProcessImpl = async (command, args) => {
    calls.push(args.join(' '))
    if (args.join(' ').includes('version_info')) return { code: 0, stdout: '3.12.1', stderr: '' }
    if (args.join(' ').includes('import cli_anything_e_mobile')) return { code: 0, stdout: '1.0.0', stderr: '' }
    return { code: 1, stdout: '', stderr: 'no' }
  }
  await ensureBundledRuntime({
    config: { pythonPath: 'C:/fake/python.exe' },
    env: { PATH: '' },
    home: '/nonexistent',
    platform: 'win32',
    dshHome,
    pluginRoot: PLUGIN_ROOT,
    runProcessImpl,
  })
  assert.equal(calls.some((c) => c.includes('pip')), false)
})

test('ensureBundledRuntime：无可用 Python 时抛 BUNDLED_RUNTIME_FAILED', async () => {
  const dshHome = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-emobile-runtime3-'))
  const runProcessImpl = async () => ({ code: 1, stdout: '', stderr: 'no python' })
  await assert.rejects(
    ensureBundledRuntime({
      config: { pythonPath: 'C:/fake/python.exe' },
      env: { PATH: '' },
      home: '/nonexistent',
      platform: 'win32',
      dshHome,
      pluginRoot: PLUGIN_ROOT,
      runProcessImpl,
    }),
    (error) => error.code === 'BUNDLED_RUNTIME_FAILED',
  )
})
