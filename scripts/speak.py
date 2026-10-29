#!/usr/bin/env python3
"""Client for the voice daemon (stdlib only, so hooks start fast).

  speak.py               Stop hook: read hook JSON from stdin and speak last_assistant_message
  speak.py --prompt      UserPromptSubmit hook: stop this session's speech
  speak.py --stop        /voice-stop: stop all speech, including other sessions' queued responses
  speak.py --toggle      mute / unmute
  speak.py --dry-run     print the cleaned text from stdin instead of speaking
"""
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

STATE_DIR = os.path.expanduser("~/.cache/claude-voice")
SOCK_PATH = os.path.join(STATE_DIR, "voice.sock")
MUTED_FLAG = os.path.join(STATE_DIR, "muted")
PROMPTED_FLAG = os.path.join(STATE_DIR, "prompted")  # mtime = last /voice-stop; "-<session>" = last prompt there
LOCK_PATH = os.path.join(STATE_DIR, "daemon.lock")  # held by the running daemon
LOG_PATH = os.path.join(STATE_DIR, "daemon.log")
DAEMON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voiced.py")
DAEMON_STARTUP_SECONDS = 300  # first run downloads the model
STOP_REPLY = "Stopped."  # the reply to /voice-stop, never spoken


def clean(text):
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)  # fenced code blocks
    text = re.sub(r"`([^`\s]+(?: [^`\s]+)?)`", r"\1", text)  # short inline code: keep words
    text = re.sub(r"`[^`]*`", " ", text)  # long inline code: drop
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links -> link text
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\S*\w\.[A-Za-z]\w*:\d+\S*", " ", text)  # file:line refs
    text = re.sub(r"^\s*\|?\s*:?-{3,}.*$", " ", text, flags=re.M)  # table separator rows
    text = re.sub(r"^\s*\|(.*)\|\s*$", r"\1.", text, flags=re.M)  # table rows: drop outer pipes
    text = text.replace("|", ",")
    text = re.sub(r"^\s*(#+|>|[-*+]|\d+\.)\s+", "", text, flags=re.M)  # headings, quotes, bullets
    text = re.sub(r"(\*{1,3}|~~)(\S(?:.*?\S)?)\1", r"\2", text)  # bold, italic, strikethrough
    text = re.sub(r"(?<!\w)(_{1,3})(\S(?:.*?\S)?)\1(?!\w)", r"\2", text)  # _italic_, not snake_case
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def log(line):
    with open(LOG_PATH, "a") as f:
        print(f"speak.py: {line}", file=f)


def send(msg):
    # The daemon answers "ok" once it has handled the message. A connection that was
    # accepted into the backlog of a daemon that is shutting down gets no answer.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(SOCK_PATH)
        s.sendall(json.dumps(msg).encode())
        s.shutdown(socket.SHUT_WR)
        if s.recv(16) != b"ok":
            raise ConnectionError("no answer from daemon")


def daemon_running():
    with open(LOCK_PATH, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False  # closing the file releases the lock


def spawn_daemon():
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    with open(LOG_PATH, "a") as f:
        return subprocess.Popen([uv, "run", "--script", DAEMON], stdout=f, stderr=f,
                                stdin=subprocess.DEVNULL, start_new_session=True)


def send_starting_daemon(msg, started):
    # The daemon holds LOCK_PATH, so a second one started by a racing hook exits at once (code 0).
    proc = None
    deadline = time.monotonic() + DAEMON_STARTUP_SECONDS
    while True:
        if prompted_since(started, msg["session"]):
            return  # the user moved on; this reply is stale
        try:
            return send(msg)
        except OSError:
            pass
        if proc and proc.poll() is not None:
            if proc.returncode != 0:
                log(f"daemon exited with code {proc.returncode}, see above")
                return
            proc = None
        if proc is None and not daemon_running():
            try:
                proc = spawn_daemon()
            except OSError as e:
                log(f"cannot start daemon (is uv installed?): {e}")
                return
        if time.monotonic() > deadline:
            log(f"daemon did not answer within {DAEMON_STARTUP_SECONDS}s")
            return
        time.sleep(0.5)


def session_flag(session):
    return PROMPTED_FLAG + "-" + re.sub(r"[^\w-]", "", session)


def prompted_since(t, session):
    for path in (PROMPTED_FLAG, session_flag(session)):
        try:
            if os.path.getmtime(path) > t:
                return True
        except FileNotFoundError:
            pass
    return False


def stop(session=None):
    msg = {"cmd": "stop"}
    if session is not None:
        msg["session"] = session
    try:
        send(msg)
    except OSError:
        pass  # daemon not running, nothing to stop


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    arg = sys.argv[1] if len(sys.argv) > 1 else ""

    if arg == "--prompt":
        session = json.load(sys.stdin).get("session_id", "")
        open(session_flag(session), "w").close()
        stop(session)
    elif arg == "--stop":
        open(PROMPTED_FLAG, "w").close()
        stop()
    elif arg == "--toggle":
        if os.path.exists(MUTED_FLAG):
            os.unlink(MUTED_FLAG)
            print("Voice on.")
        else:
            open(MUTED_FLAG, "w").close()
            stop()
            print("Voice off.")
    elif arg == "--dry-run":
        print(clean(sys.stdin.read()))
    else:
        started = time.time()
        hook = json.load(sys.stdin)
        message = hook.get("last_assistant_message") or ""
        if message.strip() == STOP_REPLY:
            return
        text = clean(message)
        if text and not os.path.exists(MUTED_FLAG):
            send_starting_daemon({"cmd": "speak", "session": hook.get("session_id", ""), "text": text},
                                 started)


if __name__ == "__main__":
    main()
