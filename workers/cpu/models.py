"""CPU speaker diarization, Korean transcription and text observations."""

from __future__ import annotations

import wave
from collections import defaultdict
from pathlib import Path

import av
import numpy as np
import onnxruntime as ort
import sherpa_onnx
from faster_whisper import WhisperModel
from tokenizers import Tokenizer

RATE = 16000
MAX_SECONDS = 600
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


def read_mono(path: Path) -> np.ndarray:
    """Decode local supported media, bound decoded duration and resample on CPU."""
    chunks = []
    count = 0
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=RATE)
    try:
        with av.open(
            str(path), options={"protocol_whitelist": "file", "format_whitelist": "wav,mp3,mov"}
        ) as source:
            if len(source.streams.audio) != 1:
                raise InvalidSample("one audio stream is required")
            for frame in source.decode(audio=0):
                for output in resampler.resample(frame):
                    samples = output.to_ndarray().reshape(-1)
                    count += len(samples)
                    if count > MAX_SECONDS * RATE or not np.isfinite(samples).all():
                        raise InvalidSample("audio exceeds supported limits")
                    chunks.append(samples)
            for output in resampler.resample(None):
                samples = output.to_ndarray().reshape(-1)
                count += len(samples)
                if count > MAX_SECONDS * RATE or not np.isfinite(samples).all():
                    raise InvalidSample("audio exceeds supported limits")
                chunks.append(samples)
    except (av.error.FFmpegError, ValueError, EOFError) as error:
        raise InvalidSample("unsupported or invalid recording") from error
    if count < RATE // 2:
        raise InvalidSample("too little audio")
    return np.concatenate(chunks)


def exclusive_intervals(segments, duration_ms: int) -> list[dict]:
    """Keep only time spans attributed to one speaker; exclude overlapping speech."""
    events = defaultdict(list)
    speakers = {}
    for segment in segments:
        speaker = speakers.setdefault(segment.speaker, f"spk_{len(speakers) + 1}")
        if len(speakers) > 8:
            raise InvalidSample("too many speaker clusters")
        start = max(0, round(segment.start * 1000))
        end = min(duration_ms, round(segment.end * 1000))
        if end > start:
            events[start].append((speaker, 1))
            events[end].append((speaker, -1))
    active = {}
    previous = 0
    intervals = []
    for moment, changes in sorted(events.items()):
        if len(active) == 1 and moment > previous:
            speaker = next(iter(active))
            if (
                intervals
                and intervals[-1]["speaker_id"] == speaker
                and intervals[-1]["end_ms"] == previous
            ):
                intervals[-1]["end_ms"] = moment
            else:
                intervals.append({"speaker_id": speaker, "start_ms": previous, "end_ms": moment})
        for speaker, change in changes:
            active[speaker] = active.get(speaker, 0) + change
            if not active[speaker]:
                del active[speaker]
        previous = moment
    intervals = [item for item in intervals if item["end_ms"] - item["start_ms"] >= 300]
    if not intervals or len(intervals) > 2000:
        raise InvalidSample("no usable exclusive speaker intervals")
    return intervals


class Models:
    def __init__(self, assets: Path):
        self.assets = assets
        self._whisper = None
        self._encoder = None
        self._tokenizer = None
        self._diarizer = None

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
            start = min(
                round((first / RATE + segment.start) * 1000), len(channel) * 1000 // RATE - 1
            )
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

    def recording_audio_prep(self, path: Path) -> list[dict]:
        samples = read_mono(path)
        if self._diarizer is None:
            config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                        model=str(self.assets / "diarization/segmentation.onnx")
                    ),
                    num_threads=2,
                ),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(self.assets / "diarization/embedding.onnx"), num_threads=2
                ),
                clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.5),
                min_duration_on=0.3,
                min_duration_off=0.5,
            )
            if not config.validate():
                raise InvalidSample("diarization model configuration is invalid")
            self._diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
        segments = self._diarizer.process(samples).sort_by_start_time()
        return exclusive_intervals(segments, len(samples) * 1000 // RATE)

    def recording_text_analysis(self, path: Path, speaker: str, intervals: list[dict]) -> dict:
        samples = read_mono(path)
        turns = []
        for interval in intervals:
            if interval["speaker_id"] != speaker:
                continue
            start, end = interval["start_ms"], interval["end_ms"]
            if not 0 <= start < end <= len(samples) * 1000 // RATE:
                raise InvalidSample("invalid confirmed speaker interval")
            clip = samples[start * RATE // 1000 : end * RATE // 1000]
            for turn in self.transcribe(clip, speaker):
                turn["start_ms"] += start
                turn["end_ms"] = min(end, turn["end_ms"] + start)
                turns.append(turn)
        return self.classify_turns(turns)

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
        batches = []
        for offset in range(0, len(texts), 16):
            encodings = self._tokenizer.encode_batch(texts[offset : offset + 16])
            inputs = {
                "input_ids": np.array([item.ids for item in encodings], dtype=np.int64),
                "attention_mask": np.array(
                    [item.attention_mask for item in encodings], dtype=np.int64
                ),
                "token_type_ids": np.array([item.type_ids for item in encodings], dtype=np.int64),
            }
            hidden = self._encoder.run(
                None, {i.name: inputs[i.name] for i in self._encoder.get_inputs()}
            )[0]
            mask = inputs["attention_mask"][:, :, None].astype(np.float32)
            pooled = (hidden * mask).sum(axis=1) / mask.sum(axis=1)
            batches.append(pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9))
        return np.concatenate(batches)

    def text_analysis(self, path: Path, speaker: str) -> dict:
        channel = read_channels(path)[SPEAKERS.index(speaker)]
        turns = self.transcribe(channel, speaker)
        return self.classify_turns(turns)

    def classify_turns(self, turns: list[dict]) -> dict:
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
