import { renderToReadableStream } from "react-dom/server";
import type { ReactNode } from "react";

/**
 * Streams a React tree to the client as an HTTP response (SSR streaming).
 * Uses renderToReadableStream so the shell is flushed before data resolves.
 */
export async function streamResponse(tree: ReactNode): Promise<Response> {
  const stream = await renderToReadableStream(tree, {
    onError(error: unknown) {
      console.error("stream error", error);
    },
  });
  return new Response(stream, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}
