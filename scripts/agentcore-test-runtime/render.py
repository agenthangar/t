#!/usr/bin/env python3
"""Validate local receipts and render an AWS request without making API calls."""
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid


def read(root, name):
    return json.loads((root / name).read_text())


def render(action, root):
    m = read(root, 'manifest.json')
    if not re.fullmatch(r'[0-9]{12}', m['account']) or not re.fullmatch(r'[a-z][a-z0-9]{5,11}', m['name']):
        raise ValueError('invalid manifest')
    if m['stack'] != 't-agentcore-test-' + m['name'] or m['runtime_name'] != 't_accept_' + m['name']:
        raise ValueError('unexpected resource names')
    if not re.fullmatch(r'(us|eu|ap|ca|sa|af|me|il|mx)-[a-z]+-[0-9]', m['region']):
        raise ValueError('unexpected region')
    if action == 'verify-manifest':
        return ''
    if action in ('verify', 'runtime') and hashlib.sha256((root / 'agent.zip').read_bytes()).hexdigest() != m['sha256']:
        raise ValueError('bundle hash changed; regenerate and review it')
    if action == 'verify':
        return ''
    if action in ('runtime', 'stack'):
        stack = read(root, 'stack.json')['Stacks'][0]
        expected = f"arn:aws:cloudformation:{m['region']}:{m['account']}:stack/{m['stack']}/"
        if not stack['StackId'].startswith(expected) or stack['StackStatus'] != 'CREATE_COMPLETE':
            raise ValueError('unexpected stack account, region, name, or status')
        outputs = {x['OutputKey']: x['OutputValue'] for x in stack['Outputs']}
        if not outputs['RoleArn'].startswith(f"arn:aws:iam::{m['account']}:role/{m['stack']}-ExecutionRole-"):
            raise ValueError('unexpected execution role')
        if not outputs['Bucket'].startswith(m['stack'] + '-codebucket-'):
            raise ValueError('unexpected code bucket')
        if action == 'stack':
            return outputs['Bucket']
        request = read(root, 'runtime-template.json')
        request['roleArn'] = outputs['RoleArn']
        request['agentRuntimeArtifact']['codeConfiguration']['code']['s3']['bucket'] = outputs['Bucket']
        request['clientToken'] = str(uuid.uuid5(uuid.NAMESPACE_URL, expected + m['sha256']))
        with (root / 'runtime-request.json').open('x') as file:
            json.dump(request, file, indent=2)
        return ''
    receipt = read(root, 'runtime-create.json')
    rid = receipt['agentRuntimeId']
    if not re.fullmatch(re.escape(m['runtime_name']) + r'-[A-Za-z0-9]{10}', rid):
        raise ValueError('unexpected runtime ID')
    if receipt['agentRuntimeArn'] != f"arn:aws:bedrock-agentcore:{m['region']}:{m['account']}:runtime/{rid}":
        raise ValueError('unexpected runtime ARN')
    if action == 'runtime-id':
        return rid
    if action == 'identity-name':
        arn = receipt.get('workloadIdentityDetails', {}).get('workloadIdentityArn', '')
        if not arn:
            return ''
        prefix = f"arn:aws:bedrock-agentcore:{m['region']}:{m['account']}:workload-identity-directory/default/workload-identity/"
        if not arn.startswith(prefix) or not re.fullmatch(re.escape(m['runtime_name']) + r'-[A-Za-z0-9_.-]+', arn[len(prefix):]):
            raise ValueError('workload identity ownership needs operator review')
        return arn[len(prefix):]
    raise ValueError('unknown action')


if __name__ == '__main__':
    try:
        print(render(sys.argv[1], Path(sys.argv[2])))
    except (OSError, ValueError, KeyError, IndexError) as error:
        raise SystemExit('Invalid/missing receipt; inspect before continuing: ' + str(error))
