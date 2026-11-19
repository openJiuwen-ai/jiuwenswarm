import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import type { DocumentId } from './documentIdentity';
import { documentWithId, type WpsObject } from './wpsDocument';

/** Each WPS app's shell folder: the surface it edits, its open documents and the active one, and its pane. */
const APPS = {
  wpp: ['presentation', 'Presentations', 'ActivePresentation', 'taskpane.wps-presentation.html'],
  et: ['workbook', 'Workbooks', 'ActiveWorkbook', 'taskpane.wps-spreadsheet.html'],
  wps: ['document', 'Documents', 'ActiveDocument', 'taskpane.wps-writer.html'],
} as const;
type App = keyof typeof APPS;
const SHELL_URL = 'http://127.0.0.1:19001/office/wps/wpp/index.html';
const presentationWithId = (application: WpsObject, id: DocumentId) => documentWithId(application, 'presentation', id);

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

/** WPS with ``docs`` open and ``app``'s shell loaded; ``click`` presses the ribbon button on a document. */
function wps(app: App, ...docs: WpsObject[]) {
  const [, open, active] = APPS[app];
  const panes: { ID: string; url: string; Visible: boolean; Width?: number }[] = [];
  const storage = new Map<string, string>();
  const application: WpsObject = {
    [open]: collection(docs),
    PluginStorage: { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => {
      storage.set(key, value);
    } },
    CreateTaskPane: (url: string) => panes[panes.push({ ID: `pane-${panes.length}`, url, Visible: false }) - 1],
    GetTaskPane: (id: string) => panes.find(pane => pane.ID === id),
  };
  const window: Record<string, (...args: unknown[]) => unknown> = {};
  const alerts: string[] = [];
  const shell = ['shell.js', `${app}/main.js`]
    .map(file => readFileSync(new URL(`../public/wps/${file}`, import.meta.url), 'utf8')).join('\n');
  new Function('Application', 'window', 'location', 'devicePixelRatio', 'alert', shell)(
    application, window, new URL(`../${app}/index.html`, SHELL_URL), 2, (message: string) => alerts.push(message));
  return { application, panes, alerts, window, open: (doc: WpsObject) => docs.push(doc), click(doc: WpsObject) {
    application[active] = doc;
    window.OnAction({ Id: 'WorkSwarm.ShowTaskPane' });
  } };
}

describe('the WPS shell', () => {
  it.each(Object.keys(APPS) as App[])('opens one pane per document in %s, which finds its document by the GUID the '
    + 'shell wrote into it', app => {
    const [surface, , , page] = APPS[app];
    const [plan, budget] = [presentation('Plan'), presentation('Budget')];
    const host = wps(app, plan, budget);
    host.click(plan);
    host.click(budget);
    host.click(plan);

    expect(host.panes.map(pane => pane.Visible)).toEqual([true, false]);
    const urls = host.panes.map(pane => new URL(pane.url));
    expect(urls.map(url => url.pathname)).toEqual(Array(2).fill(`/office/${page}`));
    const [planPane, budgetPane] = urls.map(url => url.searchParams.get('doc') as DocumentId);
    expect(documentWithId(host.application, surface, planPane)).toBe(plan);
    expect(documentWithId(host.application, surface, budgetPane)).toBe(budget);
  });

  it('gives a copy opened beside its presentation a GUID of its own; a pane refuses a GUID two decks share', () => {
    const plan = presentation('Plan.pptx');
    const host = wps('wpp', plan);
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
    const icon = String(wps('wpp').window.GetImage());
    expect(icon).not.toMatch(/^[a-z]+:/);
    expect(new URL(icon, SHELL_URL).pathname).toBe('/office/assets/icon-80.png');
  });

  it('tells the user when a presentation refuses its GUID, as protected view may', () => {
    const locked = presentation('Locked.pptx');
    locked.CustomDocumentProperties.Add = () => { throw new Error('The document is read-only'); };
    const host = wps('wpp', locked);
    host.click(locked);
    expect(host.panes).toEqual([]);
    expect(host.alerts).toEqual(['WorkSwarm 无法在这份演示文稿中打开：The document is read-only']);
  });
});
