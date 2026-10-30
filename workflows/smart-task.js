export const meta = {
  name: 'smart-task',
  description: 'Cost- and quota-aware classify, route, implement, test and review loop with Claude/Codex fallback',
  whenToUse:
    'Invoked by /cost-aware-orchestrator:smart-task. Requires args {task, timestamp, plugin_root}; optional {classification, task_id, handoff}. Workflow scripts have no shell or clock, so the caller supplies the timestamp and plugin root.',
  phases: [
    { title: 'Classify', detail: 'deterministic keyword classifier' },
    { title: 'Route', detail: 'engine state, config and routing decisions' },
    { title: 'Scout', detail: 'cheap repository lookup for nontrivial tasks' },
    { title: 'Plan', detail: 'architect pass for complex or critical tasks' },
    { title: 'Implement', detail: 'routed engine implements' },
    { title: 'Test', detail: 'configured project test command' },
    { title: 'Fix', detail: 'failing tests: one fix pass, then one escalation' },
    { title: 'Review', detail: 'independent review, preferably on the other engine' },
    { title: 'Fallback', detail: 'usage limit: mark engine, write handoff, switch' },
    { title: 'Finalize', detail: 'record results in engine state' },
  ],
}

// `args` may arrive as a JSON string depending on the invoking runtime.
const ARGS = typeof args === 'string' ? (() => { try { return JSON.parse(args) } catch (e) { return { task: args } } })() : (args || {})

const ROUTES = ['trivial', 'standard', 'complex', 'critical']
const PLUGIN = 'cost-aware-orchestrator'
const TASK = String(ARGS.task || '').trim()
const ROOT = String(ARGS.plugin_root || '')
if (!TASK) throw new Error('smart-task requires args.task')
// The plugin root is interpolated into shell commands: allow only a plain absolute path.
if (!/^\/[A-Za-z0-9._\/ -]+$/.test(ROOT)) throw new Error(`smart-task requires a plain absolute args.plugin_root, got ${JSON.stringify(ROOT)}`)

const SCRIPTS = `${ROOT}/scripts`
const q = s => `'${String(s).replace(/'/g, `'\\''`)}'`
const PY = name => `python3 ${q(`${SCRIPTS}/${name}`)}`

function fnv1a(text) {
  let h = 0x811c9dc5
  for (const ch of text) { h ^= ch.codePointAt(0); h = Math.imul(h, 0x01000193) >>> 0 }
  return h.toString(16).padStart(8, '0')
}

const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const slug = TASK.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+/, '').slice(0, 32).replace(/-+$/, '') || 'task'
const TASK_ID = SAFE_ID.test(String(ARGS.task_id || '')) ? ARGS.task_id : `${slug}-${fnv1a(`${TASK}|${ARGS.timestamp || 'no-timestamp'}`)}`
const RUN = `.claude/orchestrator/runs/${TASK_ID}`

// Newline-joined so heredoc delimiters stay alone on their lines; any failure aborts.
const script = lines => ['set -e', ...lines].join('\n')

// Write text to a file without any shell interpretation of its contents.
function heredoc(path, text) {
  const delim = `ORCH_${fnv1a(text)}_EOF`
  if (text.split('\n').includes(delim)) throw new Error('text collides with heredoc delimiter')
  return `cat > ${q(path)} <<'${delim}'\n${text}\n${delim}`
}

// ---------------------------------------------------------------------------
// bash(): workflow scripts cannot spawn processes, so a minimal Haiku agent runs
// each command verbatim and returns its exit code and output.
// ponytail: one cheap agent per command; a native workflow shell would remove this hop.
const BASH_SCHEMA = {
  type: 'object',
  required: ['exit_code', 'stdout', 'stderr'],
  properties: {
    exit_code: { type: 'number' },
    stdout: { type: 'string', description: 'verbatim, last 6000 characters at most' },
    stderr: { type: 'string', description: 'verbatim, last 2000 characters at most' },
  },
}

async function bash(command, label, phaseName, timeoutMs = 120000) {
  const r = await agent(
    `Run exactly this command once with the Bash tool (timeout ${timeoutMs} ms) from the project root.
Do not change it, do not run anything else, do not interpret or fix anything.
Report the exit code, stdout and stderr verbatim.

<command>
${command}
</command>`,
    { label, phase: phaseName, schema: BASH_SCHEMA, model: 'haiku', effort: 'low' },
  )
  if (!r) throw new Error(`bash helper failed to run ${label}`)
  return r
}

function parseJson(text, label) {
  const start = text.indexOf('{'), end = text.lastIndexOf('}')
  try { return JSON.parse(text.slice(start, end + 1)) } catch (e) { throw new Error(`${label}: unparseable JSON output`) }
}

function sections(stdout) {
  const parts = stdout.split(/^===([A-Z_]+)===\s*$/m)
  const out = {}
  for (let i = 1; i < parts.length; i += 2) out[parts[i]] = parts[i + 1].trim()
  return out
}

// ---------------------------------------------------------------------------
// Schemas for agent results.
const SCOUT_SCHEMA = {
  type: 'object',
  required: ['files', 'impact'],
  properties: {
    files: { type: 'array', items: { type: 'string' } },
    symbols: { type: 'array', items: { type: 'string' } },
    impact: { type: 'string' },
    route: { type: 'string', enum: ROUTES },
    route_reason: { type: 'string' },
  },
}
const PLAN_SCHEMA = {
  type: 'object',
  required: ['assessment', 'steps', 'risks', 'test_strategy'],
  properties: {
    assessment: { type: 'string' },
    steps: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
    test_strategy: { type: 'string' },
    rollback: { type: 'string' },
  },
}
const IMPL_SCHEMA = {
  type: 'object',
  required: ['status', 'summary', 'files_changed', 'completed_work', 'remaining_work', 'usage_limited'],
  properties: {
    status: { type: 'string', enum: ['done', 'partial', 'blocked'] },
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    completed_work: { type: 'string' },
    remaining_work: { type: 'string', description: 'empty when done' },
    tests_run: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
    reroute: { type: 'string', enum: ['up', 'down'] },
    reroute_reason: { type: 'string' },
    usage_limited: { type: 'boolean', description: 'true only if a usage/quota limit message stopped the work' },
  },
}
const FINDINGS_SCHEMA = {
  type: 'object',
  required: ['findings'],
  properties: {
    exit_code: { type: 'number', description: 'codex wrapper exit code, when Codex was used' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'title'],
        properties: {
          severity: { type: 'string', enum: ['blocker', 'high', 'medium', 'low'] },
          category: { type: 'string', enum: ['correctness', 'efficiency', 'best-practice', 'security', 'standards', 'reduction'] },
          title: { type: 'string' },
          file: { type: 'string' },
          detail: { type: 'string' },
        },
      },
    },
  },
}

// ---------------------------------------------------------------------------
phase('Classify')
let classification = ARGS.classification
if (!classification || !ROUTES.includes(classification.route)) {
  const r = await bash(
    script([`mkdir -p ${q(RUN)}`, heredoc(`${RUN}/task.txt`, TASK), `${PY('classify_task.py')} --task-file ${q(`${RUN}/task.txt`)}`]),
    'classify', 'Classify',
  )
  if (r.exit_code !== 0) throw new Error(`classify_task.py exited ${r.exit_code}: ${r.stderr.slice(-500)}`)
  classification = parseJson(r.stdout, 'classify_task.py')
}
let ROUTE = classification.route
let requiresCodexReview = Boolean(classification.requires_codex_review)
log(`${TASK_ID}: ${ROUTE} (${classification.reason})`)

phase('Route')
// implEngine 'auto' lets the router derive the implementer, so review lands on the other engine.
async function routeAll(phaseName, implEngine = 'auto') {
  const codexReviewFlag = requiresCodexReview ? ' --requires-codex-review' : ''
  const extra = {
    implementation: ` --task-id ${q(TASK_ID)}`,
    review: ` --task-id ${q(TASK_ID)} --implementation-engine ${implEngine}${codexReviewFlag}`,
  }
  const routeCmd = role => `${PY('route_engine.py')} --role ${role} --task-route ${ROUTE}${extra[role] || ''}`
  const r = await bash(
    script([
      `mkdir -p ${q(RUN)}`,
      heredoc(`${RUN}/task.txt`, TASK),
      `echo ===STATE===`, `${PY('engine_state.py')} status`,
      `echo ===CONFIG===`, `${PY('engine_state.py')} config`,
      `echo ===IMPLEMENTATION===`, routeCmd('implementation'),
      `echo ===SCOUT===`, routeCmd('scout'),
      `echo ===ARCHITECTURE===`, routeCmd('architecture'),
      `echo ===REVIEW===`, routeCmd('review'),
      `echo ===STYLE_REVIEW===`, routeCmd('style_review'),
    ]),
    'route', phaseName,
  )
  if (r.exit_code !== 0) throw new Error(`routing failed (exit ${r.exit_code}); invalid config stops orchestration: ${r.stderr.slice(-500)}`)
  const s = sections(r.stdout)
  const out = {}
  for (const key of ['STATE', 'CONFIG', 'IMPLEMENTATION', 'SCOUT', 'ARCHITECTURE', 'REVIEW', 'STYLE_REVIEW']) out[key] = parseJson(s[key] || '', key)
  return out
}
let routing = await routeAll('Route')
const CONFIG = routing.CONFIG
let impl = routing.IMPLEMENTATION
log(`implementation → ${impl.engine} ${impl.model} ${impl.effort}: ${impl.reason}`)

const fallbackEvents = []
const rerouteEvents = []
const handoffs = []
let requiresHumanReview = Boolean(impl.requires_human_review)

function noEngine(filesChanged = []) {
  return {
    task_id: TASK_ID, route: ROUTE,
    implementation_engine: 'none', implementation_model: '', implementation_effort: '',
    review_engine: 'none', review_findings: [], files_changed: filesChanged, tests: { skipped: true },
    fallback_events: fallbackEvents, reroute_events: rerouteEvents, requires_human_review: true,
    summary: `No engine available: ${impl.reason}. Wait for a quota reset or approve manual work.`,
  }
}

async function reroute(source, to, reason) {
  if (rerouteEvents.length || !ROUTES.includes(to) || to === ROUTE || (ROUTE === 'critical' && to !== 'critical')) {
    log(`reroute ignored: ${source} ${ROUTE} → ${to} (${reason || 'no reason'})`)
    return false
  }
  const from = ROUTE
  if (ROUTES.indexOf(to) > ROUTES.indexOf(from)) requiresCodexReview = true
  ROUTE = to
  routing = await routeAll(source === 'scout' ? 'Scout' : 'Implement')
  impl = routing.IMPLEMENTATION
  requiresHumanReview ||= Boolean(impl.requires_human_review)
  rerouteEvents.push({ source, from, to, reason: reason || '' })
  log(`reroute: ${source} ${from} → ${to} (${reason || 'no reason'})`)
  return true
}

if (impl.engine === 'none') return noEngine()

// ---------------------------------------------------------------------------
// Engine runners. Each returns {limited, ok, summary, files_changed, completed_work, remaining_work}.
const CODEX_EFFORT = { trivial: 'low', standard: 'medium', complex: 'high', critical: 'high' }

function claudeTier() {
  const d = CONFIG.defaults
  if (ROUTE === 'trivial') return { model: d.scout_model, effort: d.scout_effort }
  if (ROUTE === 'standard') return { model: d.implementation_model, effort: d.implementation_effort }
  if (ROUTE === 'complex') return { model: d.implementation_model, effort: 'high' }
  return { model: d.architect_model, effort: d.architect_effort }
}

function codexTier() {
  const codex = CONFIG.codex || {}
  const t = (codex.tiers || {})[ROUTE] || {}
  return { model: t.model || codex.default_model || '', effort: t.effort || CODEX_EFFORT[ROUTE], implement: t.implement !== false }
}

async function writeHandoff(source, target, completed, remaining, files, phaseName, testOutput = '') {
  const maxFiles = (CONFIG.codex || {}).max_files_per_fallback || 5
  const fileArgs = files.slice(0, target === 'codex' ? maxFiles : files.length).map(f => ` --relevant-file ${q(f)}`).join('')
  const r = await bash(
    script([
      `mkdir -p ${q(RUN)}`,
      heredoc(`${RUN}/task.txt`, TASK),
      heredoc(`${RUN}/completed.txt`, completed),
      heredoc(`${RUN}/remaining.txt`, remaining),
      heredoc(`${RUN}/test-output.txt`, testOutput),
      `${PY('write_handoff.py')} --task-id ${q(TASK_ID)} --objective @${q(`${RUN}/task.txt`)} --task-route ${ROUTE}` +
        ` --source-engine ${q(source)} --target-engine ${target}` +
        ` --completed-work @${q(`${RUN}/completed.txt`)} --remaining-work @${q(`${RUN}/remaining.txt`)}` +
        ` --test-output @${q(`${RUN}/test-output.txt`)}${fileArgs}`,
    ]),
    `handoff:${target}`, phaseName,
  )
  if (r.exit_code !== 0) throw new Error(`write_handoff.py exited ${r.exit_code}: ${r.stderr.slice(-500)}`)
  const path = r.stdout.trim().split('\n').pop()
  handoffs.push(path)
  return path
}

let scoutFiles = []
let context = ''
let planned = false

// Architect plan for complex or critical work; appends to `context`, runs at most once.
async function plan() {
  if (planned || (ROUTE !== 'complex' && ROUTE !== 'critical')) return
  planned = true
  phase('Plan')
  const ar = routing.ARCHITECTURE
  if (ar.engine !== 'claude') return log(`plan skipped: ${ar.reason}`)
  const p = await agent(`Task (${ROUTE}): ${TASK}${context}\n\nAssess and return a minimal implementation plan.`, {
    agentType: `${PLUGIN}:architect`, label: 'plan', phase: 'Plan', schema: PLAN_SCHEMA, model: ar.model, effort: ar.effort,
  })
  if (p) {
    context += `\n\nArchitect plan:\n${p.steps.map((s, i) => `${i + 1}. ${s}`).join('\n')}\nRisks: ${p.risks.join('; ')}\nTest strategy: ${p.test_strategy}`
    if (p.rollback) context += `\nRollback: ${p.rollback}`
  }
}

// A resumed handoff already carries its context: no scout or plan.
if (ROUTE !== 'trivial' && !ARGS.handoff) {
  phase('Scout')
  const sr = routing.SCOUT
  if (sr.engine === 'claude') {
    const scout = await agent(`Task (${ROUTE}): ${TASK}\n\nFind the files and symbols this task touches and summarize the impact.`, {
      agentType: `${PLUGIN}:scout`, label: 'scout', phase: 'Scout', schema: SCOUT_SCHEMA, model: sr.model, effort: sr.effort,
    })
    if (scout) {
      scoutFiles = scout.files || []
      context += `\n\nScout findings:\nFiles: ${scoutFiles.join(', ')}\nSymbols: ${(scout.symbols || []).join(', ')}\nImpact: ${scout.impact}`
      if (scout.route) await reroute('scout', scout.route, scout.route_reason)
    }
  } else {
    log(`scout skipped: ${sr.reason}`)
  }
}
if (impl.engine === 'none') return noEngine()
if (!ARGS.handoff) await plan()

async function runClaude(tier, handoffPath) {
  let brief
  if (handoffPath) {
    brief = `Read the orchestration handoff at ${handoffPath}. Complete only its "Remaining work". ` +
      `Do not redo anything listed under "Do not redo". Follow its constraints and output contract.`
  } else {
    const scope = impl.scope === 'single_atomic_task'
      ? '\n\nQuota is constrained: complete only the first atomic step and list everything else under remaining_work.'
      : ''
    brief = `Task (${ROUTE}): ${TASK}${context}${scope}`
  }
  phase('Implement')
  const r = await agent(brief, {
    agentType: `${PLUGIN}:implementer`, label: 'implement:claude', phase: 'Implement', schema: IMPL_SCHEMA,
    model: tier.model, effort: tier.effort,
  })
  if (!r) return { limited: false, ok: false, summary: 'Claude implementer failed (ordinary error, not handed off)', files_changed: [], completed_work: '', remaining_work: TASK }
  return { limited: Boolean(r.usage_limited), ok: r.status === 'done', ...r }
}

async function runCodex(tier, handoffPath) {
  phase('Implement')
  const env = `CODEX_SANDBOX=workspace-write CODEX_MODEL=${q(tier.model)} CODEX_EFFORT=${q(tier.effort)}`
  // ponytail: one Bash call caps a Codex run at 10 minutes; split larger work into handoffs.
  const r = await bash(
    `${env} bash ${q(`${SCRIPTS}/codex_with_fallback.sh`)} ${q(handoffPath)} > ${q(`${RUN}/codex.out`)} 2> ${q(`${RUN}/codex.err`)}; ` +
      `code=$?; echo ===OUTPUT===; tail -c 3000 ${q(`${RUN}/codex.out`)}; echo; tail -c 1000 ${q(`${RUN}/codex.err`)}; ` +
      `echo ===FILES===; { git diff --name-only HEAD 2>/dev/null || git diff --name-only; git ls-files --others --exclude-standard; } | sort -u; exit $code`,
    'implement:codex', 'Implement', 600000,
  )
  const s = sections(r.stdout)
  const files = (s.FILES || '').split('\n').filter(f => f && !f.startsWith('.claude/'))
  if (r.exit_code === 42) {
    return { limited: true, ok: false, summary: 'Codex usage limit', files_changed: files,
      completed_work: 'Codex stopped at its usage limit; any partial changes are in the current diff.', remaining_work: TASK }
  }
  return {
    limited: false, ok: r.exit_code === 0,
    summary: r.exit_code === 0 ? (s.OUTPUT || '').slice(-1500) : `Codex failed (exit ${r.exit_code}, ordinary error, not handed off): ${(s.OUTPUT || r.stderr).slice(-800)}`,
    files_changed: files, completed_work: r.exit_code === 0 ? 'Codex implementation run' : '', remaining_work: r.exit_code === 0 ? '' : TASK,
  }
}

async function markLimit(engine, phaseName) {
  await bash(`${PY('engine_state.py')} mark-limit --engine ${engine} --message ${q(`${engine} usage limit during ${TASK_ID}`)}`, `mark-limit:${engine}`, phaseName)
}

function fallbackAllowed(from, to) {
  if (to === 'codex' && !codexTier().implement) return false  // e.g. critical work stays on Claude
  return (CONFIG.engines || {})[to] !== false && (CONFIG.fallback || {})[`${from}_to_${to}`] !== false
}

// ---------------------------------------------------------------------------
// Implementation with bidirectional fallback, at most one attempt per engine.
const tried = new Set()
let engine = impl.engine
let tier = { model: impl.model, effort: impl.effort }
let handoffPath = ARGS.handoff && /^[A-Za-z0-9._\/-]+\.md$/.test(ARGS.handoff) ? ARGS.handoff : null
let result = null
const enginesUsed = new Set()

while (true) {
  enginesUsed.add(engine)
  if (engine === 'codex' && !handoffPath) {
    handoffPath = await writeHandoff('claude', 'codex', '', `${TASK}${context}`, scoutFiles, 'Implement')
  }
  const out = engine === 'claude' ? await runClaude(tier, handoffPath) : await runCodex(tier, handoffPath)
  if (!out.limited && engine === 'claude' && !out.ok && out.reroute) {
    const index = ROUTES.indexOf(ROUTE) + (out.reroute === 'up' ? 1 : out.reroute === 'down' ? -1 : 99)
    if (await reroute('implementer', ROUTES[index], out.reroute_reason)) {
      if (impl.engine === 'none') return noEngine(out.files_changed || [])
      const before = context
      await plan()  // an upgrade into complex/critical gets its plan now
      handoffPath = await writeHandoff(engine, impl.engine, out.completed_work || '', `${out.remaining_work || TASK}${context.slice(before.length)}`,
        out.files_changed && out.files_changed.length ? out.files_changed : scoutFiles, 'Implement')
      engine = impl.engine
      tier = { model: impl.model, effort: impl.effort }
      continue
    }
  }
  if (!out.limited) { result = out; break }

  phase('Fallback')
  tried.add(engine)
  await markLimit(engine, 'Fallback')
  const other = engine === 'claude' ? 'codex' : 'claude'
  if (tried.has(other) || !fallbackAllowed(engine, other)) {
    fallbackEvents.push({ phase: 'implementation', from: engine, to: null, reason: `${engine} usage limit; no fallback left` })
    requiresHumanReview = true
    result = out
    break
  }
  const completed = out.completed_work || ''
  const remaining = out.remaining_work || TASK
  handoffPath = await writeHandoff(engine, other, completed, remaining, out.files_changed.length ? out.files_changed : scoutFiles, 'Fallback')
  fallbackEvents.push({ phase: 'implementation', from: engine, to: other, handoff: handoffPath, reason: `${engine} usage limit` })
  log(`fallback: ${engine} → ${other} with ${handoffPath}`)
  engine = other
  tier = other === 'codex' ? codexTier() : claudeTier()
}

// ---------------------------------------------------------------------------
phase('Test')
const testCommand = ((CONFIG.commands || {}).test || '').trim()
let tests = { command: testCommand || null, skipped: true }
const fixEvents = []

async function runTests(label, phaseName) {
  const t = await bash(
    `mkdir -p ${q(RUN)}; ( ${testCommand} ) > ${q(`${RUN}/test.out`)} 2>&1; code=$?; tail -c 4000 ${q(`${RUN}/test.out`)}; exit $code`,
    label, phaseName, 600000,
  )
  return { command: testCommand, skipped: false, exit_code: t.exit_code, passed: t.exit_code === 0, output_tail: t.stdout.slice(-2000) }
}

if (testCommand && result.ok !== false) {
  tests = await runTests('test', 'Test')
  // Quality ladder, bounded: one fix pass on the same engine, then one escalation.
  const other = engine === 'claude' ? 'codex' : 'claude'
  const limitedNow = new Set(fallbackEvents.map(e => e.from))
  const attempts = [{ engine, tier, why: 'fix pass on the same engine' }]
  if (fallbackAllowed(engine, other) && !limitedNow.has(other)) {
    attempts.push({ engine: other, tier: other === 'codex' ? codexTier() : claudeTier(), why: 'escalation to the other engine' })
  } else if (engine === 'claude') {
    attempts.push({ engine, tier: { model: CONFIG.defaults.architect_model, effort: CONFIG.defaults.architect_effort }, why: 'escalation one Claude tier up' })
  }
  for (const a of attempts) {
    if (tests.passed) break
    phase('Fix')
    log(`tests failed (exit ${tests.exit_code}): ${a.why} (${a.engine} ${a.tier.model})`)
    const hp = await writeHandoff(engine, a.engine, 'The implementation is in the current diff; the project tests fail.',
      'Make the failing tests pass with the smallest change. Do not rewrite or broaden the implementation.',
      result.files_changed && result.files_changed.length ? result.files_changed : scoutFiles, 'Fix', tests.output_tail)
    const out = a.engine === 'claude' ? await runClaude(a.tier, hp) : await runCodex(a.tier, hp)
    enginesUsed.add(a.engine)
    if (out.limited) {
      await markLimit(a.engine, 'Fix')
      fallbackEvents.push({ phase: 'fix', from: a.engine, to: null, reason: `${a.engine} usage limit during fix` })
      break
    }
    fixEvents.push({ engine: a.engine, model: a.tier.model, why: a.why })
    result.files_changed = [...new Set([...(result.files_changed || []), ...(out.files_changed || [])])]
    tests = await runTests(`test:after-${a.engine}`, 'Fix')
  }
  if (!tests.passed) requiresHumanReview = true
}

// ---------------------------------------------------------------------------
phase('Review')
if (fallbackEvents.length || engine !== impl.engine) routing = await routeAll('Review', engine)
let review = routing.REVIEW
let reviewFindings = []
let reviewEngine = review.engine
let styleFindings = []
const styleRoute = routing.STYLE_REVIEW
const styleReview = styleRoute.engine === 'claude' && (result.files_changed || []).length > 0
if (review.requires_human_review) requiresHumanReview = true

const SKIP_NOTE = '\nThe style review is skipped: also cover standards, best-practice and reduction.'
const reviewBrief = `Objective: ${TASK}
Task id: ${TASK_ID}; route: ${ROUTE}; implemented by: ${[...enginesUsed].join(' then ')}.
Review the current uncommitted changes (git diff HEAD plus untracked files outside .claude/).
Files reported changed: ${(result.files_changed || []).join(', ') || '(see diff)'}
Tests: ${tests.skipped ? 'not configured' : `${tests.command} → exit ${tests.exit_code}\n${tests.output_tail}`}${styleReview ? '' : SKIP_NOTE}`

// Cheap standards/best-practice/reduction pass; null means the agent failed.
let lintResult = { skipped: true }
async function runStyleReview() {
  if (!styleReview) return []
  const lintCommand = ((CONFIG.commands || {}).lint || '').trim()
  let lint = 'not configured'
  if (lintCommand) {
    const l = await bash(`out=$( ( ${lintCommand} ) 2>&1 ); code=$?; printf '%s' "$out" | tail -c 4000; exit $code`, 'lint', 'Review', 600000)
    lint = l.stdout
    lintResult = { command: lintCommand, exit_code: l.exit_code, passed: l.exit_code === 0, output_tail: l.stdout.slice(-2000) }
  }
  const out = await agent(`${reviewBrief}\nLint output (do not repeat these findings):\n${lint}`, {
    agentType: `${PLUGIN}:style-reviewer`, label: 'review:style', phase: 'Review', schema: FINDINGS_SCHEMA,
    model: styleRoute.model, effort: styleRoute.effort,
  })
  return out ? out.findings : null
}

async function claudeReview(r) {
  const out = await agent(styleReview && styleFindings !== null ? reviewBrief : reviewBrief + SKIP_NOTE, {
    agentType: `${PLUGIN}:verifier`, label: 'review:claude', phase: 'Review', schema: FINDINGS_SCHEMA, model: r.model, effort: r.effort,
  })
  return out ? out.findings : null
}

if (!result.ok && result.status !== 'partial') {
  reviewEngine = 'none'
  log('review skipped: implementation did not complete')
} else if (review.engine === 'claude') {
  let style
  ;[reviewFindings, style] = await Promise.all([claudeReview(review), runStyleReview()])
  styleFindings = style
} else if (review.engine === 'codex') {
  const [out, style] = await Promise.all([agent(`${reviewBrief}\n\nPlugin scripts directory: ${SCRIPTS}\nWrite the request to ${RUN}/review.md.`, {
    agentType: `${PLUGIN}:codex-reviewer`, label: 'review:codex', phase: 'Review', schema: FINDINGS_SCHEMA, model: 'haiku', effort: 'low',
  }), runStyleReview()])
  styleFindings = style
  if (out && out.exit_code === 42) {
    phase('Fallback')
    await markLimit('codex', 'Fallback')
    if (!tried.has('claude-review') && fallbackAllowed('codex', 'claude')) {
      tried.add('claude-review')
      const d = CONFIG.defaults
      const r = ROUTE === 'critical'
        ? { model: d.critical_review_model, effort: d.critical_review_effort }
        : { model: d.review_model, effort: d.review_effort }
      fallbackEvents.push({ phase: 'review', from: 'codex', to: 'claude', reason: 'codex usage limit' })
      reviewEngine = 'claude'
      reviewFindings = await claudeReview(r)
    } else {
      fallbackEvents.push({ phase: 'review', from: 'codex', to: null, reason: 'codex usage limit; no fallback left' })
      requiresHumanReview = true
    }
  } else {
    reviewFindings = out ? out.findings : null
  }
}
if (reviewFindings === null) { reviewFindings = []; requiresHumanReview = true; log('review agent failed; human review required') }
if (styleFindings === null) { styleFindings = []; requiresHumanReview = true; log('style review failed; human review required') }
if (reviewFindings.some(f => f.severity === 'blocker')) requiresHumanReview = true
const SEVERITY = ['blocker', 'high', 'medium', 'low']
const rank = f => (SEVERITY.indexOf(f.severity) + 5) % 5  // unknown severities sort last
reviewFindings = [...reviewFindings, ...styleFindings].sort((a, b) => rank(a) - rank(b))

// ---------------------------------------------------------------------------
phase('Finalize')
const success = result.ok && (tests.skipped || tests.passed)
const limitedEngines = new Set(fallbackEvents.map(e => e.from))
const records = [...enginesUsed, ...(reviewEngine === 'codex' ? ['codex'] : [])]
  .filter((e, i, all) => all.indexOf(e) === i && !limitedEngines.has(e))
  .map(e => `${PY('engine_state.py')} record-result --engine ${e} --task-id ${q(TASK_ID)} --status ${success ? 'success' : 'failed'} > /dev/null`)
if (records.length) await bash(records.join(' && '), 'record-result', 'Finalize')

return {
  task_id: TASK_ID,
  route: ROUTE,
  implementation_engine: engine,
  implementation_model: tier.model,
  implementation_effort: tier.effort,
  review_engine: reviewEngine,
  review_findings: reviewFindings,
  files_changed: result.files_changed || [],
  tests,
  lint: lintResult,
  fallback_events: fallbackEvents,
  reroute_events: rerouteEvents,
  fix_events: fixEvents,
  requires_human_review: requiresHumanReview,
  summary: result.summary,
  remaining_work: result.remaining_work || '',
  handoffs,
}
