"""Route supervised Codex resume permissions to the owned app-server."""
import json
import os
from pathlib import Path
import runpy


FIELDS = {'sandbox_mode': 'sandbox', 'approval_policy': 'approvalPolicy',
          'approvals_reviewer': 'approvalsReviewer', 'default_permissions': 'permissions'}
OPTIONS = {'--sandbox': 'sandbox_mode', '-s': 'sandbox_mode',
           '--ask-for-approval': 'approval_policy', '-a': 'approval_policy',
           '--permission-profile': 'default_permissions'}
SANDBOX_TYPES = {'read-only': 'readOnly', 'workspace-write': 'workspaceWrite',
                 'danger-full-access': 'dangerFullAccess'}


class ResumePermissions:
    def __init__(self, argv, cwd):
        self.sid = argv[2]
        self.cwd = Path(cwd).resolve()
        self.frontend = list(argv[:3])
        self.config = {}
        self.roots = []
        self.request = None
        self.error = ''
        self.verified = False
        args = iter(argv[3:])
        for arg in args:
            if arg == '--':
                self.frontend.extend([arg, *args])
                break
            key, equals, inline = arg.partition('=')
            if key.startswith('-') and key[:2] in ('-s', '-a', '-c', '-m', '-C') and len(key) > 2 and not key.startswith('--'):
                key, equals, inline = key[:2], '=', arg[2:]
            if key in (*OPTIONS, '--add-dir', '-c', '--config', '-m', '--model', '-C', '--cd',
                       '--enable', '--disable'):
                value = inline if equals else next(args, None)
                if value is None:
                    raise ValueError('Missing value for ' + key)
                if key in OPTIONS:
                    self.config[OPTIONS[key]] = value
                elif key == '--add-dir':
                    self.roots.append(str((self.cwd / value).resolve()))
                elif key in ('-c', '--config'):
                    name, separator, literal = value.partition('=')
                    name = name.strip()
                    if name in FIELDS or name == 'sandbox_workspace_write.writable_roots':
                        if not separator:
                            raise ValueError('Missing configuration value for ' + name)
                        try:
                            parsed = runpy.run_path(str(Path(__file__).with_name('t_toml.py')))['loads']('value='+literal)
                            self.config[name] = parsed['value']
                        except ValueError:
                            self.config[name] = literal
                    elif name.split('.')[0] in ('permissions', 'sandbox_workspace_write', 'network'):
                        raise ValueError('Cannot verify the resumed Codex permission setting ' + name)
                    else:
                        self.frontend.extend([key, value])
                else:
                    self.frontend.extend([key, value])
            elif arg in ('--dangerously-bypass-approvals-and-sandbox', '--yolo'):
                self.config.update(sandbox_mode='danger-full-access', approval_policy='never')
            elif arg == '--approve-for-me':
                self.config.update(sandbox_mode='workspace-write', approval_policy='on-request',
                                   approvals_reviewer='auto_review')
            elif arg == '--no-daemon':
                continue
            elif arg in ('--full-auto', '--not-so-yolo'):
                raise ValueError('Cannot verify the resumed Codex permission setting ' + arg)
            else:
                self.frontend.append(arg)
        if 'sandbox_mode' in self.config and 'default_permissions' in self.config:
            raise ValueError('Codex resume received both sandbox mode and a named permission profile')
        if not all(isinstance(self.config[k], str) for k in FIELDS if k in self.config):
            raise ValueError('Invalid Codex resume permission value')
        if 'sandbox_mode' in self.config and self.config['sandbox_mode'] not in SANDBOX_TYPES:
            raise ValueError('Unsupported Codex resume sandbox mode')
        configured = self.config.get('sandbox_workspace_write.writable_roots', [])
        if not isinstance(configured, list) or not all(isinstance(root, str) for root in configured):
            raise ValueError('Invalid Codex writable roots')
        self.configured_roots = [str((self.cwd / value).resolve()) for value in configured]
        self.params = {field: self.config[key] for key, field in FIELDS.items() if key in self.config}
        if 'sandbox_workspace_write.writable_roots' in self.config:
            self.params['config'] = {'sandbox_workspace_write.writable_roots': self.configured_roots}
        self.runtime_roots = None
        self.server = [arg for key, value in self.config.items() for arg in ('-c', key+'='+json.dumps(value))]

    def client(self, message):
        if self.verified or not (self.params or self.roots):
            return
        method = message.get('method')
        if method in ('turn/start', 'thread/start'):
            raise ValueError('Codex tried to start work before verifying the selected thread permissions')
        if method != 'thread/resume':
            return
        params = message['params']
        if params.get('threadId') != self.sid:
            raise ValueError('Codex requested a different thread during resume')
        if self.request is not None:
            raise ValueError('Codex requested a second resume before verifying the selected thread')
        self.request = message['id']
        if self.roots or 'config' in self.params:
            # An explicit list, including [], replaces saved roots rather than
            # preserving roots restored by the frontend from the old thread.
            inherited = [] if 'config' in self.params else (params.get('runtimeWorkspaceRoots') or [])
            roots = [str(self.cwd), *inherited, *self.configured_roots, *self.roots]
            params['runtimeWorkspaceRoots'] = list(dict.fromkeys(roots))
            if 'config' in self.params:
                self.runtime_roots = params['runtimeWorkspaceRoots']
        if 'config' in self.params:
            params['config'] = {**(params.get('config') or {}), **self.params['config']}
        params.update({key: value for key, value in self.params.items() if key != 'config'})

    def server_reply(self, message):
        if self.request is None or message.get('id') != self.request or 'result' not in message:
            return
        result = message['result']
        sandbox = result.get('sandbox') or {}
        checks = [(result.get('thread', {}).get('id') == self.sid, 'thread')]
        for key in ('approvalPolicy', 'approvalsReviewer'):
            if key in self.params:
                checks.append((result.get(key) == self.params[key], key))
        if 'sandbox' in self.params:
            checks.append((sandbox.get('type') == SANDBOX_TYPES[self.params['sandbox']], 'sandbox'))
        if 'permissions' in self.params:
            profile = result.get('activePermissionProfile') or {}
            checks.append((profile.get('id') == self.params['permissions'], 'permission profile'))
        if self.roots:
            checks.append((set(self.roots) <= set(result.get('runtimeWorkspaceRoots') or []), 'workspace roots'))
        if self.runtime_roots is not None:
            checks.append((set(self.runtime_roots) == set(result.get('runtimeWorkspaceRoots') or []),
                           'workspace roots'))
        if sandbox.get('type') == 'workspaceWrite':
            checks.append((set(self.configured_roots + self.roots) <= set(sandbox.get('writableRoots') or []),
                           'writable roots'))
            if self.runtime_roots is not None:
                allowed = set(self.runtime_roots)
                # Codex can materialize its built-in temporary roots in this
                # legacy policy as well as the selected workspace roots.
                if not sandbox.get('excludeSlashTmp'):
                    allowed.add(str(Path('/tmp').resolve()))
                if not sandbox.get('excludeTmpdirEnvVar') and os.environ.get('TMPDIR'):
                    allowed.add(str(Path(os.environ['TMPDIR']).resolve()))
                actual = {str(Path(root).resolve()) for root in sandbox.get('writableRoots') or []}
                checks.append((actual <= allowed, 'writable roots'))
        for valid, label in checks:
            if not valid:
                raise ValueError('Codex did not apply the selected ' + label + ' during resume')
        self.verified = True
        self.request = None
