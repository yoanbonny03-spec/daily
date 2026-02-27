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
    body {{ font-family: sans-serif; max-width: 760px; margin: 50px auto; padding: 0 20px; color: #222; }}
    h2 {{ margin-bottom: 20px; }}
    .row {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }}
    label {{ font-size: 15px; }}
    input[type=text] {{ font-size: 15px; padding: 7px 10px; border: 1px solid #ccc;
                        border-radius: 4px; width: 140px; }}
    button {{ font-size: 15px; padding: 8px 18px; color: #fff; border: none;
              border-radius: 4px; cursor: pointer; }}
    .btn-run  {{ background: #2563eb; }}
    .btn-run:hover  {{ background: #1d4ed8; }}
    .btn-fields {{ background: #059669; }}
    .btn-fields:hover {{ background: #047857; }}
    pre {{ background: #f3f4f6; padding: 16px; border-radius: 6px;
           overflow-x: auto; white-space: pre-wrap; font-size: 13px; line-height: 1.5; }}
    .ok  {{ border-left: 4px solid #16a34a; }}
    .err {{ border-left: 4px solid #dc2626; }}
    .hint {{ color: #666; font-size: 13px; margin-top: 6px; }}
  </style>
</head>
<body>
  <h2>Daily Report — ручной запуск</h2>

  <form method="POST" action="/run">
    <div class="row">
      <label>Дата (дд.мм.гггг):
        <input type="text" name="date" value="{run_date}" placeholder="26.02.2026">
      </label>
      <button class="btn-run" type="submit">&#9654; Запустить</button>
    </div>
  </form>

  <form method="POST" action="/fields" style="margin-top:8px">
    <button class="btn-fields" type="submit">&#128270; Показать поля и воронки AmoCRM</button>
  </form>

  <p class="hint">
    Страница обновится сама когда скрипт завершится (обычно 10–60 сек).<br>
    Если зависло больше 2 минут — смотри логи Railway.
  </p>

  {output}
</body>
</html>"""


def _run_script(args: list[str]) -> tuple[str, int]:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable] + args
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=script_dir)
    combined = (result.stdout + result.stderr).strip() or "(нет вывода)"
    return combined, result.returncode


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        yesterday = (date.today() - timedelta(days=1)).strftime("%d.%m.%Y")
        self._write(PAGE.format(run_date=yesterday, output=""))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        params = parse_qs(raw)

        path = self.path.split("?")[0]

        if path == "/fields":
            output_text, rc = _run_script(["helper_find_field_ids.py"])
        else:  # /run
            run_date = params.get("date", [""])[0].strip()
            script_args = ["main.py"]
            if run_date:
                script_args += ["--date", run_date]
            output_text, rc = _run_script(script_args)

        css = "ok" if rc == 0 else "err"
        output_block = f'<pre class="{css}">{html.escape(output_text)}</pre>'

        run_date = params.get("date", [""])[0].strip()
        yesterday = (date.today() - timedelta(days=1)).strftime("%d.%m.%Y")
        self._write(PAGE.format(run_date=run_date or yesterday, output=output_block))

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
