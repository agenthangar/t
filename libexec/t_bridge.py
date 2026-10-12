#!/usr/bin/env python3
"""Private newline JSON bridge. stdout belongs exclusively to the protocol."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import sys
from t_core import Core, CoreError


def handle(core, request):
    request_id = request.get('id') if isinstance(request, dict) else None
    response = {'version': 1, 'id': request_id}
    try:
        if not isinstance(request, dict) or request.get('version') != 1:
            raise CoreError('invalid_request', 'protocol version 1 is required')
        if not isinstance(request.get('method'), str) or not isinstance(request.get('params', {}), dict):
            raise CoreError('invalid_request', 'method and object parameters are required')
        # Existing shared operations may render diagnostics; keep them off the wire.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            response['result'] = core.call(request['method'], request.get('params', {}))
    except CoreError as error:
        response['error'] = {'code': error.code, 'message': str(error), 'retryable': False}
    except (Exception, SystemExit):
        # Neither provider errors nor transcripts/config contents belong in errors.
        response['error'] = {'code': 'operation_failed', 'message': 'operation failed; refresh current state', 'retryable': False}
    return response


def serve(core, source, destination):
    for line in source:
        try:
            request = json.loads(line)
        except ValueError:
            request = None
        def progress(operation_id, stage):
            destination.write(json.dumps({'version': 1, 'id': request.get('id'), 'event': 'progress',
                                          'operationId': operation_id, 'stage': stage}) + '\n')
            destination.flush()
        core.progress = progress
        destination.write(json.dumps(handle(core, request), ensure_ascii=False) + '\n')
        destination.flush()


def main():
    serve(Core(), sys.stdin, sys.stdout)


if __name__ == '__main__':
    main()
