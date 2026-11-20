# Setup notes

## Reinstall or update

Run `sh scripts/install.sh` from an updated checkout to install its pinned dependencies and refresh the launcher. Quit ALOUD before updating.

## Optional integrations

**Read with ALOUD.** Install the app first, open it once, then run the command below. Select text in another app and choose **Services → Read with ALOUD**. You can assign **⌃⌥R** in macOS Keyboard Shortcuts.

```sh
"$HOME/.local/share/aloud-macos/venv/bin/aloud-quick-action"
```

**Piper voices.** Install Piper using the command below and put your selected model's `.onnx` and `.onnx.json` files in `~/Library/Application Support/ALOUD/piper`. Restart ALOUD after adding the files. ALOUD recognizes `en_US-lessac-high` and `de_DE-thorsten-high`. Review the model card for each voice before redistributing its files.

```sh
uv pip install --python "$HOME/.local/share/aloud-macos/venv/bin/python" 'piper-tts>=1.8,<2'
```

**Equation OCR.** Install RapidLaTeXOCR using the command below. Place the matching `image_resizer.onnx`, `encoder.onnx`, `decoder.onnx` and `tokenizer.json` files in `~/Library/Application Support/ALOUD/latex-ocr`. Text-based equations can be read without these model files. [RapidLaTeXOCR](https://github.com/RapidAI/RapidLaTeXOCR)

```sh
uv pip install --python "$HOME/.local/share/aloud-macos/venv/bin/python" 'rapid-latex-ocr==0.0.9'
```

## Local files

| Location | Contents |
| --- | --- |
| `~/Applications/ALOUD.app` | Finder launcher |
| `~/.local/share/aloud-macos/venv` | Installed application and Python dependencies |
| `~/Library/Application Support/ALOUD` | Library, saved web articles, preferences, and optional models |
| `~/Library/Caches/ALOUD` | Rendered PDF pages and speech cache |
| `~/Documents/ALOUD Notes` | Notes exported as Markdown |
| `~/Library/Logs/ALOUD.log` | Launcher log |

With `ALOUD_DATA_DIR` set, the library, notes, and cache use that directory. The **Send to ALOUD** bookmarklet pairs with that library. Create it again in the app if you switch libraries or reinstall the Quick Action for a different data directory.

## Build the disk image

On an Apple-silicon build Mac with `uv` and the Xcode command-line tools, install the pinned dependencies and build the preview.

```sh
uv venv --managed-python --python 3.12
uv pip sync requirements-macos-arm64.txt
.venv/bin/python scripts/build-dmg.py dist/ALOUD-preview.dmg
```

The disk image contains **ALOUD.app** and an **Applications** shortcut. The app carries its own Python runtime and reading dependencies. Kokoro and Alba download their models when first used. To include Alba's models in a build, pass `--alba-assets DIR` with the verified `model.safetensors`, `tokenizer.model`, `alba.safetensors`, and `cavaalign.onnx` files listed in [the Alba configuration](../aloud/pocket.py). The sample PDF is in the app’s `Contents/Resources` folder.

For a public download, use a **Developer ID Application** identity in the build Mac’s Keychain and a `notarytool` Keychain profile. The build signs the app and disk image, submits the image to Apple, and staples the accepted ticket.

```sh
.venv/bin/python scripts/build-dmg.py dist/ALOUD-macos-arm64.dmg \
    --sign-identity "Developer ID Application: Your Name (TEAMID)" \
    --notary-profile ALOUD
```
