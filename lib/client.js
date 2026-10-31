/**
 * emcli 客户端：优先使用**随包内置的 Python emcli**（目标机只要有一个 Python 即可），
 * 其次回退到目标机已安装的 emcli。进程内缓存 + 并发去重。
 */
import { candidateSpecs, probeSpec, resolveEmcli, runEmcliJson } from './emcli.js'
import { ensureBundledRuntime, hasBundledCli, pythonCandidates } from './runtime.js'

export function createEmcliClient({
  config = {},
  env,
  home,
  platform,
  runProcessImpl,
  pluginRoot,
  dshHome,
} = {}) {
  const cache = { value: null }
  const baseEnv = env || process.env
  const base = { config, env: baseEnv, home, platform, runProcessImpl }
  const probeTimeoutMs = (() => {
    const n = Number(config.probeTimeoutMs)
    return Number.isFinite(n) && n > 0 ? n : 15000
  })()
  let inflight = null
  let lastBundledError = null

  /** 对外展示的 spec：**绝不回传完整 env**（可能含进程密钥）。 */
  function publicSpec(spec) {
    if (!spec) return null
    return {
      command: spec.command,
      args: spec.args,
      source: spec.source,
      kind: spec.kind,
      version: spec.version,
      pythonVersion: spec.pythonVersion,
    }
  }

  /** 尝试内置运行时；成功返回 spec，失败返回 null（原因记在 lastBundledError）。 */
  async function tryBundled(signal) {
    lastBundledError = null
    if (config.useBundled === false) return null
    if (!pluginRoot || !hasBundledCli(pluginRoot)) return null
    try {
      const spec = await ensureBundledRuntime({
        config,
        env: baseEnv,
        home,
        platform,
        dshHome,
        pluginRoot,
        signal,
        runProcessImpl,
      })
      const probe = await probeSpec(spec, { timeoutMs: probeTimeoutMs, signal, runProcessImpl, env: spec.env })
      if (probe.ok) return { ...spec, version: probe.version }
      lastBundledError = probe.error || '内置运行时自检失败'
    } catch (error) {
      lastBundledError = String(error?.message || error)
    }
    return null
  }

  return {
    /** 解析运行时（内置优先，其次本机已安装 emcli）。 */
    async resolve(signal) {
      if (cache.value) return cache.value
      if (!inflight) {
        inflight = (async () => {
          const bundled = await tryBundled(signal)
          if (bundled) {
            cache.value = bundled
            return bundled
          }
          return resolveEmcli({ ...base, probeTimeoutMs, cache, signal })
        })().finally(() => { inflight = null })
      }
      return inflight
    },

    /** 以 `--json` 执行一次 emcli 子命令。 */
    async run(argv, { timeoutMs, signal, spawnImpl } = {}) {
      const resolved = await this.resolve(signal)
      const runEnv = { ...(env || process.env), ...(resolved.env || {}) }
      return runEmcliJson(resolved, argv, { timeoutMs, signal, spawnImpl, env: runEnv })
    },

    /** 定位诊断：内置运行时状态 + 本机 Python 候选 + 已安装 emcli 探测。 */
    async diagnose({ limit = 25, budgetMs = 20000, signal } = {}) {
      const python = pluginRoot
        ? pythonCandidates({ config, env: baseEnv, home, platform }).map((c) => ({ command: c.command, args: c.args, source: c.source }))
        : []
      let bundled = null
      if (config.useBundled !== false && pluginRoot && hasBundledCli(pluginRoot)) {
        try {
          const spec = await ensureBundledRuntime({
            config, env: baseEnv, home, platform, dshHome, pluginRoot, signal, runProcessImpl,
          })
          const probe = await probeSpec(spec, { timeoutMs: probeTimeoutMs, signal, runProcessImpl, env: spec.env })
          bundled = { ok: probe.ok, spec: publicSpec(spec), error: probe.error }
          if (probe.ok) {
            cache.value = { ...spec, version: probe.version }
            return { ok: true, resolved: publicSpec(cache.value), bundled, python, tried: [], truncated: false }
          }
        } catch (error) {
          bundled = { ok: false, error: String(error?.message || error) }
        }
      }
      const specs = candidateSpecs(base)
      const deadline = Date.now() + Math.max(1000, Number(budgetMs) || 20000)
      const tried = []
      for (const spec of specs.slice(0, limit)) {
        if (signal?.aborted) break
        const remaining = deadline - Date.now()
        if (remaining <= 0) break
        const probe = await probeSpec(spec, { timeoutMs: Math.min(probeTimeoutMs, remaining), signal, runProcessImpl })
        tried.push({
          command: spec.command,
          args: spec.args,
          source: spec.source,
          kind: spec.kind,
          ok: probe.ok,
          version: probe.version,
          error: probe.error,
        })
        if (probe.ok) {
          const resolved = { ...spec, version: probe.version }
          cache.value = resolved
          return { ok: true, resolved: publicSpec(resolved), bundled, python, tried, truncated: specs.length > limit }
        }
      }
      return { ok: false, bundled, python, tried, truncated: specs.length > limit, budgetExceeded: Date.now() >= deadline }
    },
  }
}
