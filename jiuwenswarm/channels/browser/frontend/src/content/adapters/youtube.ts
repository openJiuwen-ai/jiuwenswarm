/**
 * YouTube-specific page extractor.
 *
 * Extracts the auto-generated transcript from `ytInitialData` (injected by
 * YouTube into every watch page as a JSON blob). Falls back to title +
 * description if the transcript is not available.
 */

export function extractYouTube(): { title: string; text: string } {
  const parts: string[] = [];

  // Title
  const titleEl =
    document.querySelector<HTMLElement>("h1.ytd-watch-metadata yt-formatted-string") ??
    document.querySelector<HTMLElement>('meta[name="title"]');
  const title =
    (titleEl instanceof HTMLMetaElement ? titleEl.content : titleEl?.innerText.trim()) ??
    document.title;

  // Channel name
  const channel = document.querySelector<HTMLElement>("#channel-name yt-formatted-string");
  if (channel) parts.push(`Channel: ${channel.innerText.trim()}`);

  // Description (expanded or collapsed)
  const desc =
    document.querySelector<HTMLElement>("#description-inline-expander") ??
    document.querySelector<HTMLElement>("#description yt-formatted-string");
  if (desc) parts.push(`Description:\n${desc.innerText.trim()}`);

  // Transcript via ytInitialData
  const transcript = extractTranscriptFromInitialData();
  if (transcript) {
    parts.push(`Transcript:\n${transcript}`);
  }

  return { title, text: parts.filter(Boolean).join("\n\n") };
}

function extractTranscriptFromInitialData(): string | null {
  try {
    // YouTube injects ytInitialData as an inline script on the page
    const scripts = document.querySelectorAll<HTMLScriptElement>("script");
    for (const script of scripts) {
      if (!script.textContent?.includes("ytInitialData")) continue;
      const match = script.textContent.match(/var ytInitialData\s*=\s*(\{.+?\});\s*(?:var|window|\/\/)/s);
      if (!match) continue;
      const data = JSON.parse(match[1]) as Record<string, unknown>;
      return walkForTranscript(data);
    }
  } catch {
    // ytInitialData not available or parse failed — silent fallback
  }
  return null;
}

function walkForTranscript(obj: unknown): string | null {
  if (!obj || typeof obj !== "object") return null;

  // Look for transcriptSegmentRenderer objects
  if (Array.isArray(obj)) {
    for (const item of obj) {
      const result = walkForTranscript(item);
      if (result) return result;
    }
    return null;
  }

  const rec = obj as Record<string, unknown>;

  // Transcript cue format
  if (typeof rec["cue"] === "object" && rec["cue"] !== null) {
    const cue = rec["cue"] as Record<string, unknown>;
    if (typeof cue["simpleText"] === "string") {
      // Collect all cues — return concatenation at the leaf level
    }
  }

  // transcriptCueGroupRenderer → cues
  if (Array.isArray(rec["cues"])) {
    const lines: string[] = [];
    for (const cue of rec["cues"] as Record<string, unknown>[]) {
      const seg = cue["transcriptCueRenderer"] as Record<string, unknown> | undefined;
      const text = (seg?.["cue"] as Record<string, unknown> | undefined)?.["simpleText"];
      if (typeof text === "string") lines.push(text);
    }
    if (lines.length > 0) return lines.join(" ");
  }

  for (const val of Object.values(rec)) {
    const result = walkForTranscript(val);
    if (result) return result;
  }
  return null;
}
