#!/usr/bin/env python3
"""One-shot local transcript audit. Discover/verify sessions before supplying paths.

Writes aggregates and review candidates, never transcript text. Reports remain
private: candidate IDs/hashes and usage can still identify personal sessions.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def as_text(value):
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)


def output_events(value, call_id=None):
    if isinstance(value, list):
        yield dict(kind='result', id=call_id, text='\n'.join(
            as_text(block.get('text', '')) for block in value
            if isinstance(block, dict) and block.get('type') in ('text', 'input_text', 'output_text')))
        yield from images(value)
    else:
        yield dict(kind='result', id=call_id, text=as_text(value))


def events(row):
    """Normalize Codex JSONL, Claude messages, and synthetic audit fixtures."""
    if row.get('kind') in ('call', 'result', 'image', 'usage', 'workflow'):
        yield row
        return
    payload = row.get('payload', {})
    kind = payload.get('type')
    if row.get('type') == 'response_item':
        if kind in ('function_call', 'custom_tool_call'):
            yield dict(kind='call', id=payload.get('call_id'), name=payload.get('name', ''),
                       input=payload.get('arguments', payload.get('input', '')))
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            yield from output_events(payload.get('output', ''), payload.get('call_id'))
        else:
            yield from images(payload.get('content', []))
    elif row.get('type') == 'event_msg' and kind == 'token_count':
        yield dict(kind='usage', value=(payload.get('info') or {}).get('total_token_usage'))
    message = row.get('message', {})
    if isinstance(message, dict):
        if message.get('usage'):
            yield dict(kind='usage', value=message['usage'])
        content = message.get('content', [])
        if isinstance(content, list):
            for block in content:
                if block.get('type') == 'tool_use':
                    yield dict(kind='call', id=block.get('id'), name=block.get('name', ''), input=block.get('input', {}))
                elif block.get('type') == 'tool_result':
                    yield from output_events(block.get('content', ''), block.get('tool_use_id'))
            yield from images(content)


def images(content):
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get('type') in ('image', 'image_url', 'input_image', 'output_image'):
            source = block.get('source', block.get('image_url', block.get('data', '')))
            # Only inline image bytes establish identical payloads; URLs may change.
            data = source.get('data', source.get('url', '')) if isinstance(source, dict) else source
            inline = bool(data) and ('data' in block or isinstance(source, dict) and source.get('type') == 'base64' or str(data).startswith('data:'))
            yield dict(kind='image', payload_hash=digest(as_text(data)) if inline else None)


def analyze(rows):
    sizes, calls, reads, statuses, image_hashes = [], Counter(), {}, {}, Counter()
    review_reads, repeated_status, followups = [], [], []
    counts = Counter()
    last_inspection = None
    usage, workflow = None, None
    for index, row in enumerate(rows, 1):
        for event in events(row):
            kind = event['kind']
            counts[kind] += 1
            if kind == 'result':
                sizes.append(len(as_text(event.get('text', ''))))
            elif kind == 'call':
                raw = as_text(event.get('input', ''))
                name = event.get('name', '')
                operation = name.rsplit('__', 1)[-1].rsplit('.', 1)[-1]
                signature = digest(name + '\n' + raw)
                calls[signature] += 1
                ref = dict(record=index, call_id=event.get('id'), signature=signature)
                if re.search(r'(?:^|[\W_])(?:status|task_status|pr checks|pr view)(?:$|[\W_])', operation + ' ' + raw):
                    counts['status_queries'] += 1
                    if signature in statuses:
                        repeated_status.append(dict(previous_record=statuses[signature], **ref))
                    statuses[signature] = index
                if re.search(r'\b(?:cat|sed|head|tail|read_file)\b', name + ' ' + raw):
                    if signature in reads:
                        review_reads.append(dict(previous_record=reads[signature], necessity='unreviewed', **ref))
                    reads[signature] = index
                if re.search(r'(?:runs watch|wait_task|wait_agent)', operation + ' ' + raw):
                    counts['bounded_watch_calls'] += 1
                if re.search(r'\bsleep\s+(?:[0-9](?:\.\d+)?|[1-5][0-9](?:\.\d+)?)\b', raw):
                    counts['short_sleep_calls'] += 1
                elif operation == 'sleep':
                    try:
                        duration = json.loads(raw).get('duration_ms')
                        if isinstance(duration, (float, int)) and 0 < duration < 60000:
                            counts['short_sleep_calls'] += 1
                    except (ValueError, AttributeError):
                        pass
                if re.search(r'\bscreenshot|capture_screen|getScreenshot\b', name + ' ' + raw):
                    counts['screenshot_calls'] += 1
                inspection = re.search(r'runs inspect\s+([A-Za-z0-9-]+)', raw)
                retrieval = re.search(r'runs logs\s+([A-Za-z0-9-]+)', raw)
                if inspection:
                    last_inspection = (index, inspection[1])
                if retrieval and last_inspection and retrieval[1] == last_inspection[1]:
                    followups.append(dict(inspect_record=last_inspection[0], logs_record=index,
                                          cause='unreviewed; retrieval may be appropriate'))
            elif kind == 'image' and event.get('payload_hash'):
                image_hashes[event['payload_hash']] += 1
            elif kind == 'usage':
                usage = event.get('value')
            elif kind == 'workflow':
                workflow = event.get('revision')
    ordered = sorted(sizes)
    return dict(schema_version=1,
        measured=dict(text_outputs=len(sizes), total_output_characters=sum(sizes),
            output_characters=dict(min=min(sizes, default=0), median=ordered[len(ordered)//2] if ordered else 0,
                                   p95=ordered[min(len(ordered)-1, int(len(ordered)*.95))] if ordered else 0,
                                   max=max(sizes, default=0)),
            outputs_over_20000=sum(n > 20000 for n in sizes),
            distribution={label:sum(lo <= n < hi for n in sizes) for label,lo,hi in
                          [('0-1999',0,2000),('2000-7999',2000,8000),('8000-20000',8000,20001),('over-20000',20001,float('inf'))]},
            status_queries=counts['status_queries'], bounded_watch_calls=counts['bounded_watch_calls'],
            short_sleep_calls=counts['short_sleep_calls'], image_attachments=counts['image'],
            screenshot_calls=counts['screenshot_calls'],
            identical_image_payload_repeats=sum(n-1 for n in image_hashes.values()),
            usage_last_observed=usage, workflow_revision=workflow),
        manual_review=dict(repeated_source_read_candidates=review_reads, repeated_status_candidates=repeated_status,
                           followup_retrieval_candidates=followups,
                           redundant_captures='not established; identical payloads alone do not establish unnecessary verification'),
        limitations=['Character volume is not token usage or monetary savings.',
                     'Tool envelopes may contain nested outputs; counts use recorded result events, not inferred inner operations.',
                     'Repeated commands are candidates, not evidence of unnecessary work; check edits, branch/collaborator changes and context loss.',
                     'Image bytes, usage and workflow revision are unavailable when absent from the input.',
                     'Usage snapshots are not summed: providers may report cumulative or per-message values.'])


def read_rows(path):
    with Path(path).open() as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError(f'malformed JSON at record {number}') from exc
            if not isinstance(row, dict):
                raise ValueError(f'expected object at record {number}')
            yield row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('transcripts', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path, help='private JSON report outside Git')
    args = parser.parse_args(argv)
    report = dict(reports=[dict(source_reference_sha256=digest(str(path.resolve())), **analyze(read_rows(path)))
                          for path in args.transcripts])
    # Exclusive creation avoids overwriting another report or following symlinks.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(f'Wrote {len(report["reports"])} private reports to {args.output}')


if __name__ == '__main__':
    main()
