import { parsePresentation } from './pptxPresentationParser';
import { isOoxmlArchiveLimitError } from './ooxmlArchiveLimits';
import type { PresentationWorkerRequest, PresentationWorkerResponse } from './pptxPreviewModel';

const workerScope = self as unknown as {
  onmessage: ((event: MessageEvent<PresentationWorkerRequest>) => void) | null;
  postMessage: (message: PresentationWorkerResponse) => void;
};

workerScope.onmessage = event => {
  if (event.data.type !== 'parse') return;
  void parsePresentation(event.data.buffer)
    .then(presentation => workerScope.postMessage({ type: 'ready', presentation }))
    .catch(error =>
      workerScope.postMessage({
        type: 'error',
        code: isOoxmlArchiveLimitError(error) ? 'resource-limit' : 'parse-error',
        message: error instanceof Error ? error.message : 'Unable to parse the presentation',
      }),
    );
};
