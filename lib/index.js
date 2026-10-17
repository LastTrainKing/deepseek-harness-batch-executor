/**
 * deepseek-harness-batch-executor — DeepSeek Harness Cordis plugin.
 *
 * Registers the bundled SKILL.md with the DSH skill registry through the
 * `ctx.skills` provider seam (`@deepseek-ai/dsh-skill` ^0.2.0-rc.2), the same
 * seam other skill-pack plugins in the ecosystem target. The provider:
 *
 *   - discovers SKILL.md files under the package root (skipping node_modules
 *     and VCS directories so a dev checkout and an installed package behave
 *     the same),
 *   - parses their YAML frontmatter (quoted scalars and block scalars
 *     `|` / `>`), defusing registry-unsafe names instead of aborting the
 *     whole registration,
 *   - exposes candidates to the registry and serves full skill bodies on
 *     demand.
 *
 * The skill stays a plain directory skill too: cloning this repository into
 * ~/.dsh/skills/ (or <project>/.dsh/skills/) keeps working without the
 * plugin, because SKILL.md sits at the repository root.
 *
 * Pattern reference: @dhicoc/dsh-wuyun-liuqi (skill pack shipped as a Cordis
 * plugin) — this is an independent implementation of the documented seam.
 *
 * @module deepseek-harness-batch-executor
 */

import { readFile, readdir } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { basename, dirname, join } from 'node:path'

/** Cordis plugin id. Must match the `id` in cordis.patch.yml. */
export const name = 'deepseek-harness-batch-executor'
/** Service this plugin registers against. */
export const inject = ['skills']

/** Hard validation the DSH core applies to every candidate name. */
const REGISTRY_NAME_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/

/** Never walk these while discovering SKILL.md files. */
const SKIP_DIRS = new Set(['node_modules', '.git', '.github'])

/**
 * Resolve the bundled package root defensively. Some hosts load plugin
 * bundles through an internal module loader whose `import.meta.url` is not a
 * `file:` URL; a failed resolution disables discovery (partial observation)
 * rather than throwing at module load.
 */
function bundledRoot() {
  try {
    // lib/ sits one level below the package root, where SKILL.md lives.
    return fileURLToPath(new URL('../', import.meta.url))
  } catch {
    return undefined
  }
}
const ROOT = bundledRoot()

/** YAML block scalar indicator: `|` or `>`, with optional chomping/indent. */
const BLOCK_SCALAR_RE = /^([|>])[+-]?\d*$/

/**
 * Stop discovery when either the registration or the caller has been
 * cancelled, so a disposed plugin cannot keep walking the tree.
 */
function throwIfAborted(registration, options) {
  if (registration.aborted) throw registration.reason
  if (options.signal?.aborted) throw options.signal.reason
}

/**
 * Read a YAML block scalar whose `key: |` line sits just above `start`.
 * Returns the joined text and the index of the first line after the block.
 */
function readBlockScalar(lines, start, style) {
  const raw = []
  let i = start
  for (; i < lines.length; i += 1) {
    const line = lines[i]
    if (line.trim() === '') {
      raw.push('')
      continue
    }
    // An unindented line ends the block; this includes the closing `---`.
    if (!/^[ \t]/.test(line)) break
    raw.push(line)
  }
  while (raw.length > 0 && raw[raw.length - 1] === '') raw.pop()

  const indents = raw
    .filter((l) => l !== '')
    .map((l) => (l.match(/^[ \t]*/)[0] ?? '').length)
  const indent = indents.length > 0 ? Math.min(...indents) : 0
  const text = raw.map((l) => (l === '' ? '' : l.slice(indent)))

  if (style === '|') {
    return { value: text.join('\n').trim(), next: i }
  }
  // Folded: single newlines become spaces, blank lines stay paragraph breaks.
  const folded = text.map((l, idx) => (l === '' ? '\n' : idx === 0 ? l : ` ${l}`)).join('')
  return { value: folded.replace(/\n{3,}/g, '\n\n').trim(), next: i }
}

/** Minimal YAML-frontmatter reader — enough for name / description / metadata. */
function parseFrontmatter(text) {
  // Strip an optional UTF-8 BOM and normalize CRLF -> LF so the delimiter
  // search and line regexes are consistent across editor encodings.
  const src = text.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n')
  if (!src.startsWith('---')) return { fm: {}, body: text }
  const end = src.indexOf('\n---', 3)
  if (end === -1) return { fm: {}, body: text }
  const fmText = src.slice(3, end)
  const body = src.slice(end + 4).replace(/^\n/, '')
  const lines = fmText.split('\n')
  const fm = {}
  for (let i = 0; i < lines.length; i += 1) {
    const m = lines[i].match(/^([A-Za-z0-9_-]+):[ \t]*(.*)$/)
    if (!m) continue
    const key = m[1]
    const value = m[2].trim()
    if (BLOCK_SCALAR_RE.test(value)) {
      const block = readBlockScalar(lines, i + 1, value[0])
      fm[key] = block.value
      i = block.next - 1
      continue
    }
    fm[key] = value.replace(/^(['"])([\s\S]*)\1$/, '$2')
  }
  return { fm, body }
}

/**
 * Recursively collect every parseable SKILL.md, reporting whether the walk
 * was exhaustive. An unreadable directory marks the result partial instead
 * of silently narrowing the catalog.
 */
async function collect(root, registration, options) {
  const items = []
  let complete = true

  async function walk(dir) {
    throwIfAborted(registration, options)
    let entries
    try {
      entries = await readdir(dir, { withFileTypes: true })
    } catch {
      complete = false
      return
    }
    for (const e of entries) {
      throwIfAborted(registration, options)
      const p = join(dir, e.name)
      if (e.isDirectory()) {
        if (SKIP_DIRS.has(e.name)) continue
        await walk(p)
      } else if (e.name === 'SKILL.md') {
        const text = await readFile(p, 'utf8')
        const { fm, body } = parseFrontmatter(text)
        if (fm.name) items.push({ path: p, fm, body })
      }
    }
  }

  await walk(root)
  return { items, complete }
}

/**
 * Resolve the registry-safe name for a collected skill. Prefers the
 * frontmatter `name:`; when it would fail the core's hard regex, falls back
 * to the skill's own directory name and warns. Returns undefined when no
 * registry-safe name exists — the caller skips the candidate rather than
 * letting one bad name abort the whole registration.
 */
function resolveName(path, declared) {
  if (REGISTRY_NAME_RE.test(declared)) return declared
  const base = basename(dirname(path))
  // A SKILL.md at the package root has no meaningful directory fallback.
  if (base !== basename(ROOT) && REGISTRY_NAME_RE.test(base)) {
    console.warn(
      `[${name}] SKILL.md frontmatter name "${declared}" is not registry-safe; ` +
        `using directory name "${base}" (${path})`)
    return base
  }
  console.warn(
    `[${name}] skipping SKILL.md with registry-unsafe name "${declared}" ` +
      `and no usable directory fallback (${path})`)
  return undefined
}

/**
 * One provider instance per registration. The catalog cache lives in this
 * closure rather than at module scope, so an HMR remount or a second
 * registration can never reuse candidates built by a disposed provider.
 */
function createProvider(control) {
  let cache = null

  async function build(options) {
    if (cache !== null) return cache
    const walk = ROOT === undefined
      ? { items: [], complete: false }
      : await collect(ROOT, control.signal, options)

    // Deduplicate by registry-safe name; keep the first occurrence.
    const byName = new Map()
    for (const item of walk.items) {
      const safe = resolveName(item.path, item.fm.name)
      if (safe === undefined) continue
      if (!byName.has(safe)) byName.set(safe, { ...item, fm: { ...item.fm, name: safe } })
    }

    const candidates = [...byName.values()].map(({ path, fm }) => ({
      name: fm.name,
      description: fm.description ?? '',
      invocation: { modelInvocable: true, userInvocable: true },
      provider: name,
      source: 'bundled',
      resourceBase: { kind: 'directory', path: dirname(path) },
      path,
      rank: 0,
      locator: pathToFileURL(path),
      metadata: fm,
    }))

    const catalog = { candidates, complete: walk.complete }
    // Only memoize a trustworthy catalog; a partial walk retries next time.
    if (catalog.complete) cache = catalog
    return catalog
  }

  return {
    name,
    async list(options = {}) {
      throwIfAborted(control.signal, options)
      const catalog = await build(options)
      return { candidates: catalog.candidates, complete: catalog.complete }
    },
    async get(candidate, options = {}) {
      throwIfAborted(control.signal, options)
      const text = await readFile(candidate.path, 'utf8')
      const { body } = parseFrontmatter(text)
      return {
        name: candidate.name,
        description: candidate.description,
        invocation: candidate.invocation,
        provider: candidate.provider,
        source: candidate.source,
        resourceBase: candidate.resourceBase,
        path: candidate.path,
        content: body,
        metadata: candidate.metadata,
      }
    },
  }
}

/** Register the bundled skill provider on `ctx.skills`. */
export function apply(ctx) {
  ctx.skills.registerProvider((control) => createProvider(control))
}
