/** The conversation as a Markdown file the user keeps: who said what, and the steps between, by name. */
import type { Entry } from './conversationModel';
import { copy, toolLabel } from './copy';

export function transcriptMarkdown(
  title: string, entries: readonly Entry[], documents: ReadonlyMap<string, string>,
): string {
  const blocks = [`# ${title}`];
  let steps: string[] = [];
  const flush = (): void => {
    if (steps.length) blocks.push(`> ${copy.transcriptSteps(steps.join('、'))}`);
    steps = [];
  };
  for (const entry of entries) {
    if (entry.kind === 'tool' || entry.kind === 'outgoing') {
      steps.push(entry.kind === 'tool' ? toolLabel(entry.name)
        : copy.outgoingTo(documents.get(entry.targetSessionId) || copy.otherConversation));
      continue;
    }
    flush();
    switch (entry.kind) {
      case 'user':
        blocks.push(`**${copy.transcriptUser}**`, entry.text);
        break;
      case 'assistant':
        if (entry.text) blocks.push(`**${copy.transcriptAssistant}**`, entry.text);
        break;
      case 'incoming':
        blocks.push(`**${copy.incomingFrom(entry.sourceTitle || copy.otherConversation)}**`, entry.text);
        break;
      case 'notice':
        blocks.push(`_${entry.text}_`);
        break;
      case 'changes':
        break;
    }
  }
  flush();
  return `${blocks.join('\n\n')}\n`;
}

/** The file name: the document's name without its extension, and the day. */
export function transcriptFileName(documentName: string, now: Date): string {
  const base = documentName.replace(/\.[^.]+$/, '') || copy.untitledConversation;
  const day = [now.getFullYear(), now.getMonth() + 1, now.getDate()].map(part => String(part).padStart(2, '0')).join('-');
  return `${base} ${copy.transcriptFile} ${day}.md`;
}
