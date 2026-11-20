/* global Application, WPS_Enum, alert */
/**
 * The page a WPS app loads for the add-in (``<app>/index.html``), shared by the apps: each app's ``main.js`` calls
 * ``wpsShell`` with its documents and page. The ribbon button opens the task pane of the document the user works
 * in. WPS has one task pane per window, not per document, so each document gets a pane of its own, opened with the
 * document's GUID (``?doc=``), which the shell first writes into the document's custom document properties; the
 * pane finds its document by it (src/wpsDocument.ts).
 */

/** ``DOCUMENT_ID_SETTING`` of src/documentIdentity.ts. */
const DOCUMENT_ID = 'jiuwenswarm.doc_id';
/** The pane of each document, by GUID, kept for the WPS session. */
const PANES = 'jiuwenswarm.task_panes';
const MSO_PROPERTY_TYPE_STRING = 4;
const PANE_WIDTH = 460;

/**
 * The ribbon's callbacks for one app: ``documents`` and ``active`` name its collection of open documents and the
 * active one (``Workbooks``, ``ActiveWorkbook``), ``page`` the pane's page and ``noun`` the document as the user
 * calls it.
 */
function wpsShell({ documents, active, page, noun }) {
  function OnAddinLoad(ribbonUI) {
    Application.ribbonUI = ribbonUI;
    if (typeof Application.Enum !== 'object' && typeof WPS_Enum === 'object') Application.Enum = WPS_Enum;
    return true;
  }

  function idProperty(doc) {
    const properties = doc.CustomDocumentProperties;
    for (let index = 1; index <= properties.Count; index += 1) {
      const property = properties.Item(index);
      if (property.Name === DOCUMENT_ID) return property;
    }
    return null;
  }

  /**
   * The document's GUID, written into it the first time. A copy of a file carries the file's GUID: of the open
   * documents that share one, the first opened keeps it and each other gets a new one, so no pane edits the
   * original while the user works in the copy.
   */
  function documentId(doc) {
    const own = idProperty(doc);
    if (!own) {
      const id = crypto.randomUUID();
      doc.CustomDocumentProperties.Add(DOCUMENT_ID, false, MSO_PROPERTY_TYPE_STRING, id);
      return id;
    }
    const shared = String(own.Value);
    const open = Application[documents];
    const holders = [];
    for (let index = 1; index <= open.Count; index += 1) {
      const property = idProperty(open.Item(index));
      if (property && String(property.Value) === shared) holders.push(property);
    }
    for (const property of holders.slice(1)) property.Value = crypto.randomUUID();
    return String(own.Value);
  }

  function paneOf(id) {
    try {
      return id ? Application.GetTaskPane(id) : null;
    } catch {
      return null;
    }
  }

  function OnAction() {
    try {
      showPaneOf(Application[active]);
    } catch (error) {
      // Protected view, for one, may refuse the GUID's document property: say so rather than do nothing.
      alert(`WorkSwarm 无法在这份${noun}中打开：${error instanceof Error ? error.message : String(error)}`);
    }
    return true;
  }

  function showPaneOf(doc) {
    if (!doc) return;
    const id = documentId(doc);
    const panes = JSON.parse(Application.PluginStorage.getItem(PANES) || '{}');
    let pane = paneOf(panes[id]);
    if (!pane) {
      const url = new URL(`../../${page}`, location.href);
      url.searchParams.set('doc', id);
      pane = Application.CreateTaskPane(url.href);
      pane.Width = Math.round(PANE_WIDTH * devicePixelRatio);
      panes[id] = pane.ID;
      Application.PluginStorage.setItem(PANES, JSON.stringify(panes));
    }
    const visible = !pane.Visible;
    for (const [other, paneId] of Object.entries(panes)) {
      const shown = other === id ? null : paneOf(paneId);
      if (shown) shown.Visible = false;
    }
    pane.Visible = visible;
  }

  function OnGetEnabled() {
    return Boolean(Application[active]);
  }

  /** WPS appends what this returns to the add-in's URL, so it is a path relative to the app's folder, not a URL. */
  function GetImage() {
    return '../../assets/icon-80.png';
  }

  Object.assign(window, { OnAddinLoad, OnAction, OnGetEnabled, GetImage });
}
