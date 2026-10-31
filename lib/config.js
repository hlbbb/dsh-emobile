import z from '@deepseek-ai/schemastery'

/**
 * dsh-emobile 插件配置。
 *
 * 说明：本机的 Python 可能是 **Anaconda / Miniconda** 环境，`emcli` 不一定在 PATH 上，
 * 所以插件把「解释器 / 可执行文件」做成显式配置项；不配时也会自动探测 Conda 根目录。
 */
export const Config = z.object({
  /** emcli 可执行文件绝对路径（.exe）。最高优先级；不依赖预装时可留空。 */
  emcliPath: z.string().default(''),
  /** Python 解释器绝对路径（Anaconda 环境的 python.exe 亦可）；用于 `python -m cli_anything_e_mobile`。 */
  pythonPath: z.string().default(''),
  /** 是否优先使用随包内置的 emcli Python 源码 + 依赖（默认 true，开箱即用）。 */
  useBundled: z.boolean().default(true),
  /** 内置依赖安装目录；留空则用 <DSH_HOME>/storages/dsh-emobile/python-libs。 */
  runtimeDir: z.string().default(''),
  /** 首次离线装配依赖的超时（毫秒）。 */
  bootstrapTimeoutMs: z.number().min(10000).default(300000),
  /** 是否允许发送类工具。默认 false（只读）。 */
  allowSend: z.boolean().default(false),
  /** 只读类工具的子进程超时（毫秒）。 */
  requestTimeoutMs: z.number().min(1000).default(60000),
  /** 发送类工具的子进程超时（毫秒）；大文件可调大。 */
  sendTimeoutMs: z.number().min(1000).default(600000),
  /** 探测 emcli `--version` 的超时（毫秒）。 */
  probeTimeoutMs: z.number().min(1000).default(15000),
  /** 列表类工具默认返回条数。 */
  defaultLimit: z.number().min(1).default(50),
})

export default Config
