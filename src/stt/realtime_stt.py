# ============================================================
# Project YUNA Link - Realtime STT (Desktop Audio Loopback)
# Debug Version
# ============================================================

import os
import re
import time
import queue
import threading
import traceback
from dataclasses import dataclass
from typing import Optional

import numpy as np
import soundcard as sc

try:
    from faster_whisper import WhisperModel
except Exception as e:
    WhisperModel = None
    _FW_IMPORT_ERROR = e
else:
    _FW_IMPORT_ERROR = None

try:
    from silero_vad import load_silero_vad, get_speech_timestamps
except Exception as e:
    load_silero_vad = None
    get_speech_timestamps = None
    _SILERO_IMPORT_ERROR = e
else:
    _SILERO_IMPORT_ERROR = None


@dataclass
class SpeechEvent:
    speaker_id: str
    text: str
    is_final: bool
    start_ts: float
    end_ts: float
    energy: float
    is_overlap: bool
    confidence: float = 0.0
    speaker_changed: bool = False
    direction: Optional[str] = None
    distance_class: Optional[str] = None


class RealtimeSTT:
    def __init__(
        self,
        model_size: str = "small",
        device: str = "cuda",
        sample_rate: int = 16000,
        block_duration: float = 0.1,
        silence_duration: float = 0.5,
        partial_window_sec: float = 1.0,
        min_utterance_sec: float = 0.35,
        voice_band_low_hz: float = 120.0,
        voice_band_high_hz: float = 4300.0,
        voice_band_ratio_threshold: float = 0.35,
        rms_threshold: float = 0.002,
        use_silero_vad: bool = False,
        debug_log_audio: bool = True,
    ):
        print("[STT] Initializing (Desktop Loopback / Default Device / Debug)...")

        if WhisperModel is None:
            raise RuntimeError(
                "faster-whisper の読み込みに失敗しました。"
                f" 元エラー: {_FW_IMPORT_ERROR}"
            )

        if use_silero_vad and load_silero_vad is None:
            raise RuntimeError(
                "silero-vad の読み込みに失敗しました。"
                f" 元エラー: {_SILERO_IMPORT_ERROR}"
            )

        compute_type = "float16" if device == "cuda" else "int8"
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
        )

        self.vad_model = load_silero_vad() if use_silero_vad else None

        self.sample_rate = sample_rate
        self.block_duration = block_duration
        self.block_frames = int(sample_rate * block_duration)

        self.silence_duration = silence_duration
        self.partial_window_sec = partial_window_sec
        self.min_utterance_sec = min_utterance_sec

        self.voice_band_low_hz = voice_band_low_hz
        self.voice_band_high_hz = voice_band_high_hz
        self.voice_band_ratio_threshold = voice_band_ratio_threshold
        self.rms_threshold = rms_threshold
        self.debug_log_audio = debug_log_audio

        self.audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
        self.running = False
        self.capture_error: Optional[BaseException] = None

        self.buffer: list[np.ndarray] = []
        self.last_voice_time = 0.0
        self.start_time: Optional[float] = None
        self.speaking = False
        self.last_partial_text = ""

        self.last_debug_print = 0.0

        self.speaker = self._find_default_speaker()
        print(f"[STT] Using default speaker: {self.speaker.name}")

        self.hallucination_patterns = [
            r"^ご視聴ありがとうございました[。！!]*$",
            r"^ありがとうございました[。！!]*$",
            r"^ご清聴ありがとうございました[。！!]*$",
            r"^字幕.*$",
            r"^チャンネル登録.*$",
            r"^高評価.*$",
            r"^thanks for watching[.! ]*$",
            r"^thank you[.! ]*$",
            r"^subtitles by .*",
        ]

    def _find_default_speaker(self):
        sp = sc.default_speaker()
        if sp is None:
            raise RuntimeError("既定の再生デバイスが取得できませんでした。")
        return sp

    def _capture_worker(self):
        try:
            print("[STT] Opening loopback microphone...")
            mic = sc.get_microphone(id=str(self.speaker.name), include_loopback=True)
            print(f"[STT] Loopback microphone opened: {mic}")

            with mic.recorder(samplerate=self.sample_rate) as recorder:
                print("[STT] Listening Desktop Audio...")

                while self.running:
                    data = recorder.record(numframes=self.block_frames)

                    if data is None or data.size == 0:
                        continue

                    if data.ndim == 2:
                        mono = data.mean(axis=1).astype(np.float32)
                    else:
                        mono = data.astype(np.float32)

                    try:
                        self.audio_queue.put(mono, timeout=0.5)
                    except queue.Full:
                        try:
                            _ = self.audio_queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self.audio_queue.put_nowait(mono)
                        except queue.Full:
                            pass

        except BaseException as e:
            self.capture_error = e
            print("[STT][ERROR] capture worker crashed:")
            traceback.print_exc()

    def _rms(self, audio: np.ndarray) -> float:
        if audio.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(audio), dtype=np.float64)))

    def _voice_band_ratio(self, audio: np.ndarray) -> float:
        if audio.size == 0:
            return 0.0

        spectrum = np.fft.rfft(audio)
        freqs = np.fft.rfftfreq(audio.size, d=1.0 / self.sample_rate)
        power = np.abs(spectrum) ** 2

        total = float(np.sum(power)) + 1e-12
        mask = (freqs >= self.voice_band_low_hz) & (freqs <= self.voice_band_high_hz)
        voice = float(np.sum(power[mask]))

        return voice / total

    def _bandpass_voice(self, audio: np.ndarray) -> np.ndarray:
        if audio.size == 0:
            return audio

        spectrum = np.fft.rfft(audio)
        freqs = np.fft.rfftfreq(audio.size, d=1.0 / self.sample_rate)
        mask = (freqs >= self.voice_band_low_hz) & (freqs <= self.voice_band_high_hz)
        spectrum[~mask] = 0
        return np.fft.irfft(spectrum, n=audio.size).astype(np.float32)

    def _has_speech_silero(self, audio: np.ndarray) -> bool:
        if self.vad_model is None or get_speech_timestamps is None:
            return True

        wav = audio.astype(np.float32)
        timestamps = get_speech_timestamps(
            wav,
            self.vad_model,
            sampling_rate=self.sample_rate,
            threshold=0.5,
            min_speech_duration_ms=120,
            min_silence_duration_ms=100,
            speech_pad_ms=30,
            return_seconds=False,
        )
        return len(timestamps) > 0

    def _looks_like_voice(self, audio: np.ndarray) -> tuple[bool, str, float, float]:
        rms = self._rms(audio)
        if rms < self.rms_threshold:
            return False, "low_rms", rms, 0.0

        ratio = self._voice_band_ratio(audio)
        if ratio < self.voice_band_ratio_threshold:
            return False, "low_voice_band_ratio", rms, ratio

        if self.vad_model is not None:
            if not self._has_speech_silero(audio):
                return False, "silero_no_speech", rms, ratio

        return True, "ok", rms, ratio

    def _normalize_text(self, text: str) -> str:
        text = text.strip()
        text = re.sub(r"\s+", " ", text)
        return text

    def _is_hallucination_text(self, text: str) -> bool:
        if not text:
            return True

        norm = self._normalize_text(text).lower()

        if re.fullmatch(r"[。、,.!！?？…ー\-~ ]*", norm):
            return True

        for pat in self.hallucination_patterns:
            if re.fullmatch(pat, norm, flags=re.IGNORECASE):
                return True

        return False

    def _transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""

        filtered = self._bandpass_voice(audio)

        segments, _ = self.model.transcribe(
            filtered,
            language="ja",
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=300,
                speech_pad_ms=80,
            ),
        )

        text = "".join(seg.text for seg in segments).strip()
        return self._normalize_text(text)

    def on_event(self, event: SpeechEvent):
        kind = "FINAL" if event.is_final else "PART"
        print(f"[STT][{kind}][{event.speaker_id}] {event.text}")

    def run(self):
        self.running = True

        worker = threading.Thread(target=self._capture_worker, daemon=True)
        worker.start()

        print("[STT] Main loop started.")

        try:
            while self.running:
                if self.capture_error is not None:
                    raise RuntimeError(f"capture worker failed: {self.capture_error}")

                try:
                    audio = self.audio_queue.get(timeout=1.0)
                except queue.Empty:
                    print("[STT][DEBUG] waiting audio...")
                    continue

                now = time.time()
                is_voice, reason, rms, ratio = self._looks_like_voice(audio)

                if self.debug_log_audio and now - self.last_debug_print >= 0.5:
                    self.last_debug_print = now
                    print(
                        f"[STT][DEBUG] rms={rms:.5f} "
                        f"voice_ratio={ratio:.3f} "
                        f"is_voice={is_voice} reason={reason} "
                        f"queue={self.audio_queue.qsize()}"
                    )

                if is_voice:
                    if not self.speaking:
                        self.speaking = True
                        self.start_time = now
                        self.buffer = []
                        self.last_partial_text = ""
                        print("[STT] Speech start")

                    self.buffer.append(audio)
                    self.last_voice_time = now

                else:
                    if self.speaking:
                        self.buffer.append(audio)

                        if now - self.last_voice_time > self.silence_duration:
                            self.speaking = False

                            full_audio = np.concatenate(self.buffer).astype(np.float32)
                            utterance_sec = full_audio.size / float(self.sample_rate)

                            print(f"[STT][DEBUG] finalize utterance_sec={utterance_sec:.2f}")

                            if utterance_sec >= self.min_utterance_sec:
                                text = self._transcribe(full_audio)

                                if text and not self._is_hallucination_text(text):
                                    event = SpeechEvent(
                                        speaker_id="spk_0",
                                        text=text,
                                        is_final=True,
                                        start_ts=self.start_time if self.start_time else now,
                                        end_ts=now,
                                        energy=self._rms(full_audio),
                                        is_overlap=False,
                                    )
                                    self.on_event(event)
                                else:
                                    print(f"[STT][DROP][FINAL] {text}")

                            self.buffer = []
                            self.start_time = None
                            self.last_partial_text = ""
                            print("[STT] Speech end")

                if self.speaking and self.buffer:
                    recent = np.concatenate(self.buffer).astype(np.float32)
                    max_frames = int(self.sample_rate * self.partial_window_sec)

                    if recent.size > max_frames:
                        recent = recent[-max_frames:]

                    if recent.size >= int(self.sample_rate * 0.6):
                        text = self._transcribe(recent)
                        if (
                            text
                            and text != self.last_partial_text
                            and not self._is_hallucination_text(text)
                        ):
                            self.last_partial_text = text
                            event = SpeechEvent(
                                speaker_id="spk_0",
                                text=text,
                                is_final=False,
                                start_ts=self.start_time if self.start_time else now,
                                end_ts=now,
                                energy=self._rms(recent),
                                is_overlap=True,
                            )
                            self.on_event(event)
                        elif text:
                            print(f"[STT][DROP][PART] {text}")

        except KeyboardInterrupt:
            print("\n[STT] Stopped by user.")
        finally:
            self.running = False


def main():
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    stt = RealtimeSTT(
        model_size="small",
        device="cuda",
        sample_rate=16000,
        block_duration=0.1,
        silence_duration=0.5,
        partial_window_sec=1.0,
        min_utterance_sec=0.35,
        voice_band_low_hz=120.0,
        voice_band_high_hz=4300.0,
        voice_band_ratio_threshold=0.35,
        rms_threshold=0.002,
        use_silero_vad=False,
        debug_log_audio=True,
    )
    stt.run()


if __name__ == "__main__":
    main()