# Streaming

The app streams server-rendered HTML to the browser.

## How it works

`streamResponse` in `src/lib/stream.ts` calls `renderToReadableStream` and returns a
`Response` whose body is a readable stream. The shell is sent immediately.

## Error handling

Errors during streaming are logged via `onError`; the response is not aborted.
