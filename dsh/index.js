/**
 * treg as a DeepSeek Harness (dsh) plugin.
 *
 * Registers one `ctx.skills` provider that publishes the generated
 * `dsh/skills/treg/SKILL.md`. The MCP connector is NOT here: it is a separate
 * row in `cordis.patch.yml`, disabled until `TREG_TOKEN` exists, so the skill
 * still installs and reads with no token at all.
 *
 * Two constraints on this file, both load-bearing:
 *
 * - **Plain ESM, no build step.** `dsh plugin add github:...` fetches sources
 *   rather than build output, and pnpm refuses to run a git dependency's
 *   `prepare` script until the user allowlists it - so a compiled bundle would
 *   fail every first install.
 * - **No `@deepseek-ai/*` dependency.** An out-of-tree bundle depending on an
 *   in-box package installs a second copy that drifts from the host's. The
 *   provider's `list`/`get` contract is the stable seam, so it is implemented
 *   directly here - the shape `dsh-skill-badge` uses for its own skill.
 */

import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const PROVIDER_NAME = 'treg'
const SKILL_NAME = 'treg'
/** Rank of the `bundled` discovery source in dsh's own skill provider. */
const BUNDLED_SKILL_RANK = 600

const SKILL_DIR_URL = new URL('./skills/treg/', import.meta.url)
const SKILL_BODY_URL = new URL('SKILL.md', SKILL_DIR_URL)
const RESOURCE_BASE = { kind: 'directory', path: fileURLToPath(SKILL_DIR_URL) }
const INVOCATION = { modelInvocable: true, userInvocable: true }

/**
 * Read the one-line `description` from SKILL.md's frontmatter.
 *
 * SKILL.md is generated from `src/treg/web/skill.md`, which is the one source
 * for how treg describes itself. Restating the description here would be a
 * second copy that nothing regenerates. The frontmatter is flat scalars, so a
 * YAML dependency would be more surface than the job needs.
 *
 * @param {string} body - the raw SKILL.md contents.
 * @returns {string | undefined} the description, or undefined when absent.
 */
function readDescription(body) {
  const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---/.exec(body)
  if (!frontmatter) return undefined
  const line = /^description:[ \t]*(.+)$/m.exec(frontmatter[1])
  if (!line) return undefined
  const raw = line[1].trim()
  if (raw.startsWith('"') && raw.endsWith('"') && raw.length > 1) {
    return raw.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, '\\')
  }
  if (raw.startsWith("'") && raw.endsWith("'") && raw.length > 1) {
    return raw.slice(1, -1).replace(/''/g, "'")
  }
  return raw
}

/**
 * Load the packaged skill, or `undefined` when it cannot be read.
 *
 * @param {AbortSignal | undefined} signal - lookup cancellation.
 * @returns {Promise<{ description: string, content: string } | undefined>}
 */
async function loadSkill(signal) {
  const content = await readFile(SKILL_BODY_URL, { encoding: 'utf8', signal })
  const description = readDescription(content)
  if (description === undefined) return undefined
  return { description, content }
}

const provider = {
  name: PROVIDER_NAME,
  async list(options = {}) {
    const skill = await loadSkill(options.signal)
    if (skill === undefined) return []
    return [{
      name: SKILL_NAME,
      description: skill.description,
      invocation: INVOCATION,
      provider: PROVIDER_NAME,
      source: 'bundled',
      resourceBase: RESOURCE_BASE,
      rank: BUNDLED_SKILL_RANK,
      locator: SKILL_BODY_URL,
    }]
  },
  async get(_candidate, options = {}) {
    const skill = await loadSkill(options.signal)
    if (skill === undefined) return undefined
    return {
      name: SKILL_NAME,
      description: skill.description,
      invocation: INVOCATION,
      provider: PROVIDER_NAME,
      source: 'bundled',
      resourceBase: RESOURCE_BASE,
      content: skill.content,
    }
  },
}

/** Cordis plugin name. */
export const name = 'treg-skill'
/** The registry this plugin contributes to. */
export const inject = ['skills']

/**
 * Register the packaged treg skill on `ctx.skills`.
 *
 * @param {import('@deepseek-ai/cordis').Context} ctx - the plugin context.
 */
export function apply(ctx) {
  ctx.skills.registerProvider(() => provider)
}
