"""scripts/check-deployment.sh: the key it sends goes to curl in a header file, never on a command line (which every
account on the machine can read in /proc), curl reads no ~/.curlrc, and no proxy of the environment gets the requests.
The programs it runs are stand-ins in a directory first on PATH, sudo among them (the real one would run here without
a password); curl is the real one, behind a stand-in that records its arguments and the header file it was given."""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parent.parent
CURL = shutil.which("curl")
KEY = "k-not-on-a-command-line-0123456789"

CURL_STUB = '''import json, os, stat, sys
args = sys.argv[1:]
info = {"args": args}
for i, a in enumerate(args[:-1]):
    if a == "-H" and args[i + 1].startswith("@"):
        f = args[i + 1][1:]
        info["header"] = {"path": f, "mode": stat.S_IMODE(os.stat(f).st_mode),
                          "dir_mode": stat.S_IMODE(os.stat(os.path.dirname(f)).st_mode), "text": open(f).read()}
with open(os.environ["STUB_LOG"], "a") as out:
    out.write(json.dumps(info) + "\\n")
os.execv(os.environ["REAL_CURL"], [os.environ["REAL_CURL"]] + args)
'''


@unittest.skipUnless(CURL, "machine: no curl")
class TheKey(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.seen = seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                body = {"/health": {"status": "ok", "loaded": True, "model": "m", "max_context": 1},
                        "/v1/models": {"object": "list", "data": [{"id": "m"}]}}.get(self.path, {})
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)                   # runs first
        self.url = f"http://127.0.0.1:{srv.server_address[1]}"
        b = self.d / "bin"
        b.mkdir()
        stubs = {"curl": f"#!/bin/sh\nexec {sys.executable} -I {self.d / 'curl_stub.py'} \"$@\"\n",
                 "systemctl": '#!/bin/sh\ncase "$1" in is-active) echo active ;; is-enabled) echo enabled ;; esac\n',
                 "nvidia-smi": "#!/bin/sh\necho '0, 1 MiB, 16384 MiB, 0 %, 30, 1000 MHz, 30 W'\n",
                 # never the real one, which runs here without a password: this one notes its arguments and runs
                 # nothing, as a sudo that wants a password and is not to wait for one (-n)
                 "sudo": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$STUB_LOG.sudo"\nexit 1\n',
                 "pgrep": "#!/bin/sh\nexit 1\n",
                 "jq": '#!/bin/sh\ncat >> "$STUB_LOG.jq"; echo >> "$STUB_LOG.jq"\n'}
        (self.d / "curl_stub.py").write_text(CURL_STUB)
        for name, text in stubs.items():
            (b / name).write_text(text)
            (b / name).chmod(0o755)
        self.home = self.d / "home"                     # its ~/.curlrc would send every answer to /dev/null
        self.home.mkdir()
        (self.home / ".curlrc").write_text("output = /dev/null\n")
        self.log = self.d / "calls.jsonl"

    def run_check(self, url: str | None = None, **env: str) -> subprocess.CompletedProcess:
        e = {"PATH": f"{self.d / 'bin'}:/usr/bin:/bin", "HOME": str(self.home), "STUB_LOG": str(self.log),
             "REAL_CURL": CURL, "STRATA_LOG": str(self.d / "no.log"), **env}
        return subprocess.run(["bash", str(ROOT / "scripts/check-deployment.sh"), url or self.url], env=e,
                              capture_output=True, text=True, timeout=120)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_the_key_goes_in_a_header_file_never_on_a_command_line(self):
        r = self.run_check(STRATA_KEY=KEY)
        calls = self.calls()
        self.assertEqual(len(calls), 4, r.stdout + r.stderr)          # /health, /v1/models twice, /v1/status
        for c in calls:
            self.assertEqual(c["args"][0], "-q", c["args"])          # first, or curl reads ~/.curlrc
            self.assertFalse(any(KEY in a for a in c["args"]), "the key is on curl's command line")
            h = c["header"]
            self.assertEqual((h["mode"], h["dir_mode"]), (0o600, 0o700), h)
            self.assertEqual(h["text"], f"Authorization: Bearer {KEY}\n")
            self.assertFalse(os.path.exists(h["path"]) or os.path.exists(os.path.dirname(h["path"])),
                             "the header file was left behind")
        self.assertEqual(sorted(p for p, _ in self.seen), ["/health", "/v1/models", "/v1/models", "/v1/status"])
        self.assertEqual({a for _, a in self.seen}, {f"Bearer {KEY}"})
        self.assertIn('"loaded": true', (self.d / "calls.jsonl.jq").read_text())   # not sent to ~/.curlrc's /dev/null

    def test_no_key_no_header(self):
        r = self.run_check()
        self.assertEqual(len(self.calls()), 4, r.stdout + r.stderr)
        for c in self.calls():
            self.assertNotIn("-H", c["args"])
        self.assertEqual({a for _, a in self.seen}, {None})

    def test_a_key_with_a_control_character_is_not_sent(self):
        # it is a problem, so the check ends with exit 1, whatever else it finds
        for ch in ("\n", "\r", "\t", "\x1b", "\x7f"):
            with self.subTest(ch=repr(ch)):
                self.seen.clear()
                self.log.unlink(missing_ok=True)
                r = self.run_check(STRATA_KEY=f"k-one{ch}k-two")
                self.assertIn("PROBLEM: STRATA_KEY holds a control character: it is not sent", r.stdout)
                self.assertIn("\n  STRATA_KEY holds a control character: it is not sent\n",
                              r.stdout.partition(" problem(s):")[2], r.stdout)
                self.assertEqual(r.returncode, 1, r.stdout)
                for c in self.calls():
                    self.assertNotIn("-H", c["args"])
                self.assertEqual({a for _, a in self.seen}, {None})

    def sudo_calls(self) -> list[str]:
        f = self.d / "calls.jsonl.sudo"
        return f.read_text().splitlines() if f.exists() else []

    def events(self, r: subprocess.CompletedProcess) -> list[str]:
        """The lines of the section of health checks and service events"""
        return r.stdout.partition("== health checks and service events")[2].partition("\n\n")[0].splitlines()[1:]

    def test_the_events_are_not_crowded_out_by_routine_health_checks(self):
        # systemd writes a Starting and a Finished line for every health check, every 2 minutes; in the last 12 lines
        # they left no room for the events (a start, the preflight, wait-ready, a finding of the watchdog, a
        # failure).  sudo here is a stand-in that prints a journal of its own and runs nothing.  Without -n first, a
        # sudo that wants a password would wait for one: the stand-in refuses such a call
        journal = self.d / "journal.txt"
        kinds = ["host strata-preflight.sh[{}]: strata-preflight: ok: 4 GPUs, 58 GiB usable",
                 "host strata-wait-ready.sh[{}]: strata-wait-ready: loaded after 544 s",
                 "host systemd[1]: Started Strata Qwen3.8-Flash-Next GSQ-RCO on 4x P100. ({})",
                 "host strata-health[{}]: strata-health [warning]: no engine process under server.py (check 1/8)",
                 "host systemd[1]: strata-q3.service: Failed with result 'exit-code'. ({})"]
        events = [kinds[i % len(kinds)].format(i) for i in range(14)]
        routine = ["host systemd[1]: Starting Strata health check (see scripts/strata-health.sh)...",
                   "host systemd[1]: Finished Strata health check (see scripts/strata-health.sh).",
                   "host systemd[1]: strata-q3-health.service: Deactivated successfully."]
        journal.write_text("".join("\n".join(routine + [e]) + "\n" for e in events) + "\n".join(routine * 20) + "\n")
        sudo = self.d / "bin" / "sudo"
        sudo.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$STUB_LOG.sudo"\n[ "$1" = -n ] || exit 1\n'
                        'case "$2" in true) exit 0 ;; journalctl) cat "$JOURNAL" ;; *) exit 1 ;; esac\n')
        r = self.run_check(JOURNAL=str(journal))
        self.assertEqual(self.events(r), events[-12:], r.stdout)              # the last 12, in their order
        self.assertEqual(self.sudo_calls(), ["-n true", "-n journalctl -u strata-q3.service -u strata-q3-health.service "
                                                        "--since -2h --no-pager"])
        journal.write_text("\n".join(routine * 20) + "\n")
        r = self.run_check(JOURNAL=str(journal))
        self.assertEqual(self.events(r), ["(no health-check or service events in the last 2 h)"], r.stdout)

    def test_without_a_password_for_sudo_the_journal_is_left_out(self):
        # the stand-in of setUp refuses every call, as a sudo that wants a password does with -n
        r = self.run_check()
        self.assertEqual(self.events(r), ["(sudo asks for a password: run 'sudo -v' first to include the system "
                                          "journal)"], r.stdout)
        self.assertEqual(self.sudo_calls(), ["-n true"])

    def test_no_proxy_of_the_environment_gets_the_key(self):
        # curl takes a proxy for http:// from http_proxy, all_proxy or ALL_PROXY (not HTTP_PROXY), also for
        # 127.0.0.1, and -q does not stop that: without --noproxy '*' each request, with the key, went to the proxy
        proxied = []

        class P(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                proxied.append((self.path, self.headers.get("Authorization")))
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        proxy = http.server.HTTPServer(("127.0.0.1", 0), P)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        # also for the name localhost, which a --noproxy that names only 127.0.0.1 would proxy
        for var, url in [(v, self.url) for v in ("http_proxy", "all_proxy", "ALL_PROXY", "HTTP_PROXY")] + \
                [("http_proxy", self.url.replace("127.0.0.1", "localhost"))]:
            with self.subTest(var=var, url=url):
                self.seen.clear()
                proxied.clear()
                self.run_check(url, STRATA_KEY=KEY, **{var: f"http://127.0.0.1:{proxy.server_address[1]}"})
                self.assertEqual(proxied, [], f"requests went to the proxy of {var}")
                self.assertEqual(sorted(p for p, _ in self.seen),
                                 ["/health", "/v1/models", "/v1/models", "/v1/status"])
                self.assertEqual({a for _, a in self.seen}, {f"Bearer {KEY}"})


if __name__ == "__main__":
    unittest.main()
