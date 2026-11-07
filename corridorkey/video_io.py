from __future__ import annotations

import glob
import json
import math
import os
from fractions import Fraction
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import cv2
import numpy as np
import torch

try:
    import OpenEXR  # OpenEXR>=3.3; required for valid DWAA / DWAB writing
except Exception:  # regular compression remains available through OpenCV
    OpenEXR = None

from . import color_utils as cu


def _write_openexr_dwa(path: str, frame: np.ndarray, *, dtype: str, compression: str) -> None:
    """Write one HxW, HxWx3 BGR, or HxWx4 BGRA float frame using OpenEXR DWA."""
    if OpenEXR is None or not hasattr(OpenEXR, "File"):
        raise RuntimeError(
            f"EXR compression {compression} requires OpenEXR>=3.3. "
            "Install it with `python -m pip install OpenEXR`."
        )
    enum_name = f"{compression}_COMPRESSION"
    if not hasattr(OpenEXR, enum_name):
        raise RuntimeError(f"Installed OpenEXR package does not support {compression} compression")
    pixels = np.asarray(frame)
    cast = np.float16 if dtype == "half" else np.float32
    pixels = np.ascontiguousarray(pixels.astype(cast, copy=False))
    if pixels.ndim == 2:
        channels = {"Y": pixels}
    elif pixels.ndim == 3 and pixels.shape[-1] == 1:
        channels = {"Y": pixels[..., 0]}
    elif pixels.ndim == 3 and pixels.shape[-1] == 3:
        # Writer receives BGR data to stay consistent with OpenCV convention.
        channels = {"B": pixels[..., 0], "G": pixels[..., 1], "R": pixels[..., 2]}
    elif pixels.ndim == 3 and pixels.shape[-1] == 4:
        channels = {"B": pixels[..., 0], "G": pixels[..., 1],
                    "R": pixels[..., 2], "A": pixels[..., 3]}
    else:
        raise ValueError(f"Unsupported EXR frame shape for {compression}: {pixels.shape}")
    header = {"compression": getattr(OpenEXR, enum_name), "type": OpenEXR.scanlineimage}
    with OpenEXR.File(header, channels) as exr_file:
        exr_file.write(path)


@dataclass
class WriterConfig:
    ffmpeg_bin: str = "ffmpeg"
    codec: str = "libx264"
    crf: int = 12
    preset: str = "medium"
    pix_fmt: str = "yuv444p10le"
    bitrate: str | None = None
    threads: int = 2


_CORRIDORKEY_FFMPEG_BIN = "ffmpeg"


def _resolve_tool(binary: str) -> str | None:
    """Resolve a multimedia executable, including a sibling of a configured FFmpeg."""
    value = str(binary or "").strip().strip('"')
    if not value:
        return None
    found = shutil.which(value)
    if found:
        return str(Path(found).resolve())
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    return None


def _ffprobe_binary() -> str | None:
    ffmpeg_path = _resolve_tool(_CORRIDORKEY_FFMPEG_BIN)
    if ffmpeg_path:
        sibling = Path(ffmpeg_path).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.is_file():
            return str(sibling.resolve())
    return _resolve_tool("ffprobe")


def _probe_video_with_ffprobe(video_path: str) -> dict:
    """Read video stream metadata without relying on OpenCV's bundled FFmpeg."""
    ffprobe = _ffprobe_binary()
    if not ffprobe:
        raise FileNotFoundError(
            "ffprobe was not found. Install the FFmpeg full build (ffmpeg.exe and ffprobe.exe together), "
            "or add its bin folder to PATH.")
    source = Path(video_path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"Input video does not exist: {source}")
    command = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration:format=duration",
        "-of", "json", str(source),
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(command, capture_output=True, text=True, timeout=60,
                            creationflags=flags, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffprobe returned no details").strip()
        raise RuntimeError(f"FFmpeg could not inspect the input video:\n{detail}")
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams") or []
        if not streams:
            raise ValueError("No video stream was found")
        stream = streams[0]
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
        if width <= 0 or height <= 0:
            raise ValueError("The video stream has invalid dimensions")

        fps_text = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1")
        try:
            fps = float(Fraction(fps_text))
        except (ValueError, ZeroDivisionError, TypeError):
            fps = 0.0
        if not math.isfinite(fps) or fps <= 0:
            fps_text = str(stream.get("r_frame_rate") or "24/1")
            try:
                fps = float(Fraction(fps_text))
            except (ValueError, ZeroDivisionError, TypeError):
                fps = 24.0

        count = 0
        raw_count = stream.get("nb_frames")
        if raw_count not in (None, "N/A", ""):
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                count = 0
        duration = stream.get("duration")
        if duration in (None, "N/A", ""):
            duration = (payload.get("format") or {}).get("duration")
        try:
            duration_value = float(duration) if duration not in (None, "N/A", "") else 0.0
        except (TypeError, ValueError):
            duration_value = 0.0
        if count <= 0 and duration_value > 0 and fps > 0:
            count = max(1, int(round(duration_value * fps)))
        if count <= 0:
            # Rare containers omit both nb_frames and duration. Count only in this
            # fallback case, avoiding an unnecessary full preliminary decode for MP4/MOV.
            count_command = [
                ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                "-show_entries", "stream=nb_read_frames", "-of", "json", str(source),
            ]
            counted = subprocess.run(count_command, capture_output=True, text=True,
                                     timeout=300, creationflags=flags, check=False)
            if counted.returncode == 0:
                streams_counted = (json.loads(counted.stdout).get("streams") or [])
                if streams_counted:
                    try:
                        count = int(streams_counted[0].get("nb_read_frames") or 0)
                    except (TypeError, ValueError):
                        count = 0
        if count <= 0:
            raise ValueError("Could not determine the video frame count")
        return {"width": width, "height": height, "fps": fps, "frame_count": count}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read video metadata for {source}: {exc}") from exc


class FFmpegVideoCapture:
    """OpenCV-compatible sequential reader backed by the system FFmpeg executable.

    This is used when OpenCV's own VideoCapture backend cannot initialise the
    Windows video decoder. Frames are streamed as BGR24, matching cv2.VideoCapture.
    """
    def __init__(self, video_path: str, start_frame: int = 0):
        self.video_path = str(Path(video_path).expanduser())
        self.meta = _probe_video_with_ffprobe(self.video_path)
        self.width = int(self.meta["width"])
        self.height = int(self.meta["height"])
        self.fps = float(self.meta["fps"])
        self.frame_count = int(self.meta["frame_count"])
        self.start_frame = max(0, int(start_frame))
        self.position = 0
        self.process = None
        self._prefetched_frame = None
        self._start()
        # Validate that the source really decodes before handing the reader to
        # the model. If it does not, open_video_capture can still try OpenCV.
        first = self._read_raw_frame()
        if first is None:
            self.release()
            raise RuntimeError(f"FFmpeg could not decode the first frame of {self.video_path}")
        self._prefetched_frame = first

    def _start(self) -> None:
        ffmpeg = _resolve_tool(_CORRIDORKEY_FFMPEG_BIN)
        if not ffmpeg:
            raise FileNotFoundError(f"FFmpeg executable not found: {_CORRIDORKEY_FFMPEG_BIN}")
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-i", self.video_path, "-map", "0:v:0", "-an", "-sn", "-dn"]
        if self.start_frame > 0:
            # select operates on decoded frame numbers, so frame-index seeking remains
            # exact even for clips whose timestamps are not perfectly CFR.
            command += ["-vf", f"select=gte(n\\,{self.start_frame})"]
        command += ["-vsync", "0", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, bufsize=2 ** 20, creationflags=flags)
        # Do not reject a very short clip merely because FFmpeg has already
        # finished writing its frames into the pipe buffer.

    def isOpened(self) -> bool:
        return self.process is not None

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.width)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.height)
        if prop == cv2.CAP_PROP_FPS:
            return float(self.fps)
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return float(self.frame_count)
        if prop == cv2.CAP_PROP_POS_FRAMES:
            return float(self.start_frame + self.position)
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        if prop != cv2.CAP_PROP_POS_FRAMES:
            return False
        target = max(0, min(int(round(float(value))), max(0, self.frame_count - 1)))
        if target == self.start_frame and self.position == 0:
            return True
        self.release()
        self.start_frame = target
        self.position = 0
        self._prefetched_frame = None
        self._start()
        first = self._read_raw_frame()
        if first is None:
            self.release()
            return False
        self._prefetched_frame = first
        return True

    def _read_raw_frame(self):
        if self.process is None or self.process.stdout is None:
            return None
        frame_bytes = self.width * self.height * 3
        data = bytearray()
        try:
            while len(data) < frame_bytes:
                chunk = self.process.stdout.read(frame_bytes - len(data))
                if not chunk:
                    return None
                data.extend(chunk)
            return np.frombuffer(data, dtype=np.uint8).reshape(self.height, self.width, 3).copy()
        except (OSError, ValueError):
            return None

    def read(self):
        if self.process is None:
            return False, None
        if self._prefetched_frame is not None:
            frame = self._prefetched_frame
            self._prefetched_frame = None
        else:
            frame = self._read_raw_frame()
            if frame is None:
                return False, None
        self.position += 1
        return True, frame

    def release(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass


def open_video_capture(video_path: str):
    """Prefer FFmpeg CLI decoding; fall back to OpenCV only if FFmpeg tools are absent."""
    ffmpeg = _resolve_tool(_CORRIDORKEY_FFMPEG_BIN)
    ffprobe = _ffprobe_binary()
    if ffmpeg and ffprobe:
        try:
            return FFmpegVideoCapture(video_path)
        except Exception as ffmpeg_exc:
            # OpenCV remains a compatibility fallback for unusual containers when
            # system FFmpeg metadata probing cannot read the clip.
            cv_cap = cv2.VideoCapture(str(video_path))
            if cv_cap.isOpened():
                return cv_cap
            cv_cap.release()
            raise RuntimeError(
                f"Could not open video: {video_path}\n"
                f"FFmpeg reader: {ffmpeg_exc}\n"
                "OpenCV VideoCapture could not open it either. Check that the file is a "
                "valid video and that its codec is supported by your FFmpeg build.") from ffmpeg_exc
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(
            f"Could not open video: {video_path}. Install the FFmpeg full build "
            "(ffmpeg.exe + ffprobe.exe) or use a supported video codec.")
    return cap


def require_ffmpeg(ffmpeg_bin: str) -> None:
    global _CORRIDORKEY_FFMPEG_BIN
    resolved = _resolve_tool(ffmpeg_bin)
    if not resolved:
        raise FileNotFoundError(
            f"ffmpeg binary not found: {ffmpeg_bin}. "
            "Install ffmpeg or pass --ffmpeg_bin.")
    _CORRIDORKEY_FFMPEG_BIN = resolved


def safe_remove(path: str | os.PathLike | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def none_path(path: str | None) -> str | None:
    if path is None:
        return None
    text = str(path).strip()
    if text.lower() in ("", "none", "null", "off", "false", "-"):
        return None
    return str(path)


def raw_clip_name(path: str | os.PathLike) -> str:
    norm = os.path.normpath(str(path))
    name = os.path.splitext(os.path.basename(norm))[0]
    if name.lower() in {"input", "frames", "rgb"}:
        return os.path.basename(os.path.dirname(norm)) or name
    return name


_IMAGE_FRAME_EXTENSIONS = {".exr", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


def frame_paths(frame_dir: str | os.PathLike) -> list[str]:
    """Return ordered image frames, accepting either a folder or one still image.

    CorridorKey uses the frame-directory reader for both image sequences and
    single still inputs. A still path must therefore be returned as a one-item
    frame list rather than being globbed as though it were a directory.
    """
    source = Path(frame_dir).expanduser()
    if source.is_file():
        return [str(source.resolve())] if source.suffix.lower() in _IMAGE_FRAME_EXTENSIONS else []
    if not source.is_dir():
        return []
    paths: list[str] = []
    for pattern in ("*.exr", "*.png", "*.jpg", "*.jpeg", "*.tif", "*.tiff", "*.bmp"):
        paths.extend(glob.glob(str(source / pattern)))
    return sorted(paths, key=lambda item: (Path(item).name.casefold(), item))


def ceil_to_multiple(x: int, q: int) -> int:
    return max(q, ((int(x) + q - 1) // q) * q)


def scaled_hw(h: int, w: int, scale: float) -> tuple[int, int]:
    scale = float(scale)
    if scale <= 0:
        raise ValueError(f"scale must be > 0, got {scale}")
    return max(1, int(round(h * scale))), max(1, int(round(w * scale)))


def resize_hwc_lanczos(img: np.ndarray, hw: tuple[int, int]) -> np.ndarray:
    h, w = hw
    if img.shape[:2] == (h, w):
        return img
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_LANCZOS4)


def resize_chw_lanczos(x: np.ndarray, hw: tuple[int, int]) -> np.ndarray:
    h, w = hw
    if x.shape[-2:] == (h, w):
        return x
    if x.shape[0] == 1:
        y = cv2.resize(x[0], (w, h), interpolation=cv2.INTER_LANCZOS4)
        return y[None].astype(x.dtype, copy=False)
    y = cv2.resize(np.moveaxis(x, 0, -1), (w, h),
                   interpolation=cv2.INTER_LANCZOS4)
    return np.moveaxis(y, -1, 0).astype(x.dtype, copy=False)


def center_pad_to_shape(img: np.ndarray, h_out: int, w_out: int):
    h, w = img.shape[:2]
    pad_t = (h_out - h) // 2
    pad_b = h_out - h - pad_t
    pad_l = (w_out - w) // 2
    pad_r = w_out - w - pad_l
    if pad_t or pad_b or pad_l or pad_r:
        img = cv2.copyMakeBorder(
            img, pad_t, pad_b, pad_l, pad_r, cv2.BORDER_REFLECT_101)
    return img, (pad_l, pad_r, pad_t, pad_b)


def crop_chw_to_native(x: np.ndarray, native_hw: tuple[int, int], pad):
    h, w = native_hw
    pad_l, _, pad_t, _ = pad
    return x[..., pad_t:pad_t + h, pad_l:pad_l + w]


def crop_model_chw_to_output(x: np.ndarray, model_native_hw, pad, output_hw):
    cropped = crop_chw_to_native(x, model_native_hw, pad)
    return resize_chw_lanczos(cropped, output_hw)


def read_image_rgb(path: str | os.PathLike):
    """Read EXR or display-coded stills as RGB float32 without bit-depth loss."""
    path = str(path)
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Could not read image frame: {path}")

    is_exr = Path(path).suffix.lower() == ".exr"
    if is_exr:
        # EXR values are already scene-linear float values; do not normalize.
        img = img.astype(np.float32, copy=False)
    elif img.dtype == np.uint8:
        img = img.astype(np.float32) / 255.0
    elif img.dtype == np.uint16:
        img = img.astype(np.float32) / 65535.0
    else:
        img = img.astype(np.float32, copy=False)

    # Normalize image channel layouts to RGB; input alpha is not used by the
    # matting model, while output Cutout alpha remains fully preserved.
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    elif img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
    elif img.ndim == 3 and img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    elif img.ndim != 3 or img.shape[2] != 3:
        raise ValueError(f"Unsupported image channel layout {img.shape} in {path}")
    return img


def read_hint_image(path: str | os.PathLike) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(path)
    if img.dtype == np.uint8:
        img = img.astype(np.float32) / 255.0
    elif img.dtype == np.uint16:
        img = img.astype(np.float32) / 65535.0
    else:
        img = img.astype(np.float32)
    if img.ndim == 3:
        img = img[:, :, 3] if img.shape[2] == 4 else img[:, :, 0]
    return np.clip(img, 0.0, 1.0)


def has_linear_adjust(meta: dict) -> bool:
    return (
        abs(float(meta.get("linear_brightness", 1.0)) - 1.0) > 1e-6
        or abs(float(meta.get("linear_contrast", 1.0)) - 1.0) > 1e-6
    )


def apply_linear_adjust(linear: np.ndarray, meta: dict) -> np.ndarray:
    gain = float(meta.get("linear_brightness", 1.0))
    contrast = float(meta.get("linear_contrast", 1.0))
    if abs(gain - 1.0) <= 1e-6 and abs(contrast - 1.0) <= 1e-6:
        return linear
    pivot = float(meta.get("linear_contrast_pivot", 0.18))
    return gain * (pivot + contrast * (linear - pivot))


def invert_linear_adjust(linear: np.ndarray, meta: dict) -> np.ndarray:
    gain = float(meta.get("linear_brightness", 1.0))
    contrast = float(meta.get("linear_contrast", 1.0))
    if abs(gain - 1.0) <= 1e-6 and abs(contrast - 1.0) <= 1e-6:
        return linear
    pivot = float(meta.get("linear_contrast_pivot", 0.18))
    return (linear / gain - pivot) / contrast + pivot


def probe_video_meta(video_path: str, *, num_frames: int, shape_quantum: int,
                     native_resolution: bool, start_mode: str, seed: int,
                     model_scale: float, linear_brightness: float,
                     linear_contrast: float,
                     linear_contrast_pivot: float) -> dict:
    cap = open_video_capture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    try:
        native_fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
        w_native = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h_native = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        n_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        cap.release()
    if w_native <= 0 or h_native <= 0 or n_total <= 0:
        raise RuntimeError(
            f"Video metadata is incomplete for {video_path}: "
            f"{w_native}x{h_native}, {n_total} frames, {native_fps} fps. "
            "Check the file with FFmpeg or transcode it to H.264 MP4.")

    q = int(shape_quantum)
    if native_resolution:
        h_model, w_model = scaled_hw(h_native, w_native, model_scale)
        h_out = ceil_to_multiple(h_model, q)
        w_out = ceil_to_multiple(w_model, q)
        pad_l = (w_out - w_model) // 2
        pad_r = w_out - w_model - pad_l
        pad_t = (h_out - h_model) // 2
        pad_b = h_out - h_model - pad_t
        output_hw = (h_native, w_native)
        native_hw = (h_native, w_native)
    else:
        raise ValueError("non-native inference is intentionally not exposed")

    n_to_load = n_total if num_frames <= 0 else min(num_frames, n_total)
    start_frame = 0
    max_start = max(n_total - n_to_load, 0)
    if max_start > 0 and num_frames > 0:
        if start_mode == "middle":
            start_frame = max_start // 2
        elif start_mode == "random_middle":
            lo = max_start // 4
            hi = (3 * max_start) // 4
            start_frame = int(np.random.default_rng(int(seed)).integers(lo, hi + 1))
        elif start_mode != "begin":
            raise ValueError(f"unknown start mode {start_mode!r}")

    meta = {
        "native_fps": native_fps,
        "native_hw": native_hw,
        "output_hw": output_hw,
        "model_native_hw": (h_model, w_model),
        "model_scale": float(model_scale),
        "source_hw": (h_native, w_native),
        "shape_hw": (h_out, w_out),
        "start_frame": start_frame,
        "num_frames": n_to_load,
        "pad": (pad_l, pad_r, pad_t, pad_b),
        "native_resolution": True,
        "linear_brightness": float(linear_brightness),
        "linear_contrast": float(linear_contrast),
        "linear_contrast_pivot": float(linear_contrast_pivot),
    }
    print(f"  probed {n_to_load} frames @ {w_out}x{h_out} "
          f"(native {w_native}x{h_native}, fps {native_fps:.2f}, "
          f"start={start_frame}, pad={meta['pad']})")
    return meta


def _read_frame_dir_views(path: str, *, exr_linear: bool):
    raw = read_image_rgb(path)
    if exr_linear:
        linear = raw.astype(np.float32, copy=False)
        display = np.clip(cu.linear_to_srgb(np.maximum(linear, 0.0)), 0.0, 1.0)
    else:
        display = np.clip(raw.astype(np.float32, copy=False), 0.0, 1.0)
        linear = cu.srgb_to_linear(display)
    return display, linear


def probe_frame_dir_meta(frame_dir: str, *, num_frames: int,
                         shape_quantum: int, fps: float,
                         exr_linear: bool, model_scale: float) -> dict:
    paths = frame_paths(frame_dir)
    if not paths:
        raise RuntimeError(f"No frames found under {frame_dir}")
    if num_frames > 0:
        paths = paths[:num_frames]
    first_display, _ = _read_frame_dir_views(paths[0], exr_linear=exr_linear)
    h_native, w_native = first_display.shape[:2]
    h_model, w_model = scaled_hw(h_native, w_native, model_scale)
    h_out = ceil_to_multiple(h_model, shape_quantum)
    w_out = ceil_to_multiple(w_model, shape_quantum)
    pad_l = (w_out - w_model) // 2
    pad_r = w_out - w_model - pad_l
    pad_t = (h_out - h_model) // 2
    pad_b = h_out - h_model - pad_t
    meta = {
        "frame_paths": paths,
        "native_fps": float(fps),
        "native_hw": (h_native, w_native),
        "output_hw": (h_native, w_native),
        "model_native_hw": (h_model, w_model),
        "model_scale": float(model_scale),
        "source_hw": (h_native, w_native),
        "shape_hw": (h_out, w_out),
        "start_frame": 0,
        "num_frames": len(paths),
        "pad": (pad_l, pad_r, pad_t, pad_b),
        "native_resolution": True,
        "exr_linear": bool(exr_linear),
        "linear_brightness": 1.0,
        "linear_contrast": 1.0,
        "linear_contrast_pivot": 0.18,
    }
    print(f"  probed {len(paths)} frames from {frame_dir} @ {w_out}x{h_out} "
          f"(native {w_native}x{h_native}, fps {fps:.2f}, "
          f"exr_linear={exr_linear}, pad={meta['pad']})")
    return meta


def preprocess_video_frame_views(frame_bgr, meta: dict, *, need_model: bool):
    h_out, w_out = meta["shape_hw"]
    h_model, w_model = meta["model_native_hw"]
    frame = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    frame = resize_hwc_lanczos(frame, (h_model, w_model))
    frame, _ = center_pad_to_shape(frame, h_out, w_out)
    review = torch.from_numpy(frame.transpose(2, 0, 1)).to(dtype=torch.bfloat16)
    if not need_model and not has_linear_adjust(meta):
        return None, review, review
    linear = cu.srgb_to_linear(frame)
    linear_model = apply_linear_adjust(linear, meta)
    if has_linear_adjust(meta):
        foundation_np = cu.linear_to_vfm_sdr(linear_model)
        foundation = torch.from_numpy(
            foundation_np.transpose(2, 0, 1)).to(dtype=torch.bfloat16)
    else:
        foundation = review
    if not need_model:
        return None, foundation, review
    model = cu.linear_to_asinh(linear_model)
    model_t = torch.from_numpy(model.transpose(2, 0, 1)).to(dtype=torch.bfloat16)
    return model_t, foundation, review


def preprocess_frame_dir_path(path: str, meta: dict, *, need_model: bool):
    h_out, w_out = meta["shape_hw"]
    h_model, w_model = meta["model_native_hw"]
    display, linear = _read_frame_dir_views(
        path, exr_linear=bool(meta.get("exr_linear", False)))
    display = resize_hwc_lanczos(display, (h_model, w_model))
    linear = resize_hwc_lanczos(linear, (h_model, w_model))
    display, _ = center_pad_to_shape(np.clip(display, 0.0, 1.0), h_out, w_out)
    display_t = torch.from_numpy(
        display.transpose(2, 0, 1)).to(dtype=torch.bfloat16)
    if not need_model:
        return None, display_t, display_t
    linear, _ = center_pad_to_shape(linear, h_out, w_out)
    model = cu.linear_to_asinh(linear)
    model_t = torch.from_numpy(model.transpose(2, 0, 1)).to(dtype=torch.bfloat16)
    return model_t, display_t, display_t


class SequentialVideoWindowReader:
    def __init__(self, video_path: str, meta: dict):
        self.video_path = video_path
        self.meta = meta
        self.cap = open_video_capture(video_path)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open video: {video_path}")
        self.start_frame = int(meta["start_frame"])
        self.n_frames = int(meta["num_frames"])
        if self.start_frame:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.start_frame)
        self.next_t = 0
        self.cache: dict[int, tuple[torch.Tensor, torch.Tensor, torch.Tensor]] = {}

    def close(self):
        self.cap.release()

    def _read_until(self, t_end: int):
        while self.next_t < t_end:
            ret, frame = self.cap.read()
            if not ret:
                raise RuntimeError(
                    f"{self.video_path}: failed decoding frame "
                    f"{self.start_frame + self.next_t}")
            self.cache[self.next_t] = preprocess_video_frame_views(
                frame, self.meta, need_model=True)
            self.next_t += 1

    def window(self, t0: int, t1: int):
        self._read_until(t1)
        model = torch.stack([self.cache[t][0] for t in range(t0, t1)])
        foundation = torch.stack([self.cache[t][1] for t in range(t0, t1)])
        review = torch.stack([self.cache[t][2] for t in range(t0, t1)])
        for old_t in [k for k in self.cache if k < t0]:
            del self.cache[old_t]
        return model, foundation, review


class SequentialFrameDirWindowReader:
    def __init__(self, frame_dir: str, meta: dict):
        self.frame_dir = frame_dir
        self.meta = meta
        self.paths = meta["frame_paths"]
        self.next_t = 0
        self.cache: dict[int, tuple[torch.Tensor, torch.Tensor, torch.Tensor]] = {}

    def close(self):
        pass

    def _read_until(self, t_end: int):
        while self.next_t < t_end:
            self.cache[self.next_t] = preprocess_frame_dir_path(
                self.paths[self.next_t], self.meta, need_model=True)
            self.next_t += 1

    def window(self, t0: int, t1: int):
        self._read_until(t1)
        model = torch.stack([self.cache[t][0] for t in range(t0, t1)])
        foundation = torch.stack([self.cache[t][1] for t in range(t0, t1)])
        review = torch.stack([self.cache[t][2] for t in range(t0, t1)])
        for old_t in [k for k in self.cache if k < t0]:
            del self.cache[old_t]
        return model, foundation, review


def iter_video_frame_chunks(video_path: str, meta: dict, *, chunk_size: int,
                            need_model: bool):
    cap = open_video_capture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    if int(meta["start_frame"]):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(meta["start_frame"]))
    t = 0
    try:
        while t < int(meta["num_frames"]):
            model_list = [] if need_model else None
            foundation_list = []
            t0 = t
            while t < int(meta["num_frames"]) and len(foundation_list) < chunk_size:
                ret, frame = cap.read()
                if not ret:
                    break
                model, foundation, _review = preprocess_video_frame_views(
                    frame, meta, need_model=need_model)
                if need_model:
                    model_list.append(model)
                foundation_list.append(foundation)
                t += 1
            if not foundation_list:
                break
            yield t0, (torch.stack(model_list) if need_model else None), torch.stack(foundation_list)
    finally:
        cap.release()
    if t < int(meta["num_frames"]):
        raise RuntimeError(
            f"{video_path}: decoded only {t}/{meta['num_frames']} requested frames")


def iter_frame_dir_chunks(frame_dir: str, meta: dict, *, chunk_size: int,
                          need_model: bool):
    paths = meta["frame_paths"]
    t = 0
    while t < len(paths):
        t0 = t
        model_list = [] if need_model else None
        foundation_list = []
        while t < len(paths) and len(foundation_list) < chunk_size:
            model, foundation, _review = preprocess_frame_dir_path(
                paths[t], meta, need_model=need_model)
            if need_model:
                model_list.append(model)
            foundation_list.append(foundation)
            t += 1
        yield t0, (torch.stack(model_list) if need_model else None), torch.stack(foundation_list)


def preprocess_hint_frame(frame_bgr, meta: dict) -> torch.Tensor:
    h_out, w_out = meta["shape_hw"]
    h_model, w_model = meta["model_native_hw"]
    if frame_bgr is None:
        raise RuntimeError("hint frame decode failed")
    if frame_bgr.ndim == 3:
        hint = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    else:
        hint = frame_bgr
    if hint.dtype == np.uint8:
        hint = hint.astype(np.float32) / 255.0
    elif hint.dtype == np.uint16:
        hint = hint.astype(np.float32) / 65535.0
    else:
        hint = hint.astype(np.float32)
    if hint.shape[:2] != (h_model, w_model):
        hint = cv2.resize(hint, (w_model, h_model),
                          interpolation=cv2.INTER_LANCZOS4)
    hint, _ = center_pad_to_shape(np.clip(hint, 0.0, 1.0), h_out, w_out)
    return torch.from_numpy(hint[None]).to(dtype=torch.bfloat16)


def load_initial_hint_frame(path_or_dir: str, meta: dict) -> torch.Tensor:
    paths = frame_paths(path_or_dir) if os.path.isdir(path_or_dir) else [path_or_dir]
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        raise RuntimeError(f"No hint frame found at {path_or_dir}")
    hint = read_hint_image(paths[0])
    native_hw = meta["native_hw"]
    model_hw = meta["model_native_hw"]
    if hint.shape[:2] == native_hw and model_hw != native_hw:
        hint = cv2.resize(hint, (model_hw[1], model_hw[0]),
                          interpolation=cv2.INTER_LANCZOS4)
    elif hint.shape[:2] != model_hw:
        raise ValueError(
            f"{paths[0]}: hint shape {hint.shape[:2]} does not match "
            f"native {native_hw} or model {model_hw}")
    hint, pad_check = center_pad_to_shape(
        hint, meta["shape_hw"][0], meta["shape_hw"][1])
    if pad_check != meta["pad"]:
        raise ValueError(f"{paths[0]}: hint pad {pad_check} != input pad {meta['pad']}")
    print(f"  loaded initial frame-1 hint from {paths[0]}")
    return torch.from_numpy(hint[None]).to(dtype=torch.bfloat16)


class SequentialHintVideoWindowReader:
    def __init__(self, hint_video_path: str, meta: dict):
        self.hint_video_path = hint_video_path
        self.meta = meta
        self.cap = open_video_capture(hint_video_path)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open hint video: {hint_video_path}")
        self.start_frame = int(meta["start_frame"])
        self.n_frames = int(meta["num_frames"])
        hint_n_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        required_frames = self.start_frame + self.n_frames
        if hint_n_frames > 0 and hint_n_frames < required_frames:
            raise ValueError(
                f"Hint video {hint_video_path} has only {hint_n_frames} frames, "
                f"but the source video requires {required_frames} frames "
                f"(start={self.start_frame}, num={self.n_frames})")
        if self.start_frame:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.start_frame)
        self.next_t = 0
        self.cache: dict[int, torch.Tensor] = {}

    def close(self):
        self.cap.release()

    def _read_until(self, t_end: int):
        while self.next_t < t_end:
            ret, frame = self.cap.read()
            if not ret:
                raise RuntimeError(
                    f"{self.hint_video_path}: failed decoding hint frame "
                    f"{self.start_frame + self.next_t}")
            self.cache[self.next_t] = preprocess_hint_frame(frame, self.meta)
            self.next_t += 1

    def window(self, t0: int, t1: int):
        self._read_until(t1)
        hint = torch.stack([self.cache[t] for t in range(t0, t1)])
        for old_t in [k for k in self.cache if k < t0]:
            del self.cache[old_t]
        return hint


class SequentialHintFrameDirWindowReader:
    def __init__(self, hint_dir_path: str, meta: dict):
        self.hint_dir_path = hint_dir_path
        self.meta = meta
        self.paths = frame_paths(hint_dir_path)
        if len(self.paths) < int(meta["num_frames"]):
            raise RuntimeError(
                f"{hint_dir_path}: only {len(self.paths)} hint frames for "
                f"{meta['num_frames']} source frames")
        self.next_t = 0
        self.cache: dict[int, torch.Tensor] = {}

    def close(self):
        pass

    def _read_until(self, t_end: int):
        while self.next_t < t_end:
            frame = cv2.imread(self.paths[self.next_t], cv2.IMREAD_UNCHANGED)
            self.cache[self.next_t] = preprocess_hint_frame(frame, self.meta)
            self.next_t += 1

    def window(self, t0: int, t1: int):
        self._read_until(t1)
        hint = torch.stack([self.cache[t] for t in range(t0, t1)])
        for old_t in [k for k in self.cache if k < t0]:
            del self.cache[old_t]
        return hint


def make_hint_reader(path: str | None, meta: dict):
    path = none_path(path)
    if path is None:
        return None
    if os.path.isdir(path):
        return SequentialHintFrameDirWindowReader(path, meta)
    return SequentialHintVideoWindowReader(path, meta)


def window_starts(T_all: int, chunk: int, stride: int) -> list[int]:
    if T_all <= chunk:
        return [0]
    starts = list(range(0, T_all - chunk + 1, stride))
    if starts[-1] != T_all - chunk:
        starts.append(T_all - chunk)
    return starts


def taper_weights(Tc: int, overlap: int, *, is_first: bool, is_last: bool,
                  device=None, dtype=torch.float32) -> torch.Tensor:
    w = torch.ones(Tc, device=device, dtype=dtype)
    if overlap <= 0 or Tc <= 1:
        return w
    n = min(overlap, Tc)
    ramp = 0.5 * (1.0 - torch.cos(
        math.pi * (torch.arange(n, device=device, dtype=dtype) + 1) / (overlap + 1)
    ))
    if not is_first:
        w[:n] = ramp
    if not is_last:
        w[Tc - n:] = ramp.flip(0)
    return w


class ThreadedVideoWriter:
    def __init__(self, path: str, fps: float, size: tuple[int, int],
                 cfg: WriterConfig, *, maxsize: int = 2, channels: int = 3,
                 color_trc: str | None = None):
        self.path = path
        self.fps = float(fps)
        self.size = size
        self.cfg = cfg
        self.maxsize = maxsize
        self.channels = channels
        self.color_trc = color_trc
        self.proc = None
        self.q = queue.Queue(maxsize=maxsize)
        self.err = None
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self._start()

    def _start(self):
        require_ffmpeg(self.cfg.ffmpeg_bin)
        w, h = self.size
        
        in_pix_fmt = "bgra64le" if self.channels == 4 else "bgr48le"
        
        cmd = [
            self.cfg.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-nostats", "-f", "rawvideo", "-pix_fmt", in_pix_fmt,
            "-s:v", f"{w}x{h}", "-r", f"{self.fps:.8f}", "-i", "pipe:0",
            "-an"
        ]
        
        if self.channels == 4:
            cmd.extend([
                "-c:v", "prores_ks",
                "-profile:v", "4",
                "-pix_fmt", "yuva444p10le"
            ])
        else:
            cmd.extend([
                "-c:v", self.cfg.codec,
                "-preset", self.cfg.preset,
                "-pix_fmt", self.cfg.pix_fmt
            ])
            if self.cfg.bitrate:
                cmd.extend(["-b:v", self.cfg.bitrate])
            else:
                cmd.extend(["-crf", str(int(self.cfg.crf))])
                
        trc = self.color_trc if self.color_trc is not None else "iec61966-2-1"
        cmd.extend([
            "-threads", str(max(1, self.cfg.threads)),
            "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", trc, "-movflags", "+write_colr",
            self.path
        ])
        
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self.thread.start()

    def _worker(self):
        try:
            while True:
                item = self.q.get()
                if item is None:
                    self.q.task_done()
                    return
                self.proc.stdin.write(item.tobytes())
                self.q.task_done()
        except Exception as exc:
            self.err = exc

    def write(self, frame: np.ndarray):
        self.write_many(frame[None])

    def write_many(self, frames: np.ndarray):
        if self.err is not None:
            raise self.err
        arr = np.ascontiguousarray(frames)
        if arr.ndim != 4 or arr.shape[-1] != self.channels:
            raise ValueError(
                f"expected batched frames as NHWC uint16 with {self.channels} channels, got {arr.shape}")
        if arr.dtype != np.uint16:
            raise ValueError(f"expected uint16 frames, got {arr.dtype}")
        self.q.put(arr)

    def release(self):
        self.q.put(None)
        self.thread.join()
        if self.proc is not None and self.proc.stdin is not None:
            self.proc.stdin.close()
        rc = self.proc.wait() if self.proc is not None else 0
        if rc != 0 and self.err is None:
            self.err = RuntimeError(f"ffmpeg writer failed with exit code {rc}: {self.path}")
        if self.err is not None:
            raise self.err


class PngSequenceWriter:
    """Write inference frames directly as lossless 16-bit PNG image sequences.

    Input arrays use the same channel convention and already-display-referred
    pixel values as CorridorKey's normal video renderer: uint16 BGR, grayscale
    alpha, or BGRA for cutout. No EXR staging, color transform, or quantization
    pass is performed here.
    """

    def __init__(self, dir_path: str, fps: float, size: tuple[int, int], *,
                 channels: int = 3, maxsize: int = 2, frame_name: str = "frame"):
        if channels not in (1, 3, 4):
            raise ValueError(f"PNG writer supports 1, 3, or 4 channels, got {channels}")
        self.dir_path = str(dir_path)
        self.path = self.dir_path
        self.fps = float(fps)
        self.size = size
        self.channels = channels
        self.frame_name = frame_name
        # Match normal render overwrite behaviour while preventing stale frames
        # from a longer previous sequence from remaining in the new output.
        if os.path.isdir(self.dir_path):
            shutil.rmtree(self.dir_path)
        os.makedirs(self.dir_path, exist_ok=True)
        self.frame_idx = 0
        self.q = queue.Queue(maxsize=maxsize)
        self.err = None
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def _worker(self):
        while True:
            item = self.q.get()
            try:
                if item is None:
                    return
                # After the first write error, keep draining the queue so the
                # producer can finish and release() cannot deadlock.
                if self.err is None:
                    idx0, arr = item
                    for j in range(arr.shape[0]):
                        frame = np.ascontiguousarray(arr[j])
                        if self.channels == 1:
                            frame = frame[..., 0]
                        out_path = os.path.join(
                            self.dir_path, f"{self.frame_name}_{idx0 + j:06d}.png")
                        if not cv2.imwrite(out_path, frame, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
                            raise RuntimeError(f"PNG writer failed: {out_path}")
            except Exception as exc:
                self.err = exc
            finally:
                self.q.task_done()

    def write_many(self, frames: np.ndarray):
        if self.err is not None:
            raise self.err
        arr = np.ascontiguousarray(frames)
        if arr.ndim != 4 or arr.shape[-1] != self.channels:
            raise ValueError(
                f"expected batched frames as NHWC uint16 with {self.channels} channels, got {arr.shape}")
        if arr.dtype != np.uint16:
            raise ValueError(f"expected uint16 PNG frames, got {arr.dtype}")
        self.q.put((self.frame_idx, arr))
        self.frame_idx += arr.shape[0]

    def write(self, frame: np.ndarray):
        self.write_many(frame[None])

    def release(self):
        self.q.put(None)
        self.thread.join()
        if self.err is not None:
            raise self.err


class ExrSequenceWriter:
    """Writes float frames as an OpenEXR image sequence (scene-referred, unclamped).

    Accepts NHWC float32 batches with `channels` channels in BGR(A) order,
    matching the layout convention of ThreadedVideoWriter/cv2. Values are
    written as-is: no clamping and no quantization. `dtype` selects the EXR
    pixel type: "half" (float16, VFX standard) or "float" (full float32).
    """

    def __init__(self, dir_path: str, fps: float, size: tuple[int, int], *,
                 channels: int = 3, dtype: str = "half", compression: str = "ZIP", maxsize: int = 2,
                 frame_name: str = "frame"):
        if dtype not in ("half", "float"):
            raise ValueError(f"exr dtype must be 'half' or 'float', got {dtype!r}")
        compression_map = {
            "ZIP": "IMWRITE_EXR_COMPRESSION_ZIP", "PIZ": "IMWRITE_EXR_COMPRESSION_PIZ",
            "RLE": "IMWRITE_EXR_COMPRESSION_RLE", "ZIPS": "IMWRITE_EXR_COMPRESSION_ZIPS",
            "PXR24": "IMWRITE_EXR_COMPRESSION_PXR24", "B44": "IMWRITE_EXR_COMPRESSION_B44",
            "B44A": "IMWRITE_EXR_COMPRESSION_B44A", "DWAA": "IMWRITE_EXR_COMPRESSION_DWAA",
            "DWAB": "IMWRITE_EXR_COMPRESSION_DWAB", "NONE": "IMWRITE_EXR_COMPRESSION_NO"}
        if compression not in compression_map:
            raise ValueError(f"unsupported EXR compression: {compression!r}")
        compression_attr = compression_map[compression]
        self.compression = compression
        if compression in ("DWAA", "DWAB"):
            if OpenEXR is None or not hasattr(OpenEXR, "File"):
                raise RuntimeError(
                    f"EXR compression {compression} requires OpenEXR>=3.3. "
                    "Install it with `python -m pip install OpenEXR`."
                )
            if not hasattr(OpenEXR, f"{compression}_COMPRESSION"):
                raise ValueError(f"Installed OpenEXR package does not support {compression}")
        elif not hasattr(cv2, compression_attr):
            raise ValueError(f"OpenCV build does not support EXR compression {compression}")
        self.dir_path = str(dir_path)
        self.path = self.dir_path  # for logging parity with ThreadedVideoWriter
        self.size = size
        self.channels = channels
        self.frame_name = frame_name
        self.clamp_half = dtype == "half"
        if compression in ("DWAA", "DWAB"):
            self.imwrite_params = None
        else:
            exr_type = (cv2.IMWRITE_EXR_TYPE_HALF if dtype == "half"
                        else cv2.IMWRITE_EXR_TYPE_FLOAT)
            self.imwrite_params = [
                cv2.IMWRITE_EXR_TYPE, exr_type,
                cv2.IMWRITE_EXR_COMPRESSION, getattr(cv2, compression_attr),
            ]
        os.makedirs(self.dir_path, exist_ok=True)
        self.frame_idx = 0
        self.q = queue.Queue(maxsize=maxsize)
        self.err = None
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def _worker(self):
        try:
            while True:
                item = self.q.get()
                if item is None:
                    self.q.task_done()
                    return
                idx0, arr = item
                if self.clamp_half:
                    # float16 overflows to inf above 65504; keep half EXRs finite
                    arr = np.clip(arr, -65504.0, 65504.0)
                for j in range(arr.shape[0]):
                    out_path = os.path.join(
                        self.dir_path, f"{self.frame_name}_{idx0 + j:06d}.exr")
                    frame = np.ascontiguousarray(arr[j])
                    if frame.shape[-1] == 1:
                        frame = frame[..., 0]
                    if self.compression in ("DWAA", "DWAB"):
                        _write_openexr_dwa(out_path, frame, dtype="half" if self.clamp_half else "float",
                                           compression=self.compression)
                    elif not cv2.imwrite(out_path, frame, self.imwrite_params):
                        raise RuntimeError(f"cv2.imwrite failed: {out_path}")
                self.q.task_done()
        except Exception as exc:
            self.err = exc

    def write_many(self, frames: np.ndarray):
        if self.err is not None:
            raise self.err
        arr = np.ascontiguousarray(frames)
        if arr.ndim != 4 or arr.shape[-1] != self.channels:
            raise ValueError(
                f"expected batched frames as NHWC float32 with "
                f"{self.channels} channels, got {arr.shape}")
        if arr.dtype != np.float32:
            raise ValueError(f"expected float32 frames, got {arr.dtype}")
        self.q.put((self.frame_idx, arr))
        self.frame_idx += arr.shape[0]

    def write(self, frame: np.ndarray):
        self.write_many(frame[None])

    def release(self):
        self.q.put(None)
        self.thread.join()
        if self.err is not None:
            raise self.err


def color_chw_to_bgr(x: np.ndarray) -> np.ndarray:
    x = np.clip(np.moveaxis(x, 0, -1), 0, 1)
    return cv2.cvtColor((x * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)


def gray_1hw_to_bgr(x: np.ndarray) -> np.ndarray:
    x = np.clip(x[0], 0, 1)
    return cv2.cvtColor((x * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


def make_checker(H: int, W: int, size: int = 64,
                 light: float = 0.75, dark: float = 0.5) -> np.ndarray:
    y, x = np.indices((H, W))
    mask = (((y // size) + (x // size)) % 2 == 0).astype(np.float32)
    gray = mask * light + (1.0 - mask) * dark
    return np.stack([gray, gray, gray], axis=0).astype(np.float32)


def composite_over_checker_srgb(fg_srgb_chw: np.ndarray, alpha_1hw: np.ndarray,
                                checker_chw: np.ndarray) -> np.ndarray:
    fg_lin = cu.srgb_to_linear(fg_srgb_chw)
    bg_lin = cu.srgb_to_linear(checker_chw)
    alpha = np.clip(alpha_1hw, 0.0, 1.0)
    comp_lin = fg_lin * alpha + bg_lin * (1.0 - alpha)
    return np.clip(cu.linear_to_srgb(np.maximum(comp_lin, 0.0)), 0.0, 1.0)


def corridorkey_despill_average_srgb(fg_srgb_chw: np.ndarray,
                                     strength: float = 0.5) -> np.ndarray:
    if strength <= 0.0:
        return fg_srgb_chw
    r = fg_srgb_chw[0]
    g = fg_srgb_chw[1]
    b = fg_srgb_chw[2]
    spill = np.maximum(g - (r + b) * 0.5, 0.0)
    full = np.stack([r + spill * 0.5, g - spill, b + spill * 0.5], axis=0)
    if strength >= 1.0:
        return full.astype(np.float32, copy=False)
    return fg_srgb_chw * (1.0 - strength) + full * strength
