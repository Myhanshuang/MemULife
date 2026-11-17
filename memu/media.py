"""Native media handling. Query media stays temporary and never enters the repository."""

from __future__ import annotations

import base64
import json
import math
import subprocess
import tempfile
from pathlib import Path


# Bytes may be hostile even when the caller labels them as audio/video. Keep
# probes and decoders on concrete media demuxers: hls, dash, concat, image2
# sequences and other resource-list formats are deliberately absent. Network
# protocols are disabled. MOV external tracks/absolute references stay disabled
# by FFmpeg's defaults; none of these calls enables them.
_INPUT_OPTIONS = (
    "-protocol_whitelist", "file,pipe",
    "-format_whitelist",
    "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,wav,mp3,flac,ogg,aac,avi,"
    "aiff,au,amr,nut,mpeg,mpegts,flv,png_pipe,jpeg_pipe,webp_pipe",
)


def decode_media(item: dict, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    """Only caller-provided bytes, never server-side paths or arbitrary URLs."""
    if set(item) - {"data_base64", "mime_type", "filename"}:
        raise ValueError("Media accepts only data_base64, mime_type, filename")
    mime = item.get("mime_type", "")
    if mime.split("/")[0] not in {"image", "audio", "video"}:
        raise ValueError("Unsupported media MIME type")
    value = item.get("data_base64", "")
    if not isinstance(value, str) or len(value) > math.ceil(max_bytes / 3) * 4:
        raise ValueError("Media exceeds input size limit")
    data = base64.b64decode(value, validate=True)
    if not data or len(data) > max_bytes:
        raise ValueError("Empty or oversized media")
    return data


def probe_duration(data: bytes, mime_type: str) -> float | None:
    if mime_type.startswith("image/"):
        return None
    with tempfile.TemporaryDirectory(prefix="memu-probe-") as tmp:
        path = Path(tmp) / "input"
        path.write_bytes(data)
        result = subprocess.run(
            ["ffprobe", "-v", "error", *_INPUT_OPTIONS,
             "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, check=True, timeout=30,
        )
        duration = float(json.loads(result.stdout)["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Media duration is not positive and finite")
        return duration


def native_item(data: bytes, mime_type: str, start: float | None = None,
                end: float | None = None) -> dict:
    kind = mime_type.split("/")[0]
    if start is not None:
        if kind not in {"audio", "video"} or end is None or not 0 <= start < end:
            raise ValueError("Invalid native media segment")
        with tempfile.TemporaryDirectory(prefix="memu-segment-") as tmp:
            original = Path(tmp) / "input"
            original.write_bytes(data)
            output = Path(tmp) / ("clip.wav" if kind == "audio" else "clip.mp4")
            codec = ["-vn", "-c:a", "pcm_s16le"] if kind == "audio" else [
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-movflags", "+faststart"
            ]
            subprocess.run(
                ["ffmpeg", "-v", "error", "-nostdin", "-y", *_INPUT_OPTIONS,
                 "-ss", str(start), "-i", str(original),
                 "-t", str(end - start), *codec, str(output)],
                capture_output=True, check=True, timeout=180,
            )
            data = output.read_bytes()
            mime_type = "audio/wav" if kind == "audio" else "video/mp4"
    result = {"kind": kind, "mime_type": mime_type, "data_base64": base64.b64encode(data).decode()}
    if start is not None:
        result.update(start=start, end=end)
    return result


def redact_media(data: bytes, mime_type: str,
                 ranges: list[tuple[float, float]]) -> tuple[bytes, str]:
    """Erase selected audio/video content while retaining the original timeline.

    Audio is muted sample by sample. Every video frame whose display interval
    intersects a selected range becomes black, including boundary frames. This
    intentionally respects the source's frame precision instead of leaving a
    partial sensitive frame visible. Audio-only output is PCM WAV; video output
    is lossless H.264/ALAC MP4. Extra metadata, subtitles, and attachments are not
    copied into the redacted resource. No repository file is changed here.
    """
    kind = mime_type.split("/", 1)[0]
    if kind not in {"audio", "video"}:
        raise ValueError("Time-range redaction requires audio or video")
    if not data:
        raise ValueError("Cannot redact empty media")
    if not ranges:
        return data, mime_type
    selected = sorted((float(start), float(end)) for start, end in ranges)
    if any(not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end
           for start, end in selected):
        raise ValueError("Redaction ranges must be finite, positive intervals")
    merged: list[list[float]] = []
    for start, end in selected:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    with tempfile.TemporaryDirectory(prefix="memu-redact-") as tmp:
        original = Path(tmp) / "input"
        original.write_bytes(data)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", *_INPUT_OPTIONS,
             "-show_streams", "-show_format", "-of", "json", str(original)],
            capture_output=True, check=True, timeout=30,
        )
        info = json.loads(probe.stdout)
        duration = float(info["format"]["duration"])
        origin = float(info["format"].get("start_time", 0))
        if not math.isfinite(duration) or duration <= 0 or any(end > duration for _, end in merged):
            raise ValueError("Redaction range exceeds original media duration")
        videos = [stream for stream in info["streams"] if stream["codec_type"] == "video"]
        audios = [stream for stream in info["streams"] if stream["codec_type"] == "audio"]
        if kind == "audio" and (len(audios) != 1 or videos):
            raise ValueError("Audio range redaction requires one audio stream without video attachments")
        if kind == "video" and not videos:
            raise ValueError("The supplied video contains no video stream")
        output = Path(tmp) / ("redacted.wav" if kind == "audio" else "redacted.mp4")
        command = ["ffmpeg", "-v", "error", "-nostdin", "-y", *_INPUT_OPTIONS, "-i", str(original)]
        audio_condition = "+".join(f"gte(t,{start:.12g})*lt(t,{end:.12g})" for start, end in merged)
        audio_filter = f"aeval=exprs='if({audio_condition},0,val(ch))':c=same"
        if kind == "audio":
            command += ["-map", "0:a:0", "-af", audio_filter, "-c:a", "pcm_s32le"]
        else:
            frame_probe = subprocess.run(
                ["ffprobe", "-v", "error", *_INPUT_OPTIONS, "-select_streams", "v", "-show_frames",
                 "-show_entries", "frame=stream_index,best_effort_timestamp_time,pkt_duration_time",
                 "-of", "json", str(original)], capture_output=True, check=True, timeout=180,
            )
            frames = json.loads(frame_probe.stdout)["frames"]
            command += ["-map", "0:v", "-map", "0:a?", "-c:v", "libx264", "-preset", "ultrafast",
                        "-crf", "0", "-c:a", "alac", "-movflags", "+faststart"]
            for output_index, stream in enumerate(videos):
                stream_frames = [frame for frame in frames if frame["stream_index"] == stream["index"]]
                if not stream_frames or any("best_effort_timestamp_time" not in frame for frame in stream_frames):
                    raise ValueError("Video frame timestamps are required for safe range redaction")
                times = [float(frame["best_effort_timestamp_time"]) - origin for frame in stream_frames]
                if any(not math.isfinite(value) for value in times) or any(a > b for a, b in zip(times, times[1:])):
                    raise ValueError("Video timestamps must be finite and chronological")
                redacted = []
                for number, (frame, begin) in enumerate(zip(stream_frames, times)):
                    finish = times[number + 1] if number + 1 < len(times) else begin + float(frame.get("pkt_duration_time", 0))
                    if finish <= begin:
                        finish = duration
                    if any(begin < end and finish > start for start, end in merged):
                        redacted.append(number)
                spans: list[list[int]] = []
                for number in redacted:
                    if spans and number == spans[-1][1] + 1:
                        spans[-1][1] = number
                    else:
                        spans.append([number, number])
                enabled = "+".join(f"between(n,{start},{end})" for start, end in spans) or "0"
                command += [f"-filter:v:{output_index}",
                            f"drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='{enabled}'",
                            f"-fps_mode:v:{output_index}", "passthrough"]
            for output_index in range(len(audios)):
                command += [f"-filter:a:{output_index}", audio_filter]
        command += ["-map_metadata", "-1", "-map_chapters", "-1", "-t", str(duration), str(output)]
        subprocess.run(command, capture_output=True, check=True, timeout=180)
        redacted_bytes = output.read_bytes()
        new_mime = "audio/wav" if kind == "audio" else "video/mp4"
        actual_duration = probe_duration(redacted_bytes, new_mime)
        # Allow only one timestamp tick / audio sample of container rounding.
        tolerance = max([1 / int(stream.get("sample_rate", 48000)) for stream in audios] + [0.002])
        if actual_duration is None or abs(actual_duration - duration) > tolerance:
            raise ValueError("Redaction changed media duration; original resource was retained")
        return redacted_bytes, new_mime
