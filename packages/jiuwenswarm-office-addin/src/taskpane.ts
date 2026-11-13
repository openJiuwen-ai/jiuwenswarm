/** The task pane in PowerPoint: the document, its tool runtime and the Gateway, wired to the pane. */
import './styles.css';

import { ClientAttachment } from './clientAttachment';
import { documentDisplayName, documentId } from './documentIdentity';
import { GatewayClient } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';
import { TaskPaneController } from './paneController';
import { powerPointReadRuntime, type PowerPointRun } from './powerPointReadTools';
import { joinRuntimes, PowerPointToolDispatcher } from './powerPointToolDispatcher';
import { powerPointSlideDeck, powerPointWriteRuntime } from './powerPointWriteTools';
import { CONTRACT_APP_ID, presentationAttachDeclaration, type PresentationHost } from './presentationContract';
import { PresentationSessionFacade } from './presentationSessionFacade';
import { followOfficeTheme } from './officeTheme';
import { SlideSnapshots } from './slideSnapshots';
import { mountPane } from './view/paneView';

Office.onReady(async () => {
  const office = Office.context;
  followOfficeTheme(office);
  const id = await documentId(office.document.settings);
  const displayName = (): Promise<string> => documentDisplayName(office.document, id);
  const host: PresentationHost = {
    isSetSupported: (name, version) => office.requirements.isSetSupported(name, version),
    worker: typeof Worker === 'function',
  };
  const run: PowerPointRun = batch => PowerPoint.run(batch);
  const writes = new HostWriteQueue();
  const snapshots = new SlideSnapshots(powerPointSlideDeck(run), writes);
  const runtime = joinRuntimes(
    powerPointReadRuntime({ run, host, documentName: displayName, languages: office }),
    powerPointWriteRuntime({ run, host, journal: callId => snapshots.journal(callId) }));
  const gateway = new GatewayClient(CONTRACT_APP_ID);
  const sessions = new PresentationSessionFacade(gateway);
  const attachment = new ClientAttachment({
    gateway,
    executor: new PowerPointToolDispatcher(runtime, writes),
    declaration: async () => presentationAttachDeclaration({
      documentId: id, displayName: await displayName(), host, implemented: runtime.tools,
    }),
    createSession: async () => sessions.create(await sessions.defaultModel()),
  });
  const controller = new TaskPaneController({ attachment, sessions, documentId: id, connection: gateway, snapshots });
  mountPane(document.getElementById('app')!, controller);
  // The shared runtime keeps the pane's script running while the pane is closed: a question opens it.
  // A host without the shared runtime has no Office.addin and shows the question when the user opens the pane.
  let prompt = controller.state().conversation.prompt;
  controller.subscribe(({ conversation }) => {
    if (conversation.prompt && conversation.prompt !== prompt) void Office.addin?.showAsTaskpane().catch(() => undefined);
    prompt = conversation.prompt;
  });
  await controller.open();
});
