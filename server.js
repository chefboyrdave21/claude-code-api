'use strict';

const express = require('express');
const { spawn } = require('child_process');
const crypto = require('crypto');

const app = express();
app.use(express.json({ limit: '10mb' }));

const PORT = parseInt(process.env.PORT || '3456', 10);
const TIMEOUT_MS = parseInt(process.env.TIMEOUT || '600000', 10); // 10 min — opus with large context needs time

const DEFAULT_MODEL = 'claude-sonnet-4-6';

// Model name normalisation — accept GPT aliases, shorthand, and provider-prefixed names
const MODEL_ALIASES = {
  'gpt-4':             'claude-opus-4-6',
  'gpt-4o':            'claude-sonnet-4-6',
  'gpt-4-turbo':       'claude-opus-4-6',
  'gpt-4o-mini':       'claude-haiku-4-5',
  'gpt-3.5-turbo':     'claude-haiku-4-5',
  'gpt-3.5-turbo-16k': 'claude-haiku-4-5',
  'claude-code':        'claude-sonnet-4-6',
  'opus':               'claude-opus-4-6',
  'sonnet':             'claude-sonnet-4-6',
  'haiku':              'claude-haiku-4-5',
};

const VALID_MODELS = new Set([
  'claude-opus-4-6',
  'claude-sonnet-4-6',
  'claude-haiku-4-5',
  'claude-haiku-4-5-20251001',
]);

function normaliseModel(raw) {
  if (!raw) return DEFAULT_MODEL;
  if (MODEL_ALIASES[raw]) return MODEL_ALIASES[raw];
  // Strip provider prefix e.g. "claude-code/claude-sonnet-4-6"
  const bare = raw.includes('/') ? raw.split('/').pop() : raw;
  return VALID_MODELS.has(bare) ? bare : DEFAULT_MODEL;
}

// ---------------------------------------------------------------------------
// Request queue — Claude Code is single-threaded; serialise all runs
// ---------------------------------------------------------------------------
const queue = [];
let running = false;

function enqueue(task) {
  return new Promise((resolve, reject) => {
    queue.push({ task, resolve, reject });
    drain();
  });
}

function drain() {
  if (running || queue.length === 0) return;
  running = true;
  const { task, resolve, reject } = queue.shift();
  task()
    .then(resolve)
    .catch(reject)
    .finally(() => {
      running = false;
      drain();
    });
}

// ---------------------------------------------------------------------------
// Convert OpenAI messages array → (system, userPrompt)
// ---------------------------------------------------------------------------
function messagesToPrompt(messages) {
  const systemParts = [];
  const turns = [];

  for (const m of messages) {
    const role = (m.role || 'user').toLowerCase();
    const content = typeof m.content === 'string'
      ? m.content
      : Array.isArray(m.content)
        ? m.content.map((c) => (typeof c === 'string' ? c : c.text || '')).join('')
        : String(m.content ?? '');

    if (role === 'system') {
      systemParts.push(content);
    } else {
      turns.push({ role, content });
    }
  }

  const system = systemParts.join('\n');

  // Single user message — common case
  if (turns.length === 1 && turns[0].role === 'user') {
    return { system, prompt: turns[0].content };
  }

  // Multi-turn — format as conversation
  const lines = turns.map((t) => {
    const prefix = t.role === 'assistant' ? 'Assistant' : 'Human';
    return `${prefix}: ${t.content}`;
  });
  lines.push('Assistant:');
  return { system, prompt: lines.join('\n\n') };
}

// ---------------------------------------------------------------------------
// Spawn claude --print --output-format json and resolve with the result text.
//
// We always use --output-format json (never stream-json).  The stream-json
// mode causes claude-opus-4-6 with extended thinking to block stdout for
// several minutes before emitting any assistant events, leading to timeouts.
// --output-format json returns a clean JSON payload once the model is done.
// ---------------------------------------------------------------------------
function runClaude(model, prompt, system) {
  return new Promise((resolve, reject) => {
    const args = [
      '--print',
      '--dangerously-skip-permissions',
      '--model', model,
      '--output-format', 'json',
      '--no-session-persistence',
    ];
    if (system) args.push('--append-system-prompt', system);
    args.push(prompt);

    const child = spawn('claude', args, { stdio: ['ignore', 'pipe', 'pipe'] });

    let stdout = '';
    let stderr = '';
    let timedOut = false;

    const timer = setTimeout(() => {
      timedOut = true;
      child.kill('SIGTERM');
      setTimeout(() => child.kill('SIGKILL'), 3000);
    }, TIMEOUT_MS);

    child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });

    child.on('close', (code) => {
      clearTimeout(timer);
      if (timedOut) {
        return reject(Object.assign(new Error(`Claude timed out after ${TIMEOUT_MS / 1000}s`), { statusCode: 504 }));
      }
      if (code !== 0) {
        const msg = stderr.trim() || `claude exited with code ${code}`;
        return reject(Object.assign(new Error(msg), { statusCode: 502 }));
      }
      try {
        const parsed = JSON.parse(stdout);
        if (parsed.is_error) {
          return reject(Object.assign(new Error(parsed.result || 'Claude error'), { statusCode: 502 }));
        }
        const usage = parsed.usage || {};
        resolve({
          text: parsed.result || '',
          promptTokens: usage.input_tokens || 0,
          completionTokens: usage.output_tokens || 0,
        });
      } catch (e) {
        reject(Object.assign(new Error(`Failed to parse claude output: ${stdout.slice(0, 200)}`), { statusCode: 502 }));
      }
    });

    child.on('error', (err) => {
      clearTimeout(timer);
      reject(Object.assign(err, { statusCode: 502 }));
    });
  });
}

// ---------------------------------------------------------------------------
// OpenAI-compatible response builders
// ---------------------------------------------------------------------------
function makeId() {
  return 'chatcmpl-' + crypto.randomBytes(12).toString('hex');
}

function buildResponse(model, text, promptTokens, completionTokens) {
  return {
    id: makeId(),
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [{ index: 0, message: { role: 'assistant', content: text }, finish_reason: 'stop' }],
    usage: { prompt_tokens: promptTokens, completion_tokens: completionTokens, total_tokens: promptTokens + completionTokens },
  };
}

function buildChunk(id, model, delta, finishReason = null) {
  return {
    id, object: 'chat.completion.chunk',
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [{ index: 0, delta, finish_reason: finishReason }],
  };
}

// ---------------------------------------------------------------------------
// POST /v1/chat/completions
// ---------------------------------------------------------------------------
app.post('/v1/chat/completions', (req, res) => {
  const { model: rawModel = DEFAULT_MODEL, messages, stream = false } = req.body;

  if (!Array.isArray(messages) || messages.length === 0) {
    return res.status(400).json({ error: { message: '`messages` must be a non-empty array', type: 'invalid_request_error' } });
  }

  const model = normaliseModel(rawModel);
  const { system, prompt } = messagesToPrompt(messages);
  const id = makeId();

  const task = () => runClaude(model, prompt, system);

  if (stream) {
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.setHeader('X-Accel-Buffering', 'no');
    res.flushHeaders();

    const send = (obj) => res.write(`data: ${JSON.stringify(obj)}\n\n`);

    // Opening role chunk
    send(buildChunk(id, model, { role: 'assistant', content: '' }));

    enqueue(task)
      .then(({ text }) => {
        // Emit in ~80-char word-boundary chunks
        const words = text.split(' ');
        let buf = '';
        for (const w of words) {
          buf += w + ' ';
          if (buf.length >= 80) {
            send(buildChunk(id, model, { content: buf }));
            buf = '';
          }
        }
        if (buf) send(buildChunk(id, model, { content: buf }));
        send(buildChunk(id, model, {}, 'stop'));
        res.write('data: [DONE]\n\n');
        res.end();
      })
      .catch((err) => {
        res.write(`data: ${JSON.stringify({ error: { message: err.message, type: 'api_error' } })}\n\n`);
        res.write('data: [DONE]\n\n');
        res.end();
      });
  } else {
    enqueue(task)
      .then(({ text, promptTokens, completionTokens }) => {
        res.json(buildResponse(model, text, promptTokens, completionTokens));
      })
      .catch((err) => {
        res.status(err.statusCode || 500).json({ error: { message: err.message, type: 'api_error' } });
      });
  }
});

// ---------------------------------------------------------------------------
// GET /v1/models
// ---------------------------------------------------------------------------
app.get('/v1/models', (_req, res) => {
  const now = Math.floor(Date.now() / 1000);
  res.json({
    object: 'list',
    data: [...VALID_MODELS].sort().map((id) => ({ id, object: 'model', created: now, owned_by: 'anthropic' })),
  });
});

// ---------------------------------------------------------------------------
// GET /health
// ---------------------------------------------------------------------------
app.get('/health', (_req, res) => {
  res.json({ status: 'ok', queue_depth: queue.length, running, uptime_seconds: Math.floor(process.uptime()) });
});

// ---------------------------------------------------------------------------
// 404 catch-all
// ---------------------------------------------------------------------------
app.use((_req, res) => {
  res.status(404).json({ error: { message: 'Not found', type: 'invalid_request_error' } });
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
app.listen(PORT, '127.0.0.1', () => {
  console.log(`Claude Code API listening on 127.0.0.1:${PORT}`);
  console.log(`Timeout: ${TIMEOUT_MS / 1000}s | Queue: serial | Default model: ${DEFAULT_MODEL}`);
});
