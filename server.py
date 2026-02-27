"""
Simple HTTP trigger for manual report runs.

Open the Railway public URL in a browser, pick a date, click Run.
"""

import html
import os
import subprocess
import sys
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Daily Report</title>
  <style>
    body {{ font-family: sans-serif; max-width: 680px; margin: 60px auto; padding: 0 20px; color: #222; }}
    h2 {{ margin-bottom: 24px; }}
    label {{ font-size: 15px; }}
    input[type=text] {{ font-size: 15px; padding: 7px 10px; border: 1px solid #ccc; border-radius: 4px; width: 140px; }}
    button {{ font-size: 15px; padding: 8px 20px; background: #2563eb; color: #fff;
              border: none; border-radius: 4px; cursor: pointer; margin-left: 10px; }}
    button:hover {{ background: #1d4ed8; }}
    pre {{ background: #f3f4f6; padding: 16px; border-radius: 6px;
           overflow-x: auto; white-space: pre-wrap; font-size: 13px; line-height: 1.5; }}
    .ok {{ border-left: 4px solid #16a34a; }}
    .err {{ border-left: 4px solid #dc2626; }}
    .hint {{ color: #666; font-size: 13px; margin-top: 8px; }}
  </style>
</head>
<body>
  <h2>Daily Report — ручной запуск</h2>
  <form method="POST" action="/run">
    <label>
      Дата (дд.мм.гггг):
      <input type="text" name="date" value="{run_date}" placeholder="26.02.2026">
    </label>
    <button type="submit">&#9654; Запустить</button>
  </form>
  <p class="hint">По умолчанию подставляется вчерашняя дата. Нажмите «Запустить» — ждите, пока страница обновится (10–60 сек).</p>
  {output}
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        yesterday = (date.today() - timedelta(days=1)).strftime("%d.%m.%Y")
        self._write(PAGE.format(run_date=yesterday, output=""))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        params = parse_qs(raw)
        run_date = params.get("date", [""])[0].strip()

        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
        cmd = [sys.executable, script]
        if run_date:
            cmd += ["--date", run_date]

        result = subprocess.run(cmd, capture_output=True, text=True)
        combined = (result.stdout + result.stderr).strip() or "(нет вывода)"
        css = "ok" if result.returncode == 0 else "err"
        output = f'<pre class="{css}">{html.escape(combined)}</pre>'

        yesterday = (date.today() - timedelta(days=1)).strftime("%d.%m.%Y")
        self._write(PAGE.format(run_date=run_date or yesterday, output=output))

    def _write(self, body: str):
        enc = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(enc)))
        self.end_headers()
        self.wfile.write(enc)

    def log_message(self, fmt, *args):
        pass  # silence default request logs


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    srv = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Server running on http://0.0.0.0:{port}", flush=True)
    srv.serve_forever()
