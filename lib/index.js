/**
 * dsh-emobile —— 把本地 E-Mobile(泛微) 能力接成 DSH Agent Tools。
 *
 * 开箱即用：插件随包携带 emcli 的 Python 源码与最小依赖 wheel（vendor/），
 * 目标机**只要有 Python 3.10+（含 Anaconda）**，首次调用会自动离线装配到
 * `<DSH_HOME>/storages/dsh-emobile/python-libs`，不写死路径、不污染用户 site-packages。
 * 若目标机已装 emcli，也可通过配置 emcliPath / pythonPath 优先使用。
 */
import os from 'node:os'
import path from 'node:path'
import { Config } from './config.js'
import { createEmcliClient } from './client.js'
import { pluginRootFromModuleUrl } from './runtime.js'
import { registerEmobileTools } from './tools.js'

export const name = 'dsh-emobile'
export const inject = ['tools']
export { Config }
export { createEmcliClient } from './client.js'

export function apply(ctx, config) {
  const pluginRoot = pluginRootFromModuleUrl(import.meta.url)
  const dshHome = process.env.DSH_HOME && process.env.DSH_HOME.trim()
    ? process.env.DSH_HOME.trim()
    : path.join(os.homedir(), '.dsh')
  const client = createEmcliClient({ config, pluginRoot, dshHome })
  const count = registerEmobileTools(ctx, { config, client })
  ctx.logger?.info?.(
    'dsh-emobile：已注册 %d 个工具（内置运行时=%s；发送 allowSend=%s）',
    count,
    config?.useBundled === false ? 'off' : 'on',
    config?.allowSend === true ? 'on' : 'off',
  )
}
