/**
 * The runner of ``execute_office_js``: the model's code on the host's own Office.js, inside ``<Host>.run`` on the
 * pane's main thread, where the host context lives (a Worker has none). The first run locks the realm down (SES
 * ``lockdown``, keeping what Office.js added to the built-ins); the code then runs in a ``Compartment`` whose only
 * globals are the host's enums and ``attachImage``, and it reaches the document only through ``context``, wrapped
 * so that every write is seen before it is queued. A write the host's rules refuse stops the code
 * (``ToolExecutionBlocked``), even when the code catches the refusal; code that returns with writes still queued is
 * an error, as those writes never land.
 *
 * Code on the main thread cannot be stopped. A ``context.sync()`` past 30 s, or a call past 90 s, is answered at
 * once and may still finish in the background, so the answer says to read back before writing again; the answer's
 * ``settled`` keeps the host write queue held until the code and its syncs are done.
 */
import 'ses';

import { type ClientToolError, type ClientToolResponse, png } from './clientToolDispatcher';

/** The most a ``context.sync()`` may take, as the industry runner allows, and the most a whole call may take. */
export const SYNC_MS = 30_000;
export const CALL_MS = 90_000;

/** A write the code is about to queue: a property set, or a call of a method that changes the document. */
export interface HostWrite {
  /** The properties and calls that led to the object, as ``['workbook', 'worksheets', 'getItem']``. */
  path: readonly string[];
  name: string;
  /** The assigned value, or the call's arguments, as data: a document object among them comes as ``null``. */
  values: readonly unknown[];
}

/** One host's binding of the runner. */
export interface CodeHost {
  /** As the model reads it: ``Excel``. */
  name: string;
  /** ``Excel.run``, injected so tests can drive a fake host. */
  run: <T>(batch: (context: object) => Promise<T>) => Promise<T>;
  /** The host's enums, which the code sees under ``name``. */
  enums: Record<string, Readonly<Record<string, string>>>;
  /** Why a write is refused, or undefined to let it through. */
  screen(write: HostWrite): string | undefined;
  /** Hears each write let through, with the host object it changes, just before the code queues it. */
  writing?(target: object): void;
}

/**
 * Methods that change the document, by name, as the industry runner classes them; any other call (``load``,
 * ``get…``, ``sync``) only reads.
 */
const WRITE_METHODS = new Set(['delete', 'clear', 'clearAll', 'deleteAll', 'deleteRows', 'deleteColumns',
  'removeDuplicates', 'replaceAll', 'moveTo', 'cut', 'clearOrResetContents', 'clearFilters', 'ungroup', 'unmerge',
  'split', 'convertToText', 'breakAllLinks', 'breakLinks', 'removePageBreaks', 'autoFill', 'flashFill', 'accept',
  'acceptAll', 'reject', 'rejectAll', 'reply', 'selectItems', 'startNewList']);
const WRITE_PREFIXES = ['add', 'apply', 'attach', 'auto', 'break', 'change', 'clear', 'convert', 'copy', 'create',
  'cut', 'delete', 'detach', 'distribute', 'duplicate', 'freeze', 'hide', 'import', 'increment', 'indent', 'insert',
  'merge', 'move', 'open', 'outdent', 'protect', 'refresh', 'remove', 'replace', 'request', 'resize', 'reset',
  'save', 'scale', 'set', 'show', 'sort', 'split', 'group', 'unfreeze', 'unprotect', 'update'];
/** Names that would reach Office.js internals (``_``), a class or a prototype rather than the document. */
const HIDDEN = /^_|^(?:constructor|prototype|__proto__)$/;
const NETWORK = ['fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource'];

class CodeBlocked extends Error {}

let lockedDown = false;

function lockRealm(): void {
  if (lockedDown) return;
  const extensions = ajaxExtensions();
  // Unsafe error, console and locale taming keep the pane's own stack traces, logging and locale-aware text as
  // they were; severe override taming lets the pane's code assign over properties that lockdown freezes.
  repairIntrinsics({ errorTaming: 'unsafe', consoleTaming: 'unsafe', localeTaming: 'unsafe',
    overrideTaming: 'severe' });
  restoreAjaxExtensions(extensions);
  hardenIntrinsics();
  lockedDown = true;
}

/**
 * Office.js loads MicrosoftAjax, which extends these built-ins and their prototypes (``Function._validateParams``,
 * ``String.format``…) and calls the extensions from its own APIs, ``document.settings.set`` among them. Lockdown
 * removes every property the language does not define and gives the page a new, frozen ``Function`` and new
 * ``Error`` and ``Date``, so the extensions Office.js calls go back between its repair and harden steps.
 */
const AJAX_EXTENDED = ['Function', 'Array', 'String', 'Number', 'Boolean', 'Date', 'Error', 'Object'];
/**
 * The extensions given back: those the host scripts call (``_validateParams``, ``createDelegate``, the ``Error``
 * factories, ``String.format``) and those these call in turn; ``__typeName`` is what ``Object.getType`` reads.
 * The code shares every prototype and the statics of ``Array``, ``Object``, ``String``, ``Number`` and
 * ``Boolean``, where a MicrosoftAjax function, being sloppy, sees the page's global object as ``this`` when the
 * code calls it without a receiver: the ones given back there do not read ``this``, or only to return nothing, and
 * come back behind strict forwarders, as a sloppy function's ``caller`` and ``arguments`` would show the code the
 * page function calling it. ``Function``, ``Error`` and ``Date`` are the page's own. Any other extension stays out.
 */
const AJAX_KEPT = new Set(['Function.__typeName', 'Function.createDelegate', 'Function._validateParams',
  'Function._validateParameterCount', 'Function._validateParameter', 'Function._validateParameterType',
  'Error.__typeName', 'Error.create', 'Error.argument', 'Error.argumentNull', 'Error.argumentOutOfRange',
  'Error.argumentType', 'Error.argumentUndefined', 'Error.parameterCount', 'Error.format',
  'Error.prototype.popStackFrame', 'Date.__typeName', 'Date._expandFormat', 'Date._getTokenRegExp',
  'Date._appendPreOrPostMatch', 'Object.__typeName', 'Object.getType', 'String.__typeName', 'String.format',
  'String._toFormattedString', 'Array.__typeName', 'Number.__typeName', 'Boolean.__typeName']);
/** The built-ins whose statics are the page's own, out of the code's reach. */
const AJAX_PAGE_OWN = new Set(['Function', 'Error', 'Date']);
/** Extensions on the shared prototypes that come back as our own, strict, versions. */
const AJAX_OWN = new Set(['Function.prototype.getName', 'Function.prototype.isInstanceOfType',
  'Date.prototype.format', 'Date.prototype.localeFormat', 'Number.prototype.format', 'Number.prototype.localeFormat']);

type Descriptors = Record<string, PropertyDescriptor>;
type Extensions = Map<string, { statics: Descriptors; prototype: Descriptors }>;

const builtIn = (name: string) => (globalThis as unknown as Record<string, Function>)[name];

function ajaxExtensions(): Extensions {
  return new Map(AJAX_EXTENDED.map(name => [name, { statics: Object.getOwnPropertyDescriptors(builtIn(name)),
    prototype: Object.getOwnPropertyDescriptors(builtIn(name).prototype) }]));
}

/**
 * Gives back the kept extensions the page has, and names once those it leaves out: the properties a script
 * assigned, as the engine's own are not enumerable.
 */
function restoreAjaxExtensions(extensions: Extensions): void {
  const pageFunction = replacePageFunction();
  const left: string[] = [];
  const give = (path: string, from: Descriptors, onto: object, shared: boolean) => {
    for (const [key, descriptor] of Object.entries(from)) {
      if (!descriptor.enumerable) continue;
      if (AJAX_KEPT.has(`${path}.${key}`)) {
        const original = descriptor.value;
        Object.defineProperty(onto, key, shared && typeof original === 'function' ? { ...descriptor,
          value(this: unknown, ...args: unknown[]) { return Reflect.apply(original, this, args); } } : descriptor);
      } else if (!AJAX_OWN.has(`${path}.${key}`) && !(key in onto)) left.push(`${path}.${key}`);
    }
  };
  for (const [name, { statics, prototype }] of extensions) {
    give(name, statics, name === 'Function' ? pageFunction : builtIn(name), !AJAX_PAGE_OWN.has(name));
    give(`${name}.prototype`, prototype, builtIn(name).prototype, true);
  }
  if (left.length) console.info(`Office.js extensions left out of the locked-down realm: ${left.join(', ')}`);
  const original = (name: string, key: string) => extensions.get(name)!.prototype[key]?.value as
    Function | undefined;
  const getType = extensions.get('Object')!.statics.getType?.value as ((value: unknown) => unknown) | undefined;
  if (original('Function', 'getName')) {
    Object.defineProperty(Function.prototype, 'getName', { value(this: { __typeName?: unknown }) {
      return this.__typeName === undefined ? '' : this.__typeName;
    } });
  }
  if (original('Function', 'isInstanceOfType')) {
    Object.defineProperty(Function.prototype, 'isInstanceOfType', { value(this: unknown, value: unknown) {
      if (typeof this !== 'function') throw new TypeError('isInstanceOfType must be called on a type');
      return value !== undefined && value !== null && (value instanceof this || getType?.(value) === this);
    } });
  }
  const own = { Date: (value: unknown) => new Date(Date.prototype.getTime.call(value)),
    Number: (value: unknown) => Object(Number.prototype.valueOf.call(value)) };
  for (const [name, ownValue] of Object.entries(own)) {
    const toFormatted = original(name, '_toFormattedString');
    if (!toFormatted) continue;
    for (const [key, culture] of [['format', 'InvariantCulture'], ['localeFormat', 'CurrentCulture']]) {
      Object.defineProperty(builtIn(name).prototype, key, { value(this: unknown, format: unknown) {
        const cultures = (globalThis as { Sys?: { CultureInfo?: Record<string, unknown> } }).Sys?.CultureInfo;
        return String(toFormatted.call(ownValue(this), typeof format === 'object' && format !== null
          || typeof format === 'function' ? String(format) : format, cultures?.[culture]));
      } });
    }
  }
}

/**
 * Gives the page a ``Function`` of its own to carry the statics Office.js calls, as lockdown's takes no
 * properties: it builds functions as lockdown's does and shares its prototype, so ``instanceof Function`` holds,
 * while the code's ``Function`` and every function's ``constructor`` are others, so the statics stay out of its
 * reach. The harden step that follows reads and freezes it.
 */
function replacePageFunction(): Function {
  const evaluate = Function;
  const pageFunction = function (...source: string[]) { return evaluate(...source); };
  pageFunction.prototype = evaluate.prototype;
  Object.defineProperty(globalThis, 'Function', { value: pageFunction, writable: true, enumerable: false,
    configurable: true });
  return pageFunction;
}

/** One run of the code: what it did to the document so far. */
class Run {
  /** Writes queued since the last ``sync``. */
  queued = 0;
  /** A ``sync`` went out with writes in it: something may have landed. */
  wrote = false;
  /** A ``sync`` failed: the writes it carried may have landed in part, whether or not the code caught the error. */
  syncFailed = false;
  timedOut = false;
  /** Why a write was refused; it stands even when the code caught the refusal and went on. */
  blocked?: string;
  image?: string;
  /** Every ``context.sync()`` the host was asked for, which a timed-out run's answer does not wait for. */
  readonly syncs: Promise<unknown>[] = [];

  refuse(reason: string): CodeBlocked {
    this.blocked ??= reason;
    return new CodeBlocked(reason);
  }
}

export async function runOfficeCode(host: CodeHost, code: string): Promise<ClientToolResponse> {
  lockRealm();
  const run = new Run();
  const compartment = new Compartment({
    [host.name]: harden({ ...host.enums }),
    attachImage: (base64: unknown) => {
      if (typeof base64 !== 'string' || !base64) throw new TypeError('attachImage takes the base64 text of a PNG');
      if (run.image !== undefined) throw new Error('attachImage takes one image per call');
      run.image = base64;
    },
    ...Object.fromEntries(NETWORK.map(name => [name, () => {
      throw run.refuse(`${name} reaches the network, which code here cannot.`);
    }])),
  });
  let body: (context: unknown) => Promise<unknown>;
  try {
    body = compartment.evaluate(`(async function(context) { ${code}\n})`);
  } catch (error) {
    return failure(host, run, error);
  }
  let deadline: ReturnType<typeof setTimeout> | undefined;
  const ran = host.run(async context => {
    const returned = await body(new Membrane(host, run).wrap(context, []));
    if (run.blocked) throw new CodeBlocked(run.blocked);
    if (run.timedOut) throw new Error(`context.sync() did not finish within ${SYNC_MS / 1000} s.`);
    // A sync the code did not wait for is still the code's: answer once it is done.
    await Promise.allSettled(run.syncs);
    if (run.queued) {
      throw new Error(`The code returned with ${run.queued} write(s) still queued on the context; they were not `
        + 'applied. Add `await context.sync()` after the last change and run it again.');
    }
    return returned === undefined ? undefined : jsonOf(returned);
  });
  try {
    const result = await Promise.race([ran, new Promise<never>((_resolve, reject) => {
      deadline = setTimeout(() => {
        run.timedOut = true;
        reject(new Error(`The code did not finish within ${CALL_MS / 1000} s.`));
      }, CALL_MS);
    })]);
    return {
      ok: true, ...(result === undefined ? {} : { result }),
      ...(run.image ? { images: [png(run.image)] } : {}),
      mutationEvidence: { outcome: !run.wrote ? 'not_applied' : run.syncFailed ? 'partial' : 'applied',
        native_mutation_started: run.wrote },
    };
  } catch (error) {
    return { ...failure(host, run, error), settled: Promise.allSettled([ran, ...run.syncs]) };
  } finally {
    clearTimeout(deadline);
  }
}

function jsonOf(value: unknown): unknown {
  let text: string | undefined;
  try {
    text = JSON.stringify(value);
  } catch (error) {
    throw new Error(`The code's return value is not JSON-serializable: ${messageOf(error)}`);
  }
  return text === undefined ? undefined : JSON.parse(text);
}

function failure(host: CodeHost, run: Run, error: unknown): ClientToolResponse {
  const details = {
    outcome: run.timedOut ? 'uncertain' : run.wrote ? 'partial' : 'not_applied',
    native_mutation_started: run.wrote || run.timedOut,
  };
  let fields: Pick<ClientToolError, 'code' | 'message' | 'category'>;
  if (run.timedOut) {
    fields = { code: 'OFFICE_JS_TIMEOUT', category: 'timeout', message: `${messageOf(error)} It may still finish in `
      + 'the background: read back what it changes before any further write, and do not run the same code again.' };
  } else if (run.blocked) {
    fields = { code: 'TOOL_EXECUTION_BLOCKED', category: 'validation', message: `ToolExecutionBlocked: ${run.blocked}`
      + `${run.wrote ? ' Changes synced before this stay.' : ' Nothing was changed.'} Tell the user what was refused `
      + 'and why; do not try another way to do it.' };
  } else {
    fields = { code: 'OFFICE_JS_ERROR', category: 'host', message: `${messageOf(error)}${run.wrote
      ? ` Changes synced before the error stay in the ${host.name} document; read them back before you retry.` : ''}` };
  }
  return { ok: false, error: { ...fields, retryable: false, details } };
}

function messageOf(error: unknown): string {
  if (!(error instanceof Error)) return String(error);
  const location = (error as { debugInfo?: { errorLocation?: string } }).debugInfo?.errorLocation;
  return `${error.name}: ${error.message}${location ? ` (at ${location})` : ''}`;
}

/**
 * The document as the code sees it: each host object behind a proxy that keeps Office.js internals out of reach,
 * screens and counts every write, and times each ``context.sync()``. Data comes as a copy, without functions.
 */
class Membrane {
  private readonly proxies = new WeakMap<object, unknown>();
  private readonly targets = new WeakMap<object, unknown>();

  constructor(private readonly host: CodeHost, private readonly run: Run) {}

  wrap(value: unknown, path: readonly string[]): unknown {
    if (typeof value === 'function') return undefined;
    if (value === null || typeof value !== 'object') return value;
    if (Array.isArray(value)) return value.map(item => this.wrap(item, path));
    const prototype = Object.getPrototypeOf(value);
    if (prototype === Object.prototype || prototype === null) {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, this.wrap(item, [...path, key])]));
    }
    if (isPageObject(value)) throw this.run.refuse('The code reached a page object, which it may not use.');
    const known = this.proxies.get(value);
    if (known) return known;
    const proxy = new Proxy(Object.create(null), this.handler(value, path));
    this.proxies.set(value, proxy);
    this.targets.set(proxy, value);
    return proxy;
  }

  private unwrap(value: unknown): unknown {
    if (value === null || typeof value !== 'object') return value;
    if (this.targets.has(value)) return this.targets.get(value);
    if (Array.isArray(value)) return value.map(item => this.unwrap(item));
    if (Object.getPrototypeOf(value) === Object.prototype) {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, this.unwrap(item)]));
    }
    return value;
  }

  /** What the code passes the host, as data for the screen: each document object is ``null``. */
  private data(value: unknown): unknown {
    if (value === null || typeof value !== 'object') return value;
    if (this.targets.has(value)) return null;
    if (Array.isArray(value)) return value.map(item => this.data(item));
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, this.data(item)]));
  }

  private handler(target: object, path: readonly string[]): ProxyHandler<object> {
    const reveal = (name: string | symbol): string => {
      if (typeof name === 'string' && !HIDDEN.test(name)) return name;
      throw this.run.refuse(`${String(name)} is not reachable here.`);
    };
    return {
      get: (_shell, name) => {
        if (name === Symbol.toPrimitive || name === Symbol.toStringTag) return undefined;
        const key = reveal(name);
        const value = Reflect.get(target, key);
        if (typeof value !== 'function') return this.wrap(value, [...path, key]);
        return key === 'sync' ? () => this.sync(target, value)
          : (...args: unknown[]) => this.call(target, path, key, value, args);
      },
      set: (_shell, name, value) => {
        const key = reveal(name);
        this.write(target, { path, name: key, values: [value] });
        return Reflect.set(target, key, this.unwrap(value));
      },
      has: (_shell, name) => typeof name === 'string' && !HIDDEN.test(name) && Reflect.has(target, name),
      ownKeys: () => Reflect.ownKeys(target).filter(key => typeof key === 'string' && !HIDDEN.test(key)),
      getOwnPropertyDescriptor: (_shell, name) => {
        const key = reveal(name);
        return Reflect.has(target, key)
          ? { configurable: true, enumerable: true, writable: true, value: this.wrap(Reflect.get(target, key), path) }
          : undefined;
      },
      defineProperty: () => {
        throw this.run.refuse('Properties of document objects are set by assignment only.');
      },
      deleteProperty: () => {
        throw this.run.refuse('Properties of document objects cannot be deleted.');
      },
      getPrototypeOf: () => null,
      setPrototypeOf: () => false,
    };
  }

  private call(target: object, path: readonly string[], name: string, method: Function, args: unknown[]): unknown {
    if (path.some(step => /^on[A-Z]/.test(step))) {
      throw this.run.refuse('Code cannot listen to document events; read the state it needs instead.');
    }
    if (WRITE_METHODS.has(name) || WRITE_PREFIXES.some(prefix => name.startsWith(prefix))) {
      this.write(target, { path, name, values: args });
    }
    const result = Reflect.apply(method, target, args.map(arg => this.unwrap(arg)));
    const step = [...path, name];
    return typeof result?.then === 'function'
      ? Promise.resolve(result).then(value => this.wrap(value, step))
      : this.wrap(result, step);
  }

  private write(target: object, write: HostWrite): void {
    const reason = this.host.screen({ ...write, values: write.values.map(value => this.data(value)) });
    if (reason) throw this.run.refuse(reason);
    this.host.writing?.(target);
    this.run.queued += 1;
  }

  private async sync(context: object, sync: Function): Promise<void> {
    if (this.run.queued) this.run.wrote = true;
    this.run.queued = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const synced = (Reflect.apply(sync, context, []) as Promise<unknown>).catch((error: unknown) => {
      this.run.syncFailed = true;
      throw error;
    });
    this.run.syncs.push(synced);
    try {
      await Promise.race([synced, new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => {
          this.run.timedOut = true;
          reject(new Error(`context.sync() did not finish within ${SYNC_MS / 1000} s.`));
        }, SYNC_MS);
      })]);
    } finally {
      clearTimeout(timer);
    }
  }
}

/** The enums of a host's namespace (``Excel``, ``Word``), without its classes and functions. */
export function hostEnums(namespace: object): Record<string, Readonly<Record<string, string>>> {
  return Object.fromEntries(Object.entries(namespace).filter(([, value]) => value && typeof value === 'object'
    && Object.values(value).every(item => typeof item === 'string')));
}

function isPageObject(value: object): boolean {
  return value === globalThis || (typeof Node === 'function' && value instanceof Node)
    || (typeof Window === 'function' && value instanceof Window);
}
