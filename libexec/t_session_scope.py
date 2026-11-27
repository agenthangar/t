"""Normalize shared session selectors before command-specific dispatch."""
import os

COMMANDS = {'ls', 'list', 'open', 'kill', 'close', 'move', 'beam', 'resume'}


def normalize(argv):
    """Return legacy-compatible arguments and an explicit source selector."""
    words = list(argv)
    offset = 2 if words[:1] == ['session'] else 1
    if len(words) < offset or words[offset - 1] not in COMMANDS:
        return words, None
    prefix, arguments = words[:offset], words[offset:]
    retained, positional = [], []
    repository = source = None
    all_repos = False
    valued = {'--host', '--from', '--to', '--session', '-s', '--days'}
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == '--':
            retained.extend(arguments[index:])
            break
        name, separator, value = argument.partition('=')
        if name in ('--repo', '--source'):
            if not separator:
                index += 1
                value = arguments[index] if index < len(arguments) else ''
            if not value or value.startswith('-'):
                raise ValueError(name + ' requires a value')
            if name == '--repo':
                if repository is not None and repository != value:
                    raise ValueError('conflicting repository selectors')
                repository = value
            else:
                if source is not None and source != value:
                    raise ValueError('conflicting source selectors')
                source = value
        else:
            retained.append(argument)
            if argument in ('-a', '--all'):
                all_repos = True
            elif name in valued and not separator:
                index += 1
                if index < len(arguments):
                    retained.append(arguments[index])
            elif not argument.startswith('-'):
                positional.append(argument)
        index += 1
    if repository:
        if positional and not positional[0].isdigit():
            if positional[0] != repository:
                raise ValueError('conflicting repository selectors')
        else:
            retained.insert(0, repository)
    if all_repos and (repository or positional):
        raise ValueError('--all cannot be combined with a repository or slot')
    command = words[offset - 1]
    local_flags = {'--local', '--here'} | ({'-l'} if command in ('ls', 'list', 'open') else set())
    if source and source != 'local' and local_flags.intersection(retained):
        # Move's --here is a destination choice.
        if command not in ('move', 'beam'):
            raise ValueError('--source conflicts with the local-only option')
    if source == 'local' and command in ('kill', 'close') and {'-r', '--remote'}.intersection(retained):
        raise ValueError('--source local conflicts with --remote')
    return prefix + retained, source


def targets(hosts, source=None):
    """Resolve the explicit source set without changing destination meaning."""
    source = source or os.environ.get('T_SESSION_SOURCE')
    if not source or source == 'all':
        return {'local': 'local', **hosts}
    if source == 'local':
        return {'local': 'local'}
    if source == 'remote':
        return dict(hosts)
    name = source.removeprefix('host:')
    if name not in hosts:
        raise ValueError('unknown source host ' + name)
    return {name: hosts[name]}


if __name__ == '__main__':
    import shlex
    import sys
    try:
        arguments, source = normalize(sys.argv[1:])
        print('set -- ' + shlex.join(arguments))
        if source is not None:
            print('T_SESSION_SOURCE=' + shlex.quote(source))
    except ValueError as error:
        print('t: ' + str(error), file=sys.stderr)
        sys.exit(2)
