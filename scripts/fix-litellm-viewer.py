#!/usr/bin/env python3
"""Apply a version-specific logging fallback repair to this Python environment."""
import importlib.metadata
import pathlib

OLD = """    candidate: Final = diagnostics.run(
        lambda native: native.process_diagnostic(message, exception, stack, leaves, (redact, base64_limit, text_limit)),
        lambda: _python_process_diagnostic(message, exception, stack, leaves, redact, base64_limit, text_limit),
    )"""
NEW = """    try:
        candidate = diagnostics.run(
            lambda native: native.process_diagnostic(message, exception, stack, leaves, (redact, base64_limit, text_limit)),
            lambda: _python_process_diagnostic(message, exception, stack, leaves, redact, base64_limit, text_limit),
        )
    except (ImportError, KeyError):
        candidate = _python_process_diagnostic(message, exception, stack, leaves, redact, base64_limit, text_limit)"""

def main():
    if importlib.metadata.version('litellm') != '1.105.0':
        raise SystemExit('This repair requires LiteLLM 1.105.0; inspect other versions before patching.')
    path=pathlib.Path(importlib.metadata.distribution('litellm').locate_file('litellm/_logging.py'))
    text=path.read_text()
    if NEW in text:
        print('Logging fallback repair already installed.');return
    if text.count(OLD)!=1:
        raise SystemExit('Expected logging implementation not found; no files changed.')
    updated=text.replace(OLD,NEW)
    compile(updated,str(path),'exec')
    path.write_text(updated)
    print('Logging fallback repair installed; restart Harbor Viewer.')

if __name__=='__main__':main()
