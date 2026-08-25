"""Optional local grammar cleanup with t5-tiny-gec-hone.

A faithful port of ``Sources/BetterVoice/GrammarCorrector.swift``: same model,
same pinned revision, same SHA-256 pinning of every asset, same greedy decode
with a 96-token budget, and the same conservative guards -- if anything looks
off, the raw transcript is returned untouched so a recording never fails because
of an optional polish step.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..paths import data_dir, ensure

log = logging.getLogger(__name__)

REVISION = "d5f27b81d5316bd689977d722d3ed513bbb9122c"
BASE_URL = f"https://huggingface.co/rabden/t5-tiny-gec-hone/resolve/{REVISION}/"
MODEL_DIRECTORY_NAME = "t5-tiny-gec-hone"

#: Roughly what the assets weigh, for the setup window.
APPROXIMATE_DOWNLOAD_MB = 36

MAX_INPUT_TOKENS = 128
MAX_OUTPUT_TOKENS = 96
DECODER_LAYERS = 4
EOS_TOKEN = 1
PAD_TOKEN = 0
VOCABULARY_SIZE = 32_128


@dataclass(frozen=True)
class Asset:
    path: str
    sha256: str


ASSETS = (
    Asset(
        "onnx/encoder_model_quantized.onnx",
        "baa41f33481ca2db5d2b458d3bd50a879f0b6b054fd101fa1e68bb3c41b724a4",
    ),
    Asset(
        "onnx/decoder_model_merged_quantized.onnx",
        "dfce028e696fcb8156ac3571acf8eafca650768453bdcd6295fda910ae61d6d9",
    ),
    Asset(
        "tokenizer.json",
        "d7af4599a1914d04aaf44839418757f40ccca4c78033edeba369484978378335",
    ),
    Asset(
        "tokenizer_config.json",
        "fb20bc34a5b9e424c29d0aa8cf4754a7f0e42cf8ff5ba304dec219c54854dba9",
    ),
    Asset(
        "special_tokens_map.json",
        "5c87151ef0f72a99d1f766a4c418bd2a1f90aaa30a8e22fe5eca9641daebb64f",
    ),
)


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


class GrammarCorrector:
    """Loads the tiny T5 corrector lazily and never raises at call sites."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded = False
        self._tokenizer = None
        self._encoder = None
        self._decoder = None
        self._decoder_inputs: set[str] = set()

    @property
    def directory(self) -> Path:
        return data_dir() / MODEL_DIRECTORY_NAME

    def is_cached(self) -> bool:
        return all((self.directory / asset.path).is_file() for asset in ASSETS)

    def preload(self) -> bool:
        try:
            self._load()
            return True
        except Exception as error:
            log.warning("Grammar model could not be prepared: %s", error)
            return False

    def correct(self, text: str) -> str:
        trimmed = text.strip()
        if len(trimmed.split()) <= 1:
            return text
        try:
            self._load()
            return self._correct(trimmed)
        except Exception as error:
            # Grammar cleanup is deliberately optional: the ASR text stays usable
            # whenever the model is unavailable or behaves unexpectedly.
            log.info("Skipping grammar cleanup: %s", error)
            return text

    # -- loading ---------------------------------------------------------

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            directory = self._download()
            self._tokenizer = self._load_tokenizer(directory)
            self._encoder, self._decoder = self._load_sessions(directory)
            self._decoder_inputs = {value.name for value in self._decoder.get_inputs()}
            self._loaded = True

    def _download(self) -> Path:
        import urllib.request

        root = ensure(self.directory)
        for asset in ASSETS:
            destination = root / asset.path
            ensure(destination.parent)
            if destination.is_file() and _sha256(destination) == asset.sha256:
                continue

            temporary = destination.with_suffix(destination.suffix + ".part")
            url = BASE_URL + asset.path + "?download=true"
            with urllib.request.urlopen(url, timeout=60) as response:
                if response.status != 200:
                    raise RuntimeError(f"The grammar asset {asset.path} is unavailable.")
                temporary.write_bytes(response.read())
            if _sha256(temporary) != asset.sha256:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"The grammar asset {asset.path} is invalid.")
            temporary.replace(destination)
        return root

    @staticmethod
    def _load_tokenizer(directory: Path):
        from tokenizers import Tokenizer

        return Tokenizer.from_file(str(directory / "tokenizer.json"))

    @staticmethod
    def _load_sessions(directory: Path):
        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 2
        options.graph_optimization_level = (
            onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        )
        options.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        encoder = onnxruntime.InferenceSession(
            str(directory / "onnx/encoder_model_quantized.onnx"),
            sess_options=options,
            providers=providers,
        )
        decoder = onnxruntime.InferenceSession(
            str(directory / "onnx/decoder_model_merged_quantized.onnx"),
            sess_options=options,
            providers=providers,
        )
        return encoder, decoder

    # -- inference -------------------------------------------------------

    def _correct(self, text: str) -> str:
        encoding = self._tokenizer.encode(text)
        input_ids = encoding.ids
        if len(input_ids) > MAX_INPUT_TOKENS:
            return text

        ids = np.asarray([input_ids], dtype=np.int64)
        attention_mask = np.ones_like(ids, dtype=np.int64)
        hidden_states = self._encoder.run(
            ["last_hidden_state"], {"input_ids": ids, "attention_mask": attention_mask}
        )[0]

        decoder_inputs: dict[str, np.ndarray] = {
            "encoder_hidden_states": hidden_states,
            "encoder_attention_mask": attention_mask,
        }
        empty_cache = np.zeros((1, 4, 0, 64), dtype=np.float32)
        for layer in range(DECODER_LAYERS):
            decoder_inputs[f"past_key_values.{layer}.decoder.key"] = empty_cache
            decoder_inputs[f"past_key_values.{layer}.decoder.value"] = empty_cache

        output_names = ["logits"] + [
            f"present.{layer}.{part}"
            for layer in range(DECODER_LAYERS)
            for part in ("decoder.key", "decoder.value")
        ]

        token = PAD_TOKEN
        produced: list[int] = []
        reached_end = False
        for _ in range(MAX_OUTPUT_TOKENS):
            decoder_inputs["input_ids"] = np.asarray([[token]], dtype=np.int64)
            feed = {
                name: value
                for name, value in decoder_inputs.items()
                if name in self._decoder_inputs
            }
            outputs = self._decoder.run(output_names, feed)
            logits = outputs[0]
            next_token = int(np.argmax(logits.reshape(-1)[-VOCABULARY_SIZE:]))
            if next_token == EOS_TOKEN:
                reached_end = True
                break
            produced.append(next_token)
            token = next_token
            for index, name in enumerate(output_names[1:], start=1):
                decoder_inputs[name.replace("present.", "past_key_values.")] = outputs[index]

        if not reached_end:
            return text
        corrected = self._tokenizer.decode(produced, skip_special_tokens=True).strip()
        if not corrected:
            return text
        minimum_length = max(8, int(len(text) * 0.55))
        return corrected if len(corrected) >= minimum_length else text
