#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web console for the meeting brief helper.

  python3 webapp.py                       # http://127.0.0.1:8765/
  python3 webapp.py --port 9000 --pack /path/to/pack --store /path/to/store

Two pages: /console runs the CLI commands (brief, ask, forget, ingest) from a form, /metrics shows the
command log, model-call log, latency and token metrics, and past harness runs. It starts the real
assist.py for every command, so it behaves exactly like the CLI. Standard library only.

It listens on 127.0.0.1 by default. There is no login, so do not expose it to a network you do not
trust: the pages show people's work records.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from assist import web  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(prog='webapp.py', description='Web console for the meeting brief helper')
    ap.add_argument('--host', default='127.0.0.1', help='address to listen on (default 127.0.0.1, this machine only)')
    ap.add_argument('--port', type=int, default=8765, help='port (default 8765; 0 picks a free one)')
    ap.add_argument('--pack', help='folder that holds data/ (default: $R3_PACK, else the current folder)')
    ap.add_argument('--store', help='store folder (default: $R3_STORE, else ./.assist_store)')
    ap.add_argument('--runs', help='folder of harness runs shown on the metrics page (default: ./runs next to this file)')
    a = ap.parse_args(argv)
    if a.pack and not os.path.isdir(os.path.join(a.pack, 'data')):
        sys.exit('error: %s has no data/ folder' % a.pack)
    app = web.App(pack=a.pack, store=a.store, runs=a.runs)
    srv = web.serve(app, a.host, a.port)
    port = srv.server_address[1]
    if a.host not in ('127.0.0.1', 'localhost', '::1'):
        print('WARNING: listening on %s with no login. Anyone who can reach this port can read the records '
              'and run commands.' % a.host, file=sys.stderr)
    print('Console : http://%s:%d/console' % (a.host if a.host != '0.0.0.0' else '127.0.0.1', port))
    print('Metrics : http://%s:%d/metrics' % (a.host if a.host != '0.0.0.0' else '127.0.0.1', port))
    print('Pack    : %s\nStore   : %s\nCtrl+C to stop.' % (os.environ['R3_PACK'], os.environ['R3_STORE']))
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
