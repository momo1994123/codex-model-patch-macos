#!/usr/bin/env python3
"""
Build a `model_catalog_json` file for the Codex / ChatGPT desktop app so that a
model served by a *custom* provider (relay, local proxy, self-hosted gateway)
actually appears in the composer model picker.

Why this is needed
------------------
The desktop client's picker only renders models returned by the CLI's
`model/list` RPC. That list is built from the CLI's model catalog. When you
switch `model_provider` to a custom provider, the CLI does NOT add your
`model = "..."` to the catalog -- the picker keeps showing the built-in OpenAI
models only, and the current model falls back to the "custom" label.

Pointing `model_catalog_json` at a catalog that *contains* your model fixes the
data source. Combined with the client-side asar patch (see codex_model_patch.py)
the model then renders in the pill with its display name.

Usage
-----
  python3 build_model_catalog.py <slug> [display-name] [options]

Examples
--------
  python3 build_model_catalog.py glm-5.3-flash "GLM-5.3-Flash"
  python3 build_model_catalog.py deepseek-v4-flash "DeepSeek V4 Flash" \
      --template gpt-5.5 --out ~/.codex/model-catalog.json

Options
-------
  --template SLUG    clone an existing catalog entry for the required fields
                     (default: gpt-5.5)
  --out PATH         output catalog (default: ~/.codex/model-catalog.json)
  --codex PATH       codex executable (default: autodetect from ChatGPT.app)
  --context-window N  override context window
  --reasoning EFFORT  default reasoning effort (default: keep template's)
  --print            print the catalog to stdout instead of writing

After writing, add this line to ~/.codex/config.toml (top level, before any
`[table]` header):

  model_catalog_json = "/absolute/path/to/model-catalog.json"

then restart the app.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

DEFAULT_CODEX_CANDIDATES = [
    '/Applications/ChatGPT.app/Contents/Resources/codex',
    '/Applications/Codex.app/Contents/Resources/codex',
    os.path.expanduser('~/.local/bin/codex'),
    '/usr/local/bin/codex',
    '/opt/homebrew/bin/codex',
]


def find_codex(explicit=None):
    if explicit:
        return explicit
    for c in DEFAULT_CODEX_CANDIDATES:
        if os.path.exists(c):
            return c
    p = shutil.which('codex')
    if p:
        return p
    return None


def dump_builtin(codex):
    env = dict(os.environ)
    env.setdefault('TERM', 'xterm-256color')
    out = subprocess.run([codex, 'debug', 'models'], capture_output=True, text=True, env=env)
    if out.returncode != 0:
        sys.stderr.write(out.stderr or 'codex debug models failed\n')
        sys.exit(1)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as e:
        sys.stderr.write('could not parse catalog JSON: %s\n' % e)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument('slug')
    ap.add_argument('display_name', nargs='?')
    ap.add_argument('--template', default='gpt-5.5')
    ap.add_argument('--out', default=os.path.expanduser('~/.codex/model-catalog.json'))
    ap.add_argument('--codex')
    ap.add_argument('--context-window', type=int)
    ap.add_argument('--reasoning')
    ap.add_argument('--print', dest='to_stdout', action='store_true')
    args = ap.parse_args()

    codex = find_codex(args.codex)
    if not codex:
        sys.stderr.write('codex executable not found; pass --codex PATH\n')
        sys.exit(1)

    catalog = dump_builtin(codex)
    models = catalog.get('models')
    if not isinstance(models, list):
        sys.stderr.write('unexpected catalog shape (no "models" list)\n')
        sys.exit(1)

    tpl = next((m for m in models if m.get('slug') == args.template), None)
    if tpl is None:
        sys.stderr.write('template slug %r not in catalog; pick another with --template\n' % args.template)
        sys.exit(1)

    entry = json.loads(json.dumps(tpl))          # deep copy
    entry['slug'] = args.slug
    entry['display_name'] = args.display_name or args.slug
    entry['description'] = args.display_name or args.slug
    entry['visibility'] = 'list'                 # 'hide' would drop it from the picker
    entry['priority'] = entry.get('priority', 1000)
    if args.context_window:
        entry['context_window'] = args.context_window
        entry['max_context_window'] = args.context_window
    if args.reasoning:
        entry['default_reasoning_level'] = args.reasoning

    existing = {m.get('slug') for m in models}
    if args.slug in existing:
        models[:] = [entry if m.get('slug') == args.slug else m for m in models]
        action = 'updated'
    else:
        models.append(entry)
        action = 'added'

    data = json.dumps(catalog, ensure_ascii=False, indent=2)

    if args.to_stdout:
        print(data)
        return

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, 'w') as f:
        f.write(data + '\n')
    print('%s %s (%s) in %s' % (action, args.slug, entry['display_name'], args.out))
    print('catalog now has %d model(s): %s'
          % (len(models), ', '.join(m.get('slug', '?') for m in models)))
    print()
    print('Next: add this top-level line to ~/.codex/config.toml, then restart the app:')
    print('  model_catalog_json = "%s"' % os.path.abspath(args.out))


if __name__ == '__main__':
    main()
