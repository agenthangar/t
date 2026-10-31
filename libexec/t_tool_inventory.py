"""Read-only repository-relevant executable inventory. Never executes discovered code."""
import configparser
from email.parser import Parser
import json
import itertools
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
from urllib.parse import unquote, urlsplit
import runpy
from types import SimpleNamespace

tomllib = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name('t_toml.py'))))

LIMIT = 1048576
MAX_ITEMS = 500


def text(path, limit=LIMIT):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('not bounded regular metadata')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('metadata too large')
    return raw.decode('utf-8')


def document(path):
    raw = text(path)
    if path.suffix == '.json':
        data = json.loads(raw)
    else:
        data = tomllib.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('invalid metadata object')
    return data


def name_ok(value, ecosystem='npm'):
    pattern = r'(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*' if ecosystem == 'npm' else r'[A-Za-z0-9][A-Za-z0-9._-]*'
    return isinstance(value, str) and len(value) <= 150 and bool(re.fullmatch(pattern, value))


def normalize(value, ecosystem):
    return re.sub(r'[-_.]+', '-', value).lower() if ecosystem == 'PyPI' else value


def version_ok(value, ecosystem='npm'):
    # Conservative supported syntax, not a cross-ecosystem version comparator.
    if not isinstance(value, str) or len(value) > 80:
        return False
    numeric = r'(?:0|[1-9][0-9]*)'
    semver = rf'{numeric}\.{numeric}\.{numeric}(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?'
    pep440 = r'(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?(?:\+[a-z0-9]+(?:[._-][a-z0-9]+)*)?'
    return bool(re.fullmatch(pep440 if ecosystem == 'PyPI' else semver, value))


def within(root, relative):
    if not isinstance(relative, str) or not relative or PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts:
        raise ValueError('unsafe metadata path')
    candidate = root / relative
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('metadata path escapes package')
    return candidate


def registered():
    """Parse t's existing generated config bridge, never source shell configuration."""
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    path = Path(base) / 't/config.sh'
    repos, agents = {}, {}
    try:
        raw = text(path)
    except FileNotFoundError:
        return {}, {}
    for line in raw.splitlines():
        match = re.fullmatch(r'(DEV_REPOS|DEV_AGENT)\[([^\]]+)\]=(.*)', line)
        if match:
            words = shlex.split(match[3])
            if len(words) == 1:
                (repos if match[1] == 'DEV_REPOS' else agents)[match[2]] = words[0]
    if len(repos) > 100:
        raise ValueError('repository limit exceeded')
    return repos, agents


def row(binary, path, provenance, ecosystem='', name='', version='', reason='package/version identity unsupported'):
    return {'binary': binary, 'path': str(path), 'provenance': [provenance],
            'ecosystem': ecosystem, 'name': name, 'version': version,
            'status': 'mapped' if ecosystem else 'unsupported', 'reason': '' if ecosystem else reason}


def npm_package(root, record, provenance):
    meta = document(root / 'package.json')
    name, version = meta.get('name'), meta.get('version')
    bins = meta.get('bin', {})
    if isinstance(bins, str):
        bins = {str(name).split('/')[-1]: bins}
    if not isinstance(bins, dict) or len(bins) > MAX_ITEMS:
        raise ValueError('invalid executable metadata')
    resolved = record.get('resolved', '')
    url = urlsplit(resolved) if isinstance(resolved, str) else urlsplit('')
    public = (name_ok(name) and version_ok(version) and record.get('version') == version
              and not meta.get('private') and url.scheme == 'https'
              and url.netloc == 'registry.npmjs.org' and not url.query and not url.fragment
              and unquote(url.path).startswith('/' + name + '/-/')
              # Scoped npm packages may be private even on npm's public hostname.
              and not name.startswith('@'))
    result = []
    for binary, relative in bins.items():
        if not name_ok(binary, 'PyPI'):
            raise ValueError('invalid binary name')
        target = within(root, relative)
        if not target.is_file():
            result.append(row(binary, target, provenance, reason='declared executable missing'))
        elif public:
            result.append(row(binary, target.resolve(), provenance, 'npm', name, version))
        else:
            result.append(row(binary, target.resolve(), provenance,
                              reason='npm public registry identity/version unverified (private/scoped/local sources are not sent)'))
    return result


def node_packages(repo, provenance):
    """npm lock v2/v3 package locations; actual installed metadata supplies versions."""
    lock = document(repo / 'package-lock.json')
    packages = lock.get('packages')
    if not isinstance(packages, dict) or len(packages) > 10000:
        raise ValueError('unsupported npm lock format/size')
    result = []
    for relative, record in packages.items():
        if 'node_modules' not in PurePosixPath(relative).parts:
            continue
        root = within(repo, relative)
        if not isinstance(record, dict):
            raise ValueError('invalid npm lock entry')
        if (root / 'package.json').is_file():
            result.extend(npm_package(root, record, provenance + ' package-lock.json + installed package.json'))
        elif record.get('bin'):
            result.append(row(relative, root, provenance, reason='locked CLI package not installed'))
        if len(result) > MAX_ITEMS:
            raise ValueError('installed executable limit exceeded')
    return result


def python_packages(repo, provenance):
    """Only installed console scripts; uv's public PyPI source verifies query identity."""
    lock = document(repo / 'uv.lock') if (repo / 'uv.lock').exists() else {}
    packages = lock.get('package', [])
    if not isinstance(packages, list):
        raise ValueError('invalid uv lock')
    public = set()
    for item in packages:
        if not isinstance(item, dict):
            raise ValueError('invalid uv package')
        source = item.get('source', {})
        name, version = item.get('name'), item.get('version')
        if (isinstance(source, dict) and source.get('registry') == 'https://pypi.org/simple'
                and name_ok(name, 'PyPI') and version_ok(version, 'PyPI')):
            public.add((normalize(name, 'PyPI'), version))
    result = []
    for environment in (repo / '.venv', repo / 'venv'):
        # Bound enumeration without recursively following arbitrary repo trees.
        for count, metadata in enumerate(environment.glob('lib/python*/site-packages/*.dist-info')):
            if count >= MAX_ITEMS or len(result) > MAX_ITEMS:
                raise ValueError('installed executable limit exceeded')
            entry = metadata / 'entry_points.txt'
            if not entry.exists():
                continue
            config = configparser.ConfigParser(interpolation=None)
            config.optionxform = str
            config.read_string(text(entry))
            if not config.has_section('console_scripts'):
                continue
            info = Parser().parsestr(text(metadata / 'METADATA'), headersonly=True)
            name, version = info.get('Name'), info.get('Version')
            mapped = (name_ok(name, 'PyPI') and version_ok(version, 'PyPI')
                      and (normalize(name, 'PyPI'), version) in public
                      and not (metadata / 'direct_url.json').exists())
            for binary in config['console_scripts']:
                target = within(environment / 'bin', binary)
                if not target.is_file():
                    result.append(row(binary, target, provenance, reason='console script not installed'))
                elif mapped:
                    result.append(row(binary, target.resolve(), provenance + ' uv.lock + installed dist-info',
                                      'PyPI', normalize(name, 'PyPI'), version))
                else:
                    result.append(row(binary, target.resolve(), provenance,
                                      reason='Python public registry identity/version unverified; not sent'))
    return result


def path_tool(binary, provenance):
    """Inspect npm metadata behind a requested PATH executable, never execute it."""
    if not name_ok(binary, 'PyPI'):
        return row(str(binary)[:100], '', provenance, reason='unsupported tool declaration')
    executable = shutil.which(binary)
    if not executable:
        return row(binary, '', provenance, reason='required executable not found on PATH')
    target = Path(executable).resolve()
    cargo = Path(os.environ.get('CARGO_HOME', os.path.expanduser('~/.cargo')))
    if target.parent == (cargo / 'bin').resolve():
        try:
            installs = document(cargo / '.crates2.json').get('installs', {})
            for key, value in installs.items():
                match = re.fullmatch(r'([A-Za-z0-9_-]+) ([^ ]+) \(registry\+https://github.com/rust-lang/crates.io-index\)', key)
                if match and version_ok(match[2]) and binary in value.get('bins', []):
                    return row(binary, target, provenance + ' PATH + cargo install metadata',
                               'crates.io', match[1], match[2])
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    for parent in list(target.parents)[:5]:
        if (parent / 'package.json').is_file() and parent.parent.name == 'node_modules':
            try:
                lock = document(parent.parent / '.package-lock.json')
                record = lock.get('packages', {}).get('node_modules/' + parent.name, {})
                for candidate in npm_package(parent, record, provenance + ' PATH + npm installed lock'):
                    if candidate['binary'] == binary and candidate['path'] == str(target):
                        return candidate
            except (OSError, ValueError, TypeError, AttributeError):
                break
    return row(binary, target, provenance)


def brew_packages(repo, provenance):
    """Literal Brewfile declarations only; inspect Cellar bins, never run Ruby/brew."""
    result = []
    prefixes = {Path('/opt/homebrew'), Path('/usr/local')}
    configured = os.environ.get('HOMEBREW_PREFIX', '')
    if os.path.isabs(configured):
        prefixes.add(Path(configured))
    for line in text(repo / 'Brewfile').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.match(r'^brew [\"\']([A-Za-z0-9_./+-]+)[\"\'](?:,|$)', line)
        if not match or '..' in match[1].split('/'):
            result.append(row('Brewfile entry', '', provenance, reason='dynamic/non-formula Brewfile entry unsupported'))
            continue
        formula = match[1].split('/')[-1]
        found = False
        for prefix in sorted(prefixes):
            for index, target in enumerate(itertools.chain((prefix / 'Cellar' / formula).glob('*/bin/*'),
                                                      (prefix / 'Cellar' / formula).glob('*/sbin/*'))):
                if index >= MAX_ITEMS:
                    raise ValueError('Homebrew binary limit exceeded')
                if target.is_file():
                    found = True
                    result.append(row(target.name, target.resolve(), provenance + ' literal Brewfile + installed Cellar',
                                      reason=f'Homebrew formula {match[1]} installed {target.parent.parent.name}; advisory ecosystem identity unmapped'))
        if not found:
            result.append(row(formula, '', provenance + ' Brewfile', reason='no installed Cellar binaries found; coverage unknown'))
    return result


def inventory():
    repos, agents = registered()
    rows, notes = [], []
    if not repos:
        notes.append('No registered repositories; inventory coverage unavailable.')
    for alias, raw in repos.items():
        repo = Path(os.path.expanduser(raw))
        provenance = f'repo {alias}'
        if not repo.is_absolute() or not repo.is_dir():
            notes.append(provenance + ': checkout unavailable; inventory unknown.')
            continue
        metadata_seen = False
        for marker, adapter in [('package-lock.json', node_packages), ('.venv', python_packages), ('venv', python_packages), ('Brewfile', brew_packages)]:
            if not (repo / marker).exists() or marker == 'venv' and (repo / '.venv').exists():
                continue
            metadata_seen = True
            try:
                rows.extend(adapter(repo, provenance))
            except (OSError, ValueError, TypeError, AttributeError, configparser.Error):
                notes.append(provenance + ': ' + marker + ' inventory unavailable/unsupported.')
        tools = set()
        try:
            path = repo / '.tool-versions'
            if path.exists():
                metadata_seen = True
                for line in text(path).splitlines():
                    words = line.partition('#')[0].split()
                    if words:
                        tools.add({'nodejs': 'node', 'python': 'python3'}.get(words[0], words[0]))
            for name in ('mise.toml', '.mise.toml'):
                if (repo / name).exists():
                    metadata_seen = True
                    tools.update(document(repo / name).get('tools', {}))
            if (repo / 'package.json').exists():
                metadata_seen = True
                package = document(repo / 'package.json')
                tools.update(package.get('engines', {}))
                if not (repo / 'package-lock.json').exists():
                    notes.append(provenance + ': npm installed package coverage unknown without package-lock v2/v3.')
            if (repo / 'pyproject.toml').exists() and not any((repo / x).exists() for x in ('.venv', 'venv')):
                notes.append(provenance + ': Python CLI coverage unknown without a local virtualenv.')
        except (OSError, ValueError, TypeError, AttributeError):
            notes.append(provenance + ': tool declarations unavailable/unsupported.')
        if alias in agents:
            tools.add(agents[alias])
        for tool in sorted(tools)[:100]:
            rows.append(path_tool(tool, provenance + ' declared tool/agent'))
        if len(tools) > 100:
            notes.append(provenance + ': tool declaration limit exceeded; coverage incomplete.')
        if not metadata_seen:
            notes.append(provenance + ': no supported tool metadata; other binaries unknown.')
    dedup = {}
    for item in rows:
        key = (item['path'], item['binary'], item['ecosystem'], item['name'], item['version'])
        if key in dedup:
            dedup[key]['provenance'] = sorted(set(dedup[key]['provenance'] + item['provenance']))
        else:
            dedup[key] = item
    if len(dedup) > MAX_ITEMS:
        notes.append('Inventory limit exceeded; additional binaries not checked.')
    return list(dedup.values())[:MAX_ITEMS], notes
