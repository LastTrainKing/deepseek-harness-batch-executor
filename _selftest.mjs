/**
 * Smoke test for the plugin without a DSH runtime.
 *
 * Mocks the `ctx.skills` seam exactly as the host would call it
 * (registerProvider with a control carrying a lifecycle signal), then drives
 * the registered provider's list()/get() and asserts the bundled SKILL.md is
 * discovered and served.
 *
 * Run: node _selftest.mjs   (also wired as `npm test`)
 */
import assert from 'node:assert/strict'
import * as plugin from './lib/index.js'

const EXPECTED = 'deepseek-harness-batch-executor'

// --- mock the host seam ---------------------------------------------------
const providers = []
const ctx = {
  skills: {
    registerProvider(factory) {
      const control = {
        signal: new AbortController().signal,
        invalidate() {},
      }
      providers.push(factory(control))
    },
  },
}

// --- register ---------------------------------------------------------------
assert.equal(plugin.name, EXPECTED, 'plugin id must match cordis.patch.yml')
assert.deepEqual(plugin.inject, ['skills'])
plugin.apply(ctx)
assert.equal(providers.length, 1, 'apply must register exactly one provider')
assert.equal(providers[0].name, EXPECTED, 'provider name must match the plugin id')

// --- list -------------------------------------------------------------------
const observation = await providers[0].list({})
assert.equal(observation.complete, true, 'the catalog walk must be exhaustive')

const ours = observation.candidates.find((c) => c.name === EXPECTED)
assert.ok(ours, 'the bundled SKILL.md must be discovered')
assert.match(ours.description, /DeepSeek Harness/, 'description comes from frontmatter')
assert.equal(ours.provider, EXPECTED)
assert.equal(ours.source, 'bundled')
assert.equal(ours.resourceBase.kind, 'directory')
assert.ok(ours.path.endsWith('SKILL.md'), 'path must point at the SKILL.md file')
assert.ok(ours.locator instanceof URL, 'locator must be a file URL')
assert.equal(ours.invocation.modelInvocable, true)
assert.equal(ours.invocation.userInvocable, true)

// --- get --------------------------------------------------------------------
const definition = await providers[0].get(ours, {})
assert.equal(definition.name, EXPECTED)
assert.equal(definition.provider, EXPECTED)
assert.ok(
  definition.content.includes('<skill_instructions>'),
  'the full skill body must be served',
)
assert.ok(definition.content.length > 2000, 'the skill body must not be truncated')
assert.equal(
  definition.metadata.whenToUse.includes('batch'),
  true,
  'extra frontmatter fields must survive as metadata',
)

// --- abort behaviour ---------------------------------------------------------
const aborted = new AbortController()
aborted.abort(new Error('disposed'))
let threw = false
try {
  await providers[0].list({ signal: aborted.signal })
} catch {
  threw = true
}
assert.ok(threw, 'an aborted lookup must reject instead of returning a catalog')

console.log('selftest ok — skill discovered, served, and abort-honoured')
