/* Native demo agent so lifecycle checks observe the same process identity as a CLI. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "--version") == 0) {
        puts("playground Claude stub 1.0");
        return 0;
    }
    if (argc > 1 && (strcmp(argv[1], "auth") == 0 || strcmp(argv[1], "status") == 0 ||
                     strcmp(argv[1], "mcp") == 0 || strcmp(argv[1], "login") == 0)) {
        fputs("Playground Claude stub has no account or authentication.\n", stderr);
        return 2;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
    puts("Playground Claude stub (no real AI): type quit to exit this session.");
    char *line = NULL;
    size_t capacity = 0;
    while (getline(&line, &capacity, stdin) >= 0) {
        line[strcspn(line, "\n")] = '\0';
        if (strcmp(line, "quit") == 0 || strcmp(line, "exit") == 0) break;
        printf("stub> %s\n", line);
    }
    free(line);
    return ferror(stdin) ? 1 : 0;
}
