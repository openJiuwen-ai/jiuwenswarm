/** Temporary files in WPS's temporary directory, through which a WPS adapter hands documents to WPS and back. */
import type { WpsObject } from './wpsDocument';

export class WpsFiles {
  /** Set once a temporary file could not be removed, which is said once. */
  private leftFile = false;

  /** ``app`` names the WPS app in errors, as ``WPS Presentation``. */
  constructor(protected readonly application: WpsObject, private readonly app: string) {}

  temporaryPath(prefix: string, extension: string): string {
    const { Env, FileSystem } = this.application;
    const root = String(Env?.GetTempPath?.() ?? FileSystem?.tmpdir?.() ?? '');
    if (!root) throw new Error(`${this.app} gave no temporary directory (Env.GetTempPath, FileSystem.tmpdir)`);
    const separator = root.includes('\\') && !root.includes('/') ? '\\' : '/';
    return `${root.replace(/[\\/]+$/, '')}${separator}${prefix}-${crypto.randomUUID()}${extension}`;
  }

  write(path: string, bytes: Uint8Array): void {
    let binary = '';
    for (let at = 0; at < bytes.length; at += 32768) binary += String.fromCharCode(...bytes.subarray(at, at + 32768));
    this.application.FileSystem.writeAsBinaryString(path, binary);
  }

  /** The file's bytes. Some builds upper-case the extension of a file WPS wrote, and ``Exists`` caches, so read. */
  async read(path: string): Promise<Uint8Array> {
    try {
      return await this.readOne(path);
    } catch (error) {
      const upper = upperExtension(path);
      if (upper === path) throw error;
      return this.readOne(upper);
    }
  }

  private async readOne(path: string): Promise<Uint8Array> {
    const { FileSystem } = this.application;
    if (typeof FileSystem.ReadFileAsArrayBuffer === 'function') {
      const buffer: ArrayBuffer | ArrayBufferView = await FileSystem.ReadFileAsArrayBuffer(path);
      return buffer instanceof ArrayBuffer ? new Uint8Array(buffer)
        : new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
    }
    // readAsBinaryString sign-extends bytes from 0x80, hence the mask.
    const binary: string = FileSystem.readAsBinaryString(path);
    return Uint8Array.from(binary, char => char.charCodeAt(0) & 0xff);
  }

  /**
   * Removes the file under either spelling of its extension, one of which WPS did not use. A copy of the user's
   * document that stays behind is said once.
   */
  remove(path: string): void {
    const { FileSystem } = this.application;
    const errors: unknown[] = [];
    for (const candidate of new Set([path, upperExtension(path)])) {
      try {
        (FileSystem.unlinkSync ?? FileSystem.Remove).call(FileSystem, candidate);
        return;
      } catch (error) {
        errors.push(error);
      }
    }
    if (this.leftFile) return;
    this.leftFile = true;
    console.warn(`${this.app} kept the temporary file ${path}`, ...errors);
  }

  /** ``bytes`` as a file while ``use`` runs; removed after. */
  async withFile<T>(prefix: string, extension: string, bytes: Uint8Array, use: (path: string) => T | Promise<T>):
  Promise<T> {
    const path = this.temporaryPath(prefix, extension);
    try {
      this.write(path, bytes);
      return await use(path);
    } finally {
      this.remove(path);
    }
  }

  /** The bytes of the file ``write`` has WPS write at a temporary path (an export, a saved copy); removed after. */
  async written(prefix: string, extension: string, write: (path: string) => void): Promise<Uint8Array> {
    const path = this.temporaryPath(prefix, extension);
    try {
      write(path);
      return await this.read(path);
    } finally {
      this.remove(path);
    }
  }
}

function upperExtension(path: string): string {
  return path.replace(/\.(\w+)$/, (_match, extension: string) => `.${extension.toUpperCase()}`);
}
