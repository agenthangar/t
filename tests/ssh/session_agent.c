#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
#include <signal.h>
int main(int argc, char **argv) {
  if (argc == 3 && !strcmp(argv[1], "resume") && !strcmp(argv[2], "--help")) {
    puts("--no-daemon"); return 0;
  }
  if (getenv("T_TEST_IGNORE_HUP")) signal(SIGHUP, SIG_IGN);
  if (getenv("T_TEST_IGNORE_TERM")) signal(SIGTERM, SIG_IGN);
  FILE *f = fopen(getenv("T_TEST_AGENT_LOG"), "a");
  if (!f) return 2;
  char cwd[4096];
  if (!getcwd(cwd, sizeof cwd)) return 2;
  fprintf(f, "%s|%s", argv[0], cwd);
  for (int i = 1; i < argc; i++) fprintf(f, "|%s", argv[i]);
  fputc('\n', f);
  fclose(f);
  const char *sid = argc >= 3 && !strcmp(argv[1], "resume") ? argv[2] :
                    (strstr(argv[0], "/codex") ? getenv("T_TEST_FRESH_SID") : NULL);
  if (sid && *sid) {
    char locks[4096], lock[4096], registry[4096];
    snprintf(locks, sizeof locks, "%s/thread-writer-locks", getenv("CODEX_HOME"));
    mkdir(locks, 0700);
    snprintf(lock, sizeof lock, "%s/%s.lock", locks, sid);
    int fd = open(lock, O_CREAT | O_RDWR, 0600);
    if (fd < 0 || flock(fd, LOCK_EX | LOCK_NB)) return 3;
    snprintf(registry, sizeof registry, "%s/claude-sessions/%d",
             getenv("XDG_CACHE_HOME"), getpid());
    f = fopen(registry, "w");
    if (f) { fprintf(f, "%s\t%s\n", sid, cwd); fclose(f); }
  }
  const char *preview = getenv("T_TEST_STATIC_PREVIEW");
  if (preview && *preview) {
    pid_t child = fork();
    if (!child) {
      int null = open("/dev/null", O_RDWR);
      if (null < 0) _exit(7);
      dup2(null, 0); dup2(null, 1); dup2(null, 2); close(null);
      if (chdir(preview)) _exit(4);
      execlp("python3", "python3", "-m", "http.server", "0", "--bind", "localhost", (char *)NULL);
      _exit(5);
    }
    char receipt[4096];
    snprintf(receipt, sizeof receipt, "%s/preview.pid", preview);
    f = fopen(receipt, "w");
    if (f) { fprintf(f, "%d", child); fclose(f); }
    if (getenv("T_TEST_EXTRA_WORK") && !fork()) {
      execlp("sleep", "sleep", "180", (char *)NULL);
      _exit(6);
    }
  }
  const char *seconds = getenv("T_TEST_AGENT_SECONDS");
  const char *screen = getenv("T_TEST_AGENT_SCREEN");
  if (screen) { fputs(screen, stdout); fflush(stdout); }
  sleep(seconds ? atoi(seconds) : 60);
  return 0;
}
