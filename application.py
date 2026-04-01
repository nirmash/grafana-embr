"""Reverse proxy for Grafana — serves on port 8080, forwards to Grafana on 3000."""

import subprocess
import threading
import time
import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

GRAFANA_PORT = 3000
LISTEN_PORT = int(os.environ.get("PORT", 8080))
GRAFANA_URL = f"http://127.0.0.1:{GRAFANA_PORT}"
grafana_ready = False


def write_datasource_config():
    """Write Prometheus datasource provisioning config."""
    prom_url = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
    prov_dir = "/tmp/grafana/conf/provisioning/datasources"
    os.makedirs(prov_dir, exist_ok=True)
    config = {
        "apiVersion": 1,
        "datasources": [{
            "name": "Prometheus",
            "type": "prometheus",
            "access": "proxy",
            "url": prom_url,
            "isDefault": True,
            "editable": True,
        }],
    }
    with open(f"{prov_dir}/prometheus.yaml", "w") as f:
        f.write(f"""apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: {prom_url}
    isDefault: true
    editable: true
""")
    print(f"Datasource configured: {prom_url}", flush=True)


def start_grafana():
    global grafana_ready
    version = os.environ.get("GRAFANA_VERSION", "11.6.0")
    tarball = f"grafana-v{version}"
    url = f"https://dl.grafana.com/oss/release/grafana-{version}.linux-amd64.tar.gz"

    dest = f"/tmp/{tarball}.tar.gz"
    print(f"Downloading Grafana v{version}...", flush=True)
    subprocess.run(
        ["curl", "-fSL", "--retry", "3", "--connect-timeout", "30",
         "-o", dest, url],
        check=True,
    )
    print(f"Download complete, extracting...", flush=True)
    import tarfile
    with tarfile.open(dest, mode="r:gz") as tar:
        tar.extractall(path="/tmp")
    os.remove(dest)
    print("Extraction complete", flush=True)

    write_datasource_config()

    # Create data directories
    os.makedirs("/tmp/grafana-data", exist_ok=True)
    os.makedirs("/tmp/grafana-logs", exist_ok=True)

    env = os.environ.copy()
    env.update({
        "GF_SERVER_HTTP_PORT": str(GRAFANA_PORT),
        "GF_PATHS_DATA": "/tmp/grafana-data",
        "GF_PATHS_LOGS": "/tmp/grafana-logs",
        "GF_PATHS_PROVISIONING": "/tmp/grafana/conf/provisioning",
        "GF_AUTH_ANONYMOUS_ENABLED": "true",
        "GF_AUTH_ANONYMOUS_ORG_ROLE": "Admin",
        "GF_SECURITY_ADMIN_PASSWORD": "admin",
        "GF_SERVER_ROOT_URL": "/",
    })

    print(f"Starting Grafana on :{GRAFANA_PORT}...", flush=True)
    subprocess.Popen(
        [f"/tmp/{tarball}/bin/grafana", "server",
         f"--homepath=/tmp/{tarball}",
         f"--config=/tmp/{tarball}/conf/defaults.ini"],
        env=env,
    )

    for _ in range(60):
        try:
            urlopen(f"{GRAFANA_URL}/api/health")
            grafana_ready = True
            print("Grafana ready!", flush=True)
            return
        except Exception:
            time.sleep(1)
    print("Warning: Grafana may not be ready", flush=True)


class ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not grafana_ready and self.path in ("/", "/health", "/api/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Starting...")
            return
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_PATCH(self):
        self._proxy("PATCH")

    def _proxy(self, method):
        if not grafana_ready:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Grafana starting...")
            return

        target = f"{GRAFANA_URL}{self.path}"
        body = None
        if method in ("POST", "PUT", "PATCH"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else None

        headers = {}
        for key in ("Content-Type", "Accept", "Accept-Encoding",
                     "Authorization", "Cookie", "X-Grafana-Org-Id"):
            val = self.headers.get(key)
            if val:
                headers[key] = val

        try:
            req = Request(target, data=body, headers=headers, method=method)
            with urlopen(req) as resp:
                self._send_proxy_response(resp)
        except HTTPError as e:
            self._send_proxy_response(e)
        except URLError as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Grafana unavailable: {e}".encode())

    def _send_proxy_response(self, resp):
        resp_body = resp.read()
        status = resp.status if hasattr(resp, "status") else resp.code
        self.send_response(status)
        for key, val in resp.getheaders():
            if key.lower() not in ("transfer-encoding", "connection",
                                   "content-encoding", "content-length"):
                self.send_header(key, val)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=start_grafana, daemon=True).start()
    print(f"Proxy listening on :{LISTEN_PORT}", flush=True)
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    server.serve_forever()
