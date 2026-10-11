#!/usr/bin/env python3
"""loop.py — supervisor for the ONE long-running omp session.

The IRC daemon spools traffic to $IRC_SPOOL_DIR/messages.jsonl. This loop:

  1. starts omp once, as a persistent session (not one process per message):
     stdin = our feed of mentions and task chunks, stdout = the agent's replies;
  2. FAST lane: new trusted mentions (host-glob trust is applied upstream by
     the daemon) are forwarded into the session's stdin, tagged with sender and
     channel; each stdout reply is sent back via irc_say over the daemon's
     unix socket;
  3. LONG lane: $TASKS_DIR/queue.jsonl is durable state; when the spool is
     idle the next pending task is forwarded the same way, and the agent
     updates its own checkpoint in the file (survives restarts).

A reader thread relays every omp stdout line to the current reply target; a
reply is considered finished after REPLY_IDLE seconds of stdout quiescence.
If the omp process dies it is restarted; all durable state stays under /state.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time

START_MARKER = "\x02LOOP-BEGIN\x02"
END_MARKER = "\x02LOOP-END\x02"


def log(msg):
    print(f"[loop] {msg}", flush=True)


class Irc:
    """Minimal MCP client for the daemon's unix socket (irc_say only)."""

    def __init__(self, sock_path, default_target):
        self.sock_path = sock_path
        self.default_target = default_target
        self.mid = 0

    def say(self, text, target=None):
        target = target or self.default_target
        if not text.strip():
            return
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(120)
            s.connect(self.sock_path)
            self.mid += 1
            req = {"jsonrpc": "2.0", "id": self.mid, "method": "tools/call",
                   "params": {"name": "irc_say",
                              "arguments": {"target": target, "text": text}}}
            f = s.makefile("r", encoding="utf-8")
            s.sendall((json.dumps(req) + "\n").encode())
            deadline = time.time() + 110
            while time.time() < deadline:
                line = f.readline()
                if not line:
                    break
                try:
                    resp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if resp.get("id") == self.mid:
                    break
            s.close()
        except OSError as e:
            log(f"irc_say to {target} failed: {e}")


class Session:
    """The single persistent omp process; relays stdout to IRC."""

    def __init__(self, workdir, prompt, model, irc, reply_idle):
        self.workdir = workdir
        self.prompt = prompt
        self.model = model
        self.irc = irc
        self.reply_idle = reply_idle
        self.proc = None
        self.reply_target = irc.default_target
        self.lock = threading.Lock()

    def start(self):
        cmd = ["omp", "--mode", "text", "--system-prompt", self.prompt]
        if self.model:
            cmd += ["--model", self.model]
        self.proc = subprocess.Popen(
            cmd, cwd=self.workdir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self._relay, daemon=True).start()
        log(f"omp session started (pid {self.proc.pid})")

    def send(self, text, reply_target):
        with self.lock:
            self.reply_target = reply_target
            self.proc.stdin.write(text.rstrip() + "\n")
            self.proc.stdin.flush()

    def _relay(self):
        """Forward omp stdout lines to the current reply target until the
        output goes quiet for reply_idle seconds (one IRC reply)."""
        proc = self.proc
        while True:
            line = proc.stdout.readline()
            if not line:
                log("omp session exited; restarting in 5s")
                time.sleep(5)
                self.start()
                return
            text = line.strip()
            if not text or text.startswith((START_MARKER, END_MARKER)):
                continue
            target = self.reply_target
            self.irc.say(text, target)
            # wait out the rest of this reply
            deadline = time.time() + self.reply_idle
            while time.time() < deadline:
                if select_ready(proc.stdout, 0.5):
                    deadline = time.time() + self.reply_idle
                    nxt = proc.stdout.readline()
                    if not nxt:
                        break
                    t = nxt.strip()
                    if t and not t.startswith((START_MARKER, END_MARKER)):
                        self.irc.say(t, target)


def select_ready(stream, timeout):
    import select
    r, _, _ = select.select([stream], [], [], timeout)
    return bool(r)


def drain_mentions(spool_dir):
    """Byte-cursor over messages.jsonl; yields new trusted mention events."""
    path = os.path.join(spool_dir, "messages.jsonl")
    cur = os.path.join(spool_dir, "cursor")
    off = int(open(cur).read()) if os.path.exists(cur) else 0
    out = []
    if os.path.exists(path):
        size = os.path.getsize(path)
        if size < off:
            off = 0  # truncated/rotated
        with open(path) as f:
            f.seek(off)
            rest = f.read()
        with open(cur, "w") as f:
            f.write(str(size))
        for line in rest.splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
                            out.append(e)
    return out


def next_task(task_db):
    for line in open(task_db):
        line = line.strip()
        if not line:
            continue
        t = json.loads(line)
        if t.get("status") == "pending":
            return t
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--poll", type=float, default=15)
    ap.add_argument("--reply-idle", type=float, default=8)
    ap.add_argument("--model", default="")
    ap.add_argument("--spool", default=os.environ.get("IRC_SPOOL_DIR", "/state/irc"))
    ap.add_argument("--sock", default=os.path.join(
        os.environ.get("IRC_SPOOL_DIR", "/state/irc"), "mcp.sock"))
    args = ap.parse_args()

    default_target = next(iter(
        os.environ.get("IRC_CHANNELS", "").replace(",", " ").split()), None)
    if not default_target:
        sys.exit("[loop] IRC_CHANNELS must name at least one channel")

    irc = Irc(args.sock, default_target)
    session = Session(args.workdir, args.prompt, args.model, irc, args.reply_idle)
    session.start()

    while True:
        # ---- fast lane: trusted mentions
        mentions = drain_mentions(args.spool)
        if mentions:
            for e in mentions[:5]:
                where = e.get("chan") or f"dm:{e['from']}"
                target = e.get("chan") or e["from"]
                session.send(
                    f"{START_MARKER} IRC mention from {e['from']} in {where}: {e['text']} {END_MARKER}",
                    target)
                time.sleep(args.reply_idle)  # let this reply flush before the next
            continue  # re-check the spool immediately

        # ---- long lane: one pending durable task
        task = next_task(args.tasks)
        if task:
            cp = task.get("checkpoint", "")
            resume = (f"Resume task {task['id']} from its checkpoint:\n{cp}"
                      if cp else f"Start task {task['id']}.")
            log(f"task chunk: {task['id']}")
            session.send(
                f"{START_MARKER} Queued task {task['id']}: {task['prompt']} "
                f"{resume} Work on it now. Update the checkpoint in {args.tasks} "
                f"(set status \"done\" only when fully complete), report in the "
                f"channel with irc_say, then stop — the supervisor sends the "
                f"next chunk later. {END_MARKER}",
                default_target)
            time.sleep(args.reply_idle)
            continue

        time.sleep(args.poll)


if __name__ == "__main__":
    main()
