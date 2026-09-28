#!/usr/bin/env python3
"""
asar patch tool for the ChatGPT/Codex desktop app (macOS / Windows / Linux).

Goal: make a model that the CLI reports (with a display_name) show up in the
composer model pill, regardless of the app's auth mode / Statsig-driven
"hidden models" config.

The app's model filter (minified) reads:

    function <f>({additionalAvailableModels:e,authMethod:t,availableModels:n,
                  model:r,useHiddenModels:i}){
        return e?.has(r.model)===!0 || (i&&t!==`amazonBedrock`?n.has(r.model):!r.hidden)
    }

When `use_hidden_models` is true and the `available_models` allowlist is empty,
the auth-dependent branch drops every model and the pill falls back to the
"自定义" / "custom" label.

This tool rewrites that auth-dependent branch to a plain visibility check:

    (i&&t!==`amazonBedrock`?n.has(r.model):!r.hidden)   ->   (!r.hidden/* padding */)

Same byte length (in-place, file size preserved), with the asar header
integrity hashes refreshed so Electron's asar integrity check still passes.

Differences from the upstream tool (momo1994123/codex-model-patch):
  * FIXED header parser: data begins at (8 + header_size). Newer asar builds
    pad the JSON blob, so the upstream `16 + json_len` formula is off and its
    own assert fires ("AssertionError: (1594421, 1594424)").
  * ADAPTIVE target: the minified variable names change between app builds, so
    instead of a single hard-coded literal the branch is matched by regex and
    the padding is computed per site. Works on both the old and the current
    minified shape.

Usage:
  python codex_model_patch.py locate  [app.asar]
  python codex_model_patch.py patch   [app.asar] [--dry-run]
  python codex_model_patch.py restore [app.asar]

If app.asar is omitted, the tool searches common install locations for the
ChatGPT / Codex desktop app and picks the first match.
"""
import hashlib
import json
import os
import re
import shutil
import struct
import sys

MARKER = b'/*CODEX_CUSTOM_MODELS_V1*/'

# --- version-adaptive branch matchers -------------------------------------
# Current shape:  (i&&t!==`amazonBedrock`?n.has(r.model):!r.hidden)
BRANCH_RE = re.compile(
    rb'\(([A-Za-z_$][A-Za-z0-9_$]*)&&([A-Za-z_$][A-Za-z0-9_$]*)!==`amazonBedrock`\?'
    rb'([A-Za-z_$][A-Za-z0-9_$]*)\.has\(([A-Za-z_$][A-Za-z0-9_$]*)\.model\):!'
    rb'([A-Za-z_$][A-Za-z0-9_$]*)\.hidden\)'
)
# Legacy shape:   (u?n.has(r.model):!r.hidden)
LEGACY_RE = re.compile(
    rb'\([A-Za-z_$][A-Za-z0-9_$]*\?'
    rb'([A-Za-z_$][A-Za-z0-9_$]*)\.has\(([A-Za-z_$][A-Za-z0-9_$]*)\.model\):!'
    rb'([A-Za-z_$][A-Za-z0-9_$]*)\.hidden\)'
)
MATCHERS = (BRANCH_RE, LEGACY_RE)

# Marker byte sequence for "already patched" -- the padding comment.
PATCHED_RE = re.compile(rb'\(!([A-Za-z_$][A-Za-z0-9_$]*)\.hidden/\*')

BACKUP_SUFFIX = '.bak_codexmodels'

ENV_DIRS = 'CODEX_ASAR_DIRS'

CANDIDATE_ROOTS = [
    # macOS
    '/Applications/ChatGPT.app/Contents/Resources',
    '/Applications/Codex.app/Contents/Resources',
    os.path.expanduser('~/Applications/ChatGPT.app/Contents/Resources'),
    # Windows
    os.path.expandvars(r'%LOCALAPPDATA%\Programs\ChatGPT\resources'),
    os.path.expandvars(r'%LOCALAPPDATA%\Programs\Codex\resources'),
    os.path.expandvars(r'%LOCALAPPDATA%\ChatGPT\resources'),
    os.path.expandvars(r'%PROGRAMFILES%\ChatGPT\resources'),
    os.path.expandvars(r'%PROGRAMFILES%\Codex\resources'),
    # Linux
    '/opt/ChatGPT/resources',
    '/usr/lib/chatgpt/resources',
    os.path.expanduser('~/.local/share/chatgpt/resources'),
]


def search_roots():
    roots = list(CANDIDATE_ROOTS)
    extra = os.environ.get(ENV_DIRS, '')
    for d in extra.split(os.pathsep):
        d = d.strip()
        if d:
            roots.insert(0, d)
    return roots


def find_asar(explicit=None):
    if explicit and explicit not in ('auto', '-'):
        return explicit if os.path.exists(explicit) else None
    for root in search_roots():
        if not root:
            continue
        p = os.path.join(root, 'app.asar')
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------- asar format

def parse_header(path):
    """Return (header_dict, data_offset, json_offset, json_size, total_size).

    Chromium pickle layout:
      u32 pickle_size(=4) | u32 header_size | u32 json_padded | u32 json_len
    then json_len bytes of JSON (space/NUL padded up to the data region).

    The file data region begins at (8 + header_size). Newer Electron builds
    pad the JSON blob, so `16 + json_len` is NOT reliable -- use the header
    size field, which is authoritative.
    """
    with open(path, 'rb') as f:
        head = f.read(16)
        pickle_size, header_size, json_padded, json_len = struct.unpack('<IIII', head)
        json_offset = 12 + pickle_size          # == 16
        raw = f.read(json_len)
    hdr = json.loads(raw.decode('utf-8'))
    total = os.path.getsize(path)
    data_offset = 8 + header_size
    return hdr, data_offset, json_offset, json_len, total


def walk_files(node, prefix=''):
    for name, ent in (node.get('files') or {}).items():
        p = prefix + '/' + name if prefix else name
        if 'files' in ent:
            yield from walk_files(ent, p)
        else:
            yield p, ent


def read_blob(fh, data_offset, ent):
    fh.seek(data_offset + int(ent['offset']))
    return fh.read(int(ent['size']))


def candidate_entries(hdr, fh, data_offset):
    for p, ent in walk_files(hdr):
        if not p.endswith(('.js', '.mjs', '.cjs')):
            continue
        if 'offset' not in ent:
            continue
        size = int(ent['size'])
        if size < 64 or size > 32 * 1024 * 1024:
            continue
        yield p, ent


def find_sites(blob):
    """Return list of (start, end, replacement_bytes) for every branch site."""
    sites = []
    for rx in MATCHERS:
        for m in rx.finditer(blob):
            full = m.group(0)
            model = m.group(m.lastindex) if False else None
            # model var is the group that appears in "!<model>.hidden"
            # For BRANCH_RE that's group 5; for LEGACY_RE group 3.
            model = m.group(5) if rx is BRANCH_RE else m.group(3)
            core = b'(!' + model + b'.hidden)'
            # form: core-without-close + /* pad */ + )  == same total length
            # build: b'(!' + model + b'.hidden' + b'/*' + b' '*k + b'*/)'
            head = b'(!' + model + b'.hidden'
            fixed = len(head) + 2 + 2 + 1          # head + '/*' + '*/' + ')'
            k = len(full) - fixed
            if k < 1:
                continue
            repl = head + b'/*' + b' ' * k + b'*/)'
            assert len(repl) == len(full)
            sites.append((m.start(), m.end(), repl))
        if sites:
            break
    return sites


# ---------------------------------------------------------------------- modes

def locate(path):
    hdr, data_offset, json_offset, json_size, total = parse_header(path)
    print('asar        : %s' % path)
    print('size        : %d' % total)
    print('data_offset : %d   json_offset: %d   json_len: %d'
          % (data_offset, json_offset, json_size))
    print()

    with open(path, 'rb') as fh:
        hits = 0
        for p, ent in candidate_entries(hdr, fh, data_offset):
            blob = read_blob(fh, data_offset, ent)
            has_marker = MARKER in blob
            sites = find_sites(blob)
            already = len(PATCHED_RE.findall(blob))
            if not has_marker and not sites and not already:
                continue
            hits += 1
            if sites:
                status = 'NOT PATCHED  (%d site(s) to change)' % len(sites)
            elif already:
                status = 'ALREADY PATCHED  (%d site(s))' % already
            else:
                status = ('marker present, but no branch found - '
                          'app version may have changed')
            print('--- %s   entry.offset=%s size=%s' % (p, ent['offset'], ent['size']))
            print('    status  : %s' % status)
            for (s, e, r) in sites:
                print('      old in-file=%d  ABS=%d' % (s, data_offset + int(ent['offset']) + s))
                print('        old: %s' % blob[s:e].decode('latin1'))
                print('        new: %s' % r.decode('latin1'))
            integ = ent.get('integrity')
            if integ:
                print('    integrity algo=%s blocks=%d hash=%s'
                      % (integ.get('algorithm'), len(integ.get('blocks') or []),
                         str(integ.get('hash'))[:24] + '...'))
            print()
        if not hits:
            print('(no branch site, no patched form found)')
    return 0


def rewrite(blob, sites):
    out = bytearray()
    prev = 0
    for (s, e, r) in sites:
        out += blob[prev:s]
        out += r
        prev = e
    out += blob[prev:]
    return bytes(out)


def patch(path, dry_run):
    hdr, data_offset, json_offset, json_size, total = parse_header(path)

    targets = []
    with open(path, 'rb') as fh:
        for p, ent in candidate_entries(hdr, fh, data_offset):
            blob = read_blob(fh, data_offset, ent)
            sites = find_sites(blob)
            if sites:
                targets.append((p, ent, blob, sites))

    if not targets:
        already = []
        with open(path, 'rb') as fh:
            for p, ent in candidate_entries(hdr, fh, data_offset):
                blob = read_blob(fh, data_offset, ent)
                if PATCHED_RE.search(blob):
                    already.append(p)
        if already:
            print('already patched - nothing to do.')
            for p in already:
                print('  %s' % p)
            return 0
        print('nothing to patch: no model-filter branch present.')
        print('The app version may have changed. Run "locate" to see what is there.')
        return 1

    print('entries to patch: %d' % len(targets))
    for p, ent, blob, sites in targets:
        print('  %-60s occurrences=%d' % (p, len(sites)))
        for (s, e, r) in sites:
            print('    old: %s' % blob[s:e].decode('latin1'))
            print('    new: %s' % r.decode('latin1'))

    if dry_run:
        print('\n[dry-run] nothing written.')
        return 0

    backup = path + BACKUP_SUFFIX
    if not os.path.exists(backup):
        print('\nbackup -> %s' % backup)
        shutil.copy2(path, backup)
    else:
        print('\nbackup already exists: %s' % backup)

    with open(path, 'rb') as fh:
        fh.seek(json_offset)
        header_bytes = bytearray(fh.read(json_size))

    replacements = []
    new_blobs = []
    for p, ent, blob, sites in targets:
        new_blob = rewrite(blob, sites)
        assert len(new_blob) == len(blob)
        new_blobs.append((p, ent, blob, new_blob, sites))
        integ = ent.get('integrity')
        if not integ:
            continue
        block = int(integ.get('blockSize') or 4194304)
        old_blocks = list(integ.get('blocks') or [])
        new_blocks = [hashlib.sha256(new_blob[k:k + block]).hexdigest()
                      for k in range(0, len(new_blob), block)]
        old_top = integ.get('hash')
        new_top = hashlib.sha256(new_blob).hexdigest()
        if old_top and old_top != new_top:
            replacements.append((old_top, new_top))
        for ob, nb in zip(old_blocks, new_blocks):
            if ob != nb:
                replacements.append((ob, nb))

    seen = set()
    repl = []
    for old, new in replacements:
        if old in seen:
            continue
        seen.add(old)
        repl.append((old, new))

    changes = 0
    with open(path, 'r+b') as fh:
        for p, ent, blob, new_blob, sites in new_blobs:
            base = data_offset + int(ent['offset'])
            fh.seek(base)
            fh.write(new_blob)
            changes += len(sites)
            print('patched %s (%d occurrence(s))' % (p, len(sites)))

        for old, new in repl:
            ob = old.encode('ascii')
            nb = new.encode('ascii')
            cnt = header_bytes.count(ob)
            if cnt == 0:
                print('  WARN integrity hash not found in header: %s...' % old[:16])
                continue
            header_bytes = bytearray(bytes(header_bytes).replace(ob, nb))
            print('  integrity %s... -> %s...  (%d site(s))' % (old[:16], new[:16], cnt))

        fh.seek(json_offset)
        fh.write(bytes(header_bytes))

    # ------------------------------------------------------------ verification
    print('\nverifying...')
    ok = True
    after = os.path.getsize(path)
    if after != total:
        print('  FAIL size changed: %d -> %d' % (total, after))
        ok = False

    try:
        hdr2, do2, hs2, js2, _ = parse_header(path)
        with open(path, 'rb') as fh:
            for p, ent in candidate_entries(hdr2, fh, do2):
                integ = ent.get('integrity')
                if not integ:
                    continue
                blob = read_blob(fh, do2, ent)
                block = int(integ.get('blockSize') or 4194304)
                blocks = [hashlib.sha256(blob[k:k + block]).hexdigest()
                          for k in range(0, len(blob), block)]
                if integ.get('blocks') and list(integ['blocks']) != blocks:
                    print('  FAIL integrity blocks mismatch for %s' % p)
                    ok = False
                if integ.get('hash') and integ['hash'] != hashlib.sha256(blob).hexdigest():
                    print('  FAIL integrity hash mismatch for %s' % p)
                    ok = False
    except Exception as e:
        print('  FAIL verification error: %s' % e)
        ok = False

    hdr3, do3, _, _, _ = parse_header(path)
    still = 0
    now = 0
    with open(path, 'rb') as fh:
        for p, ent in candidate_entries(hdr3, fh, do3):
            blob = read_blob(fh, do3, ent)
            still += len(find_sites(blob))
            now += len(PATCHED_RE.findall(blob))

    print('  replacements applied      : %d' % changes)
    print('  old pattern remaining     : %d  (expect 0)' % still)
    print('  new pattern present       : %d' % now)
    if still != 0:
        ok = False

    if not ok:
        print('\nVERIFICATION FAILED - restoring from backup')
        shutil.copy2(backup, path)
        return 1

    print('\nOK  size=%d (unchanged)  backup=%s' % (after, backup))
    return 0


def restore(path):
    backup = path + BACKUP_SUFFIX
    if not os.path.exists(backup):
        print('no backup found at %s' % backup)
        return 1
    shutil.copy2(backup, path)
    print('restored %s from %s' % (path, backup))
    return 0


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = [a for a in sys.argv[1:] if a.startswith('--')]
    if not args:
        print(__doc__)
        sys.exit(2)
    mode = args[0]
    explicit = args[1] if len(args) > 1 else None

    asar = find_asar(explicit)
    if asar is None:
        print('Could not locate app.asar automatically.')
        print('Pass the path explicitly, e.g.:')
        print('  python codex_model_patch.py %s "<path to app.asar>"' % mode)
        print('\nSearched:')
        for root in search_roots():
            if root:
                print('  ' + os.path.join(root, 'app.asar'))
        print('\nTip: set %s to your install dir (os.pathsep-separated).' % ENV_DIRS)
        sys.exit(2)

    print('app.asar: %s\n' % asar)

    if mode == 'locate':
        sys.exit(locate(asar))
    if mode == 'patch':
        sys.exit(patch(asar, '--dry-run' in flags))
    if mode == 'restore':
        sys.exit(restore(asar))
    print(__doc__)
    sys.exit(2)
