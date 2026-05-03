#!/usr/bin/env python3
# Daily Ops Runner — Flask backend (Python 3.6 compatible, Flask 2.0.3)

import os
import subprocess
import tempfile

from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_path(path):
    """Strip whitespace; return None if empty."""
    p = (path or "").strip()
    return p if p else None


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Feature 1: Yarn Log Downloader ────────────────────────────────────────────

@app.route("/yarn-log", methods=["POST"])
def yarn_log():
    app_id   = (request.form.get("app_id") or "").strip()
    log_type = request.form.get("log_type", "full")   # "full" | "error"

    if not app_id:
        return render_template("index.html",
                               yarn_error="Application ID is required.")

    # Build command — shell=False, list form
    cmd = ["yarn", "logs", "-applicationId", app_id]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=120,
        )
    except FileNotFoundError:
        return render_template("index.html",
                               yarn_error="'yarn' command not found on this host.")
    except subprocess.TimeoutExpired:
        return render_template("index.html",
                               yarn_error="Yarn log fetch timed out after 120 s.")

    raw_output = result.stdout.decode("utf-8", errors="replace")

    if log_type == "error":
        # Keep only lines containing "error" (case-insensitive)
        lines = [l for l in raw_output.splitlines() if "error" in l.lower()]
        content = "\n".join(lines) if lines else "(no error lines found)\n"
    else:
        content = raw_output

    # Write to temp file and stream back as download
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".log",
        prefix=f"yarn_{app_id}_",
        delete=False,
        encoding="utf-8",
    )
    tmp.write(content)
    tmp.flush()
    tmp.close()

    download_name = f"yarn_{app_id}_{log_type}.log"
    return send_file(
        tmp.name,
        as_attachment=True,
        download_name=download_name,
        mimetype="text/plain",
    )


# ── Feature 2: File & Directory Listing ───────────────────────────────────────

@app.route("/list-dir", methods=["POST"])
def list_dir():
    raw_path = request.form.get("dir_path", "")
    dir_path = _safe_path(raw_path)

    if not dir_path:
        return render_template("index.html",
                               listing_error="Directory path is required.")

    if not os.path.exists(dir_path):
        return render_template("index.html",
                               listing_error=f"Path not found: {dir_path}")

    if not os.path.isdir(dir_path):
        return render_template("index.html",
                               listing_error=f"Not a directory: {dir_path}")

    entries = []
    try:
        for name in sorted(os.listdir(dir_path)):
            full = os.path.join(dir_path, name)
            try:
                stat   = os.stat(full)
                size   = stat.st_size
                ftype  = "DIR" if os.path.isdir(full) else "FILE"
            except OSError:
                size  = -1
                ftype = "?"
            entries.append({"type": ftype, "name": name, "size": size})
    except PermissionError:
        return render_template("index.html",
                               listing_error=f"Permission denied: {dir_path}")

    return render_template("index.html",
                           listing_path=dir_path,
                           listing_entries=entries)


# ── Feature 3: Script Trigger ─────────────────────────────────────────────────

@app.route("/run-script", methods=["POST"])
def run_script():
    script_path = _safe_path(request.form.get("script_path", ""))
    params_raw  = (request.form.get("params") or "").strip()

    if not script_path:
        return render_template("index.html",
                               script_error="Script path is required.")

    if not os.path.exists(script_path):
        return render_template("index.html",
                               script_error=f"Script not found: {script_path}")

    if not os.path.isfile(script_path):
        return render_template("index.html",
                               script_error=f"Not a file: {script_path}")

    # Build arg list — shell=False enforced
    params = params_raw.split() if params_raw else []
    cmd    = [script_path] + params

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        stdout_bytes, stderr_bytes = proc.communicate(timeout=300)
    except PermissionError:
        return render_template("index.html",
                               script_error=f"Permission denied executing: {script_path}")
    except subprocess.TimeoutExpired:
        proc.kill()
        return render_template("index.html",
                               script_error="Script timed out after 300 s.")
    except Exception as exc:
        return render_template("index.html",
                               script_error=f"Execution failed: {exc}")

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    exit_code   = proc.returncode

    return render_template(
        "index.html",
        script_stdout=stdout_text,
        script_stderr=stderr_text,
        script_exit=exit_code,
        script_path=script_path,
    )


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
