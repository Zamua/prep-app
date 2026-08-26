// The zip container, over `fflate`. Reads bound every entry from the central
// directory before anything inflates; writes reproduce CPython's
// `zipfile.ZipFile(..., ZIP_STORED)` with a fixed `ZipInfo` byte for byte, so
// `.prepdeck` is a byte-parity format rather than a merely equivalent one.
import { unzipSync, zipSync, type Unzipped, type UnzipFileInfo } from 'fflate';
import { NotAZip, ZipEntryTooLarge, type ZipCodec, type ZipEntry } from '../../app/ports.js';

/**
 * `date_time=(1980, 1, 1, 0, 0, 0)` written into the DOS fields. fflate reads
 * the stamp back through `Date`'s local getters, so the instant is built from
 * local components and the encoded fields land on 1980-01-01 00:00:00 in any
 * zone.
 */
const DOS_EPOCH = new Date(1980, 0, 1, 0, 0, 0).getTime();

/** CPython's `_open_to_write` fills an unset `external_attr` with `0o600 << 16`. */
const EXTERNAL_ATTR = 0o600 << 16;

/** `create_system = 3` (unix), which CPython picks off `sys.platform`. */
const CREATE_SYSTEM = 3;

export class FflateZip implements ZipCodec {
  read(blob: Uint8Array, opts: { maxEntryBytes?: number } = {}): ZipEntry[] {
    const max = opts.maxEntryBytes ?? Infinity;
    let unzipped: Unzipped;
    try {
      unzipped = unzipSync(blob, {
        filter: (file: UnzipFileInfo) => {
          if (file.originalSize !== undefined && file.originalSize > max) throw new ZipEntryTooLarge(file.name);
          return true;
        },
      });
    } catch (e) {
      if (e instanceof ZipEntryTooLarge) throw e;
      throw new NotAZip(e instanceof Error ? e.message : String(e));
    }
    const out: ZipEntry[] = [];
    for (const [name, bytes] of Object.entries(unzipped)) {
      // A stream that lied about its declared size is caught on the way out.
      if (bytes.length > max) throw new ZipEntryTooLarge(name);
      out.push({ name, bytes });
    }
    return out;
  }

  write(entries: readonly ZipEntry[]): Uint8Array {
    const files: Record<string, [Uint8Array, { level: 0; mtime: number; os: number; attrs: number }]> = {};
    for (const e of entries) files[e.name] = [e.bytes, { level: 0, mtime: DOS_EPOCH, os: CREATE_SYSTEM, attrs: EXTERNAL_ATTR }];
    return zipSync(files, { level: 0 });
  }
}
