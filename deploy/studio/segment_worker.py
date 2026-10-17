import logging
import math
import multiprocessing
import os
import threading
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable


_LOG = logging.getLogger(__name__)
_SAFE_CPU_ENV = {
    "ATEN_CPU_CAPABILITY": "default",
    "DNNL_MAX_CPU_ISA": "SSE41",
    "ONEDNN_MAX_CPU_ISA": "SSE41",
}


class SegmentationUnavailable(RuntimeError):
    pass


# 自动模式仅在确认支持AVX时保留PyTorch与oneDNN的原生CPU调度
def _use_safe_isa(capability: str, cpuinfo: str | None = None) -> bool:
    if capability == "safe":
        return True
    if capability == "native":
        return False
    if capability != "auto":
        raise ValueError("STUDIO_SEGMENT_CPU_CAPABILITY must be auto, safe, or native")
    if cpuinfo is None:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="ascii")
        except OSError:
            return True
    found_flags = False
    for line in cpuinfo.splitlines():
        name, _, values = line.partition(":")
        if name.strip().lower() in ("flags", "features"):
            found_flags = True
            if "avx" not in values.split():
                return True
    return not found_flags


# 在隔离进程里完成一次真实点选推理，触发模型加载与CPU算子初始化
def _warmup(segmenter: Any) -> None:
    import tempfile

    from PIL import Image, ImageDraw

    if not segmenter.ready():
        raise SegmentationUnavailable("Segmentation model is unavailable")
    with tempfile.TemporaryDirectory(prefix="studio-sam-warmup-") as directory:
        sample = Path(directory) / "sample.png"
        image = Image.new("RGB", (256, 256), (35, 38, 42))
        ImageDraw.Draw(image).rectangle((64, 64, 192, 192), fill=(235, 220, 190))
        image.save(sample)
        try:
            segmenter.segment(sample, [{"x": 500, "y": 500, "label": 1}])
        except ValueError as exc:
            if str(exc) != "Point segmentation did not leave a valid active selection":
                raise


# 子进程设置CPU能力上限后才导入PyTorch相关模块
def _worker_main(connection: Connection, model_dir: str, safe_isa: bool) -> None:
    if safe_isa:
        os.environ.update(_SAFE_CPU_ENV)
    from .regions import Segmenter

    segmenter = Segmenter(model_dir=model_dir)
    try:
        while True:
            command, args = connection.recv()
            if command == "stop":
                return
            try:
                if command == "warmup":
                    _warmup(segmenter)
                    connection.send(("ok", None))
                elif command == "segment":
                    connection.send(("ok", segmenter.segment(*args)))
                else:
                    connection.send(("error", "Segmentation worker command is invalid"))
            except ValueError:
                connection.send(("invalid", "Invalid segmentation request"))
            except Exception:
                _LOG.error("Segmentation worker operation failed")
                connection.send(("error", "Segmentation is unavailable"))
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


# 子进程承担原生推理故障；主服务只收发有界请求并监控退出状态
class SupervisedSegmenter:
    def __init__(
        self,
        model_dir: str | Path | None = None,
        *,
        cpu_capability: str | None = None,
        timeout_seconds: float | None = None,
        worker_target: Callable[[Connection, str, bool], None] | None = None,
    ) -> None:
        from .regions import DEFAULT_SAM2_MODEL_DIR

        self._model_dir = Path(
            model_dir
            or os.environ.get("STUDIO_SAM2_MODEL_DIR")
            or os.environ.get("STUDIO_SAM2_ONNX_DIR")
            or DEFAULT_SAM2_MODEL_DIR
        )
        capability = cpu_capability or os.environ.get(
            "STUDIO_SEGMENT_CPU_CAPABILITY", "auto"
        )
        self._safe_isa = _use_safe_isa(capability)
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.environ.get("STUDIO_SEGMENT_WORKER_TIMEOUT_SECONDS", "90"))
        )
        if not math.isfinite(timeout) or not 5 <= timeout <= 600:
            raise ValueError("STUDIO_SEGMENT_WORKER_TIMEOUT_SECONDS must be 5 to 600")
        self._timeout_seconds = timeout
        self._worker_target = worker_target or _worker_main
        self._context = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._process: multiprocessing.Process | None = None
        self._connection: Connection | None = None
        self._warmed = False

    def ready(self) -> bool:
        process = self._process
        return self._warmed and process is not None and process.is_alive()

    def _stop_locked(self) -> None:
        self._warmed = False
        connection, process = self._connection, self._process
        self._connection = None
        self._process = None
        if connection is not None:
            connection.close()
        if process is not None:
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            if not process.is_alive():
                process.close()

    def _exchange_locked(self, command: str, args: tuple[Any, ...] = ()) -> Any:
        connection = self._connection
        if connection is None:
            raise SegmentationUnavailable("Segmentation is unavailable")
        try:
            connection.send((command, args))
            if not connection.poll(self._timeout_seconds):
                raise SegmentationUnavailable("Segmentation worker timed out")
            status, value = connection.recv()
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._stop_locked()
            raise SegmentationUnavailable("Segmentation worker stopped unexpectedly") from exc
        except SegmentationUnavailable:
            self._stop_locked()
            raise
        if status == "invalid":
            raise ValueError(value)
        if status != "ok":
            raise SegmentationUnavailable(value)
        return value

    def _start_locked(self) -> bool:
        if self.ready():
            return True
        if not self._model_dir.is_dir() and self._worker_target is _worker_main:
            return False
        self._stop_locked()
        for attempt in range(2):
            parent, child = self._context.Pipe()
            process = self._context.Process(
                target=self._worker_target,
                args=(child, str(self._model_dir), self._safe_isa),
                daemon=True,
            )
            try:
                process.start()
            except Exception:
                parent.close()
                child.close()
                process.close()
                _LOG.error("Segmentation worker could not start")
                return False
            child.close()
            self._process = process
            self._connection = parent
            try:
                self._exchange_locked("warmup")
                self._warmed = True
                return True
            except (SegmentationUnavailable, ValueError):
                _LOG.warning(
                    "Segmentation worker warmup failed on attempt %s",
                    attempt + 1,
                )
                self._stop_locked()
        return False

    def start(self) -> bool:
        with self._lock:
            return self._start_locked()

    def segment(
        self,
        image_path: str | Path,
        points: list[dict[str, Any]],
        existing: dict[str, Any] | None = None,
        operation: str = "add",
    ) -> dict[str, Any]:
        with self._lock:
            if not self._start_locked():
                raise SegmentationUnavailable("Segmentation is unavailable")
            result = self._exchange_locked(
                "segment",
                (str(image_path), points, existing, operation),
            )
            if not isinstance(result, dict):
                raise SegmentationUnavailable("Segmentation worker returned an invalid result")
            return result

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            if connection is not None and self.ready():
                try:
                    connection.send(("stop", ()))
                except (BrokenPipeError, OSError):
                    pass
            self._stop_locked()
