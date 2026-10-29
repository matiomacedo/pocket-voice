# /// script
# requires-python = ">=3.10,<3.15"
# dependencies = ["pocket-tts", "scipy"]
# ///
"""Pocket TTS daemon: keeps the model in memory and speaks text sent over a Unix socket.

Protocol: one JSON object per connection, answered with "ok" or an error:
  {"cmd": "speak", "session": "...", "name": "...", "text": "..."}  name: announced when sessions change
  {"cmd": "stop", "session": "..."}  without "session", stops all sessions
  {"cmd": "skip"}                    stop the response playing now; the next one starts
  {"cmd": "voice", "session": "...", "name": "..."}
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
SESSIONS_PATH = os.path.join(STATE_DIR, "sessions.json")  # session -> voice, written by the daemon only
VOICE = os.environ.get("CLAUDE_VOICE", "alba")
# New sessions take the least used of these voices, so sessions sound different.
VOICES = ([v.strip() for v in os.environ.get("CLAUDE_VOICES", "").split(",") if v.strip()]
          or [VOICE] + [v for v in ("alba", "george", "eve", "michael") if v != VOICE])
RATE = os.environ.get("CLAUDE_VOICE_RATE")  # afplay playback rate, e.g. 1.2
MAX_SENTENCES = int(os.environ.get("CLAUDE_VOICE_MAX_SENTENCES") or 0)  # 0: no limit
IDLE_EXIT_SECONDS = 30 * 60
RECENT_SESSION_SECONDS = 60 * 60
OLD_STATE_SECONDS = 7 * 24 * 60 * 60


def split_sentences(text):
    parts = re.split(r"(?<=[.!?:;])\s+|\n+", text)
    return [p.strip() for p in parts if re.search(r"\w", p)]


def load_sessions():
    try:
        with open(SESSIONS_PATH) as f:
            sessions = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in sessions.items() if time.time() - v["seen"] < OLD_STATE_SECONDS}


def save_sessions(sessions):
    with open(SESSIONS_PATH + ".tmp", "w") as f:
        json.dump(sessions, f)
    os.replace(SESSIONS_PATH + ".tmp", SESSIONS_PATH)


def remove_old_prompted_flags():
    # speak.py only needs these while it waits for the daemon to start.
    for name in os.listdir(STATE_DIR):
        path = os.path.join(STATE_DIR, name)
        if name.startswith("prompted-") and time.time() - os.path.getmtime(path) > OLD_STATE_SECONDS:
            os.unlink(path)


class Speaker:
    def __init__(self, model):
        self.model = model
        self.model_lock = threading.Lock()  # voice loading and generation share the model
        self.voice_states = {}
        self.sessions = load_sessions()
        self.last_session = None  # session of the last queued response, for announcements
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

    def voice_state(self, voice):
        with self.model_lock:
            if voice not in self.voice_states:
                self.voice_states[voice] = self.model.get_state_for_audio_prompt(voice)
            return self.voice_states[voice]

    def session_voice(self, session):
        now = time.time()
        entry = self.sessions.get(session)
        if entry is None:
            recent = [e["voice"] for e in self.sessions.values() if now - e["seen"] < RECENT_SESSION_SECONDS]
            entry = self.sessions[session] = {"voice": min(VOICES, key=recent.count)}
        entry["seen"] = now
        save_sessions(self.sessions)
        return entry["voice"]

    def set_voice(self, session, voice):
        self.voice_state(voice)  # raises for an unknown voice
        self.sessions[session] = {"voice": voice, "seen": time.time()}
        save_sessions(self.sessions)

    def speak(self, session, text, name=""):
        voice = self.session_voice(session)
        sentences = split_sentences(text)
        if MAX_SENTENCES:
            sentences = sentences[:MAX_SENTENCES]
        if name and self.last_session not in (None, session):
            sentences.insert(0, name + ".")
        self.last_session = session
        with self.lock:
            self._interrupt(session)
            token = self.tokens[session] = object()
            for sentence in sentences:
                self.text_q.put((session, token, voice, sentence))

    def stop(self, session=None):
        # No session: stop everything, including responses queued by other sessions.
        with self.lock:
            if session is None:
                self.tokens.clear()
                self._kill_player()
            else:
                self._interrupt(session)

    def skip(self):
        with self.lock:
            if self.player and self.player.poll() is None:
                self._interrupt(self.player_session)

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
            session, token, voice, sentence = self.text_q.get()
            if not self._current(session, token):
                continue
            try:
                state = self.voice_state(voice)
                with self.model_lock:
                    audio = self.model.generate_audio(state, sentence)
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
                    rate = ["-r", RATE] if RATE else []
                    player = self.player = subprocess.Popen(["afplay", *rate, path])
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

    remove_old_prompted_flags()
    speaker = Speaker(TTSModel.load_model())
    speaker.voice_state(VOICES[0])

    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK_PATH)
    server.listen()
    server.settimeout(60)
    print(f"ready, voices={','.join(VOICES)}", flush=True)

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
            reply = b"ok"
            if msg.get("cmd") == "speak":
                speaker.speak(str(msg.get("session", "")), str(msg.get("text", "")), str(msg.get("name", "")))
            elif msg.get("cmd") == "stop":
                session = msg.get("session")
                speaker.stop(None if session is None else str(session))
            elif msg.get("cmd") == "skip":
                speaker.skip()
            elif msg.get("cmd") == "voice":
                try:
                    speaker.set_voice(str(msg.get("session", "")), str(msg.get("name", "")))
                except Exception as e:
                    print(f"cannot load voice: {e!r}", flush=True)
                    reply = b"unknown voice"
            try:
                conn.sendall(reply)
            except OSError:
                pass

    # Close first: clients still in the backlog get no "ok" and start a new daemon.
    server.close()
    os.unlink(SOCK_PATH)
    speaker.stop()


if __name__ == "__main__":
    main()
