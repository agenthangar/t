#!/usr/bin/env python3
"""Build a reviewable AgentCore test deployment bundle offline. Never invokes AWS."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parent


def sub(value):
    return {'Fn::Sub': value}


def bundle(account, region, name, output):
    if not re.fullmatch(r'[0-9]{12}', account):
        raise ValueError('account must be a 12-digit ID, verified before deployment')
    if not re.fullmatch(r'(us|eu|ap|ca|sa|af|me|il|mx)-[a-z]+-[0-9]', region):
        raise ValueError('use a standard commercial AWS region; availability must be verified')
    if not re.fullmatch(r'[a-z][a-z0-9]{5,11}', name):
        raise ValueError('name must be 6–12 lowercase letters/digits, starting with a letter')
    output.mkdir(mode=0o700)  # Never overwrite another bundle or deployment receipts.
    runtime = 't_accept_' + name
    stack = 't-agentcore-test-' + name
    log_arn = f'arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{runtime}-*'
    runtime_arn = f'arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime}-*'
    trust = {'Version': '2012-10-17', 'Statement': [{
        'Effect': 'Allow', 'Principal': {'Service': 'bedrock-agentcore.amazonaws.com'},
        'Action': 'sts:AssumeRole', 'Condition': {
            'StringEquals': {'aws:SourceAccount': account},
            'ArnLike': {'aws:SourceArn': runtime_arn}}}]}
    role_policy = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['s3:GetObject'], 'Resource': sub('${CodeBucket.Arn}/agent.zip')},
        {'Effect': 'Allow', 'Action': ['logs:CreateLogGroup', 'logs:DescribeLogStreams'], 'Resource': log_arn},
        {'Effect': 'Allow', 'Action': ['logs:CreateLogStream', 'logs:PutLogEvents'], 'Resource': log_arn + ':log-stream:*'},
        {'Effect': 'Allow', 'Action': ['logs:DescribeLogGroups'], 'Resource': f'arn:aws:logs:{region}:{account}:log-group:*'},
    ]}
    template = {'AWSTemplateFormatVersion': '2010-09-09',
        'Description': 'Disposable t AgentCore acceptance bootstrap; no models or outbound credentials.',
        'Resources': {
            'CodeBucket': {'Type': 'AWS::S3::Bucket', 'Properties': {
                'PublicAccessBlockConfiguration': {key: True for key in
                    ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')},
                'OwnershipControls': {'Rules': [{'ObjectOwnership': 'BucketOwnerEnforced'}]},
                'BucketEncryption': {'ServerSideEncryptionConfiguration': [{'ServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]},
                'LifecycleConfiguration': {'Rules': [{'Id': 'ExpireTestCode', 'Status': 'Enabled', 'ExpirationInDays': 1}]},
                'Tags': [{'Key': 'Purpose', 'Value': 't-agentcore-acceptance'}]}},
            'ExecutionRole': {'Type': 'AWS::IAM::Role', 'Properties': {
                'AssumeRolePolicyDocument': trust,
                'Policies': [{'PolicyName': 'FixtureCodeAndLogs', 'PolicyDocument': role_policy}],
                'Tags': [{'Key': 'Purpose', 'Value': 't-agentcore-acceptance'}]}},
        },
        'Outputs': {'Bucket': {'Value': {'Ref': 'CodeBucket'}},
                    'RoleArn': {'Value': {'Fn::GetAtt': ['ExecutionRole', 'Arn']}}}}
    request = {'agentRuntimeName': runtime,
        'agentRuntimeArtifact': {'codeConfiguration': {'code': {'s3': {'bucket': 'FILLED_FROM_STACK', 'prefix': 'agent.zip'}},
                                                       'runtime': 'PYTHON_3_13', 'entryPoint': ['agent.py']}},
        'roleArn': 'FILLED_FROM_STACK', 'networkConfiguration': {'networkMode': 'PUBLIC'},
        'protocolConfiguration': {'serverProtocol': 'HTTP'}, 'platformVersion': 'V1',
        'lifecycleConfiguration': {'idleRuntimeSessionTimeout': 60, 'maxLifetime': 600},
        'tags': {'Purpose': 't-agentcore-acceptance', 'TestRun': name}}
    with zipfile.ZipFile(output / 'agent.zip', 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        entry = zipfile.ZipInfo('agent.py', (2026, 1, 1, 0, 0, 0))
        entry.create_system = 3
        entry.compress_type = zipfile.ZIP_DEFLATED
        entry.external_attr = 0o100644 << 16
        archive.writestr(entry, (ROOT / 'agent.py').read_bytes())
    manifest = {'account': account, 'region': region, 'name': name, 'stack': stack, 'runtime_name': runtime,
                'sha256': hashlib.sha256((output / 'agent.zip').read_bytes()).hexdigest(),
                'deployed': False, 'approval_required': True}
    for filename, value in (('bootstrap.json', template), ('runtime-template.json', request), ('manifest.json', manifest)):
        (output / filename).write_text(json.dumps(value, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--account', required=True)
    p.add_argument('--region', required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    try:
        print(json.dumps(bundle(a.account, a.region, a.name, a.output), indent=2))
    except (OSError, ValueError) as error:
        p.error(str(error))
