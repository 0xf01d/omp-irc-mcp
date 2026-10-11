#!/usr/bin/env python3
"""mcp_irc_server.py — a tiny stdio MCP server exposing IRC to oh-my-pi.

stdlib only: JSON-RPC 2.0 over newline-delimited JSON (MCP stdio transport).

Modes
-----
  mcp_irc_server.py            stdio MCP server (what omp launches).
  mcp_irc_server.py --daemon   long-lived IRC client that owns the socket,
                               spools channel traffic, and serves the same
                               MCP JSON-RPC over a unix socket so short-lived
                               omp sessions never drop the nick or miss lines.

When the stdio server starts and the daemon socket exists, it proxies tool
calls to the daemon; otherwise it owns the IRC connection in-process (state
dies with the session; the supervisor normally keeps the daemon alive).

Tools: irc_connect, irc_join, irc_say, irc_channels, irc_recent.

Trust/ignore policy is host-based: senders whose IRC host matches any glob in
TRUST_FILE (one fnmatch glob per line, e.g. `o.o`, `152.53.49.131`) are
trusted; IRC_IGNORE_NICKS is always dropped. See README.
"""

import json
import fnmatch
import os
import re
import socket
import ssl
import sys
import threading
import time

# ---------------------------------------------------------------- config

def env(name, default=""):
    return os.environ.get(name, default)

IRC_SERVER   = env("IRC_SERVER")
IRC_PORT     = int(env("IRC_PORT", "6697"))
IRC_TLS      = env("IRC_TLS", "1" if IRC_PORT == 6697 else "0").lower() in ("1", "true", "yes", "on")
IRC_NICK     = env("IRC_NICK", "omp-bot")
IRC_USERNAME = env("IRC_USERNAME", IRC_NICK)
IRC_REALNAME = env("IRC_REALNAME", "oh-my-pi via mcp-irc")
IRC_NICKSERV_PASS = env("IRC_NICKSERV_PASS")
IRC_SERVER_PASS   = env("IRC_SERVER_PASS")
IRC_CHANNELS = [c for c in env("IRC_CHANNELS").replace(",", " ").split() if c]
TRUST_FILE = env("TRUST_FILE", "/state/trust.txt")
IRC_IGNORE_NICKS = {n.lower() for n in env("IRC_IGNORE_NICKS").replace(",", " ").split() if n}


def load_host_globs(path=TRUST_FILE):
    try:
        with open(path) as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        return [g for g in os.environ.get("IRC_TRUST_HOSTS", "").replace(",", " ").split() if g]


def host_trusted(host, globs=None):
    """Host-glob trust, fleet policy.json style: `o.o`, `152.53.49.131`."""
    if not host:
        return False
    globs = load_host_globs() if globs is None else globs
    return any(fnmatch.fnmatch(host.lower(), g.lower()) for g in globs)
SPOOL_DIR = env("IRC_SPOOL_DIR", "/state/irc")
SOCK_PATH = os.path.join(SPOOL_DIR, "mcp.sock")
MAX_LINE = 450  # PRIVMSG payload budget (512 - protocol overhead)
NICK_PING = re.compile(r"(?i)(?<![\w\[\]\\`^{|}-])" + re.escape(IRC_NICK) + r"(?![\w\[\]\\`^{|}-])")

SERVICES = {"nickserv", "chanserv", "memoserv", "operserv", "hostserv"}


def split_say(text):
    """Split into <=MAX_LINE-byte chunks on whitespace, vjt-style."""
    out, cur = [], ""
    for word in text.split("\n"):
        while len(word.encode()) > MAX_LINE:
            cut = word.rfind(" ", 0, MAX_LINE)
            cut = cut if cut > 0 else MAX_LINE
            out.append(word[:cut])
            word = word[cut:].lstrip()
        if cur:
            candidate = cur + " " + word
        else:
            candidate = word
        if len(candidate.encode()) <= MAX_LINE:
            cur = candidate
        else:
            if cur:
                out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out or [""]


# ---------------------------------------------------------------- IRC client

class IrcClient:
    """One IRC connection with auto-reconnect, backoff and a spool."""

    def __init__(self):
        self.lock = threading.RLock()  # reentrant: connect() holds it while _send() acquires
        self.sock = None
        self.connected = False
        self.should_run = False
        self.backoff = 1
        self.channels = set()
        self.ring = {}      # channel/"*dm*" -> deque of recent events
        self.ring_max = int(env("IRC_RING_SIZE", "200"))
        self.spool = None
        self.owner = None   # "daemon" while --daemon owns the connection

    # -- wire -----------------------------------------------------------
    def _send(self, line):
        with self.lock:
            if self.sock is None:
                raise ConnectionError("not connected")
            self.sock.sendall((line + "\r\n").encode())

    def connect(self, force=False):
        with self.lock:
            if self.connected and not force:
                return True
            self._close_locked()
            try:
                raw = socket.create_connection((IRC_SERVER, IRC_PORT), timeout=15)
                if IRC_TLS:
                    ctx = ssl.create_default_context()
                    raw = ctx.wrap_socket(raw, server_hostname=IRC_SERVER)
                self.sock = raw
                if IRC_SERVER_PASS:
                    self._send(f"PASS {IRC_SERVER_PASS}")
                # Nick-cycle on 433 is handled by the reader appending `_`.
                self._send(f"NICK {IRC_NICK}")
                self._send(f"USER {IRC_USERNAME} 0 * :{IRC_REALNAME}")
                self.connected = True
                self.backoff = 1
            except OSError as e:
                return False
        if self.connected:
            if IRC_NICKSERV_PASS:
                time.sleep(2)
                self._send(f"PRIVMSG NickServ :IDENTIFY {IRC_NICKSERV_PASS}")
            for ch in self.channels:
                self._send(f"JOIN {ch}")
        return self.connected

    def _close_locked(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.connected = False

    def reader_loop(self):
        """Reconnect with backoff; parse; spool; never dies."""
        self.should_run = True
        buf = b""
        while self.should_run:
            if not self.connected:
                if not self.connect():
                    time.sleep(self.backoff)
                    self.backoff = min(self.backoff * 2, 60)
                continue
            try:
                data = self.sock.recv(4096)
                if not data:
                    raise ConnectionError("eof")
                buf += data
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    self.handle(line.decode("utf-8", "replace"))
            except (OSError, ConnectionError):
                with self.lock:
                    self._close_locked()

    # -- protocol -------------------------------------------------------
    def handle(self, line):
        if line.startswith("PING"):
            self._send(line.replace("PING", "PONG", 1))
            return
        parts = line.split(" ", 3)
        if len(parts) < 3:
            return
        prefix, cmd, arg = parts[0], parts[1], parts[2]
        text = parts[3][1:] if len(parts) == 4 else ""
        nick = prefix[1:].split("!")[0] if prefix.startswith(":") else ""
        uhost = prefix[1:].split("!", 1)[1] if prefix.startswith(":") and "!" in prefix else ""
        host = uhost.split("@", 1)[1] if "@" in uhost else uhost
        if cmd == "433":  # nick in use: fall back
            self.nick_suffix = getattr(self, "nick_suffix", 0) + 1
            self._send(f"NICK {IRC_NICK[:20]}{self.nick_suffix}")
            return
        if cmd == "001":  # welcome: join configured channels
            for ch in IRC_CHANNELS:
                self.join(ch)
            return
        if cmd == "JOIN" and nick.lower() == IRC_NICK.lower():
            self.channels.add(arg)
            return
        if cmd == "PART" and nick.lower() == IRC_NICK.lower():
            self.channels.discard(arg)
            return
        if cmd == "PRIVMSG":
            self.on_message(nick, arg, text, host)

    def on_message(self, nick, target, text, host=""):
        if nick.lower() in SERVICES or nick.lower() in IRC_IGNORE_NICKS:
            return
        is_dm = target.lower() == IRC_NICK.lower()
        chan = "*dm*" if is_dm else target
        mention = is_dm or bool(NICK_PING.search(text))
        trusted = host_trusted(host)
        event = {"ts": time.time(), "from": nick, "host": host,
                 "chan": target if not is_dm else None,
                 "text": text, "mention": mention, "dm": is_dm, "trusted": trusted}
        with self.lock:
            ring = self.ring.setdefault(chan, [])
            ring.append(event)
            del ring[:-self.ring_max]
        self.append_spool(chan, event)

    def append_spool(self, chan, event):
        if not self.spool:
            return
        try:
            os.makedirs(self.spool, exist_ok=True)
            path = os.path.join(self.spool, "messages.jsonl")
            with open(path, "a") as f:
                f.write(json.dumps({**event, "ring": chan}) + "\n")
        except OSError:
            pass

    # -- tool surface ---------------------------------------------------
    def say(self, target, text):
        if not self.connected:
            raise ConnectionError(f"not connected to {IRC_SERVER}")
        for chunk in split_say(text):
            self._send(f"PRIVMSG {target} :{chunk}")
            time.sleep(0.3)  # light flood pacing
        return len(split_say(text))

    def join(self, channel):
        self._send(f"JOIN {channel}")
        self.channels.add(channel)
        return True

    def channels_list(self):
        return sorted(self.channels)

    def recent(self, channel, n=20, mentions_only=False):
        key = "*dm*" if channel in (None, "", "*dm*", "dm") else channel
        with self.lock:
            events = list(self.ring.get(key, []))
        if mentions_only:
            events = [e for e in events if e["mention"]]
        return events[-n:]

    def recent_global(self, n=50):
        with self.lock:
            all_events = [e for evs in self.ring.values() for e in evs]
        return sorted(all_events, key=lambda e: e["ts"])[-n:]


CLIENT = IrcClient()

# ---------------------------------------------------------------- MCP layer

TOOLS = [
    {"name": "irc_connect", "description": "Connect to the IRC server (uses IRC_* env config). Already done automatically at startup when IRC_SERVER is set.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "irc_join", "description": "Join an IRC channel, e.g. '#ai'.",
     "inputSchema": {"type": "object", "properties": {"channel": {"type": "string", "description": "Channel name starting with # or &"}}, "required": ["channel"], "additionalProperties": False}},
    {"name": "irc_say", "description": "Send a message to a channel or nick. Long text is split into IRC-sized chunks. Keep replies short and conversational.",
     "inputSchema": {"type": "object", "properties": {"target": {"type": "string", "description": "Channel (#ai) or nick"}, "text": {"type": "string"}}, "required": ["target", "text"], "additionalProperties": False}},
    {"name": "irc_channels", "description": "List channels currently joined.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "irc_recent", "description": "Recent messages from a channel (or 'dm' for private messages). Use mentions_only=true to catch things addressed to you.",
     "inputSchema": {"type": "object", "properties": {
         "channel": {"type": "string", "description": "Channel name or 'dm'"},
         "n": {"type": "integer", "minimum": 1, "maximum": 100},
         "mentions_only": {"type": "boolean"}}, "required": ["channel"], "additionalProperties": False}},
]


def tool_call(name, args):
    if name == "irc_connect":
        ok = CLIENT.connect()
        return {"connected": ok, "server": IRC_SERVER, "port": IRC_PORT, "tls": IRC_TLS, "nick": IRC_NICK}
    if name == "irc_join":
        return {"joined": CLIENT.join(args["channel"])}
    if name == "irc_say":
        return {"sent": CLIENT.say(args["target"], args["text"])}
    if name == "irc_channels":
        return {"channels": CLIENT.channels_list()}
    if name == "irc_recent":
        return {"messages": CLIENT.recent(args["channel"], int(args.get("n", 20)),
                                          bool(args.get("mentions_only", False)))}
    raise ValueError(f"unknown tool: {name}")


def handle_mcp(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "omp-irc-mcp", "version": "1.0.0"}}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params", {})
        try:
            out = tool_call(params["name"], params.get("arguments", {}))
            body = json.dumps(out)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": body}], "isError": False}}
        except Exception as e:  # surface as tool error, keep session alive
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"error: {e}"}], "isError": True}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"unknown method {method}"}}
    return None


# ---------------------------------------------------------------- transports

def serve_stdio():
    # If a daemon owns the connection, proxy to it; otherwise own it here.
    proxied = None
    if os.path.exists(SOCK_PATH):
        try:
            proxied = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            proxied.settimeout(120)
            proxied.connect(SOCK_PATH)
        except OSError:
            proxied = None
    if proxied:
        upstream = proxied.makefile("r", encoding="utf-8")
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            proxied.sendall((raw + "\n").encode())
            while True:
                resp = upstream.readline()
                if not resp:
                    raise SystemExit("daemon went away")
                resp = resp.strip()
                if not resp:
                    continue
                print(resp, flush=True)
                try:
                    if json.loads(resp).get("id") == json.loads(raw).get("id"):
                        break
                except json.JSONDecodeError:
                    break
        return

    if IRC_SERVER:
        threading.Thread(target=CLIENT.reader_loop, daemon=True).start()
        CLIENT.connect()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
            out = handle_mcp(msg)
        except Exception as e:
            out = {"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": f"parse error: {e}"}}
        if out:
            print(json.dumps(out), flush=True)


def serve_daemon():
    """Own the IRC connection forever; serve MCP over the unix socket."""
    os.makedirs(SPOOL_DIR, exist_ok=True)
    try:
        os.unlink(SOCK_PATH)
    except OSError:
        pass
    CLIENT.spool = SPOOL_DIR
    CLIENT.owner = "daemon"
    threading.Thread(target=CLIENT.reader_loop, daemon=True).start()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    srv.listen(8)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=daemon_client, args=(conn,), daemon=True).start()


def daemon_client(conn):
    f = conn.makefile("r", encoding="utf-8")
    for raw in f:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out = handle_mcp(json.loads(raw))
        except Exception as e:
            out = {"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": f"parse error: {e}"}}
        if out:
            try:
                conn.sendall((json.dumps(out) + "\n").encode())
            except OSError:
                break
    conn.close()


# ---------------------------------------------------------------- main

if __name__ == "__main__":
    if not IRC_SERVER and not env("IRC_ALLOW_NO_SERVER"):
        print("IRC_SERVER not set", file=sys.stderr)
    if "--daemon" in sys.argv:
        serve_daemon()
    else:
        serve_stdio()
