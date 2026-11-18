/** The task pane in PowerPoint. */
import { joinRuntimes } from './clientToolDispatcher';
import { POWERPOINT } from './powerPointApp';
import { powerPointPackageRuntime, workerSandbox } from './powerPointPackageTools';
import { powerPointReadRuntime, type PowerPointRun } from './powerPointReadTools';
import { powerPointSlideDeck, powerPointWriteRuntime } from './powerPointWriteTools';
import { SlideSnapshots } from './slideSnapshots';
import { startPane } from './startPane';

startPane(POWERPOINT, ({ office, host, writes, displayName }) => {
  const run: PowerPointRun = batch => PowerPoint.run(batch);
  const snapshots = new SlideSnapshots(powerPointSlideDeck(run), writes);
  const runtime = joinRuntimes(
    powerPointReadRuntime({ run, host, documentName: displayName, languages: office }),
    powerPointWriteRuntime({ run, host, journal: callId => snapshots.journal(callId) }),
    powerPointPackageRuntime({ run, host, journal: callId => snapshots.journal(callId),
      sandbox: workerSandbox(() => new Worker(new URL('./ooxmlSandbox.worker.ts', import.meta.url),
        { type: 'module' })),
      forgetUndo: () => snapshots.forget() }));
  return { runtime, snapshots };
});
