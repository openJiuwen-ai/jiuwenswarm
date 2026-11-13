/**
 * The sandbox of edit_slide_xml, edit_slide_chart and edit_slide_master: a one-shot module Worker that runs the
 * model's code on a slide package. After ``lockdown`` the code runs in a ``Compartment`` whose only globals are the
 * package as a JSZip-like ``zip``, ``markDirty`` and the XML and chart helpers, so it reaches no network, no
 * Office.js and nothing of the pane. The pane checks the package it hands back like any package from outside.
 */
import 'ses';

import { DOMParser, XMLSerializer } from '@xmldom/xmldom';
import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate';

import type { JsonObject } from './gatewayClient';
import { buildChart } from './presentationChart';
import { base64Of, bytesOf, insertChartParts, type Parts } from './presentationPackage';

/** What the pane posts: the model's code and the package it runs on. */
export interface SandboxRequest { code: string; base64: string }

/**
 * What the worker posts back: the package, edited only when the code called ``markDirty``, and what the code
 * returned as JSON (left out when it returned nothing); or why the code failed.
 */
export type SandboxReply = { base64: string; dirty: boolean; result?: unknown } | { error: string };

lockdown();

const escapeXml = (text: unknown) => String(text).replace(/[&<>"']/g, char => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', '\'': '&apos;' })[char]!);

/** The package as the code sees it: ``files``, ``file`` and ``remove`` of a JSZip archive, on ``parts``. */
function archive(parts: Parts) {
  const entry = (path: string) => ({ name: path, dir: false, async: async (type: string) => {
    const bytes = parts[path];
    if (!bytes) throw new Error(`zip has no ${path} any more`);
    switch (type) {
      case 'string': case 'text': return strFromU8(bytes);
      case 'uint8array': return bytes.slice();
      case 'base64': return base64Of(bytes);
      default: throw new Error(`async reads a part as "string", "uint8array" or "base64", not ${JSON.stringify(type)}`);
    }
  } });
  const zip = {
    get files() {
      return Object.fromEntries(Object.keys(parts).map(path => [path, entry(path)]));
    },
    file(path: unknown, content?: string | Uint8Array) {
      if (typeof path !== 'string') {
        throw new TypeError('zip.file takes a path string; list parts with Object.keys(zip.files)');
      }
      if (content === undefined) return parts[path] ? entry(path) : null;
      parts[path] = typeof content === 'string' ? strToU8(content) : Uint8Array.from(content);
      return zip;
    },
    remove(path: string) {
      delete parts[path];
      return zip;
    },
  };
  return zip;
}

globalThis.onmessage = async ({ data: { code, base64 } }: MessageEvent<SandboxRequest>) => {
  let reply: SandboxReply;
  try {
    const parts = unzipSync(bytesOf(base64));
    let dirty = false;
    const compartment = new Compartment({ zip: archive(parts), markDirty: () => { dirty = true; }, escapeXml,
      DOMParser, XMLSerializer, buildChart,
      insertChart: (_zip: unknown, spec: JsonObject) => insertChartParts(parts, spec) });
    const returned: unknown = await compartment.evaluate(`(async () => {\n${code}\n})`)();
    reply = { base64: dirty ? base64Of(zipSync(parts, { level: 6 })) : base64, dirty,
      ...(returned === undefined ? {} : { result: JSON.parse(JSON.stringify(returned) ?? 'null') }) };
  } catch (error) {
    reply = { error: String(error) };
  }
  globalThis.postMessage(reply);
};
