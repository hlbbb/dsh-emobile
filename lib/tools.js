/**
 * 把 Tool 规格登记进 DSH Tool 注册表。
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { summarizeRun } from './emcli.js'
import { gateSpec, shouldUseTextFile, toolTimeoutMs, TOOL_SPECS } from './tool-specs.js'

/** 统一输出：一个 text 字段承载 JSON 文本，模型直接读。 */
const TEXT_OUTPUT = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string', required: true } },
  },
  render: (_args, value) => [{ type: 'text', text: value.text }],
}

function textOf(payload) {
  return { text: typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2) }
}

/**
 * 把多行/超长正文落成临时文件，供 `chat send-gui --text-file` 使用。
 * 文件名按内容哈希，重复正文复用同一文件（不删除，交由操作系统临时目录清理）。
 */
function materializeText(text) {
  const dir = path.join(os.tmpdir(), 'dsh-emobile-outbox')
  fs.mkdirSync(dir, { recursive: true })
  const hash = createHash('sha1').update(String(text), 'utf8').digest('hex').slice(0, 16)
  const file = path.join(dir, `body-${hash}.txt`)
  fs.writeFileSync(file, String(text), 'utf8')
  return file
}

/**
 * @param {object} ctx  插件上下文（需含 tools 服务）
 * @param {{config: object, client: object}} deps
 * @returns {number} 注册的工具数量
 */
export function registerEmobileTools(ctx, { config, client }) {
  for (const spec of TOOL_SPECS) {
    ctx.tools.register(
      defineTool({
        name: spec.name,
        description: spec.description,
        parameters: spec.parameters,
        output: TEXT_OUTPUT,
        timeoutMs: toolTimeoutMs(spec, config),
        async execute(args, exec) {
          const input = args || {}
          const gate = gateSpec(spec, input, config)
          if (gate) return textOf({ ok: false, tool: spec.name, error: gate })
          try {
            if (spec.kind === 'diagnose') {
              const diagnosis = await client.diagnose({ signal: exec?.signal })
              return textOf({ tool: spec.name, ...diagnosis })
            }
            // 发送文本：缺正文直接拒绝；多行/超长自动落 --text-file。
            if (spec.name === 'emobile_send_text') {
              const hasFile = typeof input.textFile === 'string' && input.textFile.trim() !== ''
              const text = input.text
              if (!hasFile && (text === undefined || text === null || String(text) === '')) {
                return textOf({ ok: false, tool: spec.name, error: '请提供 text 或 textFile。' })
              }
              if (!hasFile && shouldUseTextFile(text)) {
                input.textFile = materializeText(text)
              }
            }
            const built = spec.build(input, { config })
            const result = await client.run(built.argv, {
              timeoutMs: toolTimeoutMs(spec, config),
              signal: exec?.signal,
              allowRestart: spec.allowRestart === true,
            })
            return textOf({ tool: spec.name, ...summarizeRun(result) })
          } catch (error) {
            return textOf({ ok: false, tool: spec.name, error: String(error?.message || error) })
          }
        },
      }),
    )
  }
  return TOOL_SPECS.length
}
