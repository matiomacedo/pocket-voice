# /// script
# requires-python = ">=3.10,<3.15"
# dependencies = ["pocket-tts", "scipy"]
# ///
"""Pocket TTS daemon: keeps the model in memory and speaks text sent over a Unix socket.

Protocol: one JSON object per connection, {"cmd": "speak", "session": "...", "text": "..."}
or {"cmd": "stop", "session": "..."}. A stop without "session" stops all sessions.
"""
import fcntl
import json
import os
import queue
import re
import socket
import subprocess
import tempfile
import threading
import time

import scipy.io.wavfile
from pocket_tts import TTSModel

STATE_DIR = os.path.expanduser("~/.cache/claude-voice")
SOCK_PATH = os.path.join(STATE_DIR, "voice.sock")
LOCK_PATH = os.path.join(STATE_DIR, "daemon.lock")
VOICE = os.environ.get("CLAUDE_VOICE", "alba")
IDLE_EXIT_SECONDS = 30 * 60


def split_sentences(text):
    parts = re.split(r"(?<=[.!?:;])\s+|\n+", text)
    return [p.strip() for p in parts if re.search(r"\w", p)]


class Speaker:
    def __init__(self, model, voice_state):
        self.model = model
        self.voice_state = voice_state
        # Each session's current response has a token. Both loops discard queued sentences
        # whose token is no longer current, so responses from other sessions keep their turn.
        self.tokens = {}
        self.lock = threading.Lock()
        self.player = None
        self.player_session = None
        self.text_q = queue.Queue()
        self.audio_q = queue.Queue()
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    def speak(self, session, text):
        with self.lock:
            self._interrupt(session)
            token = self.tokens[session] = object()
            for sentence in split_sentences(text):
                self.text_q.put((session, token, sentence))

    def stop(self, session=None):
        # No session: stop everything, including responses queued by other sessions.
        with self.lock:
            if session is None:
                self.tokens.clear()
                self._kill_player()
            else:
                self._interrupt(session)

    def _interrupt(self, session):
        self.tokens.pop(session, None)
        if self.player_session == session:
            self._kill_player()

    def _kill_player(self):
        if self.player and self.player.poll() is None:
            self.player.kill()

    def _current(self, session, token):
        return self.tokens.get(session) is token

    def _synth_loop(self):
        while True:
            session, token, sentence = self.text_q.get()
            if not self._current(session, token):
                continue
            try:
                audio = self.model.generate_audio(self.voice_state, sentence)
                if not self._current(session, token):
                    continue
                fd, path = tempfile.mkstemp(suffix=".wav", dir=STATE_DIR)
                os.close(fd)
                scipy.io.wavfile.write(path, self.model.sample_rate, audio.numpy())
            except Exception as e:
                print(f"generation failed: {e!r}", flush=True)
                continue
            self.audio_q.put((session, token, path))

    def _play_loop(self):
        while True:
            session, token, path = self.audio_q.get()
            with self.lock:
                player = None
                if self._current(session, token):
                    player = self.player = subprocess.Popen(["afplay", path])
                    self.player_session = session
            if player:
                player.wait()
            os.unlink(path)


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    # Only the lock holder touches the socket, so a duplicate started by a racing hook exits.
    lock = open(LOCK_PATH, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another daemon is running, exiting", flush=True)
        return

    model = TTSModel.load_model()
    speaker = Speaker(model, model.get_state_for_audio_prompt(VOICE))

    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK_PATH)
    server.listen()
    server.settimeout(60)
    print(f"ready, voice={VOICE}", flush=True)

    last_activity = time.monotonic()
    while time.monotonic() - last_activity < IDLE_EXIT_SECONDS:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        last_activity = time.monotonic()
        with conn:
            try:
                msg = json.loads(conn.makefile().read())
            except (ValueError, OSError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("cmd") == "speak":
                speaker.speak(str(msg.get("session", "")), str(msg.get("text", "")))
            elif msg.get("cmd") == "stop":
                session = msg.get("session")
                speaker.stop(None if session is None else str(session))
            try:
                conn.sendall(b"ok")
            except OSError:
                pass

    # Close first: clients still in the backlog get no "ok" and start a new daemon.
    server.close()
    os.unlink(SOCK_PATH)
    speaker.stop()


if __name__ == "__main__":
    main()
