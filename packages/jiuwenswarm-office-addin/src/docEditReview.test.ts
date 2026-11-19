// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import { type AppliedAs, DocEditReview, type ProposedEdit } from './docEditReview';
import { FakeDocument, FakeSettings } from './fakeWord';
import { HostWriteQueue } from './hostWriteQueue';
import { editCards } from './view/editCards';

const CAP: ProposedEdit = { id: 'cap-1', section: 'Liability', change_summary: 'Cap from 1M to 5M',
  old_text: 'capped at 1M per year', new_text: 'capped at 5M per year', reasoning: 'Matches the fee level.',
  alternatives: [{ label: 'Uncapped', text: 'uncapped' }] };

/** The decisions a message to ``session`` lists, the message sent. */
function told(review: DocEditReview, session: string): string {
  const { text, sent } = review.reviewed(session);
  sent();
  return text;
}

function proposed(mode = 'Off', text = 'The liability is capped at 1M per year.') {
  const doc = new FakeDocument(['Liability', text]);
  doc.changeTrackingMode = mode;
  const review = new DocEditReview(new FakeSettings(), doc.run, new HostWriteQueue());
  review.propose('call_1', 's1', [CAP.id]);
  return { doc, review };
}

describe('suggestion cards', () => {
  it.each([
    ['edit', 'TrackAll', 0, 'The liability is capped at 5M per year.', 'applied as an edit'],
    ['tracked', 'Off', 1, 'The liability is uncapped.', 'applied as a tracked change (alternative 1)'],
    ['comment', 'Off', 0, 'The liability is capped at 1M per year.', 'applied as a comment'],
  ] as const)('applies as %s, reports it once, and undoes it', async (as: AppliedAs, mode, alternative, text, line) => {
    const { doc, review } = proposed(mode);
    await review.apply('call_1', CAP, as, alternative);
    expect(doc.paras[1].text).toBe(text);
    expect(doc.changeTrackingMode).toBe(mode);
    expect(doc.revisions.length).toBe(as === 'tracked' ? 1 : 0);
    expect(doc.comments.map(comment => comment.content)).toEqual(as === 'comment' ? ['Matches the fee level.'] : []);
    // A message that was not sent leaves the decision to the next one.
    expect(review.reviewed('s1').text).toContain(`"cap-1": ${line}`);
    expect(told(review, 's1')).toContain(`"cap-1": ${line}`);
    expect(told(review, 's1')).toBe('');
    // The same call delivered again keeps what became of its cards.
    review.propose('call_1', 's1', [CAP.id]);
    expect(review.card('call_1', CAP.id)?.state.status).toBe('applied');

    await review.undo('call_1', CAP);
    expect(doc.paras[1].text).toBe('The liability is capped at 1M per year.');
    expect(doc.comments).toEqual([]);
    expect(told(review, 's1')).toContain('"cap-1": undone');
  });

  it('puts back the document\'s own quotes, and leaves words changed since or tracked changes decided in Word',
    async () => {
      const quoted = { ...CAP, old_text: 'the "cap" is 1M', new_text: 'the "cap" is 5M' };
      const { doc, review } = proposed('Off', 'So the “cap” is 1M.');
      await review.apply('call_1', quoted, 'edit');
      await review.undo('call_1', quoted);
      expect(doc.paras[1].text).toBe('So the “cap” is 1M.');

      const edited = proposed('Off', 'So the “cap” is 1M.');
      await edited.review.apply('call_1', quoted, 'edit');
      await edited.doc.userEdits(1, 'So the “cap” is 6M.');
      await edited.review.undo('call_1', quoted);
      expect(edited.doc.paras[1].text).toBe('So the “cap” is 6M.');
      expect(edited.review.card('call_1', CAP.id)).toMatchObject({ state: { status: 'applied' },
        error: expect.stringContaining('edited since') });

      const tracked = proposed();
      await tracked.review.apply('call_1', CAP, 'tracked');
      tracked.doc.revisions.length = 0;
      await tracked.review.undo('call_1', CAP);
      expect(tracked.doc.paras[1].text).toBe('The liability is capped at 5M per year.');
      expect(tracked.review.card('call_1', CAP.id)?.error).toContain('already accepted or rejected');
    });

  it('keeps a card the user is applying when the model withdraws it', async () => {
    const { doc, review } = proposed();
    const applying = review.apply('call_1', CAP, 'edit');
    expect(review.withdraw('s1', [CAP.id])).toEqual({ withdrawn: [], not_withdrawn: [
      { id: CAP.id, reason: 'the user is applying it now' }] });
    await applying;
    expect(doc.paras[1].text).toBe('The liability is capped at 5M per year.');
    expect(review.card('call_1', CAP.id)?.state.status).toBe('applied');
  });

  it('reports a skipped card and a card whose text changed, to the conversation that proposed them only', async () => {
    const { doc, review } = proposed();
    review.propose('call_2', 's1', ['other']);
    review.dismiss('call_2', 'other');
    doc.paras[1].text = 'The liability is unlimited.';
    await review.apply('call_1', CAP, 'edit');
    expect(told(review, 's2')).toBe('');
    const reviewed = told(review, 's1');
    expect(reviewed).toContain('"cap-1": failed: old_text was not found');
    expect(reviewed).toContain('"other": dismissed');
  });

  it('shows a card with the change word by word and applies the alternative the user picks', async () => {
    const { doc, review } = proposed();
    const cards = editCards(review);
    const render = () => cards.render('call_1', { edits: [CAP] as never })!;
    let node = render();
    expect(node.querySelector('.edit-diff')!.innerHTML).toBe('capped at <del>1M</del><ins>5M</ins> per year');
    node.querySelectorAll<HTMLInputElement>('input[type="radio"]')[1].dispatchEvent(new Event('change'));
    node = render();
    expect(node.querySelector('.edit-diff')!.innerHTML).toBe('<del>capped at 1M per year</del><ins>uncapped</ins>');
    [...node.querySelectorAll('button')].find(button => button.textContent === '直接修改')!.click();
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(doc.paras[1].text).toBe('The liability is uncapped.');
    node = render();
    expect(node.querySelector('.chip')!.textContent).toBe('已修改');
    expect([...node.querySelectorAll('button')].map(button => button.textContent)).toEqual(['撤销']);
  });
});
