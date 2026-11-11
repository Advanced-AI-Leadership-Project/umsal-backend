"""CPU inference for an explicitly two-channel, short Korean speech sample."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import onnxruntime as ort
from faster_whisper import WhisperModel
from tokenizers import Tokenizer

RATE = 16000
SPEAKERS = ("spk_1", "spk_2")
TOPICS = {
    "WORRY": "걱정과 불안이 있다. 일이 잘 될지 마음에 걸리고 신경이 쓰인다.",
    "SLEEP": "잠을 잘 못 자고 밤에 자주 깬다. 수면과 잠드는 데 문제가 있다.",
    "APPETITE": "밥을 잘 먹지 못한다. 식욕과 끼니, 음식과 식사량에 변화가 있다.",
    "NONE": "산책하고 따뜻한 날씨를 즐겼다. 특별한 걱정 없이 일상 이야기만 했다.",
}


class InvalidSample(ValueError):
    """Unsupported media or too little usable speech."""


def read_channels(path: Path) -> np.ndarray:
    try:
        with wave.open(str(path), "rb") as stream:
            frames = stream.getnframes()
            if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (
                RATE,
                2,
                2,
            ) or not 0 < frames <= 60 * RATE:
                raise InvalidSample("expected 16 kHz stereo 16-bit WAV, at most 60 seconds")
            data = stream.readframes(frames)
    except (wave.Error, EOFError) as error:
        raise InvalidSample("invalid WAV") from error
    if len(data) != frames * 4:
        raise InvalidSample("truncated WAV")
    return np.frombuffer(data, dtype="<i2").reshape(-1, 2).astype(np.float32).T / 32768


class Models:
    def __init__(self, assets: Path):
        self.assets = assets
        self._whisper = None
        self._encoder = None
        self._tokenizer = None

    def transcribe(self, channel: np.ndarray, speaker: str) -> list[dict]:
        active = np.flatnonzero(np.abs(channel) > 0.008)
        if not active.size:
            return []
        first = max(0, int(active[0]) - RATE // 5)
        last = min(len(channel), int(active[-1]) + RATE // 5)
        if last - first < RATE // 2:
            return []
        if self._whisper is None:
            self._whisper = WhisperModel(
                str(self.assets / "whisper"),
                device="cpu",
                compute_type="int8",
                cpu_threads=2,
                local_files_only=True,
            )
        segments, _ = self._whisper.transcribe(
            channel[first:last],
            language="ko",
            beam_size=1,
            temperature=0,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        records = []
        for segment in segments:
            start = min(round((first / RATE + segment.start) * 1000), 59999)
            end = min(round((first / RATE + segment.end) * 1000), len(channel) * 1000 // RATE)
            text = segment.text.strip()
            if text and end > start and segment.no_speech_prob < 0.65:
                records.append(
                    {
                        "speaker_id": speaker,
                        "start_ms": start,
                        "end_ms": end,
                        "text": text[:1500],
                    }
                )
        return records

    def audio_prep(self, path: Path) -> list[dict]:
        channels = read_channels(path)
        records = []
        for index, speaker in enumerate(SPEAKERS):
            turns = self.transcribe(channels[index], speaker)
            if not turns:
                raise InvalidSample("speech is required on both channels")
            records.extend({k: v for k, v in turn.items() if k != "text"} for turn in turns)
        return sorted(records, key=lambda item: item["start_ms"])

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._encoder is None or self._tokenizer is None:
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            self._encoder = ort.InferenceSession(
                str(self.assets / "e5/onnx/model_quantized.onnx"),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            self._tokenizer = Tokenizer.from_file(str(self.assets / "e5/tokenizer.json"))
            self._tokenizer.enable_truncation(max_length=256)
            self._tokenizer.enable_padding()
        encodings = self._tokenizer.encode_batch(texts)
        inputs = {
            "input_ids": np.array([item.ids for item in encodings], dtype=np.int64),
            "attention_mask": np.array([item.attention_mask for item in encodings], dtype=np.int64),
            "token_type_ids": np.array([item.type_ids for item in encodings], dtype=np.int64),
        }
        hidden = self._encoder.run(
            None, {i.name: inputs[i.name] for i in self._encoder.get_inputs()}
        )[0]
        mask = inputs["attention_mask"][:, :, None]
        pooled = (hidden * mask).sum(axis=1) / mask.sum(axis=1)
        return pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9)

    def text_analysis(self, path: Path, speaker: str) -> dict:
        channel = read_channels(path)[SPEAKERS.index(speaker)]
        turns = self.transcribe(channel, speaker)
        if not turns:
            raise InvalidSample("selected speaker has no usable speech")
        names = list(TOPICS)
        vectors = self.embed(
            ["query: " + value for value in TOPICS.values()]
            + ["passage: " + turn["text"] for turn in turns]
        )
        similarity = vectors[len(names) :] @ vectors[: len(names)].T
        best = None
        for index, scores in enumerate(similarity):
            ranking = np.argsort(scores)[::-1]
            choice = ranking[0]
            if names[choice] != "NONE" and scores[choice] >= 0.72:
                margin = float(scores[choice] - scores[ranking[1]])
                if margin >= 0.01 and (best is None or margin > best[0]):
                    best = (margin, names[choice], turns[index])
        if best is None:
            return {"category": None, "text": None, "start_ms": None, "end_ms": None}
        _, category, turn = best
        return {"category": category, **{k: turn[k] for k in ("text", "start_ms", "end_ms")}}
