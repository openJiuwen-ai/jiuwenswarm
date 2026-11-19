import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import type { DocumentId } from './documentIdentity';
import { presentationWithId } from './wpsDocument';
import type { WpsObject } from './wpsPowerPoint';

const SHELL = readFileSync(new URL('../public/wps/wpp/main.js', import.meta.url), 'utf8');
const SHELL_URL = 'http://127.0.0.1:19001/office/wps/wpp/index.html';

const collection = <T>(items: T[]) => ({
  get Count() { return items.length; }, Item: (index: number) => items[index - 1],
});

/** An open presentation; a copy of another carries its document properties, the GUID among them. */
function presentation(name: string, copyOf?: WpsObject): WpsObject {
  const properties: { Name: string; Value: unknown }[] = copyOf
    ? Array.from({ length: copyOf.CustomDocumentProperties.Count }, (_, at) => ({
      ...copyOf.CustomDocumentProperties.Item(at + 1) }))
    : [];
  const add = (Name: string, _link: boolean, _type: number, Value: unknown) => properties.push({ Name, Value });
  return { Name: name, FullName: `/decks/${name}`,
    CustomDocumentProperties: Object.assign(collection(properties), { Add: add }) };
}

/** WPS with ``decks`` open and the shell loaded; ``click`` presses the ribbon button on a deck. */
function wps(...decks: WpsObject[]) {
  const panes: { ID: string; url: string; Visible: boolean; Width?: number }[] = [];
  const storage = new Map<string, string>();
  const application: WpsObject = {
    ActivePresentation: undefined, Presentations: collection(decks),
    PluginStorage: { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => {
      storage.set(key, value);
    } },
    CreateTaskPane: (url: string) => panes[panes.push({ ID: `pane-${panes.length}`, url, Visible: false }) - 1],
    GetTaskPane: (id: string) => panes.find(pane => pane.ID === id),
  };
  const window: Record<string, (...args: unknown[]) => unknown> = {};
  const alerts: string[] = [];
  new Function('Application', 'window', 'location', 'devicePixelRatio', 'alert', SHELL)(
    application, window, new URL(SHELL_URL), 2, (message: string) => alerts.push(message));
  return { application, panes, alerts, window, open: (deck: WpsObject) => decks.push(deck), click(deck: WpsObject) {
    application.ActivePresentation = deck;
    window.OnAction({ Id: 'WorkSwarm.ShowTaskPane' });
  } };
}

describe('the WPS shell', () => {
  it('opens one pane per presentation, which finds its presentation by the GUID the shell wrote into it', () => {
    const [plan, budget] = [presentation('Plan.pptx'), presentation('Budget.pptx')];
    const host = wps(plan, budget);
    host.click(plan);
    host.click(budget);
    host.click(plan);

    expect(host.panes.map(pane => pane.Visible)).toEqual([true, false]);
    const urls = host.panes.map(pane => new URL(pane.url));
    expect(urls.map(url => url.pathname)).toEqual(Array(2).fill('/office/taskpane.wps-presentation.html'));
    const [planPane, budgetPane] = urls.map(url => url.searchParams.get('doc') as DocumentId);
    expect(presentationWithId(host.application, planPane)).toBe(plan);
    expect(presentationWithId(host.application, budgetPane)).toBe(budget);
  });

  it('gives a copy opened beside its presentation a GUID of its own; a pane refuses a GUID two decks share', () => {
    const plan = presentation('Plan.pptx');
    const host = wps(plan);
    host.click(plan);
    const copy = presentation('Plan (2).pptx', plan);
    host.open(copy);
    const [planId] = host.panes.map(pane => new URL(pane.url).searchParams.get('doc') as DocumentId);
    expect(() => presentationWithId(host.application, planId)).toThrow(/同一个标识/);

    host.click(copy);
    host.click(plan);
    expect(host.panes).toHaveLength(2);
    const [planPane, copyPane] = host.panes.map(pane => new URL(pane.url).searchParams.get('doc') as DocumentId);
    expect(planPane).toBe(planId);
    expect(presentationWithId(host.application, planPane)).toBe(plan);
    expect(presentationWithId(host.application, copyPane)).toBe(copy);
  });

  it('names the ribbon icon by a path WPS appends to the add-in URL', () => {
    const icon = String(wps().window.GetImage());
    expect(icon).not.toMatch(/^[a-z]+:/);
    expect(new URL(icon, SHELL_URL).pathname).toBe('/office/assets/icon-80.png');
  });

  it('tells the user when a presentation refuses its GUID, as protected view may', () => {
    const locked = presentation('Locked.pptx');
    locked.CustomDocumentProperties.Add = () => { throw new Error('The document is read-only'); };
    const host = wps(locked);
    host.click(locked);
    expect(host.panes).toEqual([]);
    expect(host.alerts).toEqual(['WorkSwarm 无法在这份演示文稿中打开：The document is read-only']);
  });
});
