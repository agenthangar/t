"""Guided agent-to-agent handoffs, with explicit inputs for unattended use."""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import runpy
import socket
import sys


AGENTS = ('claude', 'codex', 'cursor')


def module(name):
    return runpy.run_path(str(Path(__file__).with_name(name + '.py')))


def wanted(args):
    """Fully addressed file deliveries retain their existing direct path."""
    return (not getattr(args, 'share_action', None) and not getattr(args, 'retry', None)
            and (getattr(args, 'destination', None) in AGENTS
                 or getattr(args, 'source', None) in AGENTS
                 or not getattr(args, 'destination', None)
                 or not getattr(args, 'file', None)))


def source_for(args, ui, core):
    requested = getattr(args, 'source', None)
    if requested and requested not in AGENTS:
        return core['local_resolve'](requested)
    try:
        source = core['local_resolve'](core['local_self']())
        if not requested or source['agent'] == requested:
            return source
    except ValueError:
        pass
    if ui is None:
        raise ValueError('cannot identify the source conversation; run t share in a terminal '
                         'to choose it, or pass --from agent:<id>')
    rows = core['local_targets']()
    if requested:
        rows = [row for row in rows if row['agent'] == requested]
    if not rows:
        raise ValueError('no saved source conversations found')
    selected = ui.conversations('Share from', rows, preferred_cwd=os.getcwd())
    # Revalidate the selection before capturing any conversation text.
    return core['local_resolve'](selected['address']) if selected else None


def context_for(args, source, ui, core):
    notes = []
    if getattr(args, 'file', None):
        text = core['read_text_file'](args.file)
        label = 'Your context'
    else:
        try:
            snapshot = module('t_share_context')['snapshot'](source)
            text, label = snapshot['text'], 'Recent conversation text'
            notes.append(f"{snapshot['selected_count']} messages selected")
            if snapshot['truncated']:
                notes.append('Selected history: ' + '; '.join(snapshot['truncation_reasons']))
        except ValueError as error:
            if ui is None:
                raise ValueError(str(error) + '; use --file to provide context') from error
            text = ui.compose('Context to share', str(error))
            if text is None:
                return None
            label = 'Your note'
    if not text.strip():
        raise ValueError('context cannot be empty')
    remaining = core['MAX_CONTENT'] - core['text_size'](text)
    paths = getattr(args, 'attach', None) or []
    if len(paths) > 100:
        raise ValueError('at most 100 text attachments are allowed')
    attachments = []
    for path in paths:
        if path == '-':
            raise ValueError('attachments must name files; stdin is reserved for --file -')
        content = core['read_text_file'](path, remaining)
        remaining -= core['text_size'](content)
        attachments.append(dict(name=core['attachment_name'](Path(path).name), text=content))
    if remaining < 0:
        raise ValueError('summary and attachments exceed the 10 MiB limit')
    if attachments:
        notes.append('Attachments: ' + ', '.join(item['name'] for item in attachments))
    return text, attachments, label, notes


def run(cfg, args, api, core):
    interactive = (sys.stdin.isatty() and sys.stdout.isatty()
                   and not getattr(args, 'json', False))
    destination = getattr(args, 'destination', None)
    if not interactive and not destination:
        raise ValueError('choose a receiving agent with --to claude, --to codex, or --to cursor')
    if interactive and getattr(args, 'file', None) == '-':
        raise ValueError('pipe context into t share --to <agent> --file -, or use a file path')
    manager = module('t_share_ui')['ShareUI'](api) if interactive else nullcontext(None)
    with manager as ui:
        if ui:
            ui.intro('Share context', 'Continue a conversation with another agent')
        source = source_for(args, ui, core)
        if source is None:
            return 130
        if ui:
            ui.done('From', f"{api['_agent_glyph'](source['agent'])} {source['agent'].title()} · {source['title']}")
        if not destination:
            preferred = 'claude' if source['agent'] != 'claude' else 'codex'
            agents = [preferred] + [agent for agent in AGENTS if agent != preferred]
            destination = ui.choose('Continue with', [
                dict(key=agent, label=f"{api['_agent_glyph'](agent)} {agent.title()}",
                     detail='New conversation' if not getattr(args, 'host', None) else 'Choose a conversation')
                for agent in agents])
            if destination is None:
                return 130
        route = core['route_for'](cfg, getattr(args, 'host', None))
        store = core['Store']()
        if destination in AGENTS and route['alias'] is not None:
            if ui is None:
                raise ValueError('remote handoffs need an exact --to agent:<id>; '
                                 'run t share --host <host> in a terminal to choose one')
            rows = core['request_at'](route, dict(version=core['VERSION'], op='targets'), store)
            rows = [dict(core['identity'](row), host=route['alias'])
                    for row in rows if row['agent'] == destination]
            if not rows:
                raise ValueError(f'no {destination.title()} conversations found on {route["alias"]}')
            target = ui.conversations('Share with', rows, preferred_cwd=source['cwd'])
            if target is None:
                return 130
            destination = target['address']
        if destination in AGENTS:
            launcher = module('t_share_launch')
            plan = launcher['preview'](cfg, source, destination)
            target = dict(agent=destination, title='New conversation', host='local',
                          cwd=source['cwd'])
        else:
            core['address'](destination)
            target = core['request_at'](route, dict(version=core['VERSION'], op='resolve',
                                                   address=destination), store)
            core['identity'](target)
            if target['address'] != destination:
                raise ValueError('resolved recipient does not match the requested conversation')
            target = dict(target, host=route['alias'] or 'local')
            launcher, plan = None, None
        captured = context_for(args, source, ui, core)
        if captured is None:
            return 130
        text, attachments, label, notes = captured
        if plan:
            notes.append('Start in a new worktree; the source session stays open.')
            if plan.get('dirty'):
                notes.append('Uncommitted edits stay in the source worktree.')
        action = f"Start {target['agent'].title()}" if launcher else 'Share context'
        if ui and not ui.review(source, target, text, notes, context_label=label, action=action):
            return 130
        if launcher:
            # Files supplied explicitly are carried in the same reviewed artifact.
            content = text + ''.join(f"\n\n## Attachment: {item['name']}\n\n{item['text']}"
                                     for item in attachments)
            result = launcher['launch'](cfg, source, destination, content, plan=plan)
            result = dict(result, status='started')
            if ui:
                ui.done('Started', f"{api['_agent_glyph'](destination)} {destination.title()} · "
                        f"{result['repo']}-{result['slot']}")
            elif getattr(args, 'json', False):
                print(json.dumps(dict(version=core['VERSION'], result=result), ensure_ascii=True))
            else:
                print(f"Started {destination.title()} with the shared context · {result['repo']}-{result['slot']}")
                print(result.get('open_command') or f"Open: t session open {result['repo']} {result['slot']}")
        else:
            source = dict(source, host=socket.gethostname())
            envelope = core['make_envelope'](source, target, text, attachments)
            result = core['deliver'](store, envelope, route)
            if ui:
                ui.done('Shared', f"{target['agent'].title()} · {target['title']}")
                ui.done('Read in receiving conversation', f"t share read {result['id']}")
            else:
                core['render'](result, None, getattr(args, 'json', False))
    if interactive and launcher:
        return launcher['attach'](result)
    return 0
