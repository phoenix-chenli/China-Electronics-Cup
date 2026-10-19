"""Launcher-owned Uvicorn runner with graceful, token-bound local shutdown.

No control HTTP route is exposed. The stop token is local runtime metadata, not
a model credential. An old stop file cannot stop a new server instance.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import threading


def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--host', choices=['127.0.0.1'], default='127.0.0.1')
    args = parser.parse_args()
    root = Path(os.environ['ZHIHENG_DATA_DIR'])
    token = os.environ['ZHIHENG_SERVER_TOKEN']
    request = root / 'server-stop.json'
    done = threading.Event()
    server = uvicorn.Server(uvicorn.Config('backend.main:app', host=args.host, port=args.port))

    def watch():
        while not done.wait(.2):
            try:
                record = json.loads(request.read_text(encoding='utf-8'))
                if record.get('token') == token and record.get('pid') == os.getpid():
                    server.should_exit = True
                    return
            except (OSError, ValueError, AttributeError):
                pass
    thread = threading.Thread(target=watch, name='local-stop', daemon=True)
    thread.start()
    try:
        server.run()
    finally:
        done.set()
        thread.join(timeout=1)


if __name__ == '__main__':
    main()
