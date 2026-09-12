/**
 * Minimal Server-Sent-Events parser for a `fetch()` ReadableStream body.
 *
 * We can't use the browser's native `EventSource` here because it only
 * supports GET requests with no custom headers, and our backend's
 * `/chat/stream` and `/chat/resume` endpoints are POST + require an
 * `Authorization: Bearer <token>` header. So we read the raw stream
 * ourselves and parse `sse_starlette`'s wire format by hand:
 *
 *   event: token
 *   data: {"token": "Hello"}
 *   <blank line>
 *
 * Each event is separated by a blank line. A single event may have
 * multiple `data:` lines (we join them with "\n", per the SSE spec),
 * though this backend always sends a single `json.dumps(...)` line.
 *
 * `onEvent(eventName, dataString)` is called once per complete event.
 */
export async function consumeSSE(response, onEvent) {
  if (!response.body) {
    throw new Error("Response has no readable body (streaming not supported)");
  }
  if (!response.ok) {
    // Try to surface a useful error message from a non-streaming error response.
    let detail = `HTTP ${response.status}`;
    try {
      const text = await response.text();
      try {
        const parsed = JSON.parse(text);
        detail = parsed.detail || text || detail;
      } catch {
        detail = text || detail;
      }
    } catch {
      // ignore
    }
    throw new Error(detail);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Events are separated by a blank line. Handle \r\n\r\n and \n\n.
    let boundary;
    while ((boundary = findEventBoundary(buffer)) !== -1) {
      const rawEvent = buffer.slice(0, boundary.start);
      buffer = buffer.slice(boundary.end);
      parseAndDispatch(rawEvent, onEvent);
    }
  }

  // Flush any trailing event without a final blank line.
  if (buffer.trim().length > 0) {
    parseAndDispatch(buffer, onEvent);
  }
}

function findEventBoundary(buffer) {
  const idxLF = buffer.indexOf("\n\n");
  const idxCRLF = buffer.indexOf("\r\n\r\n");
  if (idxCRLF !== -1 && (idxLF === -1 || idxCRLF < idxLF)) {
    return { start: idxCRLF, end: idxCRLF + 4 };
  }
  if (idxLF !== -1) {
    return { start: idxLF, end: idxLF + 2 };
  }
  return -1;
}

function parseAndDispatch(rawEvent, onEvent) {
  const lines = rawEvent.split(/\r\n|\n/);
  let eventName = "message";
  const dataLines = [];

  for (const line of lines) {
    if (!line || line.startsWith(":")) continue; // comment / keep-alive
    const idx = line.indexOf(":");
    if (idx === -1) continue;
    const field = line.slice(0, idx);
    let value = line.slice(idx + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "event") eventName = value;
    else if (field === "data") dataLines.push(value);
  }

  if (dataLines.length === 0) return;
  onEvent(eventName, dataLines.join("\n"));
}
