// The zip container, over `fflate`. Reads bound every entry from the central
// directory before anything inflates. Writes fix every field a zip would
// otherwise take from the clock or the host, so exporting one deck twice
// gives identical bytes.
import { unzipSync, zipSync, type Unzipped, type UnzipFileInfo } from 'fflate';
import { NotAZip, ZipEntryTooLarge, type ZipCodec, type ZipEntry, type ZipReadOptions } from '../../app/ports.js';

const entryOverflow = (name: string, max: number): string => `${name} expands to more than ${max} bytes`;
const totalOverflow = (max: number): string => `the archive expands to more than ${max} bytes`;

/**
 * `date_time=(1980, 1, 1, 0, 0, 0)` written into the DOS fields. fflate reads
 * the stamp back through `Date`'s local getters, so the instant is built from
 * local components and the encoded fields land on 1980-01-01 00:00:00 in any
 * zone.
 */
const DOS_EPOCH = new Date(1980, 0, 1, 0, 0, 0).getTime();

/** Fixed permissions, so the archive does not carry the host's umask. */
const EXTERNAL_ATTR = 0o600 << 16;

/** `create_system = 3` (unix), fixed rather than read off the host. */
const CREATE_SYSTEM = 3;

export class FflateZip implements ZipCodec {
  read(blob: Uint8Array, opts: ZipReadOptions = {}): ZipEntry[] {
    const max = opts.maxEntryBytes ?? Infinity;
    const maxTotal = opts.maxTotalBytes ?? Infinity;
    const only = opts.only ? new Set(opts.only) : null;
    let declared = 0;
    let unzipped: Unzipped;
    try {
      unzipped = unzipSync(blob, {
        filter: (file: UnzipFileInfo) => {
          // An entry no codec reads is never inflated and never counted, so a
          // media-heavy archive costs its collection and nothing else.
          if (only !== null && !only.has(file.name)) return false;
          if (file.originalSize !== undefined) {
            if (file.originalSize > max) throw new ZipEntryTooLarge(entryOverflow(file.name, max));
            declared += file.originalSize;
            // Duplicate names collapse in the result but each one still
            // inflates, so the running sum is what bounds the heap.
            if (declared > maxTotal) throw new ZipEntryTooLarge(totalOverflow(maxTotal));
          }
          return true;
        },
      });
    } catch (e) {
      if (e instanceof ZipEntryTooLarge) throw e;
      throw new NotAZip(e instanceof Error ? e.message : String(e));
    }
    const out: ZipEntry[] = [];
    let actual = 0;
    for (const [name, bytes] of Object.entries(unzipped)) {
      // A stream that lied about its declared size is caught on the way out.
      if (bytes.length > max) throw new ZipEntryTooLarge(entryOverflow(name, max));
      actual += bytes.length;
      if (actual > maxTotal) throw new ZipEntryTooLarge(totalOverflow(maxTotal));
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
