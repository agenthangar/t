"""Exercise the pinned CI download script without network or executing an upstream binary."""
import hashlib
import os
from pathlib import Path
import subprocess
import textwrap
import pytest

ROOT=Path(__file__).parents[1]

@pytest.mark.parametrize('fallback,corrupt',[(False,False),(True,False),(True,True)])
def test_release_api_fallback_keeps_checksum_gate(tmp_path,fallback,corrupt):
    source=(ROOT/'.github/actions/test-runtime/action.yml').read_text()
    script=textwrap.dedent(source.split('        set -euo pipefail\n',1)[1].split('    - name: Required real terminal dependencies')[0])
    script='set -euo pipefail\n'+script
    binaries=tmp_path/'bin';binaries.mkdir();runtime=tmp_path/'runtime';runtime.mkdir()
    artifact=tmp_path/'asset';artifact.write_bytes(b'fixture archive')
    checksum=tmp_path/'checksums'
    checksum.write_text(('0'*64 if corrupt else hashlib.sha256(artifact.read_bytes()).hexdigest())+'  fzf-0.74.5-linux_amd64.tar.gz\n')
    log=tmp_path/'gh.log'
    commands={
        'uname':'#!/bin/sh\n[ "$1" = -s ] && echo Linux || echo x86_64\n',
        'curl':'''#!/bin/sh
[ "$FALLBACK" = 1 ] && exit 22
while [ "$1" != -o ]; do shift; done
case "$2" in *checksums.txt) cp "$FIXTURE_CHECKSUM" "$2";; *) cp "$FIXTURE_ASSET" "$2";; esac
''',
        'gh':'''#!/bin/sh
printf '%s\\n' "$*" >> "$FIXTURE_LOG"
[ "$1 $2 $3" = 'release download v0.74.5' ] || exit 7
while [ "$1" != --pattern ]; do shift; done
name=$2
case "$name" in *checksums.txt) cp "$FIXTURE_CHECKSUM" "$RUNNER_TEMP/$name";; *) cp "$FIXTURE_ASSET" "$RUNNER_TEMP/$name";; esac
''',
        'tar':'''#!/bin/sh
printf '#!/bin/sh\\necho 0.74.5\\n' > "$RUNNER_TEMP/fzf-bin/fzf"
chmod +x "$RUNNER_TEMP/fzf-bin/fzf"
'''}
    for name,body in commands.items():
        path=binaries/name;path.write_text(body);path.chmod(0o755)
    env=dict(os.environ,PATH=str(binaries)+os.pathsep+os.environ['PATH'],RUNNER_TEMP=str(runtime),GITHUB_PATH=str(tmp_path/'path'),
        FZF_VERSION='0.74.5',FALLBACK=str(int(fallback)),FIXTURE_ASSET=str(artifact),FIXTURE_CHECKSUM=str(checksum),FIXTURE_LOG=str(log))
    result=subprocess.run(['bash','-c',script],env=env,capture_output=True,text=True,timeout=10)
    assert (result.returncode!=0)==corrupt,result.stderr
    assert (runtime/'fzf-bin/fzf').exists() is not corrupt
    if fallback:
        assert len(log.read_text().splitlines())==2
        assert '--repo junegunn/fzf' in log.read_text()
    else:assert not log.exists()
