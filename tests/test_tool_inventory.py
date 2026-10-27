import importlib.util
import json
import os
from pathlib import Path
import pytest

SPEC=importlib.util.spec_from_file_location('inventory',Path(__file__).resolve().parents[1]/'libexec/t_tool_inventory.py')
i=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(i)


def put(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value,dict) else value)
    return path


@pytest.fixture
def repo(tmp_path,monkeypatch):
    root=tmp_path/'repo';root.mkdir()
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    put(tmp_path/'config/t/config.sh',f'DEV_REPOS[api]={root}\n')
    monkeypatch.setenv('PATH',str(tmp_path/'bin'))
    return root


def npm(repo,name='eslint',version='8.0.0',binary=None,source=None):
    binary=binary or name.split('/')[-1]
    root=repo/'node_modules'/name
    put(root/'package.json',{'name':name,'version':version,'bin':{binary:'bin/tool.js'}})
    put(root/'bin/tool.js','throw new Error("must not execute");')
    record={'version':version,'resolved':source or f'https://registry.npmjs.org/{name}/-/{name.split("/")[-1]}-{version}.tgz'}
    return root, record


def python(repo,name='black',version='23.1.0',binary='black'):
    root=repo/'.venv/lib/python3.12/site-packages'/f'{name}-{version}.dist-info'
    put(root/'METADATA',f'Name: {name}\nVersion: {version}\n')
    put(root/'entry_points.txt',f'[console_scripts]\n{binary} = module:main\n')
    put(repo/'.venv/bin'/binary,'#!/invalid\nraise RuntimeError("never execute")')
    put(repo/'uv.lock',f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "https://pypi.org/simple" }}\n')
    return root


def test_registered_inventory_relevance_dedup_and_no_execution(repo,monkeypatch):
    _,a=npm(repo);_,b=npm(repo,'typescript','5.0.0','tsc')
    put(repo/'package-lock.json',{'packages':{'node_modules/eslint':a,'node_modules/typescript':b}})
    python(repo)
    # Unregistered neighbor and unrelated PATH executables are never scanned.
    other=repo.parent/'other';npm(other,'private-tool')
    monkeypatch.setattr(i,'registered',lambda:({'api':str(repo),'alias':str(repo)},{}))
    rows,notes=i.inventory()
    assert {r['name'] for r in rows}=={'eslint','typescript','black'}
    assert {r['ecosystem'] for r in rows}=={'npm','PyPI'}
    assert all(len(r['provenance'])==2 for r in rows)
    assert not notes


def test_npm_identity_not_lock_version_or_private_source(repo):
    root,record=npm(repo)
    record['version']='9.0.0'
    assert i.npm_package(root,record,'repo')[0]['status']=='unsupported'
    for source in ['https://private.example/eslint.tgz','file:../private','https://registry.npmjs.org.evil/eslint/-/x.tgz','https://registry.npmjs.org/other/-/x.tgz']:
        record.update(version='8.0.0',resolved=source)
        assert i.npm_package(root,record,'repo')[0]['status']=='unsupported'
    root,record=npm(repo,'@private/cli')
    assert i.npm_package(root,record,'repo')[0]['status']=='unsupported'


def test_python_normalization_missing_lock_and_editable(repo):
    root=python(repo,'Some_Tool','1.2.0','tool')
    assert i.python_packages(repo,'repo')[0]['name']=='some-tool'
    put(root/'direct_url.json',{'url':'file:///private'})
    assert i.python_packages(repo,'repo')[0]['status']=='unsupported'
    (root/'direct_url.json').unlink();(repo/'uv.lock').unlink()
    assert i.python_packages(repo,'repo')[0]['status']=='unsupported'
    (repo/'.venv/bin/tool').unlink()
    assert 'not installed' in i.python_packages(repo,'repo')[0]['reason']


def test_declared_system_and_homebrew_tools_are_visible_unknown(repo,monkeypatch):
    put(repo/'.tool-versions','git 2.0\nterraform 1.0\ncloudflared 2026.10.2\npython 3.12\n')
    put(repo/'mise.toml','[tools]\nnode = "22"\n')
    brew=put(repo.parent/'brew/Cellar/terraform/1.0/bin/terraform','never execute');brew.chmod(0o755)
    system=put(repo.parent/'usr/bin/git','never execute');system.chmod(0o755)
    monkeypatch.setattr(i.shutil,'which',lambda name:{'terraform':str(brew),'git':str(system)}.get(name))
    rows,_=i.inventory()
    assert {r['binary'] for r in rows}=={'git','terraform','cloudflared','python3','node'}
    assert all(r['status']=='unsupported' for r in rows)
    assert str(brew) in {r['path'] for r in rows}


def test_global_npm_and_cargo_from_declared_path(repo,monkeypatch):
    globalroot=repo.parent/'global';root,record=npm(globalroot,'typescript','5.0.0','tsc')
    put(globalroot/'node_modules/.package-lock.json',{'packages':{'node_modules/typescript':record}})
    cargo=repo.parent/'cargo';binary=put(cargo/'bin/rg','binary');binary.chmod(0o755)
    put(cargo/'.crates2.json',{'installs':{'ripgrep 14.0.0 (registry+https://github.com/rust-lang/crates.io-index)':{'bins':['rg']}}})
    monkeypatch.setenv('CARGO_HOME',str(cargo))
    monkeypatch.setattr(i.shutil,'which',lambda name:str(binary if name=='rg' else root/'bin/tool.js'))
    assert i.path_tool('tsc','repo')['ecosystem']=='npm'
    assert i.path_tool('rg','repo')['ecosystem']=='crates.io'
    put(cargo/'.crates2.json',{'installs':{'ripgrep 14.0.0 (git+https://private.example)':{'bins':['rg']}}})
    assert i.path_tool('rg','repo')['status']=='unsupported'


def test_metadata_failure_and_missing_checkout_are_honest(repo,monkeypatch):
    put(repo/'package-lock.json',{'lockfileVersion':1})
    put(repo/'package.json',{'engines':{'node':'>=22'}})
    put(repo/'pyproject.toml','[project]\nname="sample"\n')
    rows,notes=i.inventory()
    assert rows[0]['status']=='unsupported'
    assert any('unavailable/unsupported' in n for n in notes)
    assert any('without a local virtualenv' in n for n in notes)
    monkeypatch.setattr(i,'registered',lambda:({'gone':'/missing/repo'},{}))
    assert 'unavailable' in i.inventory()[1][0]
    monkeypatch.setattr(i,'registered',lambda:({},{}))
    assert 'No registered' in i.inventory()[1][0]


def test_config_does_not_source_shell(repo,monkeypatch):
    config=Path(os.environ['XDG_CONFIG_HOME'])/'t/config.sh'
    put(config,f'DEV_REPOS[space]="{repo}"\nDEV_AGENT[space]=codex\ntouch /never-execute\n')
    assert i.registered()==({'space':str(repo)},{'space':'codex'})
    config.unlink();assert i.registered()==({},{})


def test_bounded_metadata_and_paths(tmp_path,monkeypatch):
    fifo=tmp_path/'fifo';os.mkfifo(fifo)
    with pytest.raises(ValueError):i.text(fifo)
    p=put(tmp_path/'large','x'*12)
    with pytest.raises(ValueError):i.text(p,10)
    with pytest.raises(ValueError):i.document(put(tmp_path/'list.json','[]'))
    for relative in ['../escape','/abs','']:
        with pytest.raises(ValueError):i.within(tmp_path,relative)
    monkeypatch.setattr(i,'tomllib',None)
    with pytest.raises(ValueError):i.document(put(tmp_path/'x.toml',''))


def test_absent_tool_metadata_and_unmapped_agent(repo,monkeypatch):
    monkeypatch.setattr(i,'registered',lambda:({'api':str(repo)},{'api':'codex'}))
    rows,notes=i.inventory()
    assert rows[0]['binary']=='codex' and rows[0]['status']=='unsupported'
    assert 'no supported tool metadata' in notes[0]


def test_brewfile_discovers_formula_binaries_without_inventing_identity(repo,monkeypatch):
    prefix=repo.parent/'homebrew'
    target=put(prefix/'Cellar/ripgrep/14.1.0/bin/rg','never execute')
    monkeypatch.setenv('HOMEBREW_PREFIX',str(prefix))
    put(repo/'Brewfile','brew "ripgrep"\nbrew "missing-tool"\nsystem "never execute"\n')
    rows,_=i.inventory()
    rg=next(row for row in rows if row['binary']=='rg')
    assert rg['path']==str(target) and rg['status']=='unsupported'
    assert '14.1.0' in rg['reason'] and not rg['ecosystem']
    assert any('dynamic/non-formula' in row['reason'] for row in rows)
    assert any('no installed Cellar' in row['reason'] for row in rows)
