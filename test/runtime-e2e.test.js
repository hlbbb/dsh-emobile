import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { ensureBundledRuntime, pythonCandidates } from '../lib/runtime.js'
import { runProcess } from '../lib/emcli.js'

const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// 动态找宿主 Python（不写死路径）；也可用 DSH_EMOBILE_TEST_PYTHON 显式指定。
const HOST_PY = process.env.DSH_EMOBILE_TEST_PYTHON
  || pythonCandidates({ env: process.env, home: os.homedir(), platform: process.platform })[0]?.command
  || ''

/**
 * 真实端到端：用一个**干净 venv（无任何第三方依赖）**模拟“目标机只有 Python、没预装 emcli”。
 * 验证离线装配真的触发（libs 落地）并能运行 emcli。
 * 依赖本机存在可创建 venv 的 Python；缺失时跳过。设置 DSH_EMOBILE_SKIP_E2E=1 可强制跳过。
 */
test('E2E：干净 venv 下离线装配并运行 emcli', async (t) => {
  if (process.env.DSH_EMOBILE_SKIP_E2E === '1') return t.skip('DSH_EMOBILE_SKIP_E2E=1')
  if (!fs.existsSync(HOST_PY)) return t.skip(`宿主 Python 不存在: ${HOST_PY}`)

  const venvDir = path.join(os.tmpdir(), 'dsh-emobile-e2e-venv')
  const venvPy = process.platform === 'win32'
    ? path.join(venvDir, 'Scripts', 'python.exe')
    : path.join(venvDir, 'bin', 'python')
  if (!fs.existsSync(venvPy)) {
    const made = await runProcess(HOST_PY, ['-m', 'venv', venvDir], { timeoutMs: 180000 })
    assert.equal(made.code, 0, `创建 venv 失败: ${made.stderr}`)
  }

  const dshHome = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-emobile-e2e-home-'))
  const spec = await ensureBundledRuntime({
    config: { pythonPath: venvPy, bootstrapTimeoutMs: 300000 },
    env: process.env,
    home: os.homedir(),
    platform: process.platform,
    dshHome,
    pluginRoot: PLUGIN_ROOT,
  })
  assert.ok(spec.source.startsWith('bundled:'))

  const libs = path.join(dshHome, 'storages', 'dsh-emobile', 'python-libs')
  assert.ok(fs.existsSync(path.join(libs, 'click')), `离线依赖未落地: ${libs}`)
  assert.ok(fs.existsSync(path.join(libs, 'requests')))

  const run = await runProcess(spec.command, [...spec.args, '--version'], { env: spec.env, timeoutMs: 60000 })
  assert.equal(run.code, 0, `运行失败: ${run.stderr}`)
  assert.match(`${run.stdout}${run.stderr}`, /cli-anything-e-mobile/)
})
