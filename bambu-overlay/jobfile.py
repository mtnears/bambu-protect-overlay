"""
Read the current print job's file from the printer to find slicer pauses and
the slicer's time estimate.

The printer's MQTT report says when a pause *is happening*, but not which pauses
are coming. The job file says: Bambu Studio writes the printer's pause template
(`M400 U1`) at each layer where a pause was added, `M73 L<n>` at every layer
change, and `M73 P<pct> R<minutes remaining>` progress lines. So once per job we:

  1. download /cache/<job name>.gcode.3mf (or /<job name>.gcode.3mf) over the
     printer's implicit-TLS FTP (port 990, user bblp, the LAN access code),
  2. stream the plate's gcode (Metadata/plate_N.gcode, named in the MQTT report),
  3. record every pause with its layer, the slicer's minutes remaining at that
     point, and an optional note, plus the total estimated minutes.

Time to a pause is then "minutes remaining now" minus "minutes remaining at the
pause", which stays right when the print runs faster or slower than estimated.

Notes: a comment on the pause line (`M400 U1 ; Insert the magnets`) or a comment
line directly above it (`; Insert the magnets`) becomes the note. Use Bambu
Studio's "Add custom G-code" on a layer to write one; plain "Add pause" works
too, just without a note.
"""
from __future__ import annotations

import ftplib
import io
import logging
import re
import socket
import ssl
import zipfile

log = logging.getLogger("bambu-overlay")

PAUSE_RE = re.compile(r"^\s*(M400\s+U1|M601)\b(.*)$", re.IGNORECASE)
LAYER_RE = re.compile(r"^M73\s+L(\d+)")
PROGRESS_RE = re.compile(r"^M73\s+P(\d+)\s+R(\d+)")
# Slicer-generated comment markers that are not user notes
MARKER_RE = re.compile(r"^;\s*[A-Z0-9_ ]+(:|$)")


class _ImplicitFTPS(ftplib.FTP_TLS):
    """Bambu printers speak FTPS with TLS from the first byte (port 990)."""

    def connect(self, host="", port=990, timeout=20, source_address=None):
        self.host, self.port, self.timeout = host, port, timeout
        raw = socket.create_connection((host, port), timeout)
        self.sock = self.context.wrap_socket(raw, server_hostname=host)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def _note_from_comment(text: str) -> str | None:
    t = text.strip()
    if t.startswith(";"):
        t = t[1:].strip()
    return t or None


def parse_gcode(lines) -> dict:
    """Scan plate gcode lines; returns {total_min, layers, pauses: [{layer, remaining_at, note}]}."""
    total = None
    remaining = None
    layer = 0
    layers = 0
    prev = ""
    pauses = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        m = LAYER_RE.match(line)
        if m:
            layer = int(m.group(1))
            layers = max(layers, layer)
        else:
            m = PROGRESS_RE.match(line)
            if m:
                remaining = int(m.group(2))
                if total is None:
                    total = remaining
            elif layer >= 1:                      # ignore start gcode
                m = PAUSE_RE.match(line)
                if m:
                    note = None
                    rest = m.group(2)
                    if ";" in rest:
                        note = _note_from_comment(rest[rest.index(";"):])
                    elif prev.lstrip().startswith(";") and not MARKER_RE.match(prev.strip()):
                        note = _note_from_comment(prev)
                    pauses.append({"layer": layer, "remaining_at": remaining, "note": note})
        if line.strip():
            prev = line
    return {"total_min": total, "layers": layers, "pauses": pauses}


def fetch_job(host: str, user: str, password: str, job_name: str, gcode_path: str | None,
              timeout: int = 30) -> dict | None:
    """Download the job's .gcode.3mf from the printer and parse its plate gcode."""
    if not job_name:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # printers use self-signed certificates
    ftp = _ImplicitFTPS(context=ctx)
    ftp.connect(host, 990, timeout)
    try:
        ftp.login(user, password)
        ftp.prot_p()
        data = None
        for path in (f"/cache/{job_name}.gcode.3mf", f"/{job_name}.gcode.3mf"):
            buf = io.BytesIO()
            try:
                ftp.retrbinary(f"RETR {path}", buf.write)
                data = buf
                break
            except ftplib.error_perm:
                continue
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
    if data is None:
        log.info("job file for %r not found on %s", job_name, host)
        return None
    z = zipfile.ZipFile(data)
    name = None
    if gcode_path:
        cand = gcode_path.lstrip("/")
        cand = cand[len("data/"):] if cand.startswith("data/") else cand
        if cand in z.namelist():
            name = cand
    if name is None:
        gc = [n for n in z.namelist() if n.startswith("Metadata/") and n.endswith(".gcode")]
        if len(gc) != 1:
            log.info("job %r: can't tell which plate gcode to read (%s)", job_name, gc[:4])
            return None
        name = gc[0]
    with z.open(name) as fh:
        result = parse_gcode(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"))
    result["plate"] = name
    return result
