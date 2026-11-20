"""Pocket TTS's Alba preset with local CavaAlign word timings."""

import hashlib
from importlib.resources import files
import json
from pathlib import Path
from tempfile import TemporaryDirectory


ALBA = "af_alba"
CHECKPOINT = "english_18m_refl_2026-10"
POCKET_REPO = "kyutai/pocket-tts-without-voice-cloning"
POCKET_REVISION = "a7056f505a4a8ca72d8b5fb1440071d6f1a35739"
ALIGNER_REPO = "cava-reader/cavaalign-v1"
HERE = Path(__file__).resolve().parent
ASSET_DIR = HERE / "assets" / "alba"
ASSETS = {
    "model.safetensors": "4e7a106c1ac5e3c43226565df57523184e574127c4c30c1ea6aadc4ba5e9bafb",
    "tokenizer.model": "d461765ae179566678c93091c5fa6f2984c31bbe990bf1aa62d92c64d91bc3f6",
    "alba.safetensors": "e1aa12e17245952e044853867deef5e77ec4474df119cbeefa4ed2a3d878a7b1",
    "cavaalign.onnx": "79b0aeafd305ba663a399251e61b3f98be608f16edefa40b73b6e67eddc548be",
}


def alba_asset(name):
    local = ASSET_DIR / name
    if not local.is_file():
        from huggingface_hub import hf_hub_download

        try:
            if name == "cavaalign.onnx":
                local = Path(hf_hub_download(ALIGNER_REPO, name, token=False))
            else:
                relative = f"embeddings/{name}" if name == "alba.safetensors" else name
                local = Path(hf_hub_download(
                    POCKET_REPO, f"languages/{CHECKPOINT}/{relative}",
                    revision=POCKET_REVISION, token=False,
                ))
        except Exception as error:
            raise RuntimeError("Alba needs a one-time model download. Check your connection and try again.") from error
    with local.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != ASSETS[name]:
            raise RuntimeError(f"The Alba model file {name} failed verification.")
    return local


class AlbaVoice:
    def __init__(self):
        import onnxruntime
        from pocket_tts import TTSModel
        import yaml

        config = yaml.safe_load(
            files("pocket_tts").joinpath("config", f"{CHECKPOINT}.yaml").read_text(encoding="utf-8")
        )
        config["weights_path"] = str(alba_asset("model.safetensors"))
        config["weights_path_without_voice_cloning"] = config["weights_path"]
        config["flow_lm"]["lookup_table"]["tokenizer_path"] = str(alba_asset("tokenizer.model"))
        voice = alba_asset("alba.safetensors")
        aligner = alba_asset("cavaalign.onnx")
        with TemporaryDirectory(prefix="aloud-alba-") as directory:
            path = Path(directory) / "voice.yaml"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            self.model = TTSModel.load_model(config=path)
        self.state = self.model.get_state_for_audio_prompt(voice)
        self.alignment = json.loads(
            (HERE / "vendor" / "cavaalign" / "align_config.json").read_text(encoding="utf-8")
        )
        options = onnxruntime.SessionOptions()
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        self.session = onnxruntime.InferenceSession(
            str(aligner), options, providers=["CPUExecutionProvider"]
        )

    def synthesize(self, text):
        import torch

        # Give each cached clip a reproducible render, independent of prefetch order.
        with torch.random.fork_rng(devices=[]):
            torch.random.default_generator.manual_seed(217)
            # Pocket TTS 4.0.1 starts each internal chunk from the same voice state.
            audio = self.model.generate_audio(self.state, text, max_tokens=25).detach().cpu().numpy()
        return audio, self._word_timings(audio, text)

    def _word_timings(self, audio, text):
        from .vendor.cavaalign.align import (
            AlignConfig, _word_spans, expansion_to_tokens, forced_align,
            linear_band_center, spans_to_words,
        )
        from .vendor.cavaalign.romanize import prepare_text
        from .vendor.cavaalign.vocab import vocab_for_tokens
        from scipy.signal import resample_poly

        offsets = _word_spans(text)
        if not offsets or not len(audio):
            return []
        sample_rate = self.alignment["sample_rate"]
        samples = resample_poly(audio, sample_rate, self.model.sample_rate)
        emissions = self._log_probs(samples)
        expansion = prepare_text(text, "en")
        tokens = expansion_to_tokens(expansion, vocab_for_tokens(self.alignment["vocab_tokens"]))
        aligned = forced_align(
            emissions, tokens, AlignConfig(**self.alignment["align"]),
            linear_band_center(len(emissions), len(tokens)),
        )
        timings = spans_to_words(
            text, aligned.spans, self.alignment["frame_shift_sec"],
            expansion=expansion, emissions=emissions,
        )
        return [[*offsets[i], start, end] for i, start, end, _ in timings]

    def _log_probs(self, audio):
        # CavaAlign's reference windowing keeps memory bounded on long captions.
        import numpy as np

        stride = 320
        chunk = 32 * stride
        out = []
        for pos in range(0, len(audio), 10 * chunk):
            start = max(0, pos - 5 * chunk)
            samples = audio[start:pos + 10 * chunk + 80]
            probabilities, frames = self.session.run(
                None, {"waveform": samples[None], "lengths": np.array([len(samples)], dtype=np.int64)}
            )
            skip = (pos - start) // stride
            out.append(probabilities[0, skip:min(int(frames[0]), skip + 320)])
        return np.concatenate(out)
