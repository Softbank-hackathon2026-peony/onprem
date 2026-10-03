"""Host-side publisher for one Minikube demo app. Requires kubectl and ngrok."""
import atexit
import hmac
import json
import os
import subprocess
import time
import signal
import socket
import tempfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import urlopen

TOKEN = os.getenv('ONPREM_TUNNEL_TOKEN', '')
CONTEXT = os.getenv('MINIKUBE_PROFILE', 'pawploy')
processes = []
active = None


def cleanup():
    global active
    for process in reversed(processes):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    processes.clear()
    active = None


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global active
        if not hmac.compare_digest(self.headers.get('X-Tunnel-Token', ''), TOKEN):
            self.send_error(403)
            return
        if self.path not in {'/publish', '/close'}:
            self.send_error(404)
            return
        owns_processes = False
        try:
            job = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            name, namespace = job['service_name'], job['namespace']
            import re
            if not all(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', value) for value in (name, namespace)):
                raise ValueError('Invalid service name')
            identity = (namespace, name)
            if self.path == '/close':
                if active and active[:2] == identity:
                    cleanup()
                result = {'closed': True}
            elif self.path == '/publish':
                if active and active[:2] != identity:
                    self.send_error(409, 'Another demo app is published; stop it first')
                    return
                if active and any(p.poll() is not None for p in processes):
                    cleanup()
                if not active:
                    owns_processes = True
                    port = int(job['port'])
                    if not 1 <= port <= 65535:
                        raise ValueError('Invalid port')
                    forward = subprocess.Popen(['kubectl', '--context', CONTEXT, '-n', namespace,
                        'port-forward', f'svc/{name}', f'18080:{port}', '--address=127.0.0.1'])
                    processes.append(forward)
                    for _ in range(60):
                        try:
                            with socket.create_connection(('127.0.0.1', 18080), timeout=2):
                                break
                        except Exception:
                            if forward.poll() is not None:
                                raise RuntimeError('port-forward exited')
                            time.sleep(1)
                    else:
                        raise RuntimeError('Local app did not respond')
                    config_paths = [server.ngrok_config]
                    if not os.getenv('NGROK_AUTHTOKEN'):
                        candidates = [os.getenv('NGROK_CONFIG'), str(Path.home() / 'Library/Application Support/ngrok/ngrok.yml'),
                            str(Path.home() / '.config/ngrok/ngrok.yml'), str(Path.home() / '.ngrok2/ngrok.yml')]
                        native_config = next((p for p in candidates if p and Path(p).is_file()), None)
                        if not native_config:
                            raise RuntimeError('Configure ngrok authtoken on the host first')
                        config_paths.insert(0, native_config)
                    processes.append(subprocess.Popen(['ngrok', 'http', 'http://127.0.0.1:18080', '--config', ','.join(config_paths)]))
                    for _ in range(30):
                        try:
                            with urlopen('http://127.0.0.1:4041/api/endpoints', timeout=2) as response:
                                endpoints = json.load(response)['endpoints']
                            url = next(e['url'] for e in endpoints if e['url'].startswith('https://'))
                            active = (*identity, url)
                            break
                        except Exception:
                            if processes[-1].poll() is not None:
                                raise RuntimeError('ngrok exited; check authentication')
                            time.sleep(1)
                    else:
                        raise RuntimeError('ngrok URL unavailable')
                result = {'url': active[2]}
            else:
                self.send_error(404)
                return
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            if owns_processes:
                cleanup()
            self.send_error(502, str(exc))


if __name__ == '__main__':
    if not TOKEN:
        raise SystemExit('ONPREM_TUNNEL_TOKEN is required')
    def stop_server(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop_server)
    atexit.register(cleanup)
    server = HTTPServer((os.getenv('ONPREM_TUNNEL_BIND', '0.0.0.0'), 8787), Handler)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as config_file:
        config_file.write('version: 3\nagent:\n  web_addr: 127.0.0.1:4041\n')
        server.ngrok_config = config_file.name
    try:
        server.serve_forever()
    finally:
        cleanup()
        server.server_close()
        os.unlink(server.ngrok_config)
