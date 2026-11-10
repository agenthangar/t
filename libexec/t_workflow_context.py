"""Read-only native SessionStart context adapter."""
import json
from pathlib import Path
import runpy
import sys


def context(tool, cwd):
    w = runpy.run_path(str(Path(__file__).with_name('t_workflow.py')))
    document, saved = w['load']()
    if saved is None:
        return ''
    text = w['resolve_context'](document, cwd, tool)['text']
    # Native hook output has finite context limits. Never silently truncate a
    # safety rule or custom instruction: request the complete scoped read.
    return text if len(text.encode('utf-8')) <= 8000 else w['fallback'](tool)


if __name__ == '__main__':
    try:
        tool, cwd = sys.argv[1:3]
        text = context(tool, cwd)
        if text:
            print(json.dumps({'additional_context': text} if tool == 'cursor' else {
                'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': text}}))
    except Exception as exc:
        print('t workflow context unavailable: ' + str(exc), file=sys.stderr)
