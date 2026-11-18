/* global Application, WPS_Enum, alert */
/**
 * The page WPS Presentation loads for the add-in. Its ribbon button opens the task pane of the presentation the
 * user works in. WPS has one task pane per window, not per document, so each presentation gets a pane of its own,
 * opened with the presentation's GUID (``?doc=``), which the shell first writes into the presentation's custom
 * document properties; the pane finds its presentation by it (src/wpsDocument.ts).
 */

/** ``DOCUMENT_ID_SETTING`` of src/documentIdentity.ts. */
const DOCUMENT_ID = 'jiuwenswarm.doc_id';
/** The pane of each presentation, by GUID, kept for the WPS session. */
const PANES = 'jiuwenswarm.task_panes';
const MSO_PROPERTY_TYPE_STRING = 4;
const PANE_WIDTH = 460;

function OnAddinLoad(ribbonUI) {
  Application.ribbonUI = ribbonUI;
  if (typeof Application.Enum !== 'object' && typeof WPS_Enum === 'object') Application.Enum = WPS_Enum;
  return true;
}

function idProperty(presentation) {
  const properties = presentation.CustomDocumentProperties;
  for (let index = 1; index <= properties.Count; index += 1) {
    const property = properties.Item(index);
    if (property.Name === DOCUMENT_ID) return property;
  }
  return null;
}

/**
 * The presentation's GUID, written into it the first time. A copy of a file carries the file's GUID: of the open
 * presentations that share one, the first opened keeps it and each other gets a new one, so no pane edits the
 * original while the user works in the copy.
 */
function documentId(presentation) {
  const own = idProperty(presentation);
  if (!own) {
    const id = crypto.randomUUID();
    presentation.CustomDocumentProperties.Add(DOCUMENT_ID, false, MSO_PROPERTY_TYPE_STRING, id);
    return id;
  }
  const shared = String(own.Value);
  const holders = [];
  for (let index = 1; index <= Application.Presentations.Count; index += 1) {
    const property = idProperty(Application.Presentations.Item(index));
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
    showPaneOf(Application.ActivePresentation);
  } catch (error) {
    // Protected view, for one, may refuse the GUID's document property: say so rather than do nothing.
    alert(`WorkSwarm 无法在这份演示文稿中打开：${error instanceof Error ? error.message : String(error)}`);
  }
  return true;
}

function showPaneOf(presentation) {
  if (!presentation) return;
  const id = documentId(presentation);
  const panes = JSON.parse(Application.PluginStorage.getItem(PANES) || '{}');
  let pane = paneOf(panes[id]);
  if (!pane) {
    const url = new URL('../../taskpane.wps-presentation.html', location.href);
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
  return Boolean(Application.ActivePresentation);
}

/** WPS appends what this returns to the add-in's URL, so it is a path relative to this folder, not a URL. */
function GetImage() {
  return '../../assets/icon-80.png';
}

Object.assign(window, { OnAddinLoad, OnAction, OnGetEnabled, GetImage });
