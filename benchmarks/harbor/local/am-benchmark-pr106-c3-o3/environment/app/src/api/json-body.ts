export function coerceJsonLikeBody(body: unknown): unknown {
  if (typeof body !== 'string') return body;

  let candidate = body.trim();
  if (!candidate) return body;

  // Recover from clients that accidentally wrap JSON in extra quote layers.
  // Example invalid payload: ""just a string"".
  for (let i = 0; i < 3; i += 1) {
    const parsed = tryParseJson(candidate);
    if (parsed.ok) {
      if (typeof parsed.value === 'string') {
        const inner = parsed.value.trim();
        if (looksLikeJson(inner)) {
          candidate = inner;
          continue;
        }
      }
      return parsed.value;
    }

    if (isQuoted(candidate)) {
      candidate = candidate.slice(1, -1).trim();
      continue;
    }
    break;
  }

  return body;
}

function tryParseJson(input: string): { ok: true; value: unknown } | { ok: false } {
  try {
    return { ok: true, value: JSON.parse(input) };
  } catch {
    return { ok: false };
  }
}

function isQuoted(input: string): boolean {
  return input.length >= 2 && input.startsWith('"') && input.endsWith('"');
}

function looksLikeJson(input: string): boolean {
  return input.startsWith('{') || input.startsWith('[');
}
