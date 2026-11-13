/** A task pane in one Office host: the document, the host's tool runtime and the Gateway, wired to the pane. */
import './styles.css';

import { attachDeclaration, type OfficeApp, type OfficeHost } from './appContract';
import { ClientAttachment } from './clientAttachment';
import { ClientToolDispatcher, type ClientToolRuntime } from './clientToolDispatcher';
import { documentCopy } from './copy';
import { documentDisplayName, documentId } from './documentIdentity';
import { GatewayClient } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';
import { followOfficeTheme } from './officeTheme';
import { TaskPaneController } from './paneController';
import { SessionFacade } from './sessionFacade';
import type { SlideSnapshots } from './slideSnapshots';
import { mountPane } from './view/paneView';

/** What a host's entry builds its tool runtime from. */
export interface HostContext {
  office: Office.Context;
  host: OfficeHost;
  writes: HostWriteQueue;
  /** The document's name, as the attach gives it. */
  displayName: () => Promise<string>;
}

/** The host's tools, and the undo of its turns where the host has one. */
export interface HostTools {
  runtime: ClientToolRuntime;
  snapshots?: SlideSnapshots;
}

export function startPane(app: OfficeApp, tools: (context: HostContext) => HostTools): void {
  Office.onReady(async () => {
    const office = Office.context;
    followOfficeTheme(office);
    const words = documentCopy[app.surface];
    const id = await documentId(office.document.settings);
    const displayName = (): Promise<string> => documentDisplayName(office.document, id, words.noun);
    const host: OfficeHost = {
      isSetSupported: (name, version) => office.requirements.isSetSupported(name, version),
      worker: typeof Worker === 'function',
    };
    const writes = new HostWriteQueue();
    const { runtime, snapshots } = tools({ office, host, writes, displayName });
    const gateway = new GatewayClient(app.appId);
    const sessions = new SessionFacade(gateway, app.clientApp);
    const attachment = new ClientAttachment({
      gateway,
      executor: new ClientToolDispatcher(app, runtime, writes),
      declaration: async () => attachDeclaration(app, {
        documentId: id, displayName: await displayName(), host, implemented: runtime.tools,
      }),
      createSession: async () => sessions.create(await sessions.defaultModel()),
    });
    const controller = new TaskPaneController(
      { attachment, sessions, documentId: id, connection: gateway, contract: app.contract, snapshots });
    mountPane(document.getElementById('app')!, controller, words);
    // The shared runtime keeps the pane's script running while the pane is closed: a question opens it.
    // A host without the shared runtime has no Office.addin and shows the question when the user opens the pane.
    let prompt = controller.state().conversation.prompt;
    controller.subscribe(({ conversation }) => {
      if (conversation.prompt && conversation.prompt !== prompt) void Office.addin?.showAsTaskpane().catch(() => undefined);
      prompt = conversation.prompt;
    });
    await controller.open();
  });
}
