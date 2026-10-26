const MEBIBYTE = 1024 * 1024;
const END_OF_CENTRAL_DIRECTORY_SIGNATURE = 0x06054b50;
const ZIP64_END_OF_CENTRAL_DIRECTORY_SIGNATURE = 0x06064b50;
const ZIP64_END_OF_CENTRAL_DIRECTORY_LOCATOR_SIGNATURE = 0x07064b50;
const CENTRAL_DIRECTORY_ENTRY_SIGNATURE = 0x02014b50;
const ZIP64_EXTRA_FIELD_ID = 0x0001;
const MAX_ZIP_COMMENT_BYTES = 65_535;

export type OoxmlArchiveLimits = {
  maxCompressedBytes: number;
  maxEntries: number;
  maxEntryUncompressedBytes: number;
  maxTotalUncompressedBytes: number;
};

export type OoxmlArchiveLimitKind = 'compressed-size' | 'entry-count' | 'entry-size' | 'uncompressed-size';

export const PRESENTATION_ARCHIVE_LIMITS: OoxmlArchiveLimits = {
  maxCompressedBytes: 50 * MEBIBYTE,
  maxEntries: 5_000,
  maxEntryUncompressedBytes: 64 * MEBIBYTE,
  maxTotalUncompressedBytes: 200 * MEBIBYTE,
};

export const SPREADSHEET_ARCHIVE_LIMITS: OoxmlArchiveLimits = {
  maxCompressedBytes: 50 * MEBIBYTE,
  maxEntries: 5_000,
  maxEntryUncompressedBytes: 64 * MEBIBYTE,
  maxTotalUncompressedBytes: 128 * MEBIBYTE,
};

export class OoxmlArchiveLimitError extends Error {
  constructor(readonly limit: OoxmlArchiveLimitKind) {
    super(`The OOXML archive exceeds the ${limit} preview limit`);
    this.name = 'OoxmlArchiveLimitError';
  }
}

export function isOoxmlArchiveLimitError(error: unknown): error is OoxmlArchiveLimitError {
  return error instanceof OoxmlArchiveLimitError;
}

export function inspectOoxmlArchive(buffer: ArrayBuffer | ArrayBufferView, limits: OoxmlArchiveLimits): void {
  if (buffer.byteLength > limits.maxCompressedBytes) throw new OoxmlArchiveLimitError('compressed-size');

  const view = buffer instanceof ArrayBuffer ? new DataView(buffer) : new DataView(buffer.buffer, buffer.byteOffset, buffer.byteLength);
  const directory = readCentralDirectory(view);
  if (directory.entryCount > BigInt(limits.maxEntries)) throw new OoxmlArchiveLimitError('entry-count');

  const directoryOffset = safeOffset(directory.offset, view.byteLength);
  const directorySize = safeOffset(directory.size, view.byteLength);
  const directoryEnd = directoryOffset + directorySize;
  if (directoryEnd > view.byteLength) throw new Error('The OOXML archive has an invalid central directory');

  let entryOffset = directoryOffset;
  let totalUncompressedBytes = 0n;
  for (let index = 0; index < Number(directory.entryCount); index += 1) {
    assertAvailable(view, entryOffset, 46);
    if (view.getUint32(entryOffset, true) !== CENTRAL_DIRECTORY_ENTRY_SIGNATURE) {
      throw new Error('The OOXML archive has an invalid central directory entry');
    }

    const fileNameLength = view.getUint16(entryOffset + 28, true);
    const extraFieldLength = view.getUint16(entryOffset + 30, true);
    const commentLength = view.getUint16(entryOffset + 32, true);
    const extraFieldOffset = entryOffset + 46 + fileNameLength;
    const nextEntryOffset = extraFieldOffset + extraFieldLength + commentLength;
    if (nextEntryOffset > directoryEnd) throw new Error('The OOXML archive has a truncated central directory entry');

    const uncompressedSize = readEntryUncompressedSize(view, entryOffset, extraFieldOffset, extraFieldLength);
    if (uncompressedSize > BigInt(limits.maxEntryUncompressedBytes)) throw new OoxmlArchiveLimitError('entry-size');
    totalUncompressedBytes += uncompressedSize;
    if (totalUncompressedBytes > BigInt(limits.maxTotalUncompressedBytes)) {
      throw new OoxmlArchiveLimitError('uncompressed-size');
    }
    entryOffset = nextEntryOffset;
  }
  if (entryOffset !== directoryEnd) throw new Error('The OOXML archive central directory size does not match its entries');
}

type CentralDirectory = {
  entryCount: bigint;
  offset: bigint;
  size: bigint;
};

function readCentralDirectory(view: DataView): CentralDirectory {
  const endOffset = findEndOfCentralDirectory(view);
  const diskNumber = view.getUint16(endOffset + 4, true);
  const directoryDiskNumber = view.getUint16(endOffset + 6, true);
  const diskEntryCount = view.getUint16(endOffset + 8, true);
  const entryCount = view.getUint16(endOffset + 10, true);
  const directorySize = view.getUint32(endOffset + 12, true);
  const directoryOffset = view.getUint32(endOffset + 16, true);
  const usesZip64 =
    diskNumber === 0xffff ||
    directoryDiskNumber === 0xffff ||
    diskEntryCount === 0xffff ||
    entryCount === 0xffff ||
    directorySize === 0xffffffff ||
    directoryOffset === 0xffffffff;

  if (usesZip64) return readZip64CentralDirectory(view, endOffset);
  if (diskNumber !== 0 || directoryDiskNumber !== 0 || diskEntryCount !== entryCount) {
    throw new Error('Multi-disk OOXML archives are not supported');
  }
  return {
    entryCount: BigInt(entryCount),
    offset: BigInt(directoryOffset),
    size: BigInt(directorySize),
  };
}

function findEndOfCentralDirectory(view: DataView): number {
  const minimumOffset = Math.max(0, view.byteLength - 22 - MAX_ZIP_COMMENT_BYTES);
  for (let offset = view.byteLength - 22; offset >= minimumOffset; offset -= 1) {
    if (view.getUint32(offset, true) !== END_OF_CENTRAL_DIRECTORY_SIGNATURE) continue;
    const commentLength = view.getUint16(offset + 20, true);
    if (offset + 22 + commentLength === view.byteLength) return offset;
  }
  throw new Error('The OOXML archive has no valid end-of-central-directory record');
}

function readZip64CentralDirectory(view: DataView, endOffset: number): CentralDirectory {
  const locatorOffset = endOffset - 20;
  assertAvailable(view, locatorOffset, 20);
  if (view.getUint32(locatorOffset, true) !== ZIP64_END_OF_CENTRAL_DIRECTORY_LOCATOR_SIGNATURE) {
    throw new Error('The OOXML archive has no valid ZIP64 locator');
  }
  const zip64DiskNumber = view.getUint32(locatorOffset + 4, true);
  const zip64EndOffset = safeOffset(view.getBigUint64(locatorOffset + 8, true), view.byteLength);
  const diskCount = view.getUint32(locatorOffset + 16, true);
  assertAvailable(view, zip64EndOffset, 56);
  if (view.getUint32(zip64EndOffset, true) !== ZIP64_END_OF_CENTRAL_DIRECTORY_SIGNATURE) {
    throw new Error('The OOXML archive has no valid ZIP64 end-of-central-directory record');
  }

  const recordSize = view.getBigUint64(zip64EndOffset + 4, true);
  if (recordSize < 44n || BigInt(zip64EndOffset) + 12n + recordSize > BigInt(locatorOffset)) {
    throw new Error('The OOXML archive has an invalid ZIP64 end-of-central-directory record');
  }
  const diskNumber = view.getUint32(zip64EndOffset + 16, true);
  const directoryDiskNumber = view.getUint32(zip64EndOffset + 20, true);
  const diskEntryCount = view.getBigUint64(zip64EndOffset + 24, true);
  const entryCount = view.getBigUint64(zip64EndOffset + 32, true);
  if (zip64DiskNumber !== 0 || diskCount !== 1 || diskNumber !== 0 || directoryDiskNumber !== 0 || diskEntryCount !== entryCount) {
    throw new Error('Multi-disk OOXML archives are not supported');
  }
  return {
    entryCount,
    size: view.getBigUint64(zip64EndOffset + 40, true),
    offset: view.getBigUint64(zip64EndOffset + 48, true),
  };
}

function readEntryUncompressedSize(view: DataView, entryOffset: number, extraFieldOffset: number, extraFieldLength: number): bigint {
  const uncompressed32 = view.getUint32(entryOffset + 24, true);
  if (uncompressed32 !== 0xffffffff) return BigInt(uncompressed32);
  return readZip64UncompressedSize(view, extraFieldOffset, extraFieldLength);
}

function readZip64UncompressedSize(view: DataView, extraFieldOffset: number, extraFieldLength: number): bigint {
  const extraFieldEnd = extraFieldOffset + extraFieldLength;
  let offset = extraFieldOffset;
  while (offset + 4 <= extraFieldEnd) {
    const fieldId = view.getUint16(offset, true);
    const fieldSize = view.getUint16(offset + 2, true);
    const fieldDataOffset = offset + 4;
    const nextFieldOffset = fieldDataOffset + fieldSize;
    if (nextFieldOffset > extraFieldEnd) throw new Error('The OOXML archive has a truncated extra field');
    if (fieldId === ZIP64_EXTRA_FIELD_ID) {
      if (fieldDataOffset + 8 > nextFieldOffset) throw new Error('The OOXML archive has an incomplete ZIP64 size field');
      return view.getBigUint64(fieldDataOffset, true);
    }
    offset = nextFieldOffset;
  }
  throw new Error('The OOXML archive is missing ZIP64 entry sizes');
}

function safeOffset(value: bigint, byteLength: number): number {
  if (value > BigInt(byteLength)) throw new Error('The OOXML archive contains an invalid offset');
  return Number(value);
}

function assertAvailable(view: DataView, offset: number, length: number): void {
  if (offset < 0 || length < 0 || offset + length > view.byteLength) throw new Error('The OOXML archive is truncated');
}
