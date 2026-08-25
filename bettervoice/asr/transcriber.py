"""Local transcription with NVIDIA Parakeet TDT, running entirely on this machine.

The macOS build ran Parakeet through FluidAudio's CoreML port. Linux has no
CoreML, so the same model is used in its ONNX form via ``onnx-asr`` on
onnxruntime -- same architecture, same weights, same "nothing leaves the
machine" promise, and the int8 export lands at roughly the same download size.
"""

from __future__ import annotations

import enum
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..errors import LocalModelUnavailable
from ..paths import data_dir, ensure

log = logging.getLogger(__name__)

DEFAULT_MODEL = "nemo-parakeet-tdt-0.6b-v2"
DEFAULT_QUANTIZATION = "int8"

_REPOS = {
    "nemo-parakeet-tdt-0.6b-v2": "istupakov/parakeet-tdt-0.6b-v2-onnx",
    "nemo-parakeet-tdt-0.6b-v3": "istupakov/parakeet-tdt-0.6b-v3-onnx",
    "nemo-parakeet-ctc-0.6b": "istupakov/parakeet-ctc-0.6b-onnx",
    "whisper-base": "istupakov/whisper-base-onnx",
}

#: Roughly what the int8 download weighs, for the setup window.
APPROXIMATE_DOWNLOAD_MB = 665


class ModelState(enum.Enum):
    MISSING = "missing"
    DOWNLOADING = "downloading"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class ModelStatus:
    state: ModelState
    percent: int = 0
    message: str = ""


def _model_files(quantization: str | None) -> list[str]:
    suffix = f".{quantization}" if quantization else ""
    return [
        f"encoder-model{suffix}.onnx",
        f"decoder_joint-model{suffix}.onnx",
        "nemo128.onnx",
        "vocab.txt",
        "config.json",
    ]


class LocalTranscriber:
    """Downloads, loads and runs the local ASR model.

    Every method here blocks; the app runs them on a worker thread and reports
    progress through ``on_status``.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        quantization: str | None = DEFAULT_QUANTIZATION,
    ) -> None:
        self.model_name = model
        self.quantization = quantization
        self._model = None
        self._lock = threading.RLock()
        self.status = ModelStatus(ModelState.MISSING)
        self.on_status: Callable[[ModelStatus], None] | None = None

    @property
    def directory(self) -> Path:
        return data_dir() / "models" / self.model_name

    @property
    def is_downloaded(self) -> bool:
        directory = self.directory
        return all((directory / name).is_file() for name in _model_files(self.quantization))

    @property
    def is_ready(self) -> bool:
        return self.status.state is ModelState.READY and self._model is not None

    def _set_status(self, state: ModelState, percent: int = 0, message: str = "") -> None:
        self.status = ModelStatus(state, percent, message)
        if self.on_status is not None:
            self.on_status(self.status)

    def load_cached(self) -> None:
        if not self.is_downloaded:
            self._set_status(ModelState.MISSING)
            return
        self._prepare(download=False)

    def download(self) -> None:
        self._prepare(download=True)

    def _prepare(self, *, download: bool) -> None:
        with self._lock:
            if self.status.state in {ModelState.LOADING, ModelState.DOWNLOADING}:
                return
            self._set_status(ModelState.DOWNLOADING if download else ModelState.LOADING)

        try:
            if download and not self.is_downloaded:
                self._download_files()
            self._set_status(ModelState.LOADING)
            self._model = self._load()
            self._set_status(ModelState.READY)
        except Exception as error:
            log.warning("Local model could not be prepared: %s", error)
            self._model = None
            self._set_status(ModelState.FAILED, message=str(error))

    def _download_files(self) -> None:
        from huggingface_hub import snapshot_download

        repository = _REPOS.get(self.model_name)
        if repository is None:
            raise LocalModelUnavailable()

        patterns = _model_files(self.quantization)
        total = self._remote_size(repository, patterns)
        directory = ensure(self.directory)
        reporter = _ProgressReporter(
            total, lambda percent: self._set_status(ModelState.DOWNLOADING, percent)
        )
        with reporter.installed():
            snapshot_download(
                repo_id=repository,
                local_dir=str(directory),
                allow_patterns=patterns,
            )

    @staticmethod
    def _remote_size(repository: str, patterns: list[str]) -> int:
        try:
            from huggingface_hub import HfApi

            info = HfApi().model_info(repository, files_metadata=True)
            wanted = set(patterns)
            return sum(
                sibling.size or 0 for sibling in info.siblings if sibling.rfilename in wanted
            )
        except Exception as error:  # pragma: no cover - offline or API change
            log.debug("Could not size the model download: %s", error)
            return 0

    def _load(self):
        import onnx_asr

        return onnx_asr.load_model(
            self.model_name, path=str(self.directory), quantization=self.quantization
        )

    def transcribe(self, audio: Path) -> str:
        model = self._model
        if model is None:
            raise LocalModelUnavailable()
        result = model.recognize(str(audio))
        if isinstance(result, list):
            result = " ".join(str(part) for part in result)
        return str(result).strip()


class _ProgressReporter:
    """Turns huggingface_hub's tqdm output into a percentage.

    ``snapshot_download`` has no progress callback, but it does route every byte
    through a tqdm subclass it looks up at call time -- so swapping that class is
    the supported way to watch a download.
    """

    def __init__(self, total_bytes: int, on_percent: Callable[[int], None]) -> None:
        self._total = total_bytes
        self._on_percent = on_percent
        self._done = 0
        self._last = -1
        self._lock = threading.Lock()

    def _advance(self, amount: int) -> None:
        if self._total <= 0:
            return
        with self._lock:
            self._done += amount
            percent = min(100, int(self._done * 100 / self._total))
            if percent == self._last:
                return
            self._last = percent
        self._on_percent(percent)

    def installed(self):
        from contextlib import contextmanager

        reporter = self

        @contextmanager
        def _context():
            import huggingface_hub.utils as hub_utils
            from tqdm.auto import tqdm as base_tqdm

            class _Tqdm(base_tqdm):
                def update(self, n=1):
                    reporter._advance(int(n or 0))
                    return super().update(n)

            original = getattr(hub_utils, "tqdm", None)
            hub_utils.tqdm = _Tqdm
            try:
                yield
            finally:
                if original is not None:
                    hub_utils.tqdm = original

        return _context()
