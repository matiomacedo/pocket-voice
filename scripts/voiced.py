# /// script
# requires-python = ">=3.10,<3.15"
# dependencies = ["pocket-tts", "scipy"]
# ///
"""Pocket TTS daemon: keeps the model in memory and speaks text sent over a Unix socket.

Protocol: one JSON object per connection, {"cmd": "speak", "text": "..."} or {"cmd": "stop"}.
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
        self.generation = 0
        self.lock = threading.Lock()
        self.player = None
        self.text_q = queue.Queue()
        self.audio_q = queue.Queue()
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    def speak(self, text):
        with self.lock:
            self._interrupt()
            for sentence in split_sentences(text):
                self.text_q.put((self.generation, sentence))

    def stop(self):
        with self.lock:
            self._interrupt()

    def _interrupt(self):
        # Bumping the generation makes both loops discard anything queued earlier.
        self.generation += 1
        if self.player and self.player.poll() is None:
            self.player.kill()

    def _synth_loop(self):
        while True:
            gen, sentence = self.text_q.get()
            if gen != self.generation:
                continue
            try:
                audio = self.model.generate_audio(self.voice_state, sentence)
                if gen != self.generation:
                    continue
                fd, path = tempfile.mkstemp(suffix=".wav", dir=STATE_DIR)
                os.close(fd)
                scipy.io.wavfile.write(path, self.model.sample_rate, audio.numpy())
            except Exception as e:
                print(f"generation failed: {e!r}", flush=True)
                continue
            self.audio_q.put((gen, path))

    def _play_loop(self):
        while True:
            gen, path = self.audio_q.get()
            with self.lock:
                player = None
                if gen == self.generation:
                    player = self.player = subprocess.Popen(["afplay", path])
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
                speaker.speak(str(msg.get("text", "")))
            elif msg.get("cmd") == "stop":
                speaker.stop()
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
