'use strict';

const express = require('express');
const { spawn } = require('child_process');
const crypto = require('crypto');

const app = express();
app.use(express.json({ limit: '10mb' }));

const PORT = parseInt(process.env.PORT || '3456', 10);
const TIMEOUT_MS = parseInt(process.env.TIMEOUT || '120000', 10);

// ---------------------------------------------------------------------------
// Request queue — Claude Code is single-threaded; serialize all runs
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
// Convert OpenAI messages array → single prompt string
// ---------------------------------------------------------------------------
function messagesToPrompt(messages) {
  return messages
    .map((m) => {
      const role = (m.role || 'user').toLowerCase();
      const content = typeof m.content === 'string'
        ? m.content
        : Array.isArray(m.content)
          ? m.content.map((c) => (typeof c === 'string' ? c : c.text || '')).join('')
          : String(m.content ?? '');

      if (role === 'system') return `System: ${content}`;
      if (role === 'assistant') return `Assistant: ${content}`;
      return `Human: ${content}`;
    })
    .join('\n\n');
}

// ---------------------------------------------------------------------------
// Spawn claude and collect / stream output
// ---------------------------------------------------------------------------
function runClaude(prompt, { stream, onChunk }) {
  return new Promise((resolve, reject) => {
    const args = ['--print', '--permission-mode', 'bypassPermissions'];
    const child = spawn('claude', args, {
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    let stdout = '';
    let stderr = '';
    let timedOut = false;

    const timer = setTimeout(() => {
      timedOut = true;
      child.kill('SIGTERM');
      setTimeout(() => child.kill('SIGKILL'), 3000);
    }, TIMEOUT_MS);

    child.stdin.write(prompt);
    child.stdin.end();

    child.stdout.on('data', (chunk) => {
      const text = chunk.toString();
      stdout += text;
      if (stream && onChunk) onChunk(text);
    });

    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
    });

    child.on('close', (code) => {
      clearTimeout(timer);
      if (timedOut) {
        return reject(Object.assign(new Error('Claude process timed out'), { code: 504 }));
      }
      if (code !== 0) {
        const msg = stderr.trim() || `claude exited with code ${code}`;
        return reject(Object.assign(new Error(msg), { code: 502 }));
      }
      resolve(stdout);
    });

    child.on('error', (err) => {
      clearTimeout(timer);
      reject(Object.assign(err, { code: 502 }));
    });
  });
}

// ---------------------------------------------------------------------------
// Build OpenAI-compatible response objects
// ---------------------------------------------------------------------------
function makeId() {
  return 'chatcmpl-' + crypto.randomBytes(12).toString('hex');
}

function buildResponse(model, content) {
  return {
    id: makeId(),
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content },
        finish_reason: 'stop',
      },
    ],
    usage: {
      prompt_tokens: null,
      completion_tokens: null,
      total_tokens: null,
    },
  };
}

function buildStreamChunk(id, model, delta, finishReason = null) {
  return {
    id,
    object: 'chat.completion.chunk',
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [
      {
        index: 0,
        delta,
        finish_reason: finishReason,
      },
    ],
  };
}

// ---------------------------------------------------------------------------
// POST /v1/chat/completions
// ---------------------------------------------------------------------------
app.post('/v1/chat/completions', (req, res) => {
  const {
    model = 'claude-code',
    messages,
    stream = false,
  } = req.body;

  if (!Array.isArray(messages) || messages.length === 0) {
    return res.status(400).json({
      error: { message: '`messages` must be a non-empty array', type: 'invalid_request_error' },
    });
  }

  const prompt = messagesToPrompt(messages);
  const id = makeId();

  if (stream) {
    // SSE headers
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.flushHeaders();

    const sendChunk = (data) => {
      res.write(`data: ${JSON.stringify(data)}\n\n`);
    };

    // Opening chunk with role
    sendChunk(buildStreamChunk(id, model, { role: 'assistant', content: '' }));

    const task = () =>
      runClaude(prompt, {
        stream: true,
        onChunk: (text) => sendChunk(buildStreamChunk(id, model, { content: text })),
      });

    enqueue(task)
      .then(() => {
        sendChunk(buildStreamChunk(id, model, {}, 'stop'));
        res.write('data: [DONE]\n\n');
        res.end();
      })
      .catch((err) => {
        const errChunk = {
          error: { message: err.message, type: 'api_error' },
        };
        res.write(`data: ${JSON.stringify(errChunk)}\n\n`);
        res.write('data: [DONE]\n\n');
        res.end();
      });
  } else {
    const task = () => runClaude(prompt, { stream: false });

    enqueue(task)
      .then((content) => {
        res.json(buildResponse(model, content.trimEnd()));
      })
      .catch((err) => {
        const status = err.code || 500;
        res.status(status).json({
          error: { message: err.message, type: 'api_error' },
        });
      });
  }
});

// ---------------------------------------------------------------------------
// GET /health
// ---------------------------------------------------------------------------
app.get('/health', (_req, res) => {
  res.json({
    status: 'ok',
    queue_depth: queue.length,
    running,
    uptime_seconds: Math.floor(process.uptime()),
  });
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
app.listen(PORT, () => {
  console.log(`Claude Code API listening on port ${PORT}`);
  console.log(`Timeout: ${TIMEOUT_MS}ms | Queue: serial`);
});
