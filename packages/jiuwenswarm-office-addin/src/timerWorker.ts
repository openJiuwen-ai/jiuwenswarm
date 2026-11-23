globalThis.onmessage = ({ data: { id, ms } }: MessageEvent<{ id: number; ms: number }>) => {
  setTimeout(() => globalThis.postMessage(id), ms);
};
