/**
 * 真实环境冒烟：验证「随包内置运行时」是否能只用 Python 装配成功，并跑一次 emcli。
 * 用法：node tools/probe-real.mjs
 * 只读，不会发送任何消息。首次运行会把内置依赖离线装到 <DSH_HOME>/storages/dsh-emobile/python-libs。
 */
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createEmcliClient } from '../lib/client.js'

const pluginRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const dshHome = process.env.DSH_HOME && process.env.DSH_HOME.trim()
  ? process.env.DSH_HOME.trim()
  : path.join(os.homedir(), '.dsh')

const config = {
  useBundled: true,
  probeTimeoutMs: 15000,
  requestTimeoutMs: 60000,
  sendTimeoutMs: 600000,
  defaultLimit: 50,
}

const client = createEmcliClient({ config, pluginRoot, dshHome })
const diagnosis = await client.diagnose()
console.log('=== 运行时定位 ===')
console.log(JSON.stringify({ ok: diagnosis.ok, resolved: diagnosis.resolved, bundled: diagnosis.bundled }, null, 2))
if (!diagnosis.ok) {
  console.log('诊断详情：')
  console.log(JSON.stringify(diagnosis, null, 2).slice(0, 4000))
  process.exit(1)
}
for (const argv of [['status'], ['policy', 'show']]) {
  const res = await client.run(argv)
  console.log(`=== emcli ${argv.join(' ')} ===`)
  console.log(JSON.stringify({ ok: res.ok, exitCode: res.exitCode, data: res.data, error: res.error }, null, 2).slice(0, 3000))
}
