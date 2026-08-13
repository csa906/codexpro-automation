#!/usr/bin/env node
import { spawn, spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';

const SERVER_NAME = 'multi-gpt';
const SERVER_VERSION = '0.1.0';
// Runtime defaults. These are the values used when a caller omits `model` or
// `reasoning_effort`, so they must match the installed multi-gpt skill. Leaving the model unset here would fall
// through to whatever the Codex CLI picks, which is how this pipeline kept running on an
// older economy tier even after the documented default moved to GPT-5.6.
const DEFAULT_MODEL = 'gpt-5.6-luna';
const DEFAULT_REASONING_EFFORT = 'max';
const DEFAULT_MAX_ITERATIONS = 5;
// This is an execution contract, rather than a caller preference.  The pipeline
// fan-outs can create many Codex children, so accepting a lower-cost override
// would silently make one advisory run heterogeneous and non-reproducible.
const EXECUTION_CONTRACT = Object.freeze({
  model: 'gpt-5.6-luna',
  reasoning_effort: 'max',
});
// Evidence files are inlined verbatim into EVERY stage prompt (Planner, Solver, Refiner,
// Merger, Organizer), so these caps are a context budget, not an I/O limit. They are sized
// against the GPT-5.6 window of 372k tokens using measured density: mixed English/Markdown
// runs ~3.5 bytes/token, and an all-Korean UTF-8 document is the worst realistic case at
// ~3.0 bytes/token. The TOTAL cap is the binding constraint because every attachment is
// concatenated into one fileContext block.
//   512 KB single file -> ~175k tokens worst case (47% of the window)
//   768 KB total       -> ~262k tokens worst case (70%), leaving room for stage
//                         scaffolding, carried candidate solutions, and the reply
// Anything larger cannot fit at all: 2 MB alone is ~700k tokens worst case, which would
// overflow into silent truncation instead of failing closed at intake.
const MAX_FILE_BYTES = 512 * 1024;
const MAX_TOTAL_FILE_BYTES = 768 * 1024;
const SUB_GPT_CONCURRENCY = 20;
const SOLVER_CONCURRENCY = SUB_GPT_CONCURRENCY;
const REFINER_CONCURRENCY = SUB_GPT_CONCURRENCY;
const MERGER_CONCURRENCY = SUB_GPT_CONCURRENCY;
const CODEX_TIMEOUT_MS = 30 * 60 * 1000;
function resolveCodexCommand() {
  for (const explicit of [process.env.MULTI_GPT_CODEX_CLI_PATH, process.env.CODEX_CLI_PATH]) {
    if (explicit && existsSync(explicit)) return explicit;
  }
  if (process.platform !== 'win32') return 'codex';
  // OpenCodex installs codex.cmd as an autostart shim. Running that shim once per
  // parallel stage launches concurrent `ensure` processes and can serialize or stall
  // the fan-out. Its preserved real CLI still reads the normal Codex config, including
  // OpenCodex's base URL and generated model catalog, without repeating setup work.
  const appData = process.env.APPDATA;
  const openCodexReal = appData ? path.join(appData, 'npm', 'codex.opencodex-real.cmd') : '';
  return openCodexReal && existsSync(openCodexReal) ? openCodexReal : 'codex.cmd';
}

const CODEX_COMMAND = resolveCodexCommand();
const MAX_ERROR_TEXT = 4000;
const JOBS_DIR = path.join(process.env.CODEX_HOME || path.join(homedir(), '.codex'), 'mcp_servers', 'multi-gpt', 'jobs');
const JOBS = new Map();
const JOB_CONTROLLERS = new Map();
const MAX_RUNNING_JOBS = 5;
const ACTIVE_CHILDREN = new Set();

const TOOLS = [
  {
    name: 'multi_gpt_start',
    description: 'Start a local Multi GPT reasoning job and return immediately with a job_id. Runs usually take 5-20 minutes, commonly 10-15 minutes. Use this for all Multi GPT runs to avoid MCP tool-call timeouts; do not check status every minute.',
    inputSchema: {
      type: 'object',
      properties: {
        prompt: { type: 'string', description: 'Original user request.' },
        files: { type: 'array', items: { type: 'string' }, description: 'Optional local file paths to read and attach as context. Each file is read-only and size-limited.' },
        model: { type: 'string', enum: ['gpt-5.6-luna'], description: 'Optional execution-contract model. Omitted values are fixed to gpt-5.6-luna.' },
        reasoning_effort: { type: 'string', enum: ['max'], description: 'Optional execution-contract reasoning effort. Omitted values are fixed to max.' },
        max_iterations: { type: 'number', description: 'Maximum Merger -> Refiner -> Judge loop iterations. Default 5.' },
      },
      required: ['prompt'],
      additionalProperties: false,
    },
  },
  {
    name: 'multi_gpt_status',
    description: 'Check the status of a Multi GPT background job. Returns the final result once completed. Runs usually take 5-20 minutes, commonly 10-15 minutes, so poll only occasionally unless tighter monitoring is explicitly needed.',
    inputSchema: {
      type: 'object',
      properties: {
        job_id: { type: 'string', description: 'Job id returned by multi_gpt_start.' },
      },
      required: ['job_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'multi_gpt_cancel',
    description: 'Cancel a running Multi GPT background job started by this MCP server process. Terminates the active codex exec child process tree for that job and records status: canceled.',
    inputSchema: {
      type: 'object',
      properties: {
        job_id: { type: 'string', description: 'Job id returned by multi_gpt_start.' },
      },
      required: ['job_id'],
      additionalProperties: false,
    },
  },

];

function jobPath(jobId) {
  if (!/^[a-zA-Z0-9_-]+$/.test(String(jobId || ''))) throw new Error('invalid job_id');
  return path.join(JOBS_DIR, `${jobId}.json`);
}

async function writeJob(job) {
  job.updated_at = new Date().toISOString();
  await mkdir(JOBS_DIR, { recursive: true });
  await writeFile(jobPath(job.job_id), JSON.stringify(job, null, 2), 'utf8');
  if (job.status === 'running') {
    JOBS.set(job.job_id, job);
  } else {
    JOBS.delete(job.job_id);
  }
}

async function writeJobIfNotCanceled(job, controller) {
  throwIfCanceled(controller);
  await writeJob(job);
  if (controller?.canceled) {
    const current = await readJob(job.job_id).catch(() => job);
    await writeJob(canceledJob(current, controller.canceledAt));
  }
  throwIfCanceled(controller);
}

function runningJobCount() {
  let count = 0;
  for (const job of JOBS.values()) {
    if (job?.status === 'running') count += 1;
  }
  return count;
}

async function readJob(jobId) {
  if (JOBS.has(jobId)) return JOBS.get(jobId);
  const file = jobPath(jobId);
  if (!existsSync(file)) throw new Error(`job not found: ${jobId}`);
  return JSON.parse(await readFile(file, 'utf8'));
}

function publicJob(job) {
  return {
    ok: true,
    job_id: job.job_id,
    status: job.status,
    created_at: job.created_at,
    updated_at: job.updated_at,
    canceled_at: job.canceled_at || null,
    model: job.model,
    reasoning_effort: job.reasoning_effort,
    requested_contract: job.requested_contract,
    enforced_launch_contract: job.enforced_launch_contract,
    max_iterations: job.max_iterations,
    file_count: job.file_count,
    result: job.result || null,
    error: job.error || null,
  };
}

function createJobController(jobId) {
  const controller = { jobId, children: new Set(), canceled: false, canceledAt: null };
  JOB_CONTROLLERS.set(jobId, controller);
  return controller;
}

function isCancelError(error) {
  return error?.code === 'JOB_CANCELED';
}

function throwIfCanceled(controller) {
  if (!controller?.canceled) return;
  const error = new Error('job canceled');
  error.code = 'JOB_CANCELED';
  throw error;
}

function markControllerCanceled(controller, canceledAt = new Date().toISOString()) {
  if (!controller) return canceledAt;
  controller.canceled = true;
  controller.canceledAt = controller.canceledAt || canceledAt;
  return controller.canceledAt;
}

async function terminateJobChildren(controller) {
  if (!controller) return;
  const results = await Promise.all([...controller.children].map(terminateChildTreeAsync));
  const failures = results.filter((result) => !result.ok);
  if (failures.length) {
    throw new Error(`failed to terminate ${failures.length} child process tree(s): ${failures.map((failure) => failure.error).join('; ')}`);
  }
}

function canceledJob(job, canceledAt = new Date().toISOString()) {
  return { ...job, status: 'canceled', canceled_at: job.canceled_at || canceledAt, result: null, error: null };
}

async function startMultiGptJob(args = {}) {
  const options = normalizeOptions(args);
  if (runningJobCount() >= MAX_RUNNING_JOBS) throw new Error('too many running Multi GPT jobs; limit is ' + MAX_RUNNING_JOBS);
  const job = {
    job_id: randomUUID(),
    status: 'running',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    model: options.model,
    reasoning_effort: options.reasoningEffort,
    requested_contract: options.requestedContract,
    enforced_launch_contract: options.enforcedLaunchContract,
    max_iterations: options.maxIterations,
    file_count: options.files.length,
    result: null,
    error: null,
  };
  const controller = createJobController(job.job_id);
  await writeJob(job);
  runBackgroundJob(job.job_id, args, controller).catch(() => {});
  return publicJob(job);
}

async function runBackgroundJob(jobId, args, controller) {
  let job = await readJob(jobId);
  try {
    const result = await codexMar(args || {}, controller);
    throwIfCanceled(controller);
    const current = await readJob(jobId).catch(() => job);
    if (current.status === 'canceled') return;
    job = { ...current, status: result?.ok ? 'completed' : 'failed', result: result?.ok ? result : null, error: result?.ok ? null : result };
    await writeJobIfNotCanceled(job, controller);
  } catch (error) {
    const current = await readJob(jobId).catch(() => job);
    if (current.status === 'canceled') return;
    if (isCancelError(error) || controller?.canceled) {
      job = canceledJob(current, controller?.canceledAt);
      await writeJob(job);
    } else {
      job = { ...current, status: 'failed', result: null, error: { ok: false, error: String(error?.message || error) } };
      await writeJobIfNotCanceled(job, controller);
    }
  } finally {
    JOB_CONTROLLERS.delete(jobId);
  }
}

async function getMultiGptJobStatus(args = {}) {
  const jobId = normalizeString(args.job_id);
  if (!jobId) throw new Error('job_id is required');
  return publicJob(await readJob(jobId));
}

async function cancelMultiGptJob(args = {}) {
  const jobId = normalizeString(args.job_id);
  if (!jobId) throw new Error('job_id is required');
  const job = await readJob(jobId);
  if (job.status !== 'running') return publicJob(job);

  const controller = JOB_CONTROLLERS.get(jobId);
  if (!controller) {
    throw new Error(`job is running but is not owned by this MCP server process: ${jobId}`);
  }

  const canceledAt = markControllerCanceled(controller);
  try {
    await terminateJobChildren(controller);
  } catch (error) {
    controller.canceled = false;
    controller.canceledAt = null;
    throw error;
  }
  const canceled = canceledJob(job, canceledAt);
  await writeJob(canceled);
  return publicJob(canceled);
}
function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function textResult(payload) {
  return { content: [{ type: 'text', text: JSON.stringify(payload, null, 2) }] };
}

function errorResult(error, extra = {}) {
  return textResult({ ok: false, error: String(error?.message || error), ...extra });
}

function truncateText(text, max = MAX_ERROR_TEXT) {
  const value = String(text || '');
  return value.length <= max ? value : `${value.slice(0, max)}... [truncated ${value.length - max} chars]`;
}

function normalizeString(value, fallback = '') {
  return typeof value === 'string' ? value.trim() : fallback;
}

function normalizeOptions(args = {}) {
  const prompt = normalizeString(args.prompt);
  if (!prompt) throw new Error('prompt is required');

  const requestedContract = {
    model: normalizeString(args.model) || null,
    reasoning_effort: normalizeString(args.reasoning_effort) || null,
  };
  const model = requestedContract.model || DEFAULT_MODEL;
  const reasoningEffort = requestedContract.reasoning_effort || DEFAULT_REASONING_EFFORT;
  assertExecutionContract(model, reasoningEffort);

  const rawMaxIterations = args.max_iterations === undefined ? DEFAULT_MAX_ITERATIONS : Number(args.max_iterations);
  const maxIterations = Number.isFinite(rawMaxIterations) ? Math.max(1, Math.min(10, Math.floor(rawMaxIterations))) : DEFAULT_MAX_ITERATIONS;

  const files = Array.isArray(args.files) ? args.files.map(String).filter(Boolean) : [];
  return {
    prompt,
    files,
    model,
    reasoningEffort,
    maxIterations,
    requestedContract,
    enforcedLaunchContract: { ...EXECUTION_CONTRACT },
  };
}

function assertExecutionContract(model, reasoningEffort) {
  if (model !== EXECUTION_CONTRACT.model) {
    throw new Error(`Multi GPT execution-contract violation: model must be exactly ${EXECUTION_CONTRACT.model}; received ${JSON.stringify(model)}`);
  }
  if (reasoningEffort !== EXECUTION_CONTRACT.reasoning_effort) {
    throw new Error(`Multi GPT execution-contract violation: reasoning_effort must be exactly ${EXECUTION_CONTRACT.reasoning_effort}; received ${JSON.stringify(reasoningEffort)}`);
  }
}

async function readContextFiles(files) {
  if (!files.length) return { fileContext: '', fileSummaries: [] };

  let total = 0;
  const blocks = [];
  const summaries = [];
  for (const file of files) {
    const resolved = path.resolve(file);
    if (!existsSync(resolved)) throw new Error(`file not found: ${resolved}`);
    const info = await stat(resolved);
    if (!info.isFile()) throw new Error(`not a file: ${resolved}`);
    if (info.size > MAX_FILE_BYTES) throw new Error(`file too large: ${resolved} (${info.size} bytes > ${MAX_FILE_BYTES})`);
    total += info.size;
    if (total > MAX_TOTAL_FILE_BYTES) throw new Error(`total file context too large (${total} bytes > ${MAX_TOTAL_FILE_BYTES})`);
    const text = await readFile(resolved, 'utf8');
    blocks.push(`<FILE path="${escapeAttribute(resolved)}">\n${text}\n</FILE>`);
    summaries.push({ path: resolved, bytes: info.size });
  }
  return {
    fileContext: `<FILES>\n${blocks.join('\n\n')}\n</FILES>`,
    fileSummaries: summaries,
  };
}

function escapeAttribute(value) {
  return String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function buildStagePrompt({ stageName, systemPrompt, userMessage, outputMode = 'text' }) {
  const outputInstruction = outputMode === 'json'
    ? 'Return ONLY valid JSON. Do not wrap it in Markdown fences. Do not include commentary.'
    : 'Return the requested stage output only. Do not expose hidden chain-of-thought.';
  return `You are executing the "${stageName}" stage of a multi-agent reasoning pipeline.\nFollow the stage instructions exactly.\n\n<SYSTEM_PROMPT>\n${systemPrompt}\n</SYSTEM_PROMPT>\n\n<OUTPUT_INSTRUCTION>\n${outputInstruction}\n</OUTPUT_INSTRUCTION>\n\n<USER_MESSAGE>\n${userMessage}\n</USER_MESSAGE>`;
}

async function runCodexStage({ stageName, systemPrompt, userMessage, outputMode, model, reasoningEffort, controller }) {
  throwIfCanceled(controller);
  assertExecutionContract(model, reasoningEffort);
  const prompt = buildStagePrompt({ stageName, systemPrompt, userMessage, outputMode });
  const tempDir = await mkdtemp(path.join(tmpdir(), 'multi-gpt-'));
  const outputPath = path.join(tempDir, 'last-message.txt');
  const args = [
    '--ask-for-approval', 'never',
    'exec',
    '--json',
    '--sandbox', 'read-only',
    '--skip-git-repo-check',
    '--model', EXECUTION_CONTRACT.model,
    '-c', `model_reasoning_effort="${reasoningEffort}"`,
    // Keep user config enabled so an installed OpenCodex base URL and model catalog remain
    // authoritative at the provider boundary. Model, effort, approval, sandbox, and transport
    // are still pinned explicitly here, so user defaults cannot weaken this execution contract.
    // Pin HTTP/SSE as well: native Responses WebSockets previously produced repeated idle
    // timeouts and discarded an otherwise healthy multi-stage run.
    '-c', 'responses_websockets=false',
    '--output-last-message', outputPath,
    '-',
  ];
  try {
    const { stdout, stderr, code, signal } = await spawnWithInput(CODEX_COMMAND, args, prompt, CODEX_TIMEOUT_MS, controller);
    throwIfCanceled(controller);
    let text = '';
    if (existsSync(outputPath)) text = await readFile(outputPath, 'utf8');
    if (!text.trim()) text = extractTextFromJsonl(stdout) || stdout.trim();
    if (code !== 0) {
      return { success: false, stage: stageName, error: `codex exec exited with ${code}${signal ? ` (${signal})` : ''}`, stderr: truncateText(stderr.trim()), text: truncateText(text.trim()) };
    }
    return { success: true, stage: stageName, text: text.trim(), events: summarizeJsonl(stdout), stderr: stderr.trim() };
  } finally {
    await rm(tempDir, { recursive: true, force: true }).catch(() => {});
  }
}

function spawnWithInput(command, args, input, timeoutMs, controller) {
  return new Promise((resolve) => {
    try {
      throwIfCanceled(controller);
    } catch (error) {
      resolve({ stdout: '', stderr: String(error?.message || error), code: -1, signal: null });
      return;
    }
    const child = spawn(command, args, { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, shell: process.platform === 'win32' });
    ACTIVE_CHILDREN.add(child);
    controller?.children?.add(child);
    let stdout = '';
    let stderr = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) terminateChildTree(child);
    }, timeoutMs);

    child.stdout.on('data', (chunk) => { stdout += chunk.toString('utf8'); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString('utf8'); });
    child.on('error', (error) => {
      ACTIVE_CHILDREN.delete(child);
      controller?.children?.delete(child);
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ stdout, stderr: stderr + String(error?.message || error), code: -1, signal: null });
    });
    child.on('close', (code, signal) => {
      ACTIVE_CHILDREN.delete(child);
      controller?.children?.delete(child);
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ stdout, stderr, code, signal });
    });
    child.stdin.write(input);
    child.stdin.end();
  });
}

function terminateChildTree(child) {
  terminateChildTreeAsync(child).catch(() => {});
}

function terminateChildTreeAsync(child) {
  return new Promise((resolve) => {
    if (!child?.pid) {
      resolve({ ok: true });
      return;
    }

    if (child.exitCode !== null || child.signalCode !== null) {
      resolve({ ok: true });
      return;
    }

    if (process.platform === 'win32') {
      const killer = spawn('taskkill.exe', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      killer.on('error', (error) => {
        if (child.exitCode !== null || child.signalCode !== null) {
          resolve({ ok: true });
        } else {
          resolve({ ok: false, error: String(error?.message || error) });
        }
      });
      killer.on('close', (code, signal) => {
        if (code === 0 || child.exitCode !== null || child.signalCode !== null) {
          resolve({ ok: true });
        } else {
          resolve({ ok: false, error: `taskkill exited with ${code}${signal ? ` (${signal})` : ''} for pid ${child.pid}` });
        }
      });
      return;
    }

    try {
      child.kill('SIGTERM');
      resolve({ ok: true });
    } catch (error) {
      resolve({ ok: false, error: String(error?.message || error) });
    }
  });
}

function terminateChildTreeSync(child) {
  if (!child?.pid) return;
  if (process.platform === 'win32') {
    spawnSync('taskkill.exe', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    return;
  }
  child.kill('SIGTERM');
}

function terminateActiveChildrenSync() {
  for (const child of ACTIVE_CHILDREN) {
    terminateChildTreeSync(child);
  }
}
function summarizeJsonl(stdout) {
  const summary = { lines: 0, eventTypes: {} };
  for (const line of stdout.split(/\r?\n/)) {
    if (!line.trim()) continue;
    summary.lines += 1;
    try {
      const event = JSON.parse(line);
      const type = event.type || event.event || event.msg?.type || event.item?.type || 'unknown';
      summary.eventTypes[type] = (summary.eventTypes[type] || 0) + 1;
    } catch {
      summary.eventTypes.non_json = (summary.eventTypes.non_json || 0) + 1;
    }
  }
  return summary;
}

function extractTextFromJsonl(stdout) {
  const texts = [];
  for (const line of stdout.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const event = JSON.parse(line);
      collectText(event, texts);
    } catch {}
  }
  return texts.join('\n').trim();
}

function collectText(value, out) {
  if (!value || typeof value !== 'object') return;
  if (typeof value.text === 'string') out.push(value.text);
  if (typeof value.content === 'string') out.push(value.content);
  if (Array.isArray(value.content)) {
    for (const item of value.content) collectText(item, out);
  }
  for (const key of ['message', 'item', 'delta', 'response']) collectText(value[key], out);
}

function parseJsonObject(text) {
  const raw = String(text || '').trim();
  if (!raw) return null;
  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidate = fenced ? fenced[1].trim() : raw;
  try { return JSON.parse(candidate); } catch {}
  const start = candidate.indexOf('{');
  const end = candidate.lastIndexOf('}');
  if (start >= 0 && end > start) {
    try { return JSON.parse(candidate.slice(start, end + 1)); } catch {}
  }
  return null;
}

async function mapLimit(items, limit, worker, controller) {
  const results = new Array(items.length);
  let next = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      if (controller?.canceled) break;
      const index = next++;
      throwIfCanceled(controller);
      results[index] = await worker(items[index], index);
    }
  });
  await Promise.all(runners);
  throwIfCanceled(controller);
  return results;
}

function plannerSystemPrompt() {
  return `Analyze the user's problem and create diverse solution approaches for a multi-agent reasoning pipeline.\nReturn JSON with this exact shape:\n{\n  "problem_analysis": {\n    "core_question": string,\n    "details": string,\n    "success_criteria": string,\n    "key_constraints": string[],\n    "precautions": string[]\n  },\n  "approaches": [\n    { "name": string, "description": string, "methodology": string }\n  ]\n}\nCreate 6 to 10 materially different approaches unless the task is obviously trivial. Keep approaches actionable and non-overlapping.`;
}

function solverSystemPrompt() {
  return 'Solve the assigned approach completely. Produce a useful standalone solution, not an outline. Mention assumptions and risks only when they affect the answer.';
}

function refinerSystemPrompt() {
  return 'Review and improve the candidate solution. Fix errors, gaps, unclear claims, and weak reasoning. Return only the refined solution content.';
}

function mergerSystemPrompt() {
  return 'Merge the candidate solutions into a stronger solution. Preserve the best ideas, remove contradictions and duplication, and return one coherent merged solution.';
}

function judgeSystemPrompt() {
  return `Evaluate candidate solutions against the problem and decide whether the set is sufficient.\nReturn JSON only. If sufficient, return:\n{ "is_sufficient": true, "best_solution_id": number }\nUse 1-based best_solution_id.\nIf not sufficient, return:\n{ "is_sufficient": false, "outstanding_solution_ids": number[], "inadequate_solution_ids": number[] }\nUse 1-based IDs. Mark inadequate only when a solution should be discarded.`;
}

function organizerSystemPrompt() {
  return 'Turn the selected best solution into a concise, user-facing final answer. Do not expose hidden chain-of-thought or internal stage transcripts. Include practical caveats only if useful.';
}

function baseUserMessage(prompt, fileContext) {
  return `# Original User Request\n${prompt}\n\n${fileContext || ''}`.trim();
}

async function runPlanner(prompt, fileContext, options, trace, controller) {
  const result = await runCodexStage({
    stageName: 'Planner',
    systemPrompt: plannerSystemPrompt(),
    userMessage: baseUserMessage(prompt, fileContext),
    outputMode: 'json',
    model: options.model,
    reasoningEffort: options.reasoningEffort,
    controller,
  });
  if (!result.success) return { success: false, error: result.error, raw: result.text, stderr: result.stderr };
  const parsed = parseJsonObject(result.text);
  if (!parsed?.problem_analysis || !Array.isArray(parsed.approaches) || parsed.approaches.length === 0) {
    return { success: false, error: 'Planner JSON parse/shape failed', raw: result.text };
  }
  const approaches = parsed.approaches.slice(0, 10).map((approach, id) => ({
    id,
    name: String(approach.name || `Approach ${id + 1}`),
    description: String(approach.description || ''),
    methodology: String(approach.methodology || ''),
  }));
  trace.push({ stage: 'Planner', status: 'ok', approaches: approaches.length });
  return { success: true, problemAnalysis: parsed.problem_analysis, approaches };
}

async function runSolvers(prompt, fileContext, problemAnalysis, approaches, options, trace, controller) {
  const results = await mapLimit(approaches, SOLVER_CONCURRENCY, async (approach, index) => {
    const userMessage = `# Original User Request\n${prompt}\n\n${fileContext || ''}\n\n# Problem Analysis\n${JSON.stringify(problemAnalysis, null, 2)}\n\n# Assigned Approach\nName: ${approach.name}\nDescription: ${approach.description}\nMethodology: ${approach.methodology}`;
    const result = await runCodexStage({
      stageName: `Solver ${index + 1}`,
      systemPrompt: solverSystemPrompt(),
      userMessage,
      outputMode: 'text',
      model: options.model,
      reasoningEffort: options.reasoningEffort,
      controller,
    });
    if (!result.success || !result.text.trim()) return { success: false, id: index, approachName: approach.name, error: result.error || 'empty solver output' };
    return { success: true, id: index, approachName: approach.name, content: result.text.trim() };
  }, controller);
  const solutions = results.filter((r) => r?.success).map((r, id) => ({ ...r, id }));
  trace.push({ stage: 'Solver', status: solutions.length ? 'ok' : 'failed', succeeded: solutions.length, attempted: approaches.length, failures: results.filter((r) => !r?.success).map((r) => ({ id: r?.id, error: r?.error })) });
  if (!solutions.length) return { success: false, error: 'All solvers failed', failures: results };
  return { success: true, solutions };
}

async function runRefiners(solutions, prompt, fileContext, problemAnalysis, options, trace, label = 'Refiner', controller) {
  const results = await mapLimit(solutions, REFINER_CONCURRENCY, async (solution, index) => {
    const userMessage = `# Original User Request\n${prompt}\n\n${fileContext || ''}\n\n# Problem Analysis\n${JSON.stringify(problemAnalysis, null, 2)}\n\n# Candidate Solution (${solution.approachName || `Solution ${solution.id + 1}`})\n${solution.content}`;
    const result = await runCodexStage({
      stageName: `${label} ${index + 1}`,
      systemPrompt: refinerSystemPrompt(),
      userMessage,
      outputMode: 'text',
      model: options.model,
      reasoningEffort: options.reasoningEffort,
      controller,
    });
    if (!result.success || !result.text.trim()) {
      return { success: true, fallback: true, id: solution.id, approachName: solution.approachName, content: solution.content, error: result.error || 'empty refiner output' };
    }
    return { success: true, fallback: false, id: solution.id, approachName: solution.approachName, content: result.text.trim() };
  }, controller);
  const refined = results.map((r, id) => ({ id, approachName: r.approachName, content: r.content }));
  trace.push({ stage: label, status: 'ok', count: refined.length, fallbacks: results.filter((r) => r.fallback).length });
  return { success: true, solutions: refined };
}

function selectWithWeights(solutions, outstandingIds, count, seed = 0) {
  const pool = [];
  for (const solution of solutions) {
    const weight = outstandingIds.includes(solution.id) ? 2 : 1;
    for (let i = 0; i < weight; i++) pool.push(solution);
  }
  const selected = [];
  const seen = new Set();
  for (const solution of deterministicShuffle(pool, seed)) {
    if (!seen.has(solution.id)) {
      selected.push(solution);
      seen.add(solution.id);
      if (selected.length >= count) break;
    }
  }
  return selected;
}

function deterministicShuffle(items, seed = 0) {
  return [...items].sort((a, b) => stableScore(a, seed) - stableScore(b, seed));
}

function stableScore(solution, seed) {
  const text = `${seed}:${solution.id}:${solution.approachName || ''}:${solution.content.length}`;
  let hash = 2166136261;
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

async function runMergers(solutions, prompt, problemAnalysis, outstandingIds, options, trace, label = 'Merger', controller) {
  throwIfCanceled(controller);
  if (solutions.length === 1) return { success: true, solutions: [{ id: 0, content: solutions[0].content, sourceIds: [solutions[0].id] }] };
  const count = Math.min(8, solutions.length);
  const effectiveOutstanding = outstandingIds?.length ? outstandingIds : solutions.map((s) => s.id);
  const groups = Array.from({ length: count }, (_, index) => selectWithWeights(solutions, effectiveOutstanding, Math.min(3, solutions.length), index));
  const results = await mapLimit(groups, MERGER_CONCURRENCY, async (group, index) => {
    const userMessage = `# Original User Request\n${prompt}\n\n# Problem Analysis\n${JSON.stringify(problemAnalysis, null, 2)}\n\n# Solutions to Merge\n${group.map((s, i) => `## Solution ${i + 1} (source ID ${s.id + 1})\n${s.content}`).join('\n\n---\n\n')}`;
    const result = await runCodexStage({
      stageName: `${label} ${index + 1}`,
      systemPrompt: mergerSystemPrompt(),
      userMessage,
      outputMode: 'text',
      model: options.model,
      reasoningEffort: options.reasoningEffort,
      controller,
    });
    if (!result.success || !result.text.trim()) return { success: true, id: index, content: group[0].content, sourceIds: [group[0].id], fallback: true, error: result.error || 'empty merger output' };
    return { success: true, id: index, content: result.text.trim(), sourceIds: group.map((s) => s.id), fallback: false };
  }, controller);
  const merged = results.filter((r) => r?.success).map((r, id) => ({ id, content: r.content, sourceIds: r.sourceIds }));
  trace.push({ stage: label, status: merged.length ? 'ok' : 'failed', count: merged.length, fallbacks: results.filter((r) => r?.fallback).length });
  if (!merged.length) return { success: false, error: 'All mergers failed' };
  return { success: true, solutions: merged };
}

async function runJudge(solutions, prompt, problemAnalysis, options, trace, iteration, controller) {
  const userMessage = `# Original User Request\n${prompt}\n\n# Problem Analysis\n${JSON.stringify(problemAnalysis, null, 2)}\n\n# Candidate Solutions\n${solutions.map((s, i) => `## Solution ${i + 1} (ID: ${i + 1})\n${s.content}`).join('\n\n---\n\n')}`;
  const result = await runCodexStage({
    stageName: `Judge ${iteration}`,
    systemPrompt: judgeSystemPrompt(),
    userMessage,
    outputMode: 'json',
    model: options.model,
    reasoningEffort: options.reasoningEffort,
    controller,
  });
  if (!result.success) return { success: false, error: result.error, raw: result.text };
  const parsed = parseJsonObject(result.text);
  if (!parsed || typeof parsed.is_sufficient !== 'boolean') return { success: false, error: 'Judge JSON parse/shape failed', raw: result.text };
  if (parsed.is_sufficient) {
    const best = Number(parsed.best_solution_id) - 1;
    const validBest = Number.isInteger(best) && best >= 0 && best < solutions.length ? best : 0;
    trace.push({ stage: 'Judge', status: 'sufficient', iteration, best_solution_id: validBest + 1 });
    return { success: true, judgment: { is_sufficient: true, best_solution_id: validBest } };
  }
  const outstanding = normalizeIds(parsed.outstanding_solution_ids, solutions.length);
  const inadequate = normalizeIds(parsed.inadequate_solution_ids, solutions.length);
  const safeOutstanding = outstanding.length ? outstanding : solutions.map((_, i) => i);
  trace.push({ stage: 'Judge', status: 'insufficient', iteration, outstanding: safeOutstanding.map((i) => i + 1), inadequate: inadequate.map((i) => i + 1) });
  return { success: true, judgment: { is_sufficient: false, outstanding_solution_ids: safeOutstanding, inadequate_solution_ids: inadequate } };
}

function normalizeIds(value, length) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map((id) => Number(id) - 1).filter((id) => Number.isInteger(id) && id >= 0 && id < length))];
}

function remapSolutions(solutions) {
  return solutions.map((solution, id) => ({ ...solution, id }));
}

async function runLoop(initialSolutions, prompt, fileContext, problemAnalysis, options, trace, controller) {
  let current = remapSolutions(initialSolutions);
  let outstandingIds = current.map((s) => s.id);
  let iterations = 0;

  if (current.length === 1) {
    const refined = await runRefiners(current, prompt, fileContext, problemAnalysis, options, trace, 'Loop Final Refiner', controller);
    return { success: true, bestSolution: refined.solutions[0] || current[0], allSolutions: refined.solutions || current, iterations };
  }

  while (iterations < options.maxIterations) {
    throwIfCanceled(controller);
    iterations += 1;
    const merged = await runMergers(current, prompt, problemAnalysis, outstandingIds, options, trace, `Loop ${iterations} Merger`, controller);
    if (!merged.success) break;
    const refined = await runRefiners(remapSolutions(merged.solutions), prompt, fileContext, problemAnalysis, options, trace, `Loop ${iterations} Refiner`, controller);
    let candidates = remapSolutions(refined.solutions);
    if (!candidates.length) return { success: false, error: 'No solutions remaining after refiner', iterations };
    if (candidates.length === 1) return { success: true, bestSolution: candidates[0], allSolutions: candidates, iterations };

    const judged = await runJudge(candidates, prompt, problemAnalysis, options, trace, iterations, controller);
    if (!judged.success) {
      trace.push({ stage: 'Judge', status: 'fallback', iteration: iterations, error: judged.error });
      current = candidates;
      outstandingIds = current.map((s) => s.id);
      break;
    }
    const judgment = judged.judgment;
    if (judgment.is_sufficient) return { success: true, bestSolution: candidates[judgment.best_solution_id], allSolutions: candidates, iterations };

    const inadequate = new Set(judgment.inadequate_solution_ids || []);
    const outstanding = new Set(judgment.outstanding_solution_ids || []);
    const filtered = [];
    const nextOutstanding = [];
    for (let i = 0; i < candidates.length; i++) {
      if (!inadequate.has(i)) {
        filtered.push(candidates[i]);
        if (outstanding.has(i)) nextOutstanding.push(filtered.length - 1);
      }
    }
    current = remapSolutions(filtered.length ? filtered : candidates);
    outstandingIds = nextOutstanding.length ? nextOutstanding : current.map((s) => s.id);
  }

  trace.push({ stage: 'Loop', status: 'max_or_fallback_finalize', iterations });
  let finalSolutions = outstandingIds.length && outstandingIds.length < current.length
    ? current.filter((s) => outstandingIds.includes(s.id))
    : current;
  if (!finalSolutions.length) finalSolutions = current;
  const finalMerge = await runMergers(finalSolutions, prompt, problemAnalysis, [], options, trace, 'Final Merger', controller);
  let finalSolution = finalMerge.success && finalMerge.solutions.length ? finalMerge.solutions[0] : finalSolutions[0];
  const finalRefine = await runRefiners([finalSolution], prompt, fileContext, problemAnalysis, options, trace, 'Final Refiner', controller);
  if (finalRefine.success && finalRefine.solutions.length) finalSolution = finalRefine.solutions[0];
  return { success: true, bestSolution: finalSolution, allSolutions: current, iterations };
}

async function runOrganizer(prompt, fileContext, problemAnalysis, bestSolution, options, trace, controller) {
  const userMessage = `# Original User Request\n${prompt}\n\n${fileContext || ''}\n\n# Problem Analysis\n${JSON.stringify(problemAnalysis, null, 2)}\n\n# Best Solution From MAR\n${bestSolution.content}`;
  const result = await runCodexStage({
    stageName: 'Organizer',
    systemPrompt: organizerSystemPrompt(),
    userMessage,
    outputMode: 'text',
    model: options.model,
    reasoningEffort: options.reasoningEffort,
    controller,
  });
  if (!result.success || !result.text.trim()) {
    trace.push({ stage: 'Organizer', status: 'fallback', error: result.error || 'empty organizer output' });
    return { success: true, finalAnswer: bestSolution.content, fallback: true };
  }
  trace.push({ stage: 'Organizer', status: 'ok' });
  return { success: true, finalAnswer: result.text.trim(), fallback: false };
}

async function codexMar(args, controller) {
  const options = normalizeOptions(args);
  const trace = [];
  const { fileContext, fileSummaries } = await readContextFiles(options.files);

  throwIfCanceled(controller);
  const planner = await runPlanner(options.prompt, fileContext, options, trace, controller);
  if (!planner.success) return { ok: false, stage: 'Planner', error: planner.error, raw: planner.raw, stderr: planner.stderr || '', metadata: metadata(options, fileSummaries, trace) };

  const solved = await runSolvers(options.prompt, fileContext, planner.problemAnalysis, planner.approaches, options, trace, controller);
  if (!solved.success) return { ok: false, stage: 'Solver', error: solved.error, failures: solved.failures, metadata: metadata(options, fileSummaries, trace) };

  const refined = await runRefiners(solved.solutions, options.prompt, fileContext, planner.problemAnalysis, options, trace, 'Initial Refiner', controller);
  const loop = await runLoop(refined.solutions, options.prompt, fileContext, planner.problemAnalysis, options, trace, controller);
  if (!loop.success) return { ok: false, stage: 'Loop', error: loop.error, metadata: metadata(options, fileSummaries, trace) };

  const organized = await runOrganizer(options.prompt, fileContext, planner.problemAnalysis, loop.bestSolution, options, trace, controller);
  return {
    ok: true,
    final_answer: organized.finalAnswer,
    metadata: {
      ...metadata(options, fileSummaries, trace),
      iterations: loop.iterations,
      organizer_fallback: organized.fallback,
      planner: {
        core_question: planner.problemAnalysis.core_question,
        approach_count: planner.approaches.length,
      },
    },
  };
}

function metadata(options, fileSummaries, trace) {
  return {
    model: options.model,
    reasoning_effort: options.reasoningEffort,
    requested_contract: options.requestedContract,
    enforced_launch_contract: options.enforcedLaunchContract,
    max_iterations: options.maxIterations,
    files: fileSummaries,
    stage_summary: trace,
  };
}

async function handleToolCall(name, args) {
  if (name === 'multi_gpt_start') return textResult(await startMultiGptJob(args || {}));
  if (name === 'multi_gpt_status') return textResult(await getMultiGptJobStatus(args || {}));
  if (name === 'multi_gpt_cancel') return textResult(await cancelMultiGptJob(args || {}));
  throw new Error(`Unknown tool: ${name}`);
}

async function handle(message) {
  const { id, method, params } = message;
  if (method === 'initialize') {
    send({ jsonrpc: '2.0', id, result: { protocolVersion: params?.protocolVersion || '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: SERVER_NAME, version: SERVER_VERSION } } });
    return;
  }
  if (method === 'notifications/initialized') return;
  if (method === 'tools/list') {
    send({ jsonrpc: '2.0', id, result: { tools: TOOLS } });
    return;
  }
  if (method === 'tools/call') {
    try {
      const result = await handleToolCall(params?.name, params?.arguments || {});
      send({ jsonrpc: '2.0', id, result });
    } catch (error) {
      send({ jsonrpc: '2.0', id, result: errorResult(error) });
    }
    return;
  }
  send({ jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${method}` } });
}

let buffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  let idx;
  while ((idx = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, idx).trim();
    buffer = buffer.slice(idx + 1);
    if (!line) continue;
    try {
      const message = JSON.parse(line);
      handle(message).catch((error) => {
        if (message?.id !== undefined) send({ jsonrpc: '2.0', id: message.id, error: { code: -32000, message: error.message || String(error) } });
      });
    } catch (error) {
      send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: error.message || String(error) } });
    }
  }
});

process.once('exit', terminateActiveChildrenSync);
process.once('SIGINT', () => {
  terminateActiveChildrenSync();
  process.exit(130);
});
process.once('SIGTERM', () => {
  terminateActiveChildrenSync();
  process.exit(143);
});
