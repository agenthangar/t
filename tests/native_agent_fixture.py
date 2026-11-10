"""An identifiable native test agent supervising a Python fixture payload."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def write_native_agent(path, script):
    compiler = shutil.which('cc')
    if not compiler:
        pytest.skip('cc is required for an identifiable agent fixture')
    payload = Path(str(path) + '.py')
    payload.write_text(script)
    source = Path(str(path) + '.c')
    source.write_text('''#include <unistd.h>
#include <sys/wait.h>
#include <stdlib.h>
#include <signal.h>
#include <errno.h>
static pid_t child;
static void forward(int sig) { if (child > 0) kill(child, sig); }
int main(int argc, char **argv) {
    char **args = calloc(argc + 2, sizeof(char *));
    args[0] = PYTHON; args[1] = PAYLOAD;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
    child = fork();
    if (child < 0) return 125;
    if (!child) { execv(args[0], args); _exit(127); }
    signal(SIGTERM, forward); signal(SIGHUP, forward); signal(SIGINT, forward);
    int status;
    while (waitpid(child, &status, 0) < 0) { if (errno != EINTR) return 125; }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
'''.replace('PYTHON', json.dumps(sys.executable)).replace('PAYLOAD', json.dumps(str(payload))))
    subprocess.run([compiler, str(source), '-o', str(path)], check=True, capture_output=True)
