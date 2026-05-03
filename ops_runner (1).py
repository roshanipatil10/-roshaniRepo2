#!/usr/bin/env python3
# Daily Ops Runner — Flask backend (Python 3.6 compatible, Flask 2.0.3)

import os
import stat
import pwd
import grp
import subprocess
import tempfile
import datetime

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

def _permissions(st_mode):
    """Convert st_mode integer to rwx string like ls -l (e.g. drwxr-xr-x)."""
    is_dir  = 'd' if stat.S_ISDIR(st_mode)  else \
              'l' if stat.S_ISLNK(st_mode)  else '-'
    bits = [
        ('r', stat.S_IRUSR), ('w', stat.S_IWUSR), ('x', stat.S_IXUSR),
        ('r', stat.S_IRGRP), ('w', stat.S_IWGRP), ('x', stat.S_IXGRP),
        ('r', stat.S_IROTH), ('w', stat.S_IWOTH), ('x', stat.S_IXOTH),
    ]
    perm = ''.join(c if st_mode & m else '-' for c, m in bits)
    return is_dir + perm


def _ls_l_line(name, full_path):
    """Build a single ls -l formatted line for one directory entry."""
    try:
        st = os.lstat(full_path)           # lstat so symlinks show as 'l'
    except OSError as e:
        return f"??????????  ?  ?  ?  ?  ?  {name}  ({e})"

    perms   = _permissions(st.st_mode)
    nlinks  = st.st_nlink

    # Owner / group names — fall back to numeric ID on lookup failure
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)

    size  = st.st_size
    mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%b %d %H:%M")

    # Append '/' suffix for dirs, '@' for symlinks — cosmetic only
    display_name = name
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(full_path)
            display_name = f"{name} -> {target}"
        except OSError:
            display_name = name + "@"
    elif stat.S_ISDIR(st.st_mode):
        display_name = name + "/"

    return f"{perms}  {nlinks:>3}  {owner:<10}  {group:<10}  {size:>10}  {mtime}  {display_name}"


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

    try:
        names = sorted(os.listdir(dir_path))
    except PermissionError:
        return render_template("index.html",
                               listing_error=f"Permission denied: {dir_path}")

    # Build total blocks line (like ls -l header) and each entry line
    total_blocks = 0
    lines = []
    for name in names:
        full = os.path.join(dir_path, name)
        try:
            total_blocks += os.lstat(full).st_blocks
        except OSError:
            pass
        lines.append(_ls_l_line(name, full))

    header   = f"total {total_blocks // 2}"   # st_blocks is 512-byte; ls shows 1K
    ls_output = header + "\n" + "\n".join(lines)

    return render_template("index.html",
                           listing_path=dir_path,
                           listing_output=ls_output)


# ── Feature 3: Script Trigger ─────────────────────────────────────────────────

# Extension → fallback interpreter when no shebang is present
_EXT_INTERPRETER = {
    ".sh":  "/bin/bash",
    ".py":  "/usr/bin/python3",
    ".pl":  "/usr/bin/perl",
    ".rb":  "/usr/bin/ruby",
    ".ksh": "/bin/ksh",
    ".csh": "/bin/csh",
}


def _resolve_interpreter(script_path):
    """
    Return the interpreter to use for script_path.

    Strategy (handles errno 8 / ENOEXEC):
    1. Read first line; if it starts with '#!' strip it and use that interpreter.
    2. Otherwise fall back to extension map.
    3. If still nothing, return None (caller will attempt direct exec and
       surface a clear error if it fails).
    """
    try:
        with open(script_path, "rb") as fh:
            first = fh.readline(256)          # read at most 256 bytes
        # Strip Windows CR so '#!...\r' doesn't break the path lookup
        first = first.rstrip(b"\r\n")
        if first.startswith(b"#!"):
            shebang = first[2:].decode("utf-8", errors="replace").strip()
            # shebang may be '/usr/bin/env python3' or '/bin/bash'
            parts = shebang.split()
            if parts:
                return parts   # e.g. ['/usr/bin/env', 'python3']
    except OSError:
        pass

    # No shebang — try extension
    _, ext = os.path.splitext(script_path)
    interp = _EXT_INTERPRETER.get(ext.lower())
    return [interp] if interp else None


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

    params = params_raw.split() if params_raw else []

    # Resolve interpreter — avoids errno 8 (ENOEXEC) for scripts without
    # execute bit or missing shebang. Always run as:
    #   [interpreter, script_path, ...params]   shell=False
    interp = _resolve_interpreter(script_path)
    if interp:
        cmd = interp + [script_path] + params
    else:
        # No shebang, no known extension — attempt direct exec as last resort
        cmd = [script_path] + params

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
                               script_error=f"Permission denied reading: {script_path}")
    except subprocess.TimeoutExpired:
        proc.kill()
        return render_template("index.html",
                               script_error="Script timed out after 300 s.")
    except OSError as exc:
        # Surface a clear message instead of raw errno 8
        return render_template("index.html",
                               script_error=(
                                   f"OS error running script: {exc}\n\n"
                                   "Tip: make sure the interpreter exists and "
                                   "the script has a valid shebang (#!/bin/bash etc.)."
                               ))
    except Exception as exc:
        return render_template("index.html",
                               script_error=f"Execution failed: {exc}")

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    exit_code   = proc.returncode

    # Show which interpreter was used — helpful for debugging
    interp_used = " ".join(cmd[:len(interp)]) if interp else "(direct exec)"

    return render_template(
        "index.html",
        script_stdout=stdout_text,
        script_stderr=stderr_text,
        script_exit=exit_code,
        script_path=script_path,
        script_interp=interp_used,
    )


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
